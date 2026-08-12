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