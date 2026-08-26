"""
api_clients/momo.py

Paystack hosted-checkout client. Initializes a transaction and hands back a
checkout link (authorization_url) for the vehicle owner to pay through on
their own time/device, via whichever channel they pick on Paystack's page --
this project no longer picks a mobile money provider and charges directly
(see git history for the old /charge-based charge_toll(), replaced because
a live account's mobile money charges require an OTP step this codebase
never handled, and because there's no webhook access on this account to
resolve an async result anyway). Resolution now happens out-of-band, by
workers/charge's cron job polling Paystack's verify endpoint.

Paystack requires amounts in the currency's lowest denomination (pesewas
for GHS), so all amounts are converted from GHS floats before the request.
Per Paystack's own /transaction/initialize docs, `amount` is a String (not
a bare number) and `metadata` must be a JSON-stringified string, not a raw
object -- both confirmed against the official doc text, not just examples.
"""

import json
import logging
import requests
from typing import Optional, TypedDict

from core.config import PAYSTACK_SECRET_KEY, PAYSTACK_BASE_URL

logger = logging.getLogger(__name__)


class InitializeResult(TypedDict):
    success: bool
    authorization_url: Optional[str]
    reference: Optional[str]
    message: str


def initialize_transaction(
    phone_number: str,
    amount_ghs: float,
    vehicle_id: Optional[int] = None,
) -> InitializeResult:
    """Start a Paystack hosted-checkout transaction and return its link.

    Args:
        phone_number: Owner's number -- used only for the placeholder email
            Paystack requires (see below) and stashed in metadata for
            cross-reference; the owner enters their own payment details on
            Paystack's page, so this is never sent as a payment channel.
        amount_ghs: Toll amount in Ghana Cedis (whole currency, not pesewas).
        vehicle_id: Stashed in metadata for cross-reference against the
            local DB from Paystack's dashboard/API if ever needed.

    Returns:
        InitializeResult. Callers store `reference` (core.db's
        set_transaction_reference) so workers/charge's cron polling can
        later resolve this to SUCCESS/FAILED via Paystack's verify endpoint
        -- nothing here is a final outcome.
    """
    if not PAYSTACK_SECRET_KEY:
        return InitializeResult(
            success=False,
            authorization_url=None,
            reference=None,
            message="PAYSTACK_SECRET_KEY not set in environment",
        )

    amount_pesewas = int(round(amount_ghs * 100))

    payload = {
        # Paystack requires an email field but never delivers to it for a
        # mobile money owner. Must not use a reserved special-use TLD
        # (.local/.test/.invalid/etc, RFC 2606/6761/6762) — confirmed
        # empirically Paystack's validator rejects those specifically with
        # "Invalid Email Address Passed" regardless of the rest of the
        # address; it does not check whether the domain actually resolves,
        # so any ordinary public TLD works even if unregistered.
        "email": f"{phone_number}@smarttoll.com",
        # Docs specify amount as a String, not a bare number.
        "amount": str(amount_pesewas),
        "currency": "GHS",
        # Docs specify metadata as a stringified JSON object, not a raw one.
        "metadata": json.dumps({"vehicle_id": vehicle_id, "phone_number": phone_number}),
    }
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    logger.debug(
        "HTTP request: POST %s headers=%s body=%s",
        f"{PAYSTACK_BASE_URL}/transaction/initialize",
        {**headers, "Authorization": "Bearer ***redacted***"},
        payload,
    )
    try:
        response = requests.post(
            f"{PAYSTACK_BASE_URL}/transaction/initialize",
            json=payload,
            headers=headers,
            timeout=10,
        )
        logger.debug(
            "HTTP response: %s %s headers=%s body=%s",
            response.status_code, response.reason, dict(response.headers), response.text,
        )
        data = response.json()
    except requests.RequestException as e:
        logger.warning("Paystack initialize request failed: %s", e)
        return InitializeResult(
            success=False, authorization_url=None, reference=None, message=str(e)
        )

    if not data.get("status"):
        logger.warning("Paystack initialize rejected: %s", data.get("message", "Unknown Paystack error"))
        return InitializeResult(
            success=False,
            authorization_url=None,
            reference=None,
            message=data.get("message", "Unknown Paystack error"),
        )

    init_data = data.get("data", {})
    result = InitializeResult(
        success=True,
        authorization_url=init_data.get("authorization_url"),
        reference=init_data.get("reference"),
        message=data.get("message", ""),
    )
    logger.debug("Paystack initialize result: %s", result)
    return result
