"""
api_clients/arkesel.py

SMS notification client using Arkesel's SMS API v2 (sms.arkesel.com), for
toll-passage notifications to vehicle owners. Separate from the actual
charge — this is informational, sent alongside (not gating) momo.charge_toll.

Response schema is inferred from Arkesel's public docs, not verified
against a real account/key (none available yet) — worth a quick sanity
check against a real response once ARKESEL_API_KEY is set.
"""

import requests
from typing import TypedDict

from core.config import ARKESEL_API_KEY, ARKESEL_SENDER_ID

ARKESEL_SEND_URL = "https://sms.arkesel.com/api/v2/sms/send"


class SmsResult(TypedDict):
    success: bool
    message: str


def _to_international(phone_number: str) -> str:
    """Convert a local Ghana number (0XXXXXXXXX) to Arkesel's expected 233XXXXXXXXX.

    Leaves already-international numbers (233... or +233...) unchanged.
    """
    digits = phone_number.strip().lstrip("+")
    if digits.startswith("0"):
        return "233" + digits[1:]
    return digits


def send_sms(phone_number: str, message: str) -> SmsResult:
    """Send an SMS via Arkesel. phone_number may be local (0XXXXXXXXX) or
    international (233XXXXXXXXX / +233XXXXXXXXX) — normalized before sending."""
    if not ARKESEL_API_KEY:
        return SmsResult(success=False, message="ARKESEL_API_KEY not set in environment")

    payload = {
        "sender": ARKESEL_SENDER_ID,
        "message": message,
        "recipients": [_to_international(phone_number)],
    }
    headers = {
        "api-key": ARKESEL_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(ARKESEL_SEND_URL, json=payload, headers=headers, timeout=10)
    except requests.RequestException as e:
        return SmsResult(success=False, message=str(e))

    try:
        data = response.json()
    except ValueError:
        return SmsResult(success=False, message=f"HTTP {response.status_code}: {response.text[:200]}")

    success = data.get("status") == "success"
    return SmsResult(success=success, message=data.get("message", data.get("status", "")))


def send_toll_notification(phone_number: str, toll_point_name: str, amount_ghs: float) -> SmsResult:
    """Notify a vehicle owner they've passed a toll point and will be charged.

    Sent before the charge is attempted (informational — doesn't gate or
    confirm payment, just tells the driver what's about to happen).
    """
    message = (
        f"You've passed {toll_point_name}. "
        f"GHS {amount_ghs:.2f} will be deducted from your account for the toll fee. "
        f"Safe travels!"
    )
    return send_sms(phone_number, message)
