"""
core/config.py

Central configuration: Worker endpoints, timing thresholds, Paystack, SMS.
Toll rates live in the database (toll_rates table) — see core/db.py.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RFID_TIMEOUT_SECONDS = 2.0

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
# Prototype values — adjust to match your Chapter 3 methodology figures.
TOLL_RATES = {
    "car": 5.00,
    "suv": 8.00,
    "bus": 10.00,
    "truck": 15.00,
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
ANPR_DETECTOR_CONFIDENCE = 0.25
# Minimum mean per-character OCR confidence before a plate read is treated as
# usable rather than a guess. The trained checkpoint reports val_accuracy
# ~23.8% (epoch 43-45), so a large share of reads will legitimately fall
# below this — that's the model's current quality, not a bug in the gate.
ANPR_OCR_MIN_CONFIDENCE = 0.5

# --- Vehicle presence (sensors/presence.py) ---
# No dedicated presence sensor (IR break-beam, ultrasonic, inductive loop) is
# available yet, so the camera doubles as the trigger via frame-differencing.
# Threshold/sustain values below were picked from a real empirical baseline on
# this hardware, 2026-08-15: 20 consecutive frames of a static scene at
# (640, 480) measured a mean-abs-pixel-diff noise floor of ~2.2-3.3 (0-255
# scale) between consecutive frames. PRESENCE_MOTION_THRESHOLD sits well above
# that (~3x the observed max), not guessed blind. Re-tune in the field once a
# real vehicle approach is observable, and swap this whole module out for
# real presence hardware if/when one is available.
PRESENCE_RESOLUTION = (640, 480)
PRESENCE_MOTION_THRESHOLD = 10.0
PRESENCE_POLL_INTERVAL_SECONDS = 0.15
# Consecutive above-threshold frames required before treating it as a real
# vehicle arrival rather than single-frame noise/a glitch.
PRESENCE_SUSTAIN_FRAMES = 3
# Consecutive below-threshold frames required before re-arming, so a vehicle
# that's still sitting in frame (e.g. mid-charge) doesn't immediately
# retrigger a second detection.
PRESENCE_CLEAR_FRAMES = 5