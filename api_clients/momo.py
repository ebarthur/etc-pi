"""
api_clients/momo.py

Mobile money charge client, using Paystack's Charge API (mobile_money
channel) to simulate an MTN MoMo toll deduction.

Paystack requires amounts in the currency's lowest denomination (pesewas
for GHS), so all amounts are converted from GHS floats before the request.
"""

import requests
from typing import Optional, TypedDict

from core.config import PAYSTACK_SECRET_KEY, PAYSTACK_BASE_URL

# Paystack's supported mobile money provider codes for Ghana.
MOMO_PROVIDER_MAP = {
    "mtn": "mtn",
    "vodafone": "vod",   # Telecel Cash, legacy Paystack code
    "airteltigo": "atl",
}
DEFAULT_PROVIDER = "mtn"


class ChargeResult(TypedDict):
    success: bool
    reference: Optional[str]
    status: str          # e.g. 'success', 'pending', 'failed'
    message: str


def charge_toll(
    phone_number: str,
    amount_ghs: float,
    provider: str = DEFAULT_PROVIDER,
) -> ChargeResult:
    """Initiate a mobile money toll deduction via Paystack.

    Args:
        phone_number: Owner's mobile money number, from ghana_card.py's
            OwnerRecord.
        amount_ghs: Toll amount in Ghana Cedis (whole currency, not pesewas).
        provider: One of MOMO_PROVIDER_MAP keys. Defaults to MTN.

    Returns:
        ChargeResult with the outcome. Callers should treat 'pending' as
        not-yet-final — Paystack mobile money charges are often
        asynchronous and may require a webhook or status poll to confirm.
    """
    if not PAYSTACK_SECRET_KEY:
        return ChargeResult(
            success=False,
            reference=None,
            status="failed",
            message="PAYSTACK_SECRET_KEY not set in environment",
        )

    provider_code = MOMO_PROVIDER_MAP.get(provider, MOMO_PROVIDER_MAP[DEFAULT_PROVIDER])
    amount_pesewas = int(round(amount_ghs * 100))

    payload = {
        # Paystack requires an email field but never delivers to it for a
        # mobile money charge. Must not use a reserved special-use TLD
        # (.local/.test/.invalid/etc, RFC 2606/6761/6762) — confirmed
        # empirically Paystack's validator rejects those specifically with
        # "Invalid Email Address Passed" regardless of the rest of the
        # address; it does not check whether the domain actually resolves,
        # so any ordinary public TLD works even if unregistered.
        "email": f"{phone_number}@smarttoll.com",
        "amount": amount_pesewas,
        "currency": "GHS",
        "mobile_money": {
            "phone": phone_number,
            "provider": provider_code,
        },
    }
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            f"{PAYSTACK_BASE_URL}/charge",
            json=payload,
            headers=headers,
            timeout=10,
        )
        data = response.json()
    except requests.RequestException as e:
        return ChargeResult(
            success=False, reference=None, status="failed", message=str(e)
        )

    if not data.get("status"):
        return ChargeResult(
            success=False,
            reference=None,
            status="failed",
            message=data.get("message", "Unknown Paystack error"),
        )

    charge_data = data.get("data", {})
    return ChargeResult(
        success=charge_data.get("status") == "success",
        reference=charge_data.get("reference"),
        status=charge_data.get("status", "unknown"),
        message=data.get("message", ""),
    )