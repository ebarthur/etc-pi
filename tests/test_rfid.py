"""
tests/test_rfid.py

Tests for rfid/reader.py against a mocked MFRC522 (see conftest.py for why
RPi.GPIO / mfrc522 are stubbed at collection time).
"""
from unittest.mock import MagicMock

from rfid.reader import RFIDReader


def _mock_mfrc522(request_status, anticoll_status=None, uid_bytes=None):
    """Build a mock MFRC522 instance with fixed MI_OK/PICC_REQIDL sentinels
    and canned return values for MFRC522_Request / MFRC522_Anticoll."""
    mock = MagicMock()
    mock.MI_OK = "MI_OK"
    mock.PICC_REQIDL = "PICC_REQIDL"
    mock.MFRC522_Request.return_value = (request_status, 0)
    if anticoll_status is not None:
        mock.MFRC522_Anticoll.return_value = (anticoll_status, uid_bytes)
    return mock


def test_read_tag_returns_uid_on_successful_scan(monkeypatch):
    mock_mfrc522 = _mock_mfrc522(
        request_status="MI_OK",
        anticoll_status="MI_OK",
        uid_bytes=[0xA1, 0xB2, 0xC3, 0xD4, 0x00],  # last byte is the BCC checksum
    )
    monkeypatch.setattr("rfid.reader.MFRC522", lambda: mock_mfrc522)

    reader = RFIDReader()
    uid = reader.read_tag(timeout=1.0)

    assert uid == "A1B2C3D4"


def test_read_tag_returns_none_on_timeout(monkeypatch):
    mock_mfrc522 = _mock_mfrc522(request_status="MI_ERR")  # never finds a tag
    monkeypatch.setattr("rfid.reader.MFRC522", lambda: mock_mfrc522)

    reader = RFIDReader()
    uid = reader.read_tag(timeout=0.1)

    assert uid is None


def test_format_uid_keeps_4_byte_uid_unchanged():
    assert RFIDReader._format_uid([0x01, 0x02, 0x03, 0x04]) == "01020304"


def test_format_uid_drops_bcc_checksum_byte():
    assert RFIDReader._format_uid([0x01, 0x02, 0x03, 0x04, 0xFF]) == "01020304"


def test_cleanup_calls_gpio_cleanup(monkeypatch):
    mock_gpio = MagicMock()
    monkeypatch.setattr("rfid.reader.GPIO", mock_gpio)
    monkeypatch.setattr("rfid.reader.MFRC522", MagicMock())

    reader = RFIDReader()
    reader.cleanup()

    mock_gpio.cleanup.assert_called_once()
