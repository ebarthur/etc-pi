"""
core/main.py

Orchestrator for the Smart Cashless Tolling System. Waits for a vehicle to
arrive (sensors/presence.py), identifies it, matches it against the local
DB, sends a toll-passage SMS notification, charges the owner via Paystack,
and logs the outcome.

Identification is RFID-first: on arrival, a short RFID window is opened; a
successful read goes straight to the charge flow. ANPR fallback
(anpr/yolov11.py) isn't built yet (models not trained) -- a timed-out RFID
window is logged and skipped rather than falling through to a fallback
that doesn't exist. See plan.md Phase 5 for where that seam gets filled in;
handle_no_tag() below is already called at the right point in the flow for
it (once a vehicle's presence is confirmed, not on every idle poll).

Usage:
    python3 -m core.main                # real run loop, needs RC522 + camera hardware
    python3 -m core.main --uid A1B2C3D4 # dev mode: process one UID, no hardware, then exit
"""

import argparse
import sys
from typing import Optional

from api_clients.arkesel import send_toll_notification
from api_clients.momo import charge_toll
from core.config import RFID_TIMEOUT_SECONDS, TOLL_GATE_NAME
from core.db import (
    _sync_once,
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


def handle_no_tag(capture_path: Optional[str] = None) -> None:
    """Called when a confirmed vehicle's RFID window times out with no tag read.

    ANPR fallback (plate detection -> get_vehicle_by_plate) belongs here once
    anpr/yolov11.py exists — see plan.md Phase 5. Until then, timeouts are
    just logged, not silently dropped. `capture_path`, when given, is where
    run_hardware_loop() already saved a fallback frame — logged now so it's
    at least traceable, ready for Phase 5 to actually read it.
    """
    detail = "no ANPR fallback available yet"
    if capture_path:
        detail += f"; frame captured to {capture_path}"
    log_audit_event("RFID_TIMEOUT", event_detail=detail)
    print(f"No tag detected within timeout ({detail}) — skipping.")


def run_hardware_loop() -> None:
    """Wait for a vehicle to arrive, then read RFID (falling through to an ANPR
    capture once that exists), until interrupted.

    Vehicle arrival is a real event now (sensors/presence.py), not a bare
    poll timeout — RFID_TIMEOUT_SECONDS only bounds how long each *arrival*
    gets to present a tag, not how often the loop wakes up while idle.
    """
    import time

    from rfid.reader import RFIDReader  # deferred: only needed on real hardware
    from sensors.presence import PresenceSensor  # deferred: only needed on real hardware

    from core.config import ANPR_CAPTURE_DIR

    reader = RFIDReader()
    presence = PresenceSensor()
    try:
        while True:
            presence.wait_for_vehicle()
            uid = reader.read_tag(timeout=RFID_TIMEOUT_SECONDS)
            if uid:
                handle_uid(uid)
            else:
                ANPR_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
                capture_path = ANPR_CAPTURE_DIR / f"{int(time.time())}.jpg"
                presence.capture_fallback_frame(capture_path)
                handle_no_tag(str(capture_path))
            presence.wait_until_clear()
    except KeyboardInterrupt:
        pass
    finally:
        reader.cleanup()
        presence.cleanup()
        # The background thread only syncs every TURSO_SYNC_INTERVAL_SECONDS
        # (default 30s), so up to that much of the most recent activity can
        # still be un-pushed at shutdown (systemctl stop, a restart, etc.).
        # One last best-effort synchronous push here closes that window —
        # safe to call even when Turso isn't configured or unreachable, see
        # _sync_once()'s docstring.
        _sync_once(log_failures=True)


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
        # Dev-mode one-shot: the process exits right after this, well
        # before the background thread's first TURSO_SYNC_INTERVAL_SECONDS
        # tick (default 30s) — daemon threads are killed outright on
        # interpreter exit, not given a chance to finish. Without this, the
        # transaction/audit rows this run just wrote would sit local-only
        # until some *other* future process happened to call init_db() and
        # sweep them up. One explicit best-effort sync here makes each
        # --uid run's own writes show up on Turso/the dashboard immediately.
        _sync_once(log_failures=True)
        return 0

    run_hardware_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
