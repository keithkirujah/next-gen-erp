"""Point of Sale appearance and behaviour preferences (per business workspace)."""
from __future__ import annotations

import json

APPEARANCE_OPTIONS = (
    ("immersive_dark", "Immersive dark (neon till)"),
    ("match_app", "Match app theme"),
    ("vibrant_light", "Vibrant light till"),
)

DEFAULT_POS_CONFIG = {
    "appearance": "immersive_dark",
    "show_live_badge": True,
    "large_pay_button": True,
    "pulse_pay_button": True,
    "show_discount_row": True,
    "compact_mode": False,
    "default_customer": "Walk-in Customer",
}


def load_pos_config(raw: str | None) -> dict:
    cfg = dict(DEFAULT_POS_CONFIG)
    if not raw:
        return cfg
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    appearance = str(data.get("appearance") or cfg["appearance"])
    if appearance not in {k for k, _ in APPEARANCE_OPTIONS}:
        appearance = "immersive_dark"
    cfg["appearance"] = appearance
    for key in ("show_live_badge", "large_pay_button", "pulse_pay_button", "show_discount_row", "compact_mode"):
        if key in data:
            cfg[key] = bool(data[key])
    if "default_customer" in data:
        cfg["default_customer"] = str(data.get("default_customer") or "Walk-in Customer")[:80]
    return cfg


def save_pos_config(cfg: dict) -> str:
    return json.dumps(load_pos_config(json.dumps(cfg)), sort_keys=True)
