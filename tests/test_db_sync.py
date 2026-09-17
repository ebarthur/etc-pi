"""
tests/test_db_sync.py

Coverage for core/db.py's Phase 4 (Turso) background sync path: local
reads/writes must keep working when Turso isn't configured (the default
for every other test in this suite, via throwaway_db in
tests/test_integration.py) or is configured but unreachable, and a sync
failure must never propagate — it's expected on a Pi with intermittent
connectivity, not exceptional.

Uses a bogus, unresolvable Turso hostname rather than mocking libsql
itself, so this exercises the real failure path _sync_once has to handle
(libsql.connect(..., sync_url=...) raising) — still fully offline and
deterministic, since "this host doesn't exist" fails fast without ever
reaching a real network peer.
"""
from pathlib import Path

import core.db as db

BOGUS_SYNC_URL = "libsql://this-host-does-not-exist.smarttoll-test.invalid"


def _use_throwaway_db(tmp_path, monkeypatch):
    # Blank Turso config before init_db() runs, regardless of what's in
    # .env: real credentials are now configured there for Phase 9 live
    # testing, but init_db() would otherwise reach out to the real Turso DB
    # during this offline test suite. Each test below sets whatever
    # TURSO_DATABASE_URL/TURSO_AUTH_TOKEN it actually wants to exercise
    # afterward, via direct calls to _sync_once()/start_background_sync()
    # rather than a second init_db() call, so this blanking doesn't affect
    # what any individual test verifies.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_sync.db")
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", "")
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "")
    db.init_db()


def test_local_writes_work_with_turso_unconfigured(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", "")
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "")

    vehicle_id = db.register_vehicle(phone_number="0551234567", rfid_uid="SYNCUID01")

    assert db.get_vehicle_by_id(vehicle_id) is not None


def test_sync_and_background_thread_are_noops_without_turso_config(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", "")
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "")
    monkeypatch.setattr(db, "_sync_thread", None)

    assert db._sync_once(log_failures=True) is True

    db.start_background_sync()
    assert db._sync_thread is None  # never started


def test_local_writes_still_work_when_turso_configured_but_unreachable(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")

    # get_connection() (the hot path every DB call goes through) must never
    # pass sync_url/auth_token to libsql.connect() — if it did, this would
    # hang/fail against BOGUS_SYNC_URL instead of writing locally.
    vehicle_id = db.register_vehicle(phone_number="0551234567", rfid_uid="SYNCUID02")

    assert db.get_vehicle_by_id(vehicle_id) is not None


def test_sync_failure_is_tolerated_and_logs_onset_once(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")
    monkeypatch.setattr(db, "_last_sync_ok", None)

    assert db._sync_once(log_failures=True) is False

    with db.get_connection() as conn:
        failures = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = 'TURSO_SYNC_FAILED'"
        ).fetchall()
    assert len(failures) == 1  # onset logged

    # A second consecutive failure must not log again (SD-card-wear guard —
    # see the comment in core.db._sync_once).
    assert db._sync_once(log_failures=True) is False
    with db.get_connection() as conn:
        failures = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = 'TURSO_SYNC_FAILED'"
        ).fetchall()
    assert len(failures) == 1


