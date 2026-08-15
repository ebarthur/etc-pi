"""
tests/test_integration.py

End-to-end test of the identify -> charge -> log loop (core.main.handle_uid)
against a throwaway SQLite file, not db/tolling.db. Paystack's and Arkesel's
HTTP calls are mocked so this suite runs fully offline and deterministically.

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

PAYSTACK_SUCCESS = {
    "status": True,
    "message": "Charge attempted",
    "data": {"status": "success", "reference": "ref_123"},
}
ARKESEL_SUCCESS = {"status": "success", "message": "sent"}


def _fake_response(json_data):
    response = MagicMock()
    response.json.return_value = json_data
    return response


def _patch_http(monkeypatch, paystack_data=PAYSTACK_SUCCESS, arkesel_data=ARKESEL_SUCCESS):
    def side_effect(url, **kwargs):
        if url.startswith(momo.PAYSTACK_BASE_URL):
            return _fake_response(paystack_data)
        if url == arkesel.ARKESEL_SEND_URL:
            return _fake_response(arkesel_data)
        raise AssertionError(f"Unexpected POST to {url}")

    monkeypatch.setattr(requests, "post", MagicMock(side_effect=side_effect))


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


def test_known_uid_charges_and_logs_success(monkeypatch, capsys):
    _patch_http(monkeypatch)
    vehicle_id = _register_test_vehicle("TESTUID01")

    main.handle_uid("TESTUID01")

    with db.get_connection() as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchone()

    assert txn["payment_status"] == "SUCCESS"
    assert txn["momo_reference"] == "ref_123"
    assert txn["toll_amount"] == 5.00  # seeded 'car' rate
    assert "charge success" in capsys.readouterr().out


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


def test_pending_charge_leaves_transaction_pending(monkeypatch):
    _patch_http(
        monkeypatch,
        paystack_data={
            "status": True,
            "message": "Charge attempted",
            "data": {"status": "pending", "reference": "ref_pending"},
        },
    )
    vehicle_id = _register_test_vehicle("TESTUID02")

    main.handle_uid("TESTUID02")

    with db.get_connection() as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchone()
        audit_row = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = 'CHARGE_PENDING'"
        ).fetchone()

    assert txn["payment_status"] == "PENDING"
    assert txn["momo_reference"] == "ref_pending"
    assert audit_row is not None


def test_failed_charge_updates_status_and_logs_audit_event(monkeypatch):
    _patch_http(
        monkeypatch,
        paystack_data={
            "status": True,
            "message": "Insufficient funds",
            "data": {"status": "failed", "reference": "ref_failed"},
        },
    )
    vehicle_id = _register_test_vehicle("TESTUID03")

    main.handle_uid("TESTUID03")

    with db.get_connection() as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE vehicle_id = ?", (vehicle_id,)
        ).fetchone()
        audit_row = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = 'CHARGE_FAILED'"
        ).fetchone()

    assert txn["payment_status"] == "FAILED"
    assert audit_row is not None
