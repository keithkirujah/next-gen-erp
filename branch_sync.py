"""
Multi-branch sync fabric for NextGen ERP.

Each shop is a branch with a stable branch_id. Changes are packaged as
sync envelopes and exchanged through:

1. Shared folder / USB drop (offline) — export / import JSONL packs
2. HTTPS relay URL (online) — POST envelopes, GET peer updates

This keeps the local-first model: the till never blocks on the network.
Background or admin-triggered sync merges remote envelopes into the local DB.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def load_branch_config(raw: str | None) -> dict:
    base = {
        "enabled": False,
        "branch_id": "",
        "branch_name": "",
        "relay_url": "",  # https://relay.example.com/v1/sync
        "relay_token": "",
        "shared_folder": "",  # optional UNC/local path for drop-folder sync
        "auto_export_minutes": 0,  # 0 = manual only
        "last_sync_at": "",
        "peer_branches": [],  # [{id,name}]
    }
    if not raw:
        # generate a stable-looking id placeholder; real id set on first save
        base["branch_id"] = "BR-" + uuid.uuid4().hex[:10].upper()
        return base
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return base
    if isinstance(data, dict):
        for k in base:
            if k in data:
                base[k] = data[k]
    base["enabled"] = bool(base.get("enabled"))
    if not str(base.get("branch_id") or "").strip():
        base["branch_id"] = "BR-" + uuid.uuid4().hex[:10].upper()
    try:
        base["auto_export_minutes"] = max(0, min(1440, int(base.get("auto_export_minutes") or 0)))
    except (TypeError, ValueError):
        base["auto_export_minutes"] = 0
    peers = base.get("peer_branches") or []
    if not isinstance(peers, list):
        base["peer_branches"] = []
    return base


def save_branch_config(cfg: dict) -> str:
    return json.dumps(load_branch_config(json.dumps(cfg)), sort_keys=True)


@dataclass
class SyncEnvelope:
    envelope_id: str
    branch_id: str
    branch_name: str
    created_at: float
    entities: list[dict]  # [{entity, entity_id, operation, payload}]

    def to_dict(self) -> dict:
        return {
            "envelope_id": self.envelope_id,
            "branch_id": self.branch_id,
            "branch_name": self.branch_name,
            "created_at": self.created_at,
            "entities": self.entities,
            "checksum": self.checksum(),
        }

    def checksum(self) -> str:
        blob = json.dumps({"e": self.entities, "b": self.branch_id}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def build_envelope_from_queue(conn, branch_id: str, branch_name: str, limit: int = 200) -> SyncEnvelope | None:
    """Package pending sync_queue rows into one envelope (does not delete queue)."""
    try:
        rows = conn.execute(
            "SELECT id, entity, entity_id, operation, payload_json, queued_at FROM sync_queue ORDER BY id LIMIT ?",
            (int(limit),),
        ).fetchall()
    except Exception:
        rows = []
    if not rows:
        return None
    entities = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        entities.append({
            "queue_id": row["id"],
            "entity": row["entity"],
            "entity_id": row["entity_id"],
            "operation": row["operation"],
            "payload": payload,
            "queued_at": row["queued_at"],
        })
    return SyncEnvelope(
        envelope_id="ENV-" + uuid.uuid4().hex[:12].upper(),
        branch_id=branch_id,
        branch_name=branch_name or branch_id,
        created_at=time.time(),
        entities=entities,
    )


def export_envelope_to_folder(envelope: SyncEnvelope, folder: str) -> str:
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"{envelope.branch_id}_{envelope.envelope_id}.json"
    file_path.write_text(json.dumps(envelope.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return str(file_path)


def import_envelopes_from_folder(folder: str, own_branch_id: str) -> list[dict]:
    path = Path(folder)
    if not path.is_dir():
        return []
    out = []
    for file in sorted(path.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("branch_id") or "") == own_branch_id:
            continue  # skip our own packs
        out.append(data)
    return out


def post_envelope_to_relay(envelope: SyncEnvelope, relay_url: str, token: str = "") -> tuple[bool, str]:
    url = str(relay_url or "").strip()
    if not url.startswith("http"):
        return False, "Relay URL missing"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "NextGenERP-BranchSync/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url.rstrip("/") + "/push", data=json.dumps(envelope.to_dict()).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, body[:300] or "OK"
    except urllib.error.HTTPError as exc:
        return False, exc.read().decode("utf-8", errors="replace")[:300] or str(exc)
    except Exception as exc:
        return False, str(exc)


def pull_envelopes_from_relay(relay_url: str, branch_id: str, token: str = "") -> tuple[bool, list[dict], str]:
    url = str(relay_url or "").strip()
    if not url.startswith("http"):
        return False, [], "Relay URL missing"
    headers = {"Accept": "application/json", "User-Agent": "NextGenERP-BranchSync/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    pull = url.rstrip("/") + f"/pull?branch_id={urllib.parse.quote(branch_id)}"
    req = urllib.request.Request(pull, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body else {}
            envelopes = data if isinstance(data, list) else data.get("envelopes") or []
            return True, list(envelopes), "OK"
    except Exception as exc:
        return False, [], str(exc)


import urllib.parse  # noqa: E402


def apply_envelope(conn, envelope: dict, conflict: str = "skip") -> tuple[int, int]:
    """
    Apply remote entities into local operational tables where safe.
    Returns (applied, skipped). For safety, only upserts customers/products
    catalogue fields and records a branch_sync_log entry — full merge of
    stock/sales is opt-in per entity to avoid double-counting cash.
    """
    applied = skipped = 0
    entities = envelope.get("entities") or []
    remote_branch = str(envelope.get("branch_id") or "")
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS branch_sync_log(
                id INTEGER PRIMARY KEY,
                envelope_id TEXT NOT NULL,
                remote_branch TEXT NOT NULL,
                entity TEXT NOT NULL,
                entity_id INTEGER,
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        # Dedup envelope
        exists = conn.execute(
            "SELECT 1 FROM branch_sync_log WHERE envelope_id=? LIMIT 1",
            (str(envelope.get("envelope_id") or ""),),
        ).fetchone()
        if exists:
            return 0, len(entities)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for item in entities:
            entity = str(item.get("entity") or "")
            payload = item.get("payload") or {}
            entity_id = item.get("entity_id")
            try:
                if entity == "customers" and payload.get("name"):
                    conn.execute(
                        "INSERT OR IGNORE INTO customers(name,contact,email,created_at) VALUES(?,?,?,?)",
                        (str(payload.get("name"))[:120], str(payload.get("contact") or "")[:80],
                         str(payload.get("email") or "")[:120], stamp),
                    )
                    status = "applied"
                    applied += 1
                elif entity == "products" and payload.get("sku"):
                    # Catalogue share only — do not overwrite local stock counts
                    row = conn.execute("SELECT id FROM products WHERE sku=? COLLATE NOCASE",
                                       (str(payload["sku"]),)).fetchone()
                    if not row:
                        conn.execute(
                            """INSERT INTO products(sku,name,unit,category,barcode,description,brand,model,
                               selling_price,cost_price,reorder_level,active,created_at,track_batches,track_stock)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,0,1)""",
                            (str(payload.get("sku"))[:60], str(payload.get("name") or payload.get("sku"))[:120],
                             str(payload.get("unit") or "each")[:20], str(payload.get("category") or "")[:60],
                             str(payload.get("barcode") or "")[:60], str(payload.get("description") or "")[:200],
                             str(payload.get("brand") or "")[:60], str(payload.get("model") or "")[:60],
                             str(payload.get("selling_price") or payload.get("unit_price") or "0"),
                             str(payload.get("cost_price") or "0"), str(payload.get("reorder_level") or "0"),
                             stamp),
                        )
                        status = "applied"
                        applied += 1
                    else:
                        status = "skipped"
                        skipped += 1
                else:
                    status = "skipped"
                    skipped += 1
                conn.execute(
                    "INSERT INTO branch_sync_log(envelope_id,remote_branch,entity,entity_id,status,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                    (str(envelope.get("envelope_id") or ""), remote_branch, entity, entity_id, status, "", stamp),
                )
            except Exception as exc:
                skipped += 1
                conn.execute(
                    "INSERT INTO branch_sync_log(envelope_id,remote_branch,entity,entity_id,status,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                    (str(envelope.get("envelope_id") or ""), remote_branch, entity, entity_id, "error", str(exc)[:200], stamp),
                )
    return applied, skipped
