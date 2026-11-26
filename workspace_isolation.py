"""Workspace identity and database guardrails for NextGen ERP.

Operational data is stored in a separate SQLite database for each normalized
business-name + business-profile pair. System identity/settings remain in the
application database. This module intentionally has no Qt dependency so its
identity rules can be tested without the desktop UI installed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path


class WorkspaceIdentityError(RuntimeError):
    """The file at a workspace path does not belong to the expected workspace."""


def _normalize(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def workspace_identity(business_name: str, profile_name: str) -> tuple[str, str, str]:
    """Return stable workspace id, normalized key, and safe display name."""
    name = " ".join(str(business_name or "").strip().split()) or "Default business"
    profile = " ".join(str(profile_name or "General business").strip().split()) or "General business"
    key = json.dumps([_normalize(name), _normalize(profile)], ensure_ascii=False, separators=(",", ":"))
    workspace_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return workspace_id, key, name


def workspace_path(root: str | Path, business_name: str, profile_name: str) -> tuple[str, Path, str]:
    workspace_id, key, display_name = workspace_identity(business_name, profile_name)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    return workspace_id, root / f"workspace_{workspace_id}.sqlite3", key


def ensure_workspace_metadata(conn: sqlite3.Connection, workspace_id: str, key: str,
                              business_name: str, profile_name: str, *, allow_existing: bool = False) -> None:
    """Create/read workspace metadata, failing closed if a path is misassigned."""
    prior_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    conn.execute("""CREATE TABLE IF NOT EXISTS workspace_metadata(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        workspace_id TEXT NOT NULL UNIQUE,
        identity_key TEXT NOT NULL,
        business_name TEXT NOT NULL,
        profile_name TEXT NOT NULL,
        schema_version INTEGER NOT NULL DEFAULT 1
    )""")
    existing = conn.execute("SELECT workspace_id, identity_key FROM workspace_metadata WHERE singleton=1").fetchone()
    if existing:
        if existing[0] != workspace_id or existing[1] != key:
            raise WorkspaceIdentityError(
                "Workspace database identity does not match the requested business. "
                "The database was not opened to prevent cross-business data exposure."
            )
        return
    # A non-empty untagged database must be migrated explicitly, never claimed silently.
    if prior_tables - {"workspace_metadata"} and not allow_existing:
        raise WorkspaceIdentityError(
            "Refusing to attach an untagged, non-empty database as a workspace. "
            "Run the legacy workspace migration first."
        )
    conn.execute("""INSERT INTO workspace_metadata
        (singleton,workspace_id,identity_key,business_name,profile_name,schema_version)
        VALUES(1,?,?,?,?,1)""", (workspace_id, key, business_name, profile_name))
    conn.commit()


def _drop_non_system_tables(conn: sqlite3.Connection, system_tables: set[str]) -> None:
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    for name in names:
        if name not in system_tables:
            conn.execute('DROP TABLE IF EXISTS "' + name.replace('"', '""') + '"')
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()


def migrate_legacy_workspace(system_conn: sqlite3.Connection, target_path: str | Path,
                             workspace_id: str, identity_key: str, business_name: str,
                             profile_name: str, system_tables: set[str]) -> Path:
    """Copy a legacy single-file ERP DB into a tenant DB, validate, then split.

    The source is not stripped until the copy is fully written and passes
    SQLite's integrity check. If copy/validation fails, the source is left intact.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        raise WorkspaceIdentityError("Refusing to overwrite an existing workspace during legacy migration.")
    queue_columns = {row[1] for row in system_conn.execute("PRAGMA table_info(sync_queue)").fetchall()}
    if "workspace_id" not in queue_columns:
        system_conn.execute("ALTER TABLE sync_queue ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '__global__'")
    system_conn.commit()

    temp_path = target_path.with_suffix(".migration.tmp")
    temp_path.unlink(missing_ok=True)
    destination = sqlite3.connect(str(temp_path))
    try:
        system_conn.backup(destination)
        destination.execute("PRAGMA foreign_keys=OFF")
        # The module catalogue (features) is business configuration and stays with
        # the isolated workspace. Only login/global settings/activity/queue are stripped.
        legacy_settings = destination.execute("SELECT key,value FROM settings").fetchall() if destination.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone() else []
        for system_table in ("users", "settings", "activity", "sync_queue"):
            if destination.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (system_table,)).fetchone():
                destination.execute('DROP TABLE "' + system_table + '"')
        if destination.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone():
            destination.execute("ALTER TABLE records RENAME TO workspace_records")
        ensure_workspace_metadata(destination, workspace_id, identity_key, business_name, profile_name, allow_existing=True)
        destination.execute("CREATE TABLE IF NOT EXISTS workspace_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        global_keys = {"business_name", "business_type", "sync_url", "device_name"}
        destination.executemany(
            "INSERT OR IGNORE INTO workspace_settings(key,value) VALUES(?,?)",
            [(key, value) for key, value in legacy_settings if key not in global_keys],
        )
        destination.execute("UPDATE workspace_metadata SET schema_version=2 WHERE singleton=1")
        destination.commit()
        check = destination.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError("Legacy workspace copy failed SQLite integrity check: " + str(check))
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        destination.close()

    temp_path.replace(target_path)
    # Only mutate the live queue after the isolated copy is validated and installed.
    # Feature/module changes are workspace-specific; only genuinely system-level
    # user/settings/activity events remain global.
    system_conn.execute(
        "UPDATE sync_queue SET workspace_id=CASE WHEN entity IN ('users','settings','activity') THEN '__global__' ELSE ? END",
        (workspace_id,),
    )
    activity_columns = {row[1] for row in system_conn.execute("PRAGMA table_info(activity)").fetchall()}
    if activity_columns and "workspace_id" not in activity_columns:
        system_conn.execute("ALTER TABLE activity ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '__global__'")
    if activity_columns:
        system_conn.execute("UPDATE activity SET workspace_id=? WHERE workspace_id='__global__'", (workspace_id,))
    system_conn.commit()
    # The validated copy now owns every operational table and legacy module record.
    _drop_non_system_tables(system_conn, system_tables)
    return target_path