def test_start_background_sync_spawns_a_daemon_thread_when_configured(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")
    monkeypatch.setattr(db, "_sync_thread", None)

    # Long interval: this test only checks the thread starts, not that a
    # sync tick fires — no need to actually wait one out.
    db.start_background_sync(interval_seconds=3600)

    assert db._sync_thread is not None
    assert db._sync_thread.daemon is True
    assert db._sync_thread.is_alive()


# --- Self-healing recovery from an unrecoverable replica conflict ---


def test_is_conflict_error_distinguishes_conflict_from_ordinary_failures():
    assert db._is_conflict_error(Exception("sync error: server returned a conflict: sent=112, got=115"))
    assert not db._is_conflict_error(Exception("Name or service not known"))
    assert not db._is_conflict_error(Exception("Connection timed out"))


def test_conflict_failures_only_trigger_repair_after_threshold(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")
    monkeypatch.setattr(db, "_consecutive_conflict_failures", 0)

    repair_calls = []
    monkeypatch.setattr(db, "_repair_replica_conflict", lambda e: repair_calls.append(e))

    def _raise_conflict():
        raise Exception("sync error: server returned a conflict: sent=1, got=2")

    monkeypatch.setattr(db, "_open_offline_connection", lambda: (_ for _ in ()).throw(
        Exception("sync error: server returned a conflict: sent=1, got=2")
    ))

    assert db._CONFLICT_REPAIR_THRESHOLD == 2  # this test assumes the current threshold

    assert db._sync_once(log_failures=False) is False
    assert repair_calls == []  # first conflict alone must not trigger repair

    assert db._sync_once(log_failures=False) is False
    assert len(repair_calls) == 1  # second consecutive conflict does


def test_non_conflict_failures_never_trigger_repair(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")
    monkeypatch.setattr(db, "_consecutive_conflict_failures", 0)

    repair_calls = []
    monkeypatch.setattr(db, "_repair_replica_conflict", lambda e: repair_calls.append(e))

    # Real failure path (unresolvable host), exercised repeatedly -- ordinary
    # offline retries must never be mistaken for the unrecoverable conflict.
    for _ in range(5):
        assert db._sync_once(log_failures=False) is False
    assert repair_calls == []


def test_push_local_only_rows_copies_missing_and_skips_existing(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)

    db.register_vehicle(phone_number="0551234567", plate_number="TEST-0001")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO transactions (transaction_id, vehicle_id, identification_method, toll_amount, payment_status) "
            "VALUES (1, 1, 'ANPR', 0.5, 'PENDING')"
        )
        conn.execute(
            "INSERT INTO transactions (transaction_id, vehicle_id, identification_method, toll_amount, payment_status) "
            "VALUES (2, 1, 'ANPR', 0.5, 'PENDING')"
        )
        conn.execute("INSERT INTO audit_log (log_id, event_type) VALUES (1, 'A')")
        conn.execute("INSERT INTO audit_log (log_id, event_type) VALUES (2, 'B')")
    local = db._Connection(db.libsql.connect(str(db.DB_PATH)))

    remote_path = tmp_path / "remote.db"
    remote = db._Connection(db.libsql.connect(str(remote_path)))
    remote.executescript(db.SCHEMA)
    # Only exercising the row-diff/copy logic here, not referential
    # integrity against a full vehicles/toll_rates seed on this throwaway
    # remote file.
    remote.execute("PRAGMA foreign_keys = OFF")
    # remote already has transaction_id=1 (with a DIFFERENT, more current
    # status than local's stale PENDING copy) and log_id=1 -- these must be
    # left untouched, only the genuinely-missing rows get copied.
    remote.execute(
        "INSERT INTO transactions (transaction_id, vehicle_id, identification_method, toll_amount, payment_status) "
        "VALUES (1, 1, 'ANPR', 0.5, 'SUCCESS')"
    )
    remote.execute("INSERT INTO audit_log (log_id, event_type) VALUES (1, 'A')")
    remote.commit()

    copied = db._push_local_only_rows(remote, local)
    remote.commit()

    assert copied == 2  # transaction_id=2 and log_id=2 only

    txn1 = remote.execute("SELECT payment_status FROM transactions WHERE transaction_id = 1").fetchone()
    assert txn1["payment_status"] == "SUCCESS"  # remote's copy was not overwritten

    txn2 = remote.execute("SELECT * FROM transactions WHERE transaction_id = 2").fetchone()
    assert txn2 is not None  # the missing one was copied over

    local.close()
    remote.close()


def test_repair_replica_conflict_durably_commits_salvaged_rows(tmp_path, monkeypatch):
    """Regression test for a real incident: _repair_replica_conflict() ran for real once
    (manually, diagnosing a stuck replica) and silently lost everything it was supposed to
    salvage -- a non-offline synced connection does not autocommit, and the function closed
    `remote` without ever calling .commit(). Every existing test above this one monkeypatches
    _repair_replica_conflict itself out entirely, so this gap had zero coverage. Verifies via a
    completely independent connection to the fake-Turso file (not the same connection object
    the repair used) -- that's the check an uncommitted write fails, since a live connection can
    still see its own uncommitted writes.
    """
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")

    vehicle_id = db.register_vehicle(phone_number="0551234567", plate_number="REPAIR-01")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO transactions (transaction_id, vehicle_id, identification_method, toll_amount, payment_status) "
            "VALUES (99, ?, 'ANPR', 0.5, 'PENDING')",
            (vehicle_id,),
        )

    # Stands in for Turso: seeded with the same vehicle but not the local-only transaction,
    # matching a real conflict scenario where only the diff needs salvaging. A real synced
    # connection would block on/fail against a real host here, so this reuses the suite's
    # _FakeRemote (no-op .sync()) wrapping a plain local connection -- same technique the
    # _check_sync_gap tests above already use, extended with .commit() (see class def) since
    # this is the first test that needs a _FakeRemote it actually writes through.
    remote_path = tmp_path / "fake_turso.db"
    remote_conn = db._Connection(db.libsql.connect(str(remote_path)))
    remote_conn.executescript(db.SCHEMA)
    remote_conn.execute("PRAGMA foreign_keys = OFF")
    remote_conn.execute(
        "INSERT INTO vehicles (vehicle_id, phone_number, plate_number) VALUES (?, '0551234567', 'REPAIR-01')",
        (vehicle_id,),
    )
    remote_conn.commit()

    # _repair_replica_conflict calls _open_synced_connection twice (scratch path for the
    # salvage, then DB_PATH via _bootstrap_fresh_replica's re-pull) -- both redirected to the
    # same fake remote regardless of which path was asked for.
    monkeypatch.setattr(db, "_open_synced_connection", lambda path=None: _FakeRemote(remote_conn))

    db._repair_replica_conflict(Exception("test-triggered repair"))

    verify = db._Connection(db.libsql.connect(str(remote_path)))
    row = verify.execute(
        "SELECT transaction_id, payment_status FROM transactions WHERE transaction_id = 99"
    ).fetchone()
    assert row is not None  # would be None with the missing remote.commit()
    assert row["payment_status"] == "PENDING"
    reset_events = verify.execute("SELECT * FROM audit_log WHERE event_type = 'TURSO_REPLICA_RESET'").fetchall()
    assert len(reset_events) == 1
    verify.close()


# --- Silent push-gap detection (_check_sync_gap) ---


class _FakeRemote:
    """Stands in for _open_synced_connection()'s real Turso-backed connection:
    a no-op .sync() (this is testing the comparison logic, not real network
    sync) wrapping a plain local libsql connection representing "what Turso
    actually has" — set up directly by each test rather than via a real push,
    so it stays independent of whatever _check_sync_gap is meant to be
    verifying.
    """

    def __init__(self, conn):
        self._conn = conn

    def sync(self) -> None:
        pass

    def execute(self, sql, params=()):
        return self._conn.execute(sql, params)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        pass


def _fake_remote_at(path, txn_max, log_max):
    # Wrapped in db._Connection (not the raw libsql connection) so .execute()
    # returns Row-style results, matching what _check_sync_gap gets from a
    # real _open_synced_connection() and from get_connection().
    conn = db._Connection(db.libsql.connect(str(path)))
    conn.executescript(db.SCHEMA)
    conn.execute("PRAGMA foreign_keys = OFF")
    if txn_max:
        conn.execute(
            "INSERT INTO transactions (transaction_id, vehicle_id, identification_method, toll_amount) "
            "VALUES (?, NULL, 'ANPR', 1.0)",
            (txn_max,),
        )
    if log_max:
        conn.execute("INSERT INTO audit_log (log_id, event_type) VALUES (?, 'X')", (log_max,))
    conn.commit()
    return conn


def _seed_local_transaction_and_log(vehicle_suffix: str) -> None:
    vehicle_id = db.register_vehicle(phone_number="0551234567", plate_number=f"GAP-{vehicle_suffix}")
    db.log_transaction(vehicle_id=vehicle_id, identification_method="ANPR", toll_amount=1.0)
    db.log_audit_event("SOME_EVENT")


def test_check_sync_gap_requires_two_consecutive_detections_before_logging(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")
    monkeypatch.setattr(db, "_consecutive_gap_detections", 0)
    monkeypatch.setattr(db, "_last_gap_ok", None)
    # A confirmed gap now also triggers a real repair (see the dedicated test for that) --
    # stubbed out here so this test stays focused on the logging/threshold logic and doesn't
    # need to survive DB_PATH actually getting reset out from under it.
    monkeypatch.setattr(db, "_repair_replica_conflict", lambda e: None)

    _seed_local_transaction_and_log("0001")  # local now has transaction_id=1, log_id=1

    # "Remote" is behind — this is the exact scenario from the live incident.
    remote_conn = _fake_remote_at(tmp_path / "fake_remote_behind.db", txn_max=0, log_max=0)
    monkeypatch.setattr(db, "_open_synced_connection", lambda path: _FakeRemote(remote_conn))

    db._check_sync_gap()
    assert db._consecutive_gap_detections == 1
    with db.get_connection() as conn:
        assert conn.execute("SELECT * FROM audit_log WHERE event_type = 'TURSO_SYNC_GAP'").fetchall() == []

    db._check_sync_gap()
    assert db._consecutive_gap_detections == 2
    with db.get_connection() as conn:
        gaps = conn.execute("SELECT * FROM audit_log WHERE event_type = 'TURSO_SYNC_GAP'").fetchall()
    assert len(gaps) == 1  # only logged once the gap is confirmed, not every tick

    remote_conn.close()


def test_check_sync_gap_no_alert_when_remote_is_caught_up(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")
    monkeypatch.setattr(db, "_consecutive_gap_detections", 0)
    monkeypatch.setattr(db, "_last_gap_ok", None)

    _seed_local_transaction_and_log("0002")

    remote_conn = _fake_remote_at(tmp_path / "fake_remote_caughtup.db", txn_max=1, log_max=1)
    monkeypatch.setattr(db, "_open_synced_connection", lambda path: _FakeRemote(remote_conn))

    db._check_sync_gap()
    assert db._consecutive_gap_detections == 0
    with db.get_connection() as conn:
        assert conn.execute("SELECT * FROM audit_log WHERE event_type LIKE 'TURSO_SYNC_GAP%'").fetchall() == []

    remote_conn.close()


def test_check_sync_gap_logs_resolution_once_gap_clears(tmp_path, monkeypatch):
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")
    monkeypatch.setattr(db, "_consecutive_gap_detections", 0)
    monkeypatch.setattr(db, "_last_gap_ok", None)
    monkeypatch.setattr(db, "_repair_replica_conflict", lambda e: None)

    _seed_local_transaction_and_log("0003")

    behind_conn = _fake_remote_at(tmp_path / "fake_remote_behind2.db", txn_max=0, log_max=0)
    monkeypatch.setattr(db, "_open_synced_connection", lambda path: _FakeRemote(behind_conn))
    db._check_sync_gap()
    db._check_sync_gap()
    assert db._last_gap_ok is False
    behind_conn.close()

    caughtup_conn = _fake_remote_at(tmp_path / "fake_remote_caughtup2.db", txn_max=1, log_max=1)
    monkeypatch.setattr(db, "_open_synced_connection", lambda path: _FakeRemote(caughtup_conn))
    db._check_sync_gap()

    assert db._last_gap_ok is True
    with db.get_connection() as conn:
        resolved = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = 'TURSO_SYNC_GAP_RESOLVED'"
        ).fetchall()
    assert len(resolved) == 1

    caughtup_conn.close()


def test_check_sync_gap_triggers_repair_once_per_confirmed_episode(tmp_path, monkeypatch):
    """The drift check is only useful if a confirmed gap actually gets fixed, not just logged --
    confirmed live (twice) that this exact failure mode recurs on its own and _sync_once() never
    sees an error to route to _repair_replica_conflict() through the normal conflict path.
    Repair should fire exactly once per confirmed episode, not every tick while still broken
    (that would reset/re-backup DB_PATH repeatedly for no benefit).
    """
    _use_throwaway_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "TURSO_DATABASE_URL", BOGUS_SYNC_URL)
    monkeypatch.setattr(db, "TURSO_AUTH_TOKEN", "fake-token-for-test")
    monkeypatch.setattr(db, "_consecutive_gap_detections", 0)
    monkeypatch.setattr(db, "_last_gap_ok", None)

    repair_calls = []
    monkeypatch.setattr(db, "_repair_replica_conflict", lambda e: repair_calls.append(e))

    _seed_local_transaction_and_log("0004")

    behind_conn = _fake_remote_at(tmp_path / "fake_remote_behind3.db", txn_max=0, log_max=0)
    monkeypatch.setattr(db, "_open_synced_connection", lambda path: _FakeRemote(behind_conn))

    db._check_sync_gap()
    assert repair_calls == []  # first detection alone must not repair yet

    db._check_sync_gap()
    assert len(repair_calls) == 1  # confirmed (2nd consecutive) detection repairs

    db._check_sync_gap()
    assert len(repair_calls) == 1  # still broken, but already handled this episode -- no re-trigger

    behind_conn.close()
