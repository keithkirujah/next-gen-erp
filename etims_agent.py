"""
eTIMS / fiscal control-unit agent for NextGen ERP.

Kenya's eTIMS expects invoices to pass through a Control Unit (OSCU/VSCU or
compatible portal). This module:

1. Builds a canonical fiscal payload from a local sale
2. Hands it to a pluggable agent (HTTP portal or local CU bridge)
3. Stores the signed response (CU invoice number, QR, timestamps)

Live mode requires the administrator to supply the bridge URL and credentials
in System Settings. Until then, "preparation" mode validates payloads only.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional


@dataclass
class FiscalLine:
    sku: str
    description: str
    quantity: str
    unit_price: str
    tax_rate: str
    tax_amount: str
    line_total: str
    serial_number: str = ""


@dataclass
class FiscalDocument:
    local_sale_id: int
    sale_number: str
    buyer_name: str
    buyer_pin: str
    currency: str
    subtotal: str
    tax_total: str
    total: str
    payment_method: str
    lines: list[FiscalLine]
    issued_at: str
    branch_id: str = ""
    till_id: str = ""


@dataclass
class FiscalResult:
    ok: bool
    mode: str  # preparation | live | error
    cu_invoice_number: str
    qr_payload: str
    message: str
    raw: dict


def load_etims_config(raw: str | None) -> dict:
    base = {
        "enabled": False,
        "mode": "preparation",  # preparation | live
        "bridge_url": "",  # e.g. https://localhost:8080/etims/sign or portal base
        "api_key": "",
        "device_serial": "",
        "control_unit_id": "",
        "branch_id": "",
        "seller_pin": "",
        "timeout_sec": 45,
        "auto_sign_on_sale": True,
    }
    if not raw:
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
    base["auto_sign_on_sale"] = bool(base.get("auto_sign_on_sale"))
    try:
        base["timeout_sec"] = max(10, min(120, int(base.get("timeout_sec") or 45)))
    except (TypeError, ValueError):
        base["timeout_sec"] = 45
    base["mode"] = "live" if str(base.get("mode")) == "live" else "preparation"
    return base


def save_etims_config(cfg: dict) -> str:
    return json.dumps(load_etims_config(json.dumps(cfg)), sort_keys=True)


def document_from_sale_row(sale: dict, lines: list[dict], cfg: dict, till_id: str = "") -> FiscalDocument:
    fiscal_lines = [
        FiscalLine(
            sku=str(line.get("sku") or ""),
            description=str(line.get("description") or line.get("name") or ""),
            quantity=str(line.get("quantity") or "0"),
            unit_price=str(line.get("unit_price") or "0"),
            tax_rate=str(sale.get("tax_rate") or "0"),
            tax_amount=str(line.get("tax_amount") or "0"),
            line_total=str(line.get("line_total") or "0"),
            serial_number=str(line.get("serial") or line.get("serial_number") or ""),
        )
        for line in lines
    ]
    return FiscalDocument(
        local_sale_id=int(sale.get("id") or 0),
        sale_number=str(sale.get("sale_number") or ""),
        buyer_name=str(sale.get("customer_name") or "Walk-in Customer"),
        buyer_pin=str(sale.get("buyer_pin") or ""),
        currency=str(sale.get("currency") or "KES"),
        subtotal=str(sale.get("subtotal") or sale.get("total") or "0"),
        tax_total=str(sale.get("tax_total") or "0"),
        total=str(sale.get("total") or "0"),
        payment_method=str(sale.get("payment_method") or ""),
        lines=fiscal_lines,
        issued_at=str(sale.get("created_at") or datetime.now(timezone.utc).isoformat()),
        branch_id=str(cfg.get("branch_id") or ""),
        till_id=str(till_id or ""),
    )


def _payload(doc: FiscalDocument, cfg: dict) -> dict:
    return {
        "document_type": "invoice",
        "seller_pin": cfg.get("seller_pin") or "",
        "device_serial": cfg.get("device_serial") or "",
        "control_unit_id": cfg.get("control_unit_id") or "",
        "branch_id": doc.branch_id or cfg.get("branch_id") or "",
        "till_id": doc.till_id,
        "local_reference": doc.sale_number,
        "local_sale_id": doc.local_sale_id,
        "buyer": {"name": doc.buyer_name, "pin": doc.buyer_pin},
        "currency": doc.currency,
        "amounts": {
            "subtotal": doc.subtotal,
            "tax_total": doc.tax_total,
            "total": doc.total,
        },
        "payment_method": doc.payment_method,
        "issued_at": doc.issued_at,
        "lines": [asdict(line) for line in doc.lines],
        "request_id": str(uuid.uuid4()),
    }


def sign_document(doc: FiscalDocument, cfg: dict) -> FiscalResult:
    """Sign (or dry-run) a fiscal document via the configured control-unit bridge."""
    cfg = load_etims_config(json.dumps(cfg) if not isinstance(cfg, dict) else json.dumps(cfg))
    # re-load through helper for safety
    if isinstance(cfg, str):
        cfg = load_etims_config(cfg)
    payload = _payload(doc, cfg)

    if not cfg.get("enabled"):
        return FiscalResult(True, "preparation", "", "", "eTIMS disabled — sale kept local only", {})

    if cfg.get("mode") != "live":
        # Preparation: validate completeness without contacting a device
        missing = [k for k in ("seller_pin",) if not str(cfg.get(k) or "").strip()]
        msg = "Preparation OK — payload ready for control unit"
        if missing:
            msg = f"Preparation warning — missing {', '.join(missing)}"
        return FiscalResult(True, "preparation", f"PREP-{doc.sale_number}", json.dumps(payload)[:500], msg, payload)

    url = str(cfg.get("bridge_url") or "").strip()
    if not url.startswith("http"):
        return FiscalResult(False, "error", "", "", "Live eTIMS requires a bridge URL (local CU agent or portal).", payload)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "NextGenERP-eTIMS-Agent/1.0",
    }
    api_key = str(cfg.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    timeout = int(cfg.get("timeout_sec") or 45)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err) if err else {"message": str(exc)}
        except json.JSONDecodeError:
            parsed = {"message": err or str(exc)}
        return FiscalResult(False, "error", "", "", str(parsed.get("message") or exc), parsed)
    except Exception as exc:
        return FiscalResult(False, "error", "", "", str(exc), payload)

    cu = str(parsed.get("cu_invoice_number") or parsed.get("invoice_number") or parsed.get("data", {}).get("cu_invoice_number") or "")
    qr = str(parsed.get("qr") or parsed.get("qr_payload") or parsed.get("data", {}).get("qr") or "")
    if not cu and not parsed.get("status"):
        return FiscalResult(False, "error", "", "", str(parsed.get("message") or "Control unit rejected the invoice"), parsed)
    return FiscalResult(True, "live", cu, qr, str(parsed.get("message") or "Signed by control unit"), parsed)


def persist_fiscal_result(conn, sale_id: int, result: FiscalResult) -> None:
    """Store CU response on the sales row (columns added by schema migration)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sales)").fetchall()}
    with conn:
        if "fiscal_cu_number" not in cols:
            conn.execute("ALTER TABLE sales ADD COLUMN fiscal_cu_number TEXT NOT NULL DEFAULT ''")
        if "fiscal_qr" not in cols:
            conn.execute("ALTER TABLE sales ADD COLUMN fiscal_qr TEXT NOT NULL DEFAULT ''")
        if "fiscal_status" not in cols:
            conn.execute("ALTER TABLE sales ADD COLUMN fiscal_status TEXT NOT NULL DEFAULT ''")
        if "fiscal_payload_json" not in cols:
            conn.execute("ALTER TABLE sales ADD COLUMN fiscal_payload_json TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "UPDATE sales SET fiscal_cu_number=?, fiscal_qr=?, fiscal_status=?, fiscal_payload_json=? WHERE id=?",
            (result.cu_invoice_number[:80], result.qr_payload[:2000], result.mode[:40],
             json.dumps(result.raw, ensure_ascii=False)[:8000], int(sale_id)),
        )
