"""Admin-configurable payment providers for NextGen ERP + POS.

Paystack is the preferred path for M-Pesa STK-style prompts: the administrator
only enters Paystack keys. The till never stores Daraja secrets.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

PROVIDER_CATALOGUE = {
    "cash": {
        "id": "cash",
        "label": "Cash",
        "description": "Physical cash. Change calculated automatically.",
        "requires_reference": False,
        "supports_change": True,
        "supports_auto_detect": False,
        "fields": [],
    },
    "paystack_mpesa": {
        "id": "paystack_mpesa",
        "label": "M-Pesa (Paystack)",
        "description": "Customer gets a phone prompt via Paystack. Till auto-detects when they pay.",
        "requires_reference": False,
        "supports_change": False,
        "supports_auto_detect": True,
        "fields": [
            {"key": "secret_key", "label": "Paystack Secret Key", "secret": True, "placeholder": "sk_live_... or sk_test_..."},
            {"key": "public_key", "label": "Paystack Public Key", "secret": False, "placeholder": "pk_live_... or pk_test_..."},
            {"key": "callback_email", "label": "Default receipt email (optional)", "secret": False, "placeholder": "shop@business.co.ke"},
            {"key": "environment", "label": "Environment", "secret": False, "placeholder": "test or live"},
        ],
    },
    "paystack_card": {
        "id": "paystack_card",
        "label": "Card (Paystack)",
        "description": "Card payments routed through Paystack when you use hosted or terminal flows.",
        "requires_reference": True,
        "supports_change": False,
        "supports_auto_detect": True,
        "fields": [
            {"key": "secret_key", "label": "Paystack Secret Key", "secret": True, "placeholder": "sk_live_... or sk_test_..."},
            {"key": "public_key", "label": "Paystack Public Key", "secret": False, "placeholder": "pk_live_..."},
            {"key": "environment", "label": "Environment", "secret": False, "placeholder": "test or live"},
        ],
    },
    "mpesa_manual": {
        "id": "mpesa_manual",
        "label": "M-Pesa (manual code)",
        "description": "Customer pays Paybill/Till themselves; cashier types the M-Pesa code.",
        "requires_reference": True,
        "supports_change": False,
        "supports_auto_detect": False,
        "fields": [
            {"key": "shortcode", "label": "Paybill / Till number (shown to cashier)", "secret": False, "placeholder": "e.g. 123456"},
            {"key": "account_hint", "label": "Account hint", "secret": False, "placeholder": "e.g. use sale number"},
        ],
    },
    "card_terminal": {
        "id": "card_terminal",
        "label": "Card (POS terminal)",
        "description": "External card machine — record approval after the customer pays.",
        "requires_reference": False,
        "supports_change": False,
        "supports_auto_detect": False,
        "fields": [
            {"key": "terminal_id", "label": "Terminal ID (optional)", "secret": False, "placeholder": ""},
        ],
    },
    "bank": {
        "id": "bank",
        "label": "Bank transfer",
        "description": "Customer transfers to your account; capture the bank reference.",
        "requires_reference": False,
        "supports_change": False,
        "supports_auto_detect": False,
        "fields": [
            {"key": "bank_name", "label": "Bank name", "secret": False, "placeholder": "KCB / Equity / …"},
            {"key": "account_name", "label": "Account name", "secret": False, "placeholder": ""},
            {"key": "account_number", "label": "Account number", "secret": False, "placeholder": ""},
        ],
    },
    "customer_account": {
        "id": "customer_account",
        "label": "Customer account (credit)",
        "description": "Charge a named customer's running account.",
        "requires_reference": False,
        "supports_change": False,
        "supports_auto_detect": False,
        "fields": [
            {"key": "default_credit_days", "label": "Default credit days", "secret": False, "placeholder": "30"},
        ],
    },
    "other": {
        "id": "other",
        "label": "Other",
        "description": "Cheque, voucher, or any other channel.",
        "requires_reference": False,
        "supports_change": False,
        "supports_auto_detect": False,
        "fields": [],
    },
}

DEFAULT_ENABLED = ["cash", "paystack_mpesa", "mpesa_manual", "card_terminal", "other"]


def default_providers_config() -> dict:
    providers = {}
    for pid, meta in PROVIDER_CATALOGUE.items():
        providers[pid] = {
            "enabled": pid in DEFAULT_ENABLED,
            "label": meta["label"],
            "credentials": {f["key"]: "" for f in meta["fields"]},
        }
    return {
        "providers": providers,
        "manager_pin": "",
        "allow_discounts": True,
        "max_discount_percent": 25,
        "require_discount_reason": False,
        "allow_returns": True,
        "require_return_reason": True,
        "auto_detect_timeout_sec": 90,
    }


def load_providers_config(raw: str | None) -> dict:
    base = default_providers_config()
    if not raw:
        return base
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return base
    if not isinstance(data, dict):
        return base
    merged = deepcopy(base)
    incoming = data.get("providers") or {}
    # Migrate legacy "paystack" / "mpesa" keys if present
    legacy_map = {
        "paystack": "paystack_mpesa",
        "mpesa": "mpesa_manual",
        "card": "card_terminal",
    }
    for old, new in legacy_map.items():
        if old in incoming and new not in incoming:
            incoming[new] = incoming[old]
    for pid, meta in PROVIDER_CATALOGUE.items():
        if pid in incoming and isinstance(incoming[pid], dict):
            row = incoming[pid]
            merged["providers"][pid]["enabled"] = bool(row.get("enabled", merged["providers"][pid]["enabled"]))
            creds = row.get("credentials") or {}
            for field in meta["fields"]:
                key = field["key"]
                if key in creds:
                    merged["providers"][pid]["credentials"][key] = str(creds[key])[:500]
            # Copy secret_key from sibling paystack providers if empty
            if pid.startswith("paystack_") and not merged["providers"][pid]["credentials"].get("secret_key"):
                for other in ("paystack_mpesa", "paystack_card"):
                    sk = (incoming.get(other) or {}).get("credentials", {}).get("secret_key") or ""
                    if sk:
                        merged["providers"][pid]["credentials"]["secret_key"] = str(sk)[:500]
                        break
    for key in ("manager_pin", "allow_discounts", "max_discount_percent",
                "require_discount_reason", "allow_returns", "require_return_reason",
                "auto_detect_timeout_sec"):
        if key in data:
            merged[key] = data[key]
    try:
        merged["max_discount_percent"] = max(0, min(100, float(merged.get("max_discount_percent") or 25)))
    except (TypeError, ValueError):
        merged["max_discount_percent"] = 25
    try:
        merged["auto_detect_timeout_sec"] = max(30, min(300, int(merged.get("auto_detect_timeout_sec") or 90)))
    except (TypeError, ValueError):
        merged["auto_detect_timeout_sec"] = 90
    merged["manager_pin"] = str(merged.get("manager_pin") or "")[:20]
    return merged


def save_providers_config(config: dict) -> str:
    clean = load_providers_config(json.dumps(config))
    return json.dumps(clean, sort_keys=True)


def enabled_payment_methods(config: dict | None = None) -> list[str]:
    cfg = config or default_providers_config()
    methods = []
    for pid, meta in PROVIDER_CATALOGUE.items():
        row = (cfg.get("providers") or {}).get(pid) or {}
        if row.get("enabled", pid in DEFAULT_ENABLED):
            methods.append(meta["label"])
    return methods or ["Cash"]


def provider_meta_by_label(label: str) -> dict | None:
    for meta in PROVIDER_CATALOGUE.values():
        if meta["label"] == label:
            return meta
    return None


def provider_requires_reference(label: str, config: dict | None = None) -> bool:
    meta = provider_meta_by_label(label)
    return bool(meta and meta.get("requires_reference"))


def provider_supports_change(label: str) -> bool:
    meta = provider_meta_by_label(label)
    return bool(meta and meta.get("supports_change"))


def provider_supports_auto_detect(label: str) -> bool:
    meta = provider_meta_by_label(label)
    return bool(meta and meta.get("supports_auto_detect"))


def provider_credentials(label: str, config: dict | None = None) -> dict:
    cfg = config or default_providers_config()
    for pid, meta in PROVIDER_CATALOGUE.items():
        if meta["label"] == label:
            row = (cfg.get("providers") or {}).get(pid) or {}
            return dict(row.get("credentials") or {})
    return {}


def paystack_secret_key(config: dict | None = None) -> str:
    cfg = config or default_providers_config()
    for pid in ("paystack_mpesa", "paystack_card"):
        row = (cfg.get("providers") or {}).get(pid) or {}
        key = str((row.get("credentials") or {}).get("secret_key") or "").strip()
        if key:
            return key
    return ""


def mask_secret(value: str) -> str:
    """Mask a secret for display in admin UI (show last 4 chars only)."""
    value = str(value or "")
    if len(value) <= 4:
        return "••••" if value else ""
    return "••••" + value[-4:]

