"""
tests/conftest.py

Stubs RPi.GPIO, mfrc522, and picamera2 in sys.modules before collection.
None of these packages are installed on this dev venv — all are Pi-only
hardware deps (see requirements.txt) — so importing rfid.reader or
sensors.presence would otherwise fail at collection time before any test
gets a chance to mock them properly.
"""
import sys
from unittest.mock import MagicMock

_rpi = MagicMock()
_gpio = MagicMock()
_rpi.GPIO = _gpio

sys.modules.setdefault("RPi", _rpi)
sys.modules.setdefault("RPi.GPIO", _gpio)
sys.modules.setdefault("mfrc522", MagicMock())

_picamera2 = MagicMock()
sys.modules.setdefault("picamera2", _picamera2)
