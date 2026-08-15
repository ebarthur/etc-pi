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
ANPR_MODEL_PATH = str(
    Path(__file__).resolve().parent.parent / "anpr" / "models" / "plate_detector.pt"
)
ANPR_CAPTURE_DIR = Path(__file__).resolve().parent.parent / "anpr" / "captures"

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