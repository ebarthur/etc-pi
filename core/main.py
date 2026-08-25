"""
core/main.py

Orchestrator for the Smart Cashless Tolling System. Waits for a vehicle to
arrive (sensors/presence.py), identifies it, matches it against the local
DB, sends a toll-passage SMS notification, charges the owner via Paystack,
and logs the outcome.

Identification is RFID-first in the code, and stays structured that way — a
tag read is unambiguous where a plate read is a probabilistic guess (see
anpr/yolov11.py's ANPR_OCR_MIN_CONFIDENCE gate) — but the RC522's real-world
read range on this hardware is well under a vehicle's approach distance, so
in practice most passes fall through the RFID window without a read and it's
ANPR (anpr/yolov11.py) that actually ends up identifying them. On a timeout,
handle_no_tag() below runs the ANPR fallback: capture a full-res frame off
sensors.presence.PresenceSensor, read it with anpr.yolov11.ANPRPipeline, and
either hand a confident plate read to handle_plate() or log the miss.

Usage:
    python3 -m core.main                # real run loop, needs RC522 + camera hardware
    python3 -m core.main --uid A1B2C3D4 # dev mode: process one UID, no hardware, then exit
"""

import argparse
import sys
from typing import Optional

from anpr.yolov11 import PlateRead
from api_clients.arkesel import send_toll_notification
from api_clients.momo import charge_toll
from core.config import RFID_TIMEOUT_SECONDS, TOLL_GATE_NAME, TOLL_REPEAT_COOLDOWN_SECONDS
from core.db import (
    Row,
    _sync_once,
    get_toll_rate,
    get_vehicle_by_plate,
    get_vehicle_by_rfid,
    has_recent_transaction,
    init_db,
    log_audit_event,
    log_transaction,
    set_transaction_reference,
    update_transaction_status,
)


