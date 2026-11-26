"""Paystack client for NextGen POS — M-Pesa / mobile money via Paystack.

Admin supplies only Paystack keys in System Settings. The till never needs
Daraja credentials. Payment auto-detection is done by polling Paystack's
verify endpoint after a charge is started (same pattern many supermarket
tills use when they "wait for the phone prompt").
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional


API_BASE = "https://api.paystack.co"


@dataclass
class PaystackResult:
    ok: bool
    status: str  # pending | success | failed | abandoned | error
    reference: str
    message: str
    raw: dict


def _request(method: str, path: str, secret_key: str, payload: dict | None = None, timeout: int = 30) -> dict:
    url = API_BASE + path
    data = None
    headers = {
        "Authorization": f"Bearer {secret_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "NextGenERP-POS/1.0",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body) if err_body else {}
        except json.JSONDecodeError:
            parsed = {"message": err_body or str(exc)}
        parsed.setdefault("status", False)
        parsed.setdefault("message", str(exc))
        parsed["_http_status"] = exc.code
        return parsed
    except Exception as exc:
        return {"status": False, "message": str(exc)}


def normalize_ke_phone(phone: str) -> str:
    """Normalize Kenyan numbers to 2547XXXXXXXX."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif digits.startswith("7") and len(digits) == 9:
        digits = "254" + digits
    elif digits.startswith("254") and len(digits) == 12:
        pass
    return digits


def initiate_mpesa_charge(
    secret_key: str,
    amount: Decimal,
    phone: str,
    email: str = "customer@pos.local",
    currency: str = "KES",
    reference: str | None = None,
) -> PaystackResult:
    """
    Start an M-Pesa charge through Paystack.
    Customer receives the STK-style prompt on their phone; we then poll verify.
    """
    reference = reference or f"NG-{uuid.uuid4().hex[:16].upper()}"
    phone_norm = normalize_ke_phone(phone)
    if len(phone_norm) != 12 or not phone_norm.startswith("254"):
        return PaystackResult(False, "error", reference, "Enter a valid Kenyan M-Pesa number (e.g. 07XX XXX XXX).", {})

    # Paystack expects amount in the currency's lowest unit (cents/cents-equivalent).
    # KES is zero-decimal on Paystack for many integrations; use major units * 100
    # when the API expects subunits. Paystack Kenya documentation: amount in kobo-like
    # subunits — for KES multiply by 100.
    amount_major = Decimal(str(amount)).quantize(Decimal("0.01"))
    amount_subunit = int((amount_major * 100).to_integral_value())

    payload = {
        "amount": amount_subunit,
        "email": email or "customer@pos.local",
        "currency": (currency or "KES").split()[0][:3].upper(),
        "reference": reference,
        "mobile_money": {
            "phone": phone_norm,
            "provider": "mpesa",
        },
    }
    data = _request("POST", "/charge", secret_key, payload)
    if not data.get("status"):
        msg = data.get("message") or data.get("data", {}).get("message") or "Paystack charge failed"
        return PaystackResult(False, "error", reference, str(msg), data)

    inner = data.get("data") or {}
    status = str(inner.get("status") or "pending").lower()
    # display_text often tells the cashier what the customer should see
    display = inner.get("display_text") or inner.get("message") or data.get("message") or "Prompt sent to phone"
    return PaystackResult(True, status if status in {"pending", "success", "failed"} else "pending",
                          str(inner.get("reference") or reference), str(display), data)


def verify_transaction(secret_key: str, reference: str) -> PaystackResult:
    """Poll Paystack for the final state of a charge (auto-detect payment)."""
    reference = str(reference or "").strip()
    if not reference:
        return PaystackResult(False, "error", "", "Missing payment reference", {})
    data = _request("GET", f"/transaction/verify/{urllib.parse.quote(reference)}", secret_key)
    # urllib.parse used above — import at call site safety
    if not data.get("status"):
        # Before the customer pays, verify may 404 / fail — treat as pending when possible
        msg = str(data.get("message") or "Waiting for customer")
        http = data.get("_http_status")
        if http in {404, 400}:
            return PaystackResult(True, "pending", reference, msg, data)
        return PaystackResult(False, "error", reference, msg, data)

    inner = data.get("data") or {}
    gateway_status = str(inner.get("status") or "").lower()
    if gateway_status == "success":
        return PaystackResult(True, "success", reference, "Payment confirmed", data)
    if gateway_status in {"failed", "abandoned", "reversed"}:
        return PaystackResult(True, gateway_status, reference, f"Payment {gateway_status}", data)
    return PaystackResult(True, "pending", reference, "Waiting for customer to complete payment…", data)


# Local import for quote in verify path
import urllib.parse  # noqa: E402
