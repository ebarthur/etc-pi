"""
core/main.py

Orchestrator for the Smart Cashless Tolling System. Waits for a vehicle to
arrive (sensors/presence.py), identifies it, matches it against the local
DB, creates a Paystack hosted-checkout link for the toll (api_clients/momo.py),
and SMSes that link to the owner to pay whenever they're ready. Nothing here
resolves the payment itself -- workers/charge's cron job polls Paystack and
updates the transaction later, since there's no webhook access on this
account and mobile money charges need a customer-side OTP step no direct
charge could complete anyway.

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
    python3 -m core.main --dev          # verbose stdout logging (core/dev_log.py) and, on
                                         # the real hardware loop, an MJPEG camera preview at
                                         # http://<pi-ip>:8000/ (core/dev_stream.py).
                                         # Combine with --uid too.
"""

import argparse
import logging
import sys
from typing import Optional

from anpr.yolov11 import PlateRead
from api_clients.arkesel import send_payment_link_sms
from api_clients.momo import initialize_transaction
from core.config import (
    LOG_IDENTIFICATION_MISSES,
    RFID_TIMEOUT_SECONDS,
    TOLL_GATE_NAME,
    TOLL_REPEAT_COOLDOWN_SECONDS,
)
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
from core.dev_log import setup_dev_logging

logger = logging.getLogger(__name__)


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
    logger.debug(
        "_charge_vehicle: vehicle_id=%s method=%s log_kwargs=%s",
        vehicle_id, identification_method, log_kwargs,
    )
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
    logger.debug("Toll rate for vehicle_id=%s (%s): GHS %.2f", vehicle_id, vehicle["vehicle_type"], toll_amount)

    transaction_id = log_transaction(
        vehicle_id=vehicle_id,
        identification_method=identification_method,
        toll_amount=toll_amount,
        **log_kwargs,
    )
    logger.info("transaction_id=%s logged (PENDING) for vehicle_id=%s", transaction_id, vehicle_id)

    # Hosted checkout, not a direct charge: the owner pays on their own time,
    # on their own device, via whichever channel they pick on Paystack's
    # page. This transaction_id has no final outcome yet either way -- a
    # successful initialize just means a payable link exists now.
    # Resolution (SUCCESS/FAILED) happens later, out-of-band, when
    # workers/charge's cron polling verifies this reference against
    # Paystack -- nothing here waits for or assumes an outcome.
    init_result = initialize_transaction(vehicle["phone_number"], toll_amount, vehicle_id=vehicle_id)
    logger.debug("Paystack initialize result for transaction_id=%s: %s", transaction_id, init_result)

    if not init_result["success"]:
        update_transaction_status(transaction_id, "FAILED")
        log_audit_event(
            "PAYMENT_INIT_FAILED", event_detail=init_result["message"], transaction_id=transaction_id
        )
        print(f"Vehicle {vehicle_id}: could not create a payment link (ref=None)")
        return

    set_transaction_reference(transaction_id, init_result["reference"], init_result["authorization_url"])

    sms_result = send_payment_link_sms(
        vehicle["phone_number"],
        TOLL_GATE_NAME,
        toll_amount,
        init_result["authorization_url"],
        plate_number=vehicle["plate_number"],
    )
    logger.debug("Payment-link SMS result for transaction_id=%s: %s", transaction_id, sms_result)
    if not sms_result["success"]:
        log_audit_event(
            "SMS_NOTIFY_FAILED", event_detail=sms_result["message"], transaction_id=transaction_id
        )
    else:
        log_audit_event(
            "PAYMENT_LINK_SENT", event_detail=init_result["reference"], transaction_id=transaction_id
        )

    print(f"Vehicle {vehicle_id}: payment link sent (ref={init_result['reference']})")


def handle_uid(uid: str) -> None:
    """Look up a scanned RFID UID and charge the matched vehicle."""
    logger.debug("RFID uid scanned: %s -- looking up in vehicles table", uid)
    vehicle = get_vehicle_by_rfid(uid)
    if vehicle is None:
        log_audit_event("UNKNOWN_RFID_UID", event_detail=f"uid={uid}")
        logger.warning("NOT REGISTERED IN DATABASE: RFID uid=%s matched no vehicle", uid)
        print(f"Unknown tag: {uid} -- not registered in database")
        return
    logger.info("RFID uid=%s matched vehicle_id=%s", uid, vehicle["vehicle_id"])
    _charge_vehicle(vehicle, "RFID", rfid_uid_scanned=uid)


