"""
api_clients/arkesel.py

SMS client using Arkesel's SMS API v2 (sms.arkesel.com). send_payment_link_sms
below is the only message core/main.py sends synchronously -- the checkout
link a vehicle owner needs to actually pay their toll (momo.py's
initialize_transaction hosted-checkout flow). Reminder and reissue SMS for a
link that's gone unpaid are sent later, out-of-band, by workers/charge's
cron polling (a separate TypeScript reimplementation of send_sms's request
shape, since that runs independently of the Pi).
"""

import logging
import requests
from typing import Optional, TypedDict

from core.config import ARKESEL_API_KEY, ARKESEL_SENDER_ID

logger = logging.getLogger(__name__)

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

    logger.debug(
        "HTTP request: POST %s headers=%s body=%s",
        ARKESEL_SEND_URL,
        {**headers, "api-key": "***redacted***"},
        payload,
    )
    try:
        response = requests.post(ARKESEL_SEND_URL, json=payload, headers=headers, timeout=10)
        logger.debug(
            "HTTP response: %s %s headers=%s body=%s",
            response.status_code, response.reason, dict(response.headers), response.text,
        )
    except requests.RequestException as e:
        logger.warning("Arkesel SMS request failed: %s", e)
        return SmsResult(success=False, message=str(e))

    try:
        data = response.json()
    except ValueError:
        logger.warning("Arkesel SMS non-JSON response: HTTP %s", response.status_code)
        return SmsResult(success=False, message=f"HTTP {response.status_code}: {response.text[:200]}")

    success = data.get("status") == "success"
    result = SmsResult(success=success, message=data.get("message", data.get("status", "")))
    logger.debug("Arkesel SMS result: %s", result)
    return result


def send_payment_link_sms(
    phone_number: str,
    toll_point_name: str,
    amount_ghs: float,
    authorization_url: str,
    plate_number: Optional[str] = None,
) -> SmsResult:
    """Send a vehicle owner their Paystack checkout link to pay a toll.

    Sent right after momo.initialize_transaction() succeeds -- this is the
    only way the owner learns there's a toll to pay and where to pay it;
    nothing else in the flow charges them directly. `plate_number` can be
    None (a vehicle may be registered with only an rfid_uid, no plate — see
    scripts/register_vehicle.py), so the copy falls back to "Your vehicle".
    """
    vehicle_ref = plate_number or "Your vehicle"
    message = (
        f"You've passed {toll_point_name}. "
        f"{vehicle_ref}'s GHS {amount_ghs:.2f} toll is ready to pay: {authorization_url} "
        f"Safe travels!"
    )
    return send_sms(phone_number, message)
