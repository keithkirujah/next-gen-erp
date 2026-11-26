"""
Hardware profiles: multi-till registers and barcode scales.

Each till PC stores its profile locally (device-level settings). Scales can
stream weight via serial (COM) or simulate for training. Weight-required
products use the live scale reading when adding to the POS cart.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


def load_hardware_profile(raw: str | None) -> dict:
    base = {
        "till_id": "TILL-1",
        "till_name": "Front counter",
        "store_code": "",
        "receipt_printer": "",
        "cash_drawer_attached": True,
        "customer_display": False,
        "scale": {
            "enabled": False,
            "mode": "manual",  # manual | serial | simulated
            "port": "COM3",
            "baud": 9600,
            "parity": "N",
            "data_bits": 8,
            "stop_bits": 1,
            "unit": "kg",
            "stable_prefix": "",  # optional parse hint
            "simulated_weight": "0.000",
        },
        "barcode_scanner": {
            "suffix": "Enter",  # scanners act as keyboards ending with Enter
            "prefix": "",
        },
        "require_serial_for_tracked": True,
    }
    if not raw:
        return base
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return base
    if not isinstance(data, dict):
        return base
    for k in ("till_id", "till_name", "store_code", "receipt_printer"):
        if k in data:
            base[k] = str(data[k])[:80]
    base["cash_drawer_attached"] = bool(data.get("cash_drawer_attached", True))
    base["customer_display"] = bool(data.get("customer_display", False))
    base["require_serial_for_tracked"] = bool(data.get("require_serial_for_tracked", True))
    scale = data.get("scale") or {}
    if isinstance(scale, dict):
        for k in base["scale"]:
            if k in scale:
                base["scale"][k] = scale[k]
    scan = data.get("barcode_scanner") or {}
    if isinstance(scan, dict):
        for k in base["barcode_scanner"]:
            if k in scan:
                base["barcode_scanner"][k] = scan[k]
    base["scale"]["enabled"] = bool(base["scale"].get("enabled"))
    base["scale"]["mode"] = str(base["scale"].get("mode") or "manual")
    try:
        base["scale"]["baud"] = int(base["scale"].get("baud") or 9600)
    except (TypeError, ValueError):
        base["scale"]["baud"] = 9600
    return base


def save_hardware_profile(cfg: dict) -> str:
    return json.dumps(load_hardware_profile(json.dumps(cfg)), sort_keys=True)


@dataclass
class ScaleReading:
    ok: bool
    weight: Decimal
    unit: str
    stable: bool
    message: str


def read_scale(profile: dict | None = None) -> ScaleReading:
    """
    Read weight from the configured scale.
    - simulated: returns configured simulated_weight
    - manual: returns zero (caller should prompt)
    - serial: best-effort pyserial read (optional dependency)
    """
    profile = load_hardware_profile(json.dumps(profile) if isinstance(profile, dict) else profile)
    scale = profile.get("scale") or {}
    unit = str(scale.get("unit") or "kg")
    if not scale.get("enabled"):
        return ScaleReading(False, Decimal("0"), unit, False, "Scale disabled in hardware profile")

    mode = str(scale.get("mode") or "manual")
    if mode == "simulated":
        try:
            w = Decimal(str(scale.get("simulated_weight") or "0"))
        except Exception:
            w = Decimal("0")
        return ScaleReading(True, w.quantize(Decimal("0.001")), unit, True, "Simulated scale")

    if mode == "manual":
        return ScaleReading(True, Decimal("0"), unit, False, "Enter weight manually")

    if mode == "serial":
        try:
            import serial  # type: ignore
        except Exception:
            return ScaleReading(False, Decimal("0"), unit, False, "pyserial not installed on this till")
        port = str(scale.get("port") or "COM3")
        baud = int(scale.get("baud") or 9600)
        try:
            with serial.Serial(port=port, baudrate=baud, timeout=1.0) as ser:
                # Read a few lines; many scales send continuous frames
                chunk = ser.read(128).decode("ascii", errors="ignore")
                weight = _parse_weight(chunk)
                if weight is None:
                    return ScaleReading(False, Decimal("0"), unit, False, f"No weight parseable from {port}")
                return ScaleReading(True, weight, unit, True, f"Read from {port}")
        except Exception as exc:
            return ScaleReading(False, Decimal("0"), unit, False, str(exc))

    return ScaleReading(False, Decimal("0"), unit, False, f"Unknown scale mode {mode}")


def _parse_weight(text: str) -> Optional[Decimal]:
    """Extract a plausible weight from scale ASCII output."""
    if not text:
        return None
    # Common patterns: "  1.234 kg", "ST,GS,  0.500", plain number
    matches = re.findall(r"(\d+\.\d+|\d+)", text.replace(",", ""))
    if not matches:
        return None
    try:
        value = Decimal(matches[-1])
        if value > Decimal("500"):  # reject absurd kg values from garbage frames
            return None
        return value.quantize(Decimal("0.001"))
    except Exception:
        return None
