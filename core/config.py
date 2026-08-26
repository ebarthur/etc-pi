"""
core/config.py

Central configuration: Worker endpoints, timing thresholds, Paystack, SMS.
Toll rates live in the database (toll_rates table) — see core/db.py.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Overridable via env: the RC522's real-world read range on this hardware
# is well under a vehicle's approach distance, so this window mostly just
# bounds how long ANPR waits its turn behind RFID at each arrival, not how
# long a tag actually gets to be read. Shorten it for a rig where a
# presence-triggered pass is over quickly (e.g. a miniature RC-car test
# track) so ANPR gets to run sooner.
RFID_TIMEOUT_SECONDS = float(os.environ.get("RFID_TIMEOUT_SECONDS") or "2.0")

PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_BASE_URL = "https://api.paystack.co"

# --- Turso (libSQL embedded replica) ---
# Both unset -> core/db.py falls back to a plain local file (dev/test, no
# network involved). Both set -> vehicles/toll_rates sync down from Turso
# and transactions/audit_log sync up, in the background, on a schedule set
# by TURSO_SYNC_INTERVAL_SECONDS. See plan.md Phase 4.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
TURSO_SYNC_INTERVAL_SECONDS = float(os.environ.get("TURSO_SYNC_INTERVAL_SECONDS") or "30")

# --- SMS (Arkesel) ---
ARKESEL_API_KEY = os.environ.get("ARKESEL_API_KEY", "")
# Arkesel sender IDs must be pre-registered/approved with Arkesel and are capped at 11
# alphanumeric characters.
ARKESEL_SENDER_ID = os.environ.get("ARKESEL_SENDER_ID", "SmartToll")
# Prototype placeholder — name the actual toll point once one exists.
TOLL_GATE_NAME = os.environ.get("TOLL_GATE_NAME", "the N1 Highway Toll Point")

# --- Toll rates (GHS) by vehicle type ---
# Prototype values — adjust to match your Chapter 3 methodology figures. Scaled to a max of
# 1.50 (2026-08-26, was 15.00) for live testing against real Paystack/Arkesel accounts.
TOLL_RATES = {
    "car": 0.50,
    "suv": 0.80,
    "bus": 1.00,
    "truck": 1.50,
}

DEFAULT_TOLL_RATE = TOLL_RATES["car"]

# --- ANPR ---
# Two separate models, two different frameworks — confirmed 2026-08-19 by
# inspecting the pickled class refs inside each file:
#   crop.pt   ultralytics YOLOv11n DetectionModel (run 'yolo11n_lpr_run1'),
#             detects the plate region. Loads with ultralytics.YOLO().
#   ocr.ckpt  PARSeq (https://github.com/baudm/parseq) scene-text-recognition
#             model saved as a full PyTorch Lightning checkpoint — state_dict
#             plus optimizer/scheduler/epoch state and a `hyper_parameters`
#             block. NOT a YOLO model and NOT loadable by ultralytics; it
#             needs the PARSeq architecture rebuilt from those hparams. See
#             anpr/parseq_infer.py.
# Both live directly in anpr/, not the anpr/models/ subdirectory an earlier
# draft of this file assumed (that path never existed on disk).
_ANPR_DIR = Path(__file__).resolve().parent.parent / "anpr"
ANPR_DETECTOR_PATH = str(_ANPR_DIR / "crop.pt")
ANPR_OCR_CHECKPOINT_PATH = str(_ANPR_DIR / "ocr.ckpt")
ANPR_CAPTURE_DIR = _ANPR_DIR / "captures"

# Minimum YOLO box confidence before a detected region is worth running OCR
# on. Deliberately low — the detector is single-class (plate/not-plate) and
# the OCR step has its own confidence gate below, so a permissive detector
# threshold costs little and avoids dropping small/distant plates outright.
# Overridable via env — expect to retune this against a printed miniature
# plate, which the detector never saw in training.
ANPR_DETECTOR_CONFIDENCE = float(os.environ.get("ANPR_DETECTOR_CONFIDENCE") or "0.25")
# Minimum mean per-character OCR confidence before a plate read is treated as
# usable rather than a guess. The trained checkpoint reports val_accuracy
# ~23.8% (epoch 43-45), so a large share of reads will legitimately fall
# below this — that's the model's current quality, not a bug in the gate.
# Overridable via env for the same reason as the detector threshold above.
ANPR_OCR_MIN_CONFIDENCE = float(os.environ.get("ANPR_OCR_MIN_CONFIDENCE") or "0.5")

# Full-res capture used for ANPR (sensors/presence.py's "main" stream and
# anpr/live_test.py's RoadsideCamera) — separate from PRESENCE_RESOLUTION
# below, which is deliberately low-res and only good for motion detection.
# The IMX708 natively does 4608x2592, but that mode caps at ~14fps; 2304x1296
# is the sensor's 56fps binned mode. Overridable via env — a miniature rig
# with the camera mounted close to a small printed plate may read fine at a
# smaller/cheaper resolution than a roadside setup needs.
ANPR_CAPTURE_RESOLUTION = (
    int(os.environ.get("ANPR_CAPTURE_WIDTH") or "2304"),
    int(os.environ.get("ANPR_CAPTURE_HEIGHT") or "1296"),
)
# The camera module is physically mounted rotated, so frames land ~90 degrees
# from upright and the sensor's own metadata doesn't report it — both models
# were trained on upright plates. Confirmed via `python3 -m anpr.live_test
# --calibrate`; re-run that after remounting the camera on a different rig.
ANPR_CAPTURE_ROTATION = int(os.environ.get("ANPR_CAPTURE_ROTATION") or "90")

# --- Vehicle presence (sensors/presence.py) ---
# No dedicated presence sensor (IR break-beam, ultrasonic, inductive loop) is
# available yet, so the camera doubles as the trigger via frame-differencing,
# on a cheap "lores" stream separate from the full-res "main" stream ANPR
# reads (see ANPR_CAPTURE_RESOLUTION above). Threshold/sustain values below
# were picked from a real empirical baseline on this hardware, 2026-08-15: 20
# consecutive frames of a static scene at (640, 480) measured a mean-abs-
# pixel-diff noise floor of ~2.2-3.3 (0-255 scale) between consecutive
# frames. PRESENCE_MOTION_THRESHOLD sits well above that (~3x the observed
# max), not guessed blind.
#
# All of these are env-overridable specifically so a miniature rig (a small
# RC car instead of a real vehicle, camera mounted close) can be retuned
# without touching code — a small subject fills much less of the frame than
# a real vehicle at roadside distance, so the noise-floor baseline above
# doesn't necessarily transfer; re-measure it for the actual rig geometry
# before trusting the default threshold. Swap this whole module out for real
# presence hardware if/when one is available.
PRESENCE_RESOLUTION = (
    int(os.environ.get("PRESENCE_WIDTH") or "640"),
    int(os.environ.get("PRESENCE_HEIGHT") or "480"),
)
PRESENCE_MOTION_THRESHOLD = float(os.environ.get("PRESENCE_MOTION_THRESHOLD") or "10.0")
PRESENCE_POLL_INTERVAL_SECONDS = float(os.environ.get("PRESENCE_POLL_INTERVAL_SECONDS") or "0.15")
# Consecutive above-threshold frames required before treating it as a real
# vehicle arrival rather than single-frame noise/a glitch.
PRESENCE_SUSTAIN_FRAMES = int(os.environ.get("PRESENCE_SUSTAIN_FRAMES") or "3")
# Consecutive below-threshold frames required before re-arming, so a vehicle
# that's still sitting in frame (e.g. mid-charge) doesn't immediately
# retrigger a second detection.
PRESENCE_CLEAR_FRAMES = int(os.environ.get("PRESENCE_CLEAR_FRAMES") or "5")

# --- Dev-mode verbose logging (core/dev_log.py) ---
# Off by default -- a real/production run should stay quiet, and this is
# stdout-only regardless of this flag (never a FileHandler): this Pi's SD
# card has already shown real corruption (plan.md Phase 0), so dev-mode
# verbosity must add console noise, not write volume. Turn on for a field-
# test session to trace every stage: presence/motion, ANPR detection+OCR, DB
# lookups/writes, SMS/charge calls. `python3 -m core.main --dev` enables this
# for one run without touching the environment.
DEV_MODE = os.environ.get("DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")

# --- Dev-mode camera preview stream (core/dev_stream.py) ---
# Full-res ANPR frame (the same frame sensors.presence.PresenceSensor hands
# to anpr.yolov11.ANPRPipeline) served as MJPEG over HTTP, gated by DEV_MODE
# above -- since Picamera2 only lets one process hold the camera at a time,
# this is what lets you actually watch what the camera/ANPR pipeline sees
# live during a field-test session, from a browser, without a second
# process fighting the running one for the camera.
DEV_STREAM_PORT = int(os.environ.get("DEV_STREAM_PORT") or "8000")
# Deliberately low: this Pi's CPU is already busy with presence detection
# and, on an RFID timeout, the ANPR models -- encoding/serving full-res
# JPEGs faster than this risks starving those. Override via env if a given
# rig has CPU headroom to spare.
DEV_STREAM_FPS = float(os.environ.get("DEV_STREAM_FPS") or "3.0")

# --- Repeat-toll cooldown ---
# Neither identification path (RFID or ANPR) has ever had any de-duplication
# — every confirmed arrival charges, unconditionally. That's a real gap once
# ANPR is the path actually doing the identifying most of the time (see
# core/main.py): a vehicle that lingers across two arrival events, or, on a
# scaled-down test rig, laps the same RC car past the gate repeatedly in a
# short run, would otherwise be billed once per pass instead of once per
# real toll event. Any transaction already logged for a vehicle within this
# window suppresses a new charge — see core/db.py's has_recent_transaction()
# and core/main.py's _charge_vehicle(). Shorten this for rig testing if you
# deliberately want to re-pass the same vehicle sooner than the default.
TOLL_REPEAT_COOLDOWN_SECONDS = float(os.environ.get("TOLL_REPEAT_COOLDOWN_SECONDS") or "60")