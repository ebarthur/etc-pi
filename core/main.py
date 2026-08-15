"""
core/main.py

Orchestrator for the Smart Cashless Tolling System. Identifies a vehicle,
matches it against the local DB, sends a toll-passage SMS notification,
charges the owner via Paystack, and logs the outcome.

Identification is RFID-only for now — ANPR (anpr/yolov11.py) isn't built
yet (models not trained), so an RFID read that times out is logged and
skipped rather than falling through to a fallback that doesn't exist. See
plan.md Phase 5 for where that seam gets filled in.

Usage:
    python3 -m core.main                # real run loop, needs RC522 hardware
    python3 -m core.main --uid A1B2C3D4 # dev mode: process one UID, no hardware, then exit
"""

import argparse
import sys
from typing import Optional

from api_clients.arkesel import send_toll_notification
from api_clients.momo import charge_toll
from core.config import RFID_TIMEOUT_SECONDS, TOLL_GATE_NAME
from core.db import (
    get_toll_rate,
    get_vehicle_by_rfid,
    init_db,
    log_audit_event,
    log_transaction,
    set_transaction_reference,
    update_transaction_status,
)


def handle_uid(uid: str) -> None:
    """Look up a scanned UID, charge the matched vehicle, and log the outcome."""
    vehicle = get_vehicle_by_rfid(uid)
    if vehicle is None:
        log_audit_event("UNKNOWN_RFID_UID", event_detail=f"uid={uid}")
        print(f"Unknown tag: {uid}")
        return

    toll_amount = get_toll_rate(vehicle["vehicle_type"])

    # Best-effort notification, sent before the charge — doesn't gate or
    # confirm payment, so a failure here shouldn't stop the charge attempt.
    sms_result = send_toll_notification(vehicle["phone_number"], TOLL_GATE_NAME, toll_amount)
    if not sms_result["success"]:
        log_audit_event("SMS_NOTIFY_FAILED", event_detail=sms_result["message"])

    transaction_id = log_transaction(
        vehicle_id=vehicle["vehicle_id"],
        identification_method="RFID",
        toll_amount=toll_amount,
        rfid_uid_scanned=uid,
    )

    result = charge_toll(vehicle["phone_number"], toll_amount)

    if result["status"] == "pending":
        # MoMo charges are frequently asynchronous. Leave the transaction as
        # PENDING (its default) — Phase 3's Paystack webhook resolves it to
        # SUCCESS/FAILED once the charge actually completes. It matches by
        # momo_reference, so that has to land on the row now, not just in
        # this audit log entry.
        set_transaction_reference(transaction_id, result["reference"])
        log_audit_event(
            "CHARGE_PENDING", event_detail=result["reference"], transaction_id=transaction_id
        )
        print(f"Vehicle {vehicle['vehicle_id']}: charge pending (ref={result['reference']})")
        return

    status = "SUCCESS" if result["success"] else "FAILED"
    update_transaction_status(transaction_id, status, momo_reference=result["reference"])
    if not result["success"]:
        log_audit_event(
            "CHARGE_FAILED", event_detail=result["message"], transaction_id=transaction_id
        )
    print(f"Vehicle {vehicle['vehicle_id']}: charge {status.lower()} (ref={result['reference']})")


def handle_no_tag() -> None:
    """Called when an RFID read times out with no tag detected.

    ANPR fallback (plate detection -> get_vehicle_by_plate) belongs here once
    anpr/yolov11.py exists — see plan.md Phase 5. Until then, timeouts are
    just logged, not silently dropped.
    """
    log_audit_event("RFID_TIMEOUT", event_detail="no ANPR fallback available yet")
    print("No tag detected within timeout (ANPR fallback not yet built) — skipping.")


def run_hardware_loop() -> None:
    """Poll the RC522 reader continuously until interrupted."""
    from rfid.reader import RFIDReader  # deferred: only needed on real hardware

    reader = RFIDReader()
    try:
        while True:
            uid = reader.read_tag(timeout=RFID_TIMEOUT_SECONDS)
            if uid:
                handle_uid(uid)
            else:
                handle_no_tag()
    except KeyboardInterrupt:
        pass
    finally:
        reader.cleanup()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uid",
        help="Dev mode: process a single UID as if scanned, without RC522 hardware, then exit",
    )
    args = parser.parse_args(argv)

    init_db()

    if args.uid:
        handle_uid(args.uid)
        return 0

    run_hardware_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
