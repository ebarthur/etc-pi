"""
tests/test_integration.py

End-to-end test of the identify -> create payment link -> log loop
(core.main.handle_uid, core.main.handle_plate) against a throwaway SQLite
file, not db/tolling.db. Paystack's and Arkesel's HTTP calls are mocked so
this suite runs fully offline and deterministically. Resolving a payment
link to SUCCESS/FAILED happens later, out-of-band, via workers/charge's
cron polling (TypeScript, not covered by this Python suite).

momo.py and arkesel.py both do a bare `import requests`, so they share the
same `requests` module object — patching `requests.post` once (routed by
URL) covers both call sites rather than needing two separate patches that
would silently clobber each other.
"""
from unittest.mock import MagicMock

import pytest
import requests

import api_clients.arkesel as arkesel
import api_clients.momo as momo
import core.db as db
import core.main as main
from anpr.yolov11 import PlateRead

PAYSTACK_INITIALIZE_SUCCESS = {
    "status": True,
    "message": "Authorization URL created",
    "data": {
        "authorization_url": "https://checkout.paystack.com/ref_123",
        "access_code": "ref_123",
        "reference": "ref_123",
    },
}
PAYSTACK_INITIALIZE_FAILURE = {
    "status": False,
    "message": "Invalid key",
    "data": {},
}
ARKESEL_SUCCESS = {"status": "success", "message": "sent"}


def _fake_response(json_data):
    response = MagicMock()
    response.json.return_value = json_data
    return response


def _patch_http(monkeypatch, paystack_data=PAYSTACK_INITIALIZE_SUCCESS, arkesel_data=ARKESEL_SUCCESS):
    """Patches requests.post and returns the list of (url, kwargs) calls made,
    so tests can inspect what was actually sent (e.g. the SMS body)."""
    calls = []

    def side_effect(url, **kwargs):
        calls.append((url, kwargs))
        if url.startswith(momo.PAYSTACK_BASE_URL):
            return _fake_response(paystack_data)
        if url == arkesel.ARKESEL_SEND_URL:
            return _fake_response(arkesel_data)
        raise AssertionError(f"Unexpected POST to {url}")

    monkeypatch.setattr(requests, "post", MagicMock(side_effect=side_effect))
    return calls


