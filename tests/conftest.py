"""
tests/conftest.py

Stubs RPi.GPIO and mfrc522 in sys.modules before collection. Neither
package is installed on this dev machine — both are Pi-only hardware
deps (see requirements.txt) — so importing rfid.reader would otherwise
fail at collection time before any test gets a chance to mock it properly.
"""
import sys
from unittest.mock import MagicMock

_rpi = MagicMock()
_gpio = MagicMock()
_rpi.GPIO = _gpio

sys.modules.setdefault("RPi", _rpi)
sys.modules.setdefault("RPi.GPIO", _gpio)
sys.modules.setdefault("mfrc522", MagicMock())
