"""
Next-generation capabilities for NextGen ERP + POS.

This module is the spine for features that turn a solid local ERP into a
category-defining hybrid:

- Serial / IMEI registry
- Customer loyalty points
- Multi-location stock foundation
- Fiscal / eTIMS readiness config (credentials + flags; device link is external)
- Accounting period lock
- Cash-drawer kick (ESC/POS)
- Feature flags so admin enables only what the shop needs

Nothing here requires leaving the local-first model. External services
(Paystack, future eTIMS device agents) plug in through configuration.
"""
from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from typing import Any


# ---------------------------------------------------------------------------
# Feature flags — admin turns pillars on without code changes
# ---------------------------------------------------------------------------
DEFAULT_FLAGS = {
    "serial_tracking": True,
    "loyalty": True,
    "multi_location": False,  # off until locations are configured
    "fiscal_etims": False,
    "period_lock": True,
    "cash_drawer": True,
    "returns_desk": True,
    "manager_overrides": True,
    "customer_credit": True,
    "layaway": False,
}


def load_flags(raw: str | None) -> dict:
    base = dict(DEFAULT_FLAGS)
    if not raw:
        return base
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return base
    if isinstance(data, dict):
        for k, v in data.items():
            if k in base:
                base[k] = bool(v)
    return base


def save_flags(flags: dict) -> str:
    clean = load_flags(json.dumps(flags))
    return json.dumps(clean, sort_keys=True)


# ---------------------------------------------------------------------------
# Fiscal / eTIMS readiness (config only — signing device is external)
# ---------------------------------------------------------------------------
DEFAULT_FISCAL = {
    "enabled": False,
    "mode": "preparation",  # preparation | device | portal
    "pin": "",
    "branch_id": "",
    "device_serial": "",
    "control_unit_id": "",
    "invoice_prefix": "INV",
    "credit_note_prefix": "CN",
    "require_buyer_pin": False,
    "notes": "Configure when your eTIMS / fiscal device or portal credentials are ready.",
}


def load_fiscal(raw: str | None) -> dict:
    base = deepcopy(DEFAULT_FISCAL)
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
    base["require_buyer_pin"] = bool(base.get("require_buyer_pin"))
    return base


def save_fiscal(cfg: dict) -> str:
    return json.dumps(load_fiscal(json.dumps(cfg)), sort_keys=True)


# ---------------------------------------------------------------------------
# Loyalty
# ---------------------------------------------------------------------------
DEFAULT_LOYALTY = {
    "enabled": True,
    "points_per_currency_unit": 1,   # 1 point per 1 KES spent
    "currency_per_point_redeem": 0.5,  # 1 point = 0.5 KES
    "min_redeem_points": 100,
    "welcome_points": 0,
}


def load_loyalty(raw: str | None) -> dict:
    base = dict(DEFAULT_LOYALTY)
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
    try:
        base["points_per_currency_unit"] = float(base.get("points_per_currency_unit") or 1)
        base["currency_per_point_redeem"] = float(base.get("currency_per_point_redeem") or 0.5)
        base["min_redeem_points"] = int(base.get("min_redeem_points") or 100)
        base["welcome_points"] = int(base.get("welcome_points") or 0)
    except (TypeError, ValueError):
        pass
    return base


def save_loyalty(cfg: dict) -> str:
    return json.dumps(load_loyalty(json.dumps(cfg)), sort_keys=True)


def points_earn(amount: Decimal | float, cfg: dict | None = None) -> int:
    cfg = cfg or DEFAULT_LOYALTY
    if not cfg.get("enabled"):
        return 0
    rate = float(cfg.get("points_per_currency_unit") or 0)
    return max(0, int(float(amount) * rate))


def points_value(points: int, cfg: dict | None = None) -> Decimal:
    cfg = cfg or DEFAULT_LOYALTY
    rate = Decimal(str(cfg.get("currency_per_point_redeem") or 0))
    return (Decimal(int(points)) * rate).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Multi-location foundation