def handle_plate(plate: PlateRead, capture_path: Optional[str] = None) -> None:
    """Look up an ANPR-read plate and charge the matched vehicle.

    Called once RFID's window times out — see this module's docstring for
    why that's the common case on this hardware, not the exception.
    `capture_path`, when given, is only used for the audit-log line on an
    unrecognized plate, so a miss stays traceable back to the frame that
    produced it.
    """
    logger.debug(
        "ANPR plate read: text=%r ocr_confidence=%.2f detector_confidence=%.2f box=%s -- looking up in vehicles table",
        plate.text, plate.ocr_confidence, plate.detector_confidence, plate.box,
    )
    vehicle = get_vehicle_by_plate(plate.text)
    if vehicle is None:
        detail = f"plate={plate.text!r} confidence={plate.confidence:.2f}"
        if capture_path:
            detail += f" frame={capture_path}"
        log_audit_event("UNKNOWN_ANPR_PLATE", event_detail=detail)
        logger.warning(
            "NOT REGISTERED IN DATABASE: ANPR plate=%r (confidence=%.2f) matched no vehicle%s",
            plate.text, plate.confidence, f" frame={capture_path}" if capture_path else "",
        )
        print(f"Unrecognized plate: {plate.text!r} (confidence={plate.confidence:.2f}) -- not registered in database")
        return
    logger.info("ANPR plate=%r matched vehicle_id=%s", plate.text, vehicle["vehicle_id"])
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

    Genuinely nothing left to try. This is by far the most common outcome of
    a presence trigger in practice (far more common than an actual charge),
    so audit-logging it is gated behind LOG_IDENTIFICATION_MISSES (off by
    default) rather than unconditional -- see that flag's docstring in
    core/config.py for why. Local logging (below) plus the captured frame
    (`capture_path`, written by the caller) is always enough to review a
    miss after the fact regardless of this flag.
    """
    detail = "no RFID tag and no ANPR plate read above confidence threshold"
    if capture_path:
        detail += f"; frame captured to {capture_path}"
    logger.warning("IDENTIFICATION FAILED: %s", detail)
    print(f"No identification within timeout ({detail}) — skipping.")
    if LOG_IDENTIFICATION_MISSES:
        log_audit_event("IDENTIFICATION_FAILED", event_detail=detail)


def run_hardware_loop(dev_active: bool = False) -> None:
    """Wait for a vehicle to arrive, then read RFID, falling through to an
    ANPR read of a freshly captured frame on a timeout, until interrupted.

    Vehicle arrival is a real event now (sensors/presence.py), not a bare
    poll timeout — RFID_TIMEOUT_SECONDS only bounds how long each *arrival*
    gets to present a tag, not how often the loop wakes up while idle.

    `dev_active`, when true (DEV_MODE env var or --dev), also starts
    core/dev_stream.py's MJPEG camera preview server on the same
    PresenceSensor instance — see that module's docstring for why it has to
    run in-process rather than as a separate script.
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

    if dev_active:
        from core.dev_stream import start_mjpeg_server  # deferred: dev-only

        start_mjpeg_server(presence)

    logger.info("Hardware loop starting -- waiting for vehicle presence")
    try:
        while True:
            presence.wait_for_vehicle()
            logger.info("Vehicle presence detected -- opening %.1fs RFID window", RFID_TIMEOUT_SECONDS)
            # Real physical event, naturally rate-limited by actual traffic (not per-frame
            # noise like the motion-diff debug logging), so this is always audit-logged --
            # unlike handle_no_read()'s miss case, there's no write-volume concern here.
            log_audit_event("VEHICLE_ARRIVED")
            uid = reader.read_tag(timeout=RFID_TIMEOUT_SECONDS)
            if uid:
                logger.info("RFID read within window: uid=%s", uid)
                handle_uid(uid)
            else:
                logger.info("No RFID read within window -- falling back to ANPR")
                frame = presence.capture_frame()
                ANPR_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
                capture_path = ANPR_CAPTURE_DIR / f"{int(time.time())}.jpg"
                cv2.imwrite(str(capture_path), frame)
                logger.debug("Frame captured to %s (shape=%s)", capture_path, frame.shape)

                plate = anpr.best_plate(frame)
                if plate is not None:
                    handle_plate(plate, str(capture_path))
                else:
                    logger.debug("ANPR found no plate read above ANPR_OCR_MIN_CONFIDENCE")
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
    parser.add_argument(
        "--plate",
        help="Dev mode: process a single plate as if ANPR read it with full confidence, "
             "without camera hardware, then exit. Mirrors --uid for the ANPR path.",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Field-test dev mode: verbose stdout logging -- presence/motion, ANPR "
             "detection+OCR, DB lookups/writes, full HTTP request/response logging for "
             "SMS/charge calls (core/dev_log.py) -- console only unless --log-file is "
             "also given. On the real hardware loop, also starts an MJPEG camera preview "
             "server at http://<pi-ip>:8000/ (core/dev_stream.py, port via "
             "DEV_STREAM_PORT). Same as setting DEV_MODE=1.",
    )
    parser.add_argument(
        "--log-file",
        help="Also append dev-mode logging to this file (implies --dev). "
             "Off by default -- see core/dev_log.py for why.",
    )
    args = parser.parse_args(argv)

    dev_active = setup_dev_logging(force=args.dev, log_file=args.log_file)
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

    if args.plate:
        # Full confidence, dummy box -- this is a manual stand-in for a real
        # ANPR read, not a real detection, so there's no meaningful
        # detector/OCR score or bounding box to report.
        handle_plate(PlateRead(text=args.plate, ocr_confidence=1.0, detector_confidence=1.0, box=(0, 0, 0, 0)))
        _sync_once(log_failures=True)
        return 0

    run_hardware_loop(dev_active)
    return 0


if __name__ == "__main__":
    sys.exit(main())