def _charge_vehicle(vehicle: Row, identification_method: str, **log_kwargs) -> None:
    """Shared charge/notify/log flow, once a vehicle has been identified —
    however it was identified (handle_uid's RFID read or handle_plate's
    ANPR read both land here).

    First checks has_recent_transaction(): neither identification path has
    ever had any de-duplication, so without this a vehicle that lingers
    across two arrival events -- or, on a scaled-down test rig, laps the
    same RC car past the gate repeatedly in a short run -- gets billed once
    per pass instead of once per real toll event. TOLL_REPEAT_COOLDOWN_SECONDS
    controls the window (core/config.py).
    """
    vehicle_id = vehicle["vehicle_id"]
    if has_recent_transaction(vehicle_id, TOLL_REPEAT_COOLDOWN_SECONDS):
        log_audit_event(
            "DUPLICATE_TOLL_SKIPPED",
            event_detail=(
                f"vehicle_id={vehicle_id} method={identification_method} "
                f"within {TOLL_REPEAT_COOLDOWN_SECONDS:.0f}s of a prior charge"
            ),
        )
        print(
            f"Vehicle {vehicle_id}: skipped, already charged within the last "
            f"{TOLL_REPEAT_COOLDOWN_SECONDS:.0f}s"
        )
        return

    toll_amount = get_toll_rate(vehicle["vehicle_type"])

    # Best-effort notification, sent before the charge — doesn't gate or
    # confirm payment, so a failure here shouldn't stop the charge attempt.
    sms_result = send_toll_notification(vehicle["phone_number"], TOLL_GATE_NAME, toll_amount)
    if not sms_result["success"]:
        log_audit_event("SMS_NOTIFY_FAILED", event_detail=sms_result["message"])

    transaction_id = log_transaction(
        vehicle_id=vehicle_id,
        identification_method=identification_method,
        toll_amount=toll_amount,
        **log_kwargs,
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
        print(f"Vehicle {vehicle_id}: charge pending (ref={result['reference']})")
        return

    status = "SUCCESS" if result["success"] else "FAILED"
    update_transaction_status(transaction_id, status, momo_reference=result["reference"])
    if not result["success"]:
        log_audit_event(
            "CHARGE_FAILED", event_detail=result["message"], transaction_id=transaction_id
        )
    print(f"Vehicle {vehicle_id}: charge {status.lower()} (ref={result['reference']})")


def handle_uid(uid: str) -> None:
    """Look up a scanned RFID UID and charge the matched vehicle."""
    vehicle = get_vehicle_by_rfid(uid)
    if vehicle is None:
        log_audit_event("UNKNOWN_RFID_UID", event_detail=f"uid={uid}")
        print(f"Unknown tag: {uid}")
        return
    _charge_vehicle(vehicle, "RFID", rfid_uid_scanned=uid)


def handle_plate(plate: PlateRead, capture_path: Optional[str] = None) -> None:
    """Look up an ANPR-read plate and charge the matched vehicle.

    Called once RFID's window times out — see this module's docstring for
    why that's the common case on this hardware, not the exception.
    `capture_path`, when given, is only used for the audit-log line on an
    unrecognized plate, so a miss stays traceable back to the frame that
    produced it.
    """
    vehicle = get_vehicle_by_plate(plate.text)
    if vehicle is None:
        detail = f"plate={plate.text!r} confidence={plate.confidence:.2f}"
        if capture_path:
            detail += f" frame={capture_path}"
        log_audit_event("UNKNOWN_ANPR_PLATE", event_detail=detail)
        print(f"Unrecognized plate: {plate.text!r} (confidence={plate.confidence:.2f})")
        return
    _charge_vehicle(
        vehicle,
        "ANPR",
        anpr_plate_detected=plate.text,
        anpr_confidence=plate.confidence,
        fallback_triggered=True,
    )


def handle_no_read(capture_path: Optional[str] = None) -> None:
    """Called when a confirmed vehicle's RFID window times out AND the ANPR
    fallback found no plate read above ANPR_OCR_MIN_CONFIDENCE.

    Genuinely nothing left to try — logged so it's traceable (and, with
    `capture_path`, reviewable) rather than silently dropped.
    """
    detail = "no RFID tag and no ANPR plate read above confidence threshold"
    if capture_path:
        detail += f"; frame captured to {capture_path}"
    log_audit_event("IDENTIFICATION_FAILED", event_detail=detail)
    print(f"No identification within timeout ({detail}) — skipping.")


def run_hardware_loop() -> None:
    """Wait for a vehicle to arrive, then read RFID, falling through to an
    ANPR read of a freshly captured frame on a timeout, until interrupted.

    Vehicle arrival is a real event now (sensors/presence.py), not a bare
    poll timeout — RFID_TIMEOUT_SECONDS only bounds how long each *arrival*
    gets to present a tag, not how often the loop wakes up while idle.
    """
    import time

    import cv2

    from anpr.yolov11 import ANPRPipeline  # deferred: heavy import
    from rfid.reader import RFIDReader  # deferred: only needed on real hardware
    from sensors.presence import PresenceSensor  # deferred: only needed on real hardware

    from core.config import ANPR_CAPTURE_DIR

    reader = RFIDReader()
    presence = PresenceSensor()
    anpr = ANPRPipeline()
    print("Loading ANPR models (first load is slow on a Pi)...")
    anpr.warmup()
    print("ANPR models ready.")
    try:
        while True:
            presence.wait_for_vehicle()
            uid = reader.read_tag(timeout=RFID_TIMEOUT_SECONDS)
            if uid:
                handle_uid(uid)
            else:
                frame = presence.capture_frame()
                ANPR_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
                capture_path = ANPR_CAPTURE_DIR / f"{int(time.time())}.jpg"
                cv2.imwrite(str(capture_path), frame)

                plate = anpr.best_plate(frame)
                if plate is not None:
                    handle_plate(plate, str(capture_path))
                else:
                    handle_no_read(str(capture_path))
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