# ---------------------------------------------------------------------------
DEFAULT_LOCATIONS = {
    "enabled": False,
    "locations": [
        {"id": "main", "name": "Main shop", "is_default": True},
    ],
    "default_location_id": "main",
}


def load_locations(raw: str | None) -> dict:
    base = deepcopy(DEFAULT_LOCATIONS)
    if not raw:
        return base
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return base
    if not isinstance(data, dict):
        return base
    base["enabled"] = bool(data.get("enabled", False))
    locs = data.get("locations")
    if isinstance(locs, list) and locs:
        cleaned = []
        for loc in locs[:50]:
            if not isinstance(loc, dict):
                continue
            lid = str(loc.get("id") or "").strip()[:40]
            name = str(loc.get("name") or "").strip()[:80]
            if lid and name:
                cleaned.append({
                    "id": lid,
                    "name": name,
                    "is_default": bool(loc.get("is_default")),
                })
        if cleaned:
            base["locations"] = cleaned
    default_id = str(data.get("default_location_id") or "").strip()
    ids = {l["id"] for l in base["locations"]}
    if default_id in ids:
        base["default_location_id"] = default_id
    else:
        base["default_location_id"] = base["locations"][0]["id"]
    return base


def save_locations(cfg: dict) -> str:
    return json.dumps(load_locations(json.dumps(cfg)), sort_keys=True)


# ---------------------------------------------------------------------------
# Period lock
# ---------------------------------------------------------------------------
def load_period_lock(raw: str | None) -> dict:
    base = {"enabled": False, "locked_through": ""}  # YYYY-MM-DD
    if not raw:
        return base
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return base
    if isinstance(data, dict):
        base["enabled"] = bool(data.get("enabled"))
        base["locked_through"] = str(data.get("locked_through") or "")[:10]
    return base


def save_period_lock(cfg: dict) -> str:
    return json.dumps(load_period_lock(json.dumps(cfg)), sort_keys=True)


def is_date_locked(date_iso: str, lock_cfg: dict) -> bool:
    if not lock_cfg.get("enabled"):
        return False
    locked = str(lock_cfg.get("locked_through") or "")
    if not locked or not date_iso:
        return False
    return str(date_iso)[:10] <= locked[:10]


# ---------------------------------------------------------------------------
# Cash drawer — ESC/POS open command (most Epson-compatible drawers)
# ---------------------------------------------------------------------------
# ESC p m t1 t2 — pulse pin 2 (m=0) for t1*2ms on, t2*2ms off
ESC_POS_OPEN_DRAWER = bytes([0x1B, 0x70, 0x00, 0x19, 0xFA])


def cash_drawer_payload() -> bytes:
    return ESC_POS_OPEN_DRAWER


def try_kick_cash_drawer(printer_name: str = "") -> tuple[bool, str]:
    """
    Best-effort drawer kick on Windows via win32 print raw bytes.
    Returns (ok, message). Safe no-op on non-Windows or missing printer.
    """
    try:
        import sys
        if sys.platform != "win32":
            return False, "Cash drawer kick is supported on Windows print queues."
        import win32print  # type: ignore
    except Exception:
        return False, "pywin32 not available — install on the till PC to kick the drawer."

    try:
        name = printer_name or win32print.GetDefaultPrinter()
        if not name:
            return False, "No printer selected for cash drawer."
        handle = win32print.OpenPrinter(name)
        try:
            job = win32print.StartDocPrinter(handle, 1, ("Cash drawer kick", None, "RAW"))
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, cash_drawer_payload())
            win32print.EndPagePrinter(handle)
            win32print.EndDocPrinter(handle)
        finally:
            win32print.ClosePrinter(handle)
        return True, f"Drawer pulse sent to {name}"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Serial / IMEI helpers (storage lives in operations DB; helpers are pure)
# ---------------------------------------------------------------------------
def normalize_serial(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip().upper() if ch.isalnum() or ch in "-_")


def validate_imei(value: str) -> bool:
    """Basic IMEI check: 15 digits + Luhn."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) != 15:
        return False
    total = 0
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0
