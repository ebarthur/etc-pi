"""
api_clients/ghana_card.py

Mock Ghana Card verification client for the Smart Cashless Tolling System.

Simulates the National Identification Authority's owner-lookup and
verification service. Backed by a static JSON file (fixtures/ghana_cards.json)
keyed by RFID tag UID.

verify_owner performs two checks, mirroring what a real /verify Worker call
would do:
  1. Lookup - does a record exist for this UID?
  2. Cross-validation - does the ghana_card_id on file match what the
     database has stored against this vehicle? A mismatch indicates the
     vehicle's registered identity is stale or was tampered with.
"""

import json
from pathlib import Path
from typing import Optional, TypedDict

DATA_PATH = Path(__file__).resolve().parent / "fixtures" / "ghana_cards.json"


class OwnerRecord(TypedDict):
    ghana_card_id: str
    name: str
    phone_number: str


class VerificationResult(TypedDict):
    verified: bool
    reason: str                       # 'OK', 'NOT_FOUND', 'ID_MISMATCH'
    owner: Optional[OwnerRecord]


_cache: Optional[dict] = None


def _load_records() -> dict:
    global _cache
    if _cache is None:
        with open(DATA_PATH, "r") as f:
            _cache = json.load(f)
    return _cache


def verify_owner(rfid_uid: str, expected_ghana_card_id: str) -> VerificationResult:
    """Verify a vehicle's owner identity against the mock Ghana Card registry.

    Args:
        rfid_uid: UID from rfid.reader.RFIDReader.read_tag().
        expected_ghana_card_id: The ghana_card_id stored on the vehicle's
            row in db.py (vehicles.ghana_card_id) — what the system
            currently believes is linked to this tag.

    Returns:
        VerificationResult. 'verified' is True only if a record exists
        AND its ghana_card_id matches expected_ghana_card_id.
    """
    records = _load_records()
    record = records.get(rfid_uid)

    if record is None:
        return VerificationResult(verified=False, reason="NOT_FOUND", owner=None)

    if record["ghana_card_id"] != expected_ghana_card_id:
        return VerificationResult(verified=False, reason="ID_MISMATCH", owner=record)

    return VerificationResult(verified=True, reason="OK", owner=record)