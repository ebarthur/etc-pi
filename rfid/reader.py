"""
rfid/reader.py

RC522 RFID reader interface for the Smart Cashless Tolling System.

Reads only the tag UID (unique serial number). The UID is used as a lookup
key against the vehicle record in the local SQLite database — no data is
read from or written to the tag's data blocks.

Usage (from core/main.py):
    from rfid.reader import RFIDReader

    reader = RFIDReader() 
    uid = reader.read_tag(timeout=2.0)
    if uid:
        ...  # look up UID in db.py
    reader.cleanup()
"""

import time
from typing import Optional

import RPi.GPIO as GPIO
from mfrc522 import MFRC522


class RFIDReader:
    """Blocking, timeout-bounded RC522 tag reader.

    Polls the RC522 for a tag UID within a bounded window. Intended to be
    called once per vehicle-detection cycle by the orchestrator, with a
    short poll interval to stay responsive on a Pi 4 without pegging a
    CPU core while idle.
    """

    # Poll interval while waiting for a tag (seconds). Short enough to
    # keep reads responsive within the caller's timeout, long enough not
    # to spin the CPU.
    POLL_INTERVAL = 0.05

    def __init__(self) -> None:
        self._mfrc522 = MFRC522()

    def read_tag(self, timeout: float = 2.0) -> Optional[str]:
        """Poll for a tag and return its UID as a hex string, or None on timeout.

        Args:
            timeout: Max seconds to wait for a tag before giving up. Should
                match the RFID -> ANPR fallback window used by main.py.

        Returns:
            UID as an uppercase hex string (e.g. "A1B2C3D4"), or None if
            no tag was detected within the timeout.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            status, _tag_type = self._mfrc522.MFRC522_Request(
                self._mfrc522.PICC_REQIDL
            )

            if status == self._mfrc522.MI_OK:
                status, uid_bytes = self._mfrc522.MFRC522_Anticoll()
                if status == self._mfrc522.MI_OK:
                    return self._format_uid(uid_bytes)

            time.sleep(self.POLL_INTERVAL)

        return None

    @staticmethod
    def _format_uid(uid_bytes) -> str:
        """Convert the UID byte list from mfrc522 into a hex string.

        The last byte returned by MFRC522_Anticoll is a BCC checksum, not
        part of the UID, so it's dropped when present.
        """
        uid = uid_bytes[:-1] if len(uid_bytes) > 4 else uid_bytes
        return "".join(f"{b:02X}" for b in uid)

    def cleanup(self) -> None:
        """Release GPIO resources. Call once on program shutdown."""
        GPIO.cleanup() 