@pytest.fixture(autouse=True)
def throwaway_db(tmp_path, monkeypatch):
    """Point core.db at a fresh SQLite file per test instead of db/tolling.db.

    Also blanks Turso config before init_db() runs: this module's Paystack/
    Arkesel HTTP mocking only keeps the suite offline if init_db() doesn't
    separately reach out to a real Turso DB — which it otherwise would
    whenever real TURSO_DATABASE_URL/TURSO_AUTH_TOKEN are set in .env (a real
    account is now configured for Phase 9 live testing; this suite must stay
    offline and deterministic regardless).
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_tolling.db")
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", "")
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "")
    db.init_db()


@pytest.fixture(autouse=True)
def mock_credentials(monkeypatch):
    """Both clients short-circuit to a "not configured" failure if their key
    is blank, so give them something non-empty regardless of local .env state."""
    monkeypatch.setattr(momo, "PAYSTACK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setattr(arkesel, "ARKESEL_API_KEY", "fake_key")


def _register_test_vehicle(rfid_uid):
    return db.register_vehicle(
        phone_number="0551234567",
        rfid_uid=rfid_uid,
        plate_number=f"GT-{rfid_uid}",
        owner_name="Test Owner",
        vehicle_type="car",
    )


def _arkesel_message(calls):
    for url, kwargs in calls:
        if url == arkesel.ARKESEL_SEND_URL:
            return kwargs["json"]["message"]
    return None


def test_known_uid_creates_payment_link_and_texts_it(monkeypatch, capsys):
    calls = _patch_http(monkeypatch)
    vehicle_id = _register_test_vehicle("TESTUID01")

    main.handle_uid("TESTUID01")

    with db.get_connection() as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchone()

    assert txn["payment_status"] == "PENDING"
    assert txn["momo_reference"] == "ref_123"
    assert txn["link_issued_at"] is not None
    assert txn["toll_amount"] == 0.50  # seeded 'car' rate
    assert "payment link sent" in capsys.readouterr().out
    assert "https://checkout.paystack.com/ref_123" in _arkesel_message(calls)
    assert "GT-TESTUID01" in _arkesel_message(calls)  # plate number included in the SMS


def test_unknown_uid_logs_audit_event_not_transaction(monkeypatch):
    _patch_http(monkeypatch)

    main.handle_uid("NOTREGISTERED")

    with db.get_connection() as conn:
        txn_count = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        audit_row = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = 'UNKNOWN_RFID_UID'"
        ).fetchone()

    assert txn_count == 0
    assert audit_row is not None
    assert "uid=NOTREGISTERED" in audit_row["event_detail"]


def test_payment_init_failure_marks_transaction_failed(monkeypatch):
    calls = _patch_http(monkeypatch, paystack_data=PAYSTACK_INITIALIZE_FAILURE)
    vehicle_id = _register_test_vehicle("TESTUID02")

    main.handle_uid("TESTUID02")

    with db.get_connection() as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchone()
        audit_row = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = 'PAYMENT_INIT_FAILED'"
        ).fetchone()

    assert txn["payment_status"] == "FAILED"
    assert txn["momo_reference"] is None
    assert audit_row is not None
    # No checkout link exists on init failure, so no SMS should have been attempted.
    assert _arkesel_message(calls) is None


def _fake_plate(text, confidence=0.9):
    return PlateRead(text=text, ocr_confidence=confidence, detector_confidence=confidence, box=(0, 0, 10, 10))


def test_known_plate_creates_payment_link_and_texts_it(monkeypatch, capsys):
    calls = _patch_http(monkeypatch)
    vehicle_id = _register_test_vehicle("TESTUID04")  # plate_number="GT-TESTUID04"

    main.handle_plate(_fake_plate("GT-TESTUID04", confidence=0.81))

    with db.get_connection() as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchone()

    assert txn["payment_status"] == "PENDING"
    assert txn["identification_method"] == "ANPR"
    assert txn["anpr_plate_detected"] == "GT-TESTUID04"
    assert txn["fallback_triggered"] == 1
    assert "payment link sent" in capsys.readouterr().out
    assert "https://checkout.paystack.com/ref_123" in _arkesel_message(calls)


def test_unknown_plate_logs_audit_event_not_transaction(monkeypatch):
    _patch_http(monkeypatch)

    main.handle_plate(_fake_plate("GT-NOTREGISTERED"))

    with db.get_connection() as conn:
        txn_count = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        audit_row = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = 'UNKNOWN_ANPR_PLATE'"
        ).fetchone()

    assert txn_count == 0
    assert audit_row is not None
    assert "GT-NOTREGISTERED" in audit_row["event_detail"]


def test_second_pass_within_cooldown_is_skipped_not_double_charged(monkeypatch):
    """Neither identification path de-dupes on its own -- a vehicle re-seen
    (RFID again, or ANPR now) within TOLL_REPEAT_COOLDOWN_SECONDS of its last
    transaction must not get a second payment link. See core.main._charge_vehicle."""
    _patch_http(monkeypatch)
    _register_test_vehicle("TESTUID05")

    main.handle_uid("TESTUID05")
    main.handle_plate(_fake_plate("GT-TESTUID05"))  # same vehicle, ANPR this time

    with db.get_connection() as conn:
        txn_count = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        audit_row = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = 'DUPLICATE_TOLL_SKIPPED'"
        ).fetchone()

    assert txn_count == 1
    assert audit_row is not None


def test_cooldown_of_zero_allows_every_pass_to_charge(monkeypatch):
    _patch_http(monkeypatch)
    monkeypatch.setattr(main, "TOLL_REPEAT_COOLDOWN_SECONDS", 0)
    _register_test_vehicle("TESTUID06")

    main.handle_uid("TESTUID06")
    main.handle_uid("TESTUID06")

    with db.get_connection() as conn:
        txn_count = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

    assert txn_count == 2


def test_handle_no_read_stays_silent_by_default(monkeypatch):
    """LOG_IDENTIFICATION_MISSES defaults off -- see its docstring in core/config.py for why
    (a miss is by far the most common outcome of a presence trigger, so logging every one was
    found to dominate DB writes on a Pi whose SD card has already shown real corruption)."""
    monkeypatch.setattr(main, "LOG_IDENTIFICATION_MISSES", False)

    main.handle_no_read(capture_path="/tmp/fake_frame.jpg")

    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM audit_log WHERE event_type = 'IDENTIFICATION_FAILED'").fetchall()
    assert rows == []


def test_handle_no_read_logs_when_flag_enabled(monkeypatch):
    monkeypatch.setattr(main, "LOG_IDENTIFICATION_MISSES", True)

    main.handle_no_read(capture_path="/tmp/fake_frame.jpg")

    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM audit_log WHERE event_type = 'IDENTIFICATION_FAILED'").fetchall()
    assert len(rows) == 1
    assert "/tmp/fake_frame.jpg" in rows[0]["event_detail"]
