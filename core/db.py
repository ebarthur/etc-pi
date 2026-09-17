"""
core/db.py

libSQL (Turso embedded replica) persistence layer for the Smart Cashless
Tolling System. Reads/writes always hit the local file at DB_PATH first —
Pi-class, no network round trip — and, when TURSO_DATABASE_URL /
TURSO_AUTH_TOKEN are set, a background thread periodically syncs that file
with Turso. With both unset (dev/test), DB_PATH is just a plain local
SQLite file and nothing ever touches the network — see plan.md Phase 4.

Three tables:
    vehicles     - registered vehicle records, keyed by internal vehicle_id
    transactions - one row per toll event, records identification method
                   (RFID or ANPR fallback) and payment outcome
    audit_log    - append-only event trail, separate from transactions,
                   for system-level events (errors, fallbacks triggered,
                   manual overrides, etc.)

Uses WAL (Write-Ahead Logging) journal mode, which allows concurrent
readers while a write is in progress — relevant here since main.py may
be writing a transaction while a dashboard/reporting script reads
concurrently, and WAL avoids the "database is locked" errors that
Pi-class SD card I/O can otherwise trigger under the default journal mode.
"""

import logging
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import libsql

from core.config import (
    TOLL_RATES,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
    TURSO_DRIFT_CHECK_INTERVAL_SECONDS,
    TURSO_SYNC_INTERVAL_SECONDS,
)

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "tolling.db"

# Substrings libsql's underlying SQLite engine uses in constraint-violation
# messages. Matched against ValueError text (see _Connection.execute) to
# re-raise as sqlite3.IntegrityError, since libsql-python surfaces every
# SQLite failure as a plain ValueError rather than sqlite3's exception
# hierarchy — callers like scripts/register_vehicle.py already catch
# sqlite3.IntegrityError specifically (e.g. duplicate rfid_uid/plate_number),
# and translating here keeps that working without changes above this layer.
_CONSTRAINT_MARKERS = ("UNIQUE constraint", "NOT NULL constraint", "FOREIGN KEY constraint", "CHECK constraint")


class Row(dict):
    """dict subclass so `row["column"]` keeps working like sqlite3.Row did.

    libsql cursors return plain tuples, not sqlite3.Row objects, so
    _Cursor.fetchone/fetchall build these from `cursor.description` +
    the raw tuple.
    """


class _Cursor:
    """Wraps a libsql cursor so fetchone/fetchall return Row dicts instead
    of plain tuples, matching the sqlite3.Row-style access the rest of this
    module (and tests/test_integration.py) relies on."""

    def __init__(self, raw_cursor: Any) -> None:
        self._cursor = raw_cursor

    def _columns(self) -> list:
        return [d[0] for d in self._cursor.description]

    def fetchone(self) -> Optional[Row]:
        row = self._cursor.fetchone()
        return None if row is None else Row(zip(self._columns(), row))

    def fetchall(self) -> list:
        columns = self._columns()
        return [Row(zip(columns, row)) for row in self._cursor.fetchall()]

    @property
    def lastrowid(self) -> Optional[int]:
        return self._cursor.lastrowid


class _Connection:
    """Wraps a libsql connection: translates constraint-violation ValueErrors
    to sqlite3.IntegrityError, and makes execute() return a _Cursor."""

    def __init__(self, raw_conn: Any) -> None:
        self._conn = raw_conn

    def execute(self, sql: str, params: Sequence = ()) -> _Cursor:
        try:
            return _Cursor(self._conn.execute(sql, params))
        except ValueError as e:
            if any(marker in str(e) for marker in _CONSTRAINT_MARKERS):
                raise sqlite3.IntegrityError(str(e)) from e
            raise

    def executemany(self, sql: str, seq_of_params: Sequence) -> _Cursor:
        try:
            return _Cursor(self._conn.executemany(sql, seq_of_params))
        except ValueError as e:
            if any(marker in str(e) for marker in _CONSTRAINT_MARKERS):
                raise sqlite3.IntegrityError(str(e)) from e
            raise

    def executescript(self, sql: str) -> None:
        self._conn.executescript(sql)

    def sync(self) -> None:
        self._conn.sync()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS toll_rates (
    vehicle_type    TEXT PRIMARY KEY,
    rate_ghs        REAL NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    rfid_uid        TEXT UNIQUE,
    plate_number    TEXT UNIQUE,
    owner_name      TEXT,
    phone_number    TEXT NOT NULL,
    ghana_card_id   TEXT,
    vehicle_type    TEXT NOT NULL DEFAULT 'car'
                        REFERENCES toll_rates(vehicle_type),
    registered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_vehicles_rfid_uid
    ON vehicles (rfid_uid);

CREATE INDEX IF NOT EXISTS idx_vehicles_plate_number
    ON vehicles (plate_number);

-- created_at below uses strftime(..., 'now') rather than plain datetime('now')
-- to keep millisecond resolution: has_recent_transaction()'s cooldown check
-- compares against this column, and datetime('now')'s whole-second truncation
-- would make even a short/zero cooldown misfire for two passes that land in
-- the same wall-clock second (a real possibility once ANPR is the path
-- actually doing the identifying most of the time -- see core/main.py).
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id               INTEGER REFERENCES vehicles(vehicle_id),
    identification_method     TEXT NOT NULL CHECK (
                                identification_method IN ('RFID', 'ANPR', 'NONE')
                              ),
    rfid_uid_scanned          TEXT,
    anpr_plate_detected       TEXT,
    anpr_confidence           REAL,
    fallback_triggered        INTEGER NOT NULL DEFAULT 0,
    toll_amount               REAL NOT NULL,
    payment_status            TEXT NOT NULL DEFAULT 'PENDING' CHECK (
                                payment_status IN ('PENDING', 'SUCCESS', 'FAILED')
                              ),
    momo_reference             TEXT,
    -- checkout_url is Paystack's authorization_url for momo_reference -- stored separately
    -- because it can't be reconstructed from the reference alone (they're independent
    -- values in Paystack's response, not derived from each other), and workers/charge's
    -- reminder SMS needs to resend the *same* link, not just know a reference exists.
    checkout_url               TEXT,
    -- link_issued_at is when the *current* checkout link (momo_reference/checkout_url) was
    -- actually sent -- separate from created_at, so a reissued link (workers/charge's cron)
    -- gets its own fresh 2-hour reminder window instead of the reminder firing immediately
    -- because created_at is already old. reminder_sent_at/reissue_count are also keyed to
    -- the current link: both reset when a link is reissued (see workers/charge/src/index.ts).
    link_issued_at             TEXT,
    reminder_sent_at           TEXT,
    reissue_count              INTEGER NOT NULL DEFAULT 0,
    created_at                 TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_transactions_vehicle_id
    ON transactions (vehicle_id);

CREATE INDEX IF NOT EXISTS idx_transactions_created_at
    ON transactions (created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id    INTEGER REFERENCES transactions(transaction_id),
    event_type         TEXT NOT NULL,
    event_detail        TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
    ON audit_log (created_at);
"""


def _replica_metadata_path() -> Path:
    return Path(str(DB_PATH) + "-info")


def _has_replica_metadata() -> bool:
    """Whether DB_PATH already has libsql's embedded-replica metadata sidecar.

    That sidecar is only written the first time a *synced* connection opens
    a given file (confirmed empirically against a real Turso DB) — a plain
    connection touching a brand-new file first permanently forfeits the
    ability to sync it later ("invalid local state: db file exists but
    metadata file does not"). get_connection() uses this to decide whether
    it's safe to open with offline=True, or whether it must stay fully plain
    (e.g. init_db()'s bootstrap below never ran, or failed because this box
    had no connectivity yet the first time it started).
    """
    return _replica_metadata_path().exists()


# Columns added after transactions already existed in a deployed db/tolling.db
# and Turso -- CREATE TABLE IF NOT EXISTS (SCHEMA above) only reaches a
# brand-new database, so an already-existing transactions table needs an
# actual migration. See _migrate_schema().
_TRANSACTIONS_MIGRATION_COLUMNS = {
    "checkout_url": "TEXT",
    "link_issued_at": "TEXT",
    "reminder_sent_at": "TEXT",
    "reissue_count": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate_schema(conn: _Connection) -> None:
    """Add any of _TRANSACTIONS_MIGRATION_COLUMNS missing from transactions.

    ALTER TABLE ADD COLUMN errors on a column that already exists, so this
    checks PRAGMA table_info first rather than assuming a fresh install --
    safe to call every init_db(), against a brand-new table (SCHEMA above
    already created these columns, so this is a no-op) or an existing one
    that predates them.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    for column, sql_type in _TRANSACTIONS_MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {sql_type}")


# Guards DB_PATH (the file itself, not its contents) against a race between
# get_connection() opening it and _repair_replica_conflict() briefly making
# it not exist while rebuilding it (move old file aside, bootstrap-pull a
# fresh one). get_connection() only holds this around the decide+connect
# step, so the normal hot path never blocks on anything -- only a
# get_connection() call that lands during an actual repair (rare: a real,
# unrecoverable sync conflict, not routine offline retries) waits for it.
_db_path_lock = threading.Lock()


def _bootstrap_fresh_replica() -> bool:
    """One-time pull of the full remote database into a brand-new DB_PATH.

    Used both by init_db() (DB_PATH doesn't exist yet -- first boot, or after
    db/ was wiped by hand) and by _repair_replica_conflict() below (DB_PATH
    was just moved aside after an unrecoverable sync conflict). See
    _has_replica_metadata()'s docstring for why this first-ever open of a
    given path must be a non-offline synced connection, and why the explicit
    .sync() call (not just connect()) is required — skipping it leaves the
    local replica's frame baseline stale, so the *next* push gets rejected
    with "server returned a conflict" against a Turso DB that already has
    other data on it.

    Best-effort: returns False (falling back to a plain local file, or in
    the repair case leaving the corrupt replica moved aside with nothing to
    replace it until the next attempt) rather than raising, so a box with no
    connectivity yet still boots.
    """
    try:
        bootstrap_conn = _open_synced_connection()
        try:
            bootstrap_conn.sync()
        finally:
            bootstrap_conn.close()
        return True
    except Exception as e:
        print(f"Turso replica bootstrap failed, starting local-only: {e}", file=sys.stderr)
        return False


def init_db() -> None:
    """Create the database file, schema, and seed default toll rates.

    Idempotent: CREATE ... IF NOT EXISTS handles schema, and the seed
    step uses INSERT OR IGNORE so re-running never overwrites rates
    you've since edited by hand.

    When Turso is configured, also does one best-effort synchronous sync
    (so a fresh process starts with an up-to-date read-mostly copy of
    vehicles/toll_rates, and any writes a prior short-lived process left
    unsynced — e.g. scripts/register_vehicle.py — get pushed up now) and
    starts the background sync thread for everything after that. Both are
    no-ops when Turso isn't configured (dev/test against a plain local file).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN and not DB_PATH.exists():
        _bootstrap_fresh_replica()
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO toll_rates (vehicle_type, rate_ghs) VALUES (?, ?)",
            list(TOLL_RATES.items()),
        )
    _sync_once(log_failures=False)
    start_background_sync()


@contextmanager
def get_connection() -> Iterator[_Connection]:
    """Yield a connection configured for this project's access pattern.

    Every hot-path DB call (every RFID scan) goes through this, and it must
    never block on the network. Two connection styles are used depending on
    whether Turso is usable yet (configured *and* DB_PATH already has replica
    metadata — see _has_replica_metadata()):

    - Turso usable: an `offline=True` synced connection. Confirmed
      empirically this is local-speed and never touches the network, at
      connect *or* write time, even against an unreachable host — as long as
      replica metadata already exists locally. This matters for more than
      speed: also confirmed empirically that a plain connection's writes are
      otherwise invisible to a later sync() — libsql only pushes WAL changes
      made through a connection that was itself opened with offline=True.
      Without this, Phase 4's Turso push (transactions/audit_log going up)
      would silently never happen, no matter how often the background thread
      calls sync(). (A *non*-offline synced connection is not an option
      either way: confirmed empirically it proxies every write to the remote
      primary, at ~3-4s per write even when Turso is reachable — exactly
      what plan.md's "local-speed, offline-tolerant" requirement rules out.)
    - Turso not usable (unconfigured, or configured but bootstrap in
      init_db() hasn't succeeded yet): a fully plain connection, no Turso
      awareness at all.

    Opens a fresh connection per call rather than holding one long-lived
    connection across the process — cheap at local speed, and it avoids
    cross-thread sharing issues now that a background sync thread runs
    alongside main.py's RFID loop.
    """
    # Held only around the decide+connect step below, not the query/commit
    # that follows -- see _db_path_lock's docstring for what this protects
    # against and why it doesn't cost the hot path anything in the normal
    # (no repair in progress) case.
    with _db_path_lock:
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN and _has_replica_metadata():
            raw = libsql.connect(
                str(DB_PATH), sync_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN, offline=True
            )
        else:
            raw = libsql.connect(str(DB_PATH))
    conn = _Connection(raw)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Background Turso sync (Phase 4) ---
#
# vehicles/toll_rates are canonical in Turso (managed centrally; the Pi is a
# read-mostly replica for these), while transactions/audit_log are written
# locally first and synced up — but libsql's embedded-replica sync is a
# whole-file operation, not per-table, so in practice every table syncs
# together on the same schedule; the vehicles/toll_rates-are-canonical
# distinction is about who normally *writes* each table, not about separate
# sync paths.
#
# TURSO_DATABASE_URL/TURSO_AUTH_TOKEN are passed to libsql.connect() in
# exactly two places: get_connection() (offline=True, see its docstring) and
# here. _open_synced_connection() (non-offline) is used only by init_db()'s
# one-time bootstrap of a brand-new DB_PATH; the periodic push/pull below
# uses _open_offline_connection() instead — confirmed empirically that a
# *non*-offline connection's sync() only reconciles its own connection's
# writes/pulls, not another connection's already-committed local WAL
# changes, so using it here would silently never push anything
# get_connection() wrote. Every attempt opens (and closes) its own
# connection to the same local file get_connection() writes to; the file on
# disk (and its WAL) is what sync() reconciles against, not any particular
# connection object, so this picks up whatever the hot path has committed
# since the last attempt.
_sync_thread: Optional[threading.Thread] = None
_sync_thread_lock = threading.Lock()
_last_sync_ok: Optional[bool] = None  # None = not yet attempted


def _open_synced_connection(path: Path = DB_PATH) -> _Connection:
    """Non-offline synced connection. Used by _bootstrap_fresh_replica() (at
    DB_PATH) and by _repair_replica_conflict() below (at a throwaway scratch
    path, so it never touches the corrupt DB_PATH file it's diffing
    against) — see their docstrings for why. Everything else that needs a
    synced connection uses _open_offline_connection() below."""
    return _Connection(
        libsql.connect(str(path), sync_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    )


def _open_offline_connection() -> _Connection:
    return _Connection(
        libsql.connect(
            str(DB_PATH), sync_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN, offline=True
        )
    )


# --- Self-healing recovery from an unrecoverable replica conflict ---
#
# workers/charge writes straight to the Turso primary (resolving payments)
# independently of this Pi's embedded-replica push. When both land on the
# same table's tail page around the same moment, the replica's next sync()
# can come back "server returned a conflict: sent=X, got=Y" -- a genuine
# frame-position mismatch libsql won't silently resolve, not a transient
# network blip. Left alone it just repeats forever (confirmed empirically:
# retried the identical failure every ~30-40s for over 20 minutes straight
# with no self-recovery), silently stranding every local write from that
# point on -- nothing reaches Turso, so nothing reaches the dashboard,
# until someone notices and manually intervenes. _CONFLICT_REPAIR_THRESHOLD
# consecutive conflict failures (as opposed to ordinary offline/connectivity
# failures, which are expected and must NOT trigger this) instead triggers
# _repair_replica_conflict() below to fix it automatically.
_CONFLICT_REPAIR_THRESHOLD = 2
_consecutive_conflict_failures = 0


def _is_conflict_error(e: Exception) -> bool:
    """Whether a sync failure is the specific unrecoverable frame-position
    conflict (see above), as opposed to an ordinary offline/connectivity
    failure that's expected and should just keep retrying untouched."""
    return "conflict" in str(e).lower()


def _push_local_only_rows(remote: _Connection, local: _Connection) -> int:
    """Copy transactions/audit_log rows that exist only in `local` (never
    reached Turso) into `remote`, by primary-key diff. Never overwrites a
    row `remote` already has -- Turso is authoritative for anything both
    sides touched (workers/charge only ever moves a transaction PENDING ->
    SUCCESS/FAILED there directly; a stale local copy of that same row is
    expected and gets corrected for free once the replica re-bootstraps
    from `remote` afterwards). Returns how many rows were copied, for
    logging.
    """
    copied = 0

    remote_txn_ids = {
        r["transaction_id"] for r in remote.execute("SELECT transaction_id FROM transactions").fetchall()
    }
    for r in local.execute("SELECT * FROM transactions").fetchall():
        if r["transaction_id"] in remote_txn_ids:
            continue
        remote.execute(
            """
            INSERT INTO transactions (
                transaction_id, vehicle_id, identification_method, rfid_uid_scanned,
                anpr_plate_detected, anpr_confidence, fallback_triggered, toll_amount,
                payment_status, momo_reference, checkout_url, created_at, link_issued_at,
                reminder_sent_at, reissue_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["transaction_id"], r["vehicle_id"], r["identification_method"], r["rfid_uid_scanned"],
                r["anpr_plate_detected"], r["anpr_confidence"], r["fallback_triggered"], r["toll_amount"],
                r["payment_status"], r["momo_reference"], r["checkout_url"], r["created_at"],
                r["link_issued_at"], r["reminder_sent_at"], r["reissue_count"],
            ),
        )
        copied += 1

    remote_log_ids = {r["log_id"] for r in remote.execute("SELECT log_id FROM audit_log").fetchall()}
    for r in local.execute("SELECT * FROM audit_log").fetchall():
        if r["log_id"] in remote_log_ids:
            continue
        remote.execute(
            "INSERT INTO audit_log (log_id, transaction_id, event_type, event_detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (r["log_id"], r["transaction_id"], r["event_type"], r["event_detail"], r["created_at"]),
        )
        copied += 1

    return copied


def _repair_replica_conflict(error: Exception) -> None:
    """Recover from a sync stuck on the unrecoverable conflict described
    above: salvage any local-only rows straight to Turso, then discard and
    re-bootstrap the local replica so its frame baseline is clean again.

    Holds _db_path_lock for the whole repair (including the network round
    trips) so get_connection() can't observe DB_PATH mid-swap -- see that
    lock's docstring. Best-effort: logs and gives up on any failure here
    rather than raising, so a repair attempt that itself hits a network
    blip just leaves things as they were for the next conflict tick to
    retry, instead of taking down the sync thread.
    """
    print("Turso replica sync conflict looks permanent -- attempting self-heal...", file=sys.stderr)
    scratch_path = DB_PATH.parent / "tolling.repair-scratch.db"

    def _clear_scratch() -> None:
        for suffix in ("", "-wal", "-shm", "-info"):
            Path(str(scratch_path) + suffix).unlink(missing_ok=True)

    with _db_path_lock:
        try:
            _clear_scratch()  # leftovers from a repair attempt that crashed mid-way
            remote = _open_synced_connection(scratch_path)
            try:
                remote.sync()  # pull remote's current state so the diff below is accurate
                local = _Connection(libsql.connect(str(DB_PATH)))
                try:
                    copied = _push_local_only_rows(remote, local)
                finally:
                    local.close()
                remote.execute(
                    "INSERT INTO audit_log (event_type, event_detail) VALUES (?, ?)",
                    (
                        "TURSO_REPLICA_RESET",
                        f"conflict={str(error)[:300]}; salvaged {copied} local-only row(s) "
                        "before resetting the local replica",
                    ),
                )
                # Confirmed empirically: a non-offline synced connection does NOT autocommit --
                # closing it with an open transaction silently discards every write above,
                # including the salvaged rows, with no error. Every other write path in this
                # module commits explicitly (see get_connection()'s yield/commit); this one
                # didn't, so the one time this ran for real it quietly threw away the exact
                # rows it was trying to save.
                remote.commit()
            finally:
                remote.close()
                _clear_scratch()

            backup_dir = DB_PATH.parent / f"backup_{time.strftime('%Y%m%d_%H%M%S')}"
            backup_dir.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm", "-info"):
                src = Path(str(DB_PATH) + suffix)
                if src.exists():
                    src.rename(backup_dir / src.name)

            if _bootstrap_fresh_replica():
                print("Turso replica self-heal complete.", file=sys.stderr)
            else:
                print("Turso replica self-heal: reset done but re-bootstrap failed; will retry.", file=sys.stderr)
        except Exception as repair_error:
            print(f"Turso replica self-heal failed: {repair_error}", file=sys.stderr)


def _sync_once(log_failures: bool) -> bool:
    """Best-effort single sync attempt. Returns whether it succeeded.

    No-op (returns True) when Turso isn't configured. Failures — including
    just failing to connect, which is where a sync attempt actually fails
    when offline — are expected on a Pi with intermittent connectivity, so
    this never raises. An unrecoverable conflict (as opposed to ordinary
    offline retries) instead triggers a self-heal after
    _CONFLICT_REPAIR_THRESHOLD consecutive occurrences — see
    _repair_replica_conflict().
    """
    global _last_sync_ok, _consecutive_conflict_failures
    if not (TURSO_DATABASE_URL and TURSO_AUTH_TOKEN):
        return True
    try:
        conn = _open_offline_connection()
        try:
            conn.sync()
        finally:
            conn.close()
    except Exception as e:
        print(f"Turso sync failed (will retry): {e}", file=sys.stderr)
        # Only audit-log the onset of an outage, not every failed tick —
        # an SD card shouldn't take a disk write every sync interval for
        # the whole time a Pi is offline (see plan.md's SD-card-wear note).
        if log_failures and _last_sync_ok is not False:
            log_audit_event("TURSO_SYNC_FAILED", event_detail=str(e)[:500])
        _last_sync_ok = False

        if _is_conflict_error(e):
            _consecutive_conflict_failures += 1
            if _consecutive_conflict_failures >= _CONFLICT_REPAIR_THRESHOLD:
                _consecutive_conflict_failures = 0
                _repair_replica_conflict(e)
        else:
            _consecutive_conflict_failures = 0
        return False

    _consecutive_conflict_failures = 0
    if log_failures and _last_sync_ok is False:
        log_audit_event("TURSO_SYNC_RESTORED")
    _last_sync_ok = True
    return True


# --- Silent push-gap detection ---
#
# _sync_once() reporting success only means the sync() call itself didn't raise --
# confirmed live 2026-08-27 that this is not the same thing as "every local write
# reached Turso": a real transaction committed locally, five straight _sync_once()
# calls each returned True, and the row still never showed up remotely. No error,
# no conflict, so none of the existing failure/repair machinery above ever notices.
# _check_sync_gap() below is a periodic, independent check for exactly that gap.
_GAP_CONFIRM_THRESHOLD = 2
_consecutive_gap_detections = 0
_last_gap_ok: Optional[bool] = None


def _check_sync_gap() -> None:
    """Compare local vs. Turso's actual newest transaction_id/log_id.

    Deliberately verifies via a throwaway independent replica (same technique
    scripts/test_turso_sync.py uses), not the live offline connection get_connection()
    and the push sync share -- that connection's own view of "did it land remotely"
    is exactly what silently lied in the incident above, so it can't be trusted to
    grade its own work. Only escalates (audit-logs, prints) after
    _GAP_CONFIRM_THRESHOLD consecutive detections, so an ordinary write that simply
    hasn't hit its next push cycle yet (this runs independently of
    TURSO_SYNC_INTERVAL_SECONDS) doesn't read as a false alarm.

    The audit_log comparison excludes this function's own TURSO_SYNC_GAP /
    TURSO_SYNC_GAP_RESOLVED rows -- confirmed empirically this is not just
    theoretical: logging TURSO_SYNC_GAP is itself a local write, so counting it
    raises local_log_max on the very next tick, before that row has had a chance
    to sync out; without this exclusion a real gap can never actually reach
    "resolved" (the row announcing the gap keeps re-triggering the same gap it
    announced) even once every toll-domain row has synced.
    """
    global _last_gap_ok, _consecutive_gap_detections
    _SELF_EVENT_TYPES = ("TURSO_SYNC_GAP", "TURSO_SYNC_GAP_RESOLVED")
    _log_max_sql = (
        "SELECT COALESCE(MAX(log_id), 0) AS m FROM audit_log WHERE event_type NOT IN ({})".format(
            ", ".join("?" for _ in _SELF_EVENT_TYPES)
        )
    )
    try:
        with get_connection() as local:
            local_txn_max = local.execute(
                "SELECT COALESCE(MAX(transaction_id), 0) AS m FROM transactions"
            ).fetchone()["m"]
            local_log_max = local.execute(_log_max_sql, _SELF_EVENT_TYPES).fetchone()["m"]
    except Exception as e:
        print(f"Turso drift check: local read failed: {e}", file=sys.stderr)
        return

    scratch_path = DB_PATH.parent / "tolling.driftcheck-scratch.db"

    def _clear_scratch() -> None:
        for suffix in ("", "-wal", "-shm", "-info"):
            Path(str(scratch_path) + suffix).unlink(missing_ok=True)

    try:
        _clear_scratch()
        remote = _open_synced_connection(scratch_path)
        try:
            remote.sync()
            remote_txn_max = remote.execute(
                "SELECT COALESCE(MAX(transaction_id), 0) AS m FROM transactions"
            ).fetchone()["m"]
            remote_log_max = remote.execute(_log_max_sql, _SELF_EVENT_TYPES).fetchone()["m"]
        finally:
            remote.close()
            _clear_scratch()
    except Exception as e:
        print(f"Turso drift check: remote verify failed: {e}", file=sys.stderr)
        return

    if remote_txn_max < local_txn_max or remote_log_max < local_log_max:
        _consecutive_gap_detections += 1
        if _consecutive_gap_detections >= _GAP_CONFIRM_THRESHOLD:
            if _last_gap_ok is not False:
                detail = (
                    f"transactions local_max={local_txn_max} remote_max={remote_txn_max}, "
                    f"audit_log local_max={local_log_max} remote_max={remote_log_max}"
                )
                log_audit_event("TURSO_SYNC_GAP", event_detail=detail)
                print(f"Turso sync gap detected: {detail}", file=sys.stderr)
                # This gap is the "sync() reports success but pushes nothing" failure, not a
                # conflict -- confirmed live (twice) that it recurs on a replica that's been
                # running a while, not just a one-time fluke, and there's no error for
                # _sync_once() to catch and route to _repair_replica_conflict() on its own. So
                # trigger the same salvage-then-reset repair directly from here once per
                # confirmed episode, rather than leaving this as alert-only and requiring
                # someone to notice and fix it by hand every time.
                _repair_replica_conflict(RuntimeError(f"silent push gap (non-error): {detail}"))
            _last_gap_ok = False
    else:
        _consecutive_gap_detections = 0
        if _last_gap_ok is False:
            log_audit_event("TURSO_SYNC_GAP_RESOLVED")
        _last_gap_ok = True


def _sync_loop(interval_seconds: float) -> None:
    last_drift_check = 0.0
    while True:
        time.sleep(interval_seconds)
        _sync_once(log_failures=True)

        now = time.monotonic()
        if now - last_drift_check >= TURSO_DRIFT_CHECK_INTERVAL_SECONDS:
            last_drift_check = now
            _check_sync_gap()


def start_background_sync(interval_seconds: float = TURSO_SYNC_INTERVAL_SECONDS) -> None:
    """Start the daemon thread that periodically syncs with Turso.

    No-op if Turso isn't configured, or if already running (safe to call
    from every init_db() without spawning duplicate threads).
    """
    global _sync_thread
    if not (TURSO_DATABASE_URL and TURSO_AUTH_TOKEN):
        return
    with _sync_thread_lock:
        if _sync_thread is not None:
            return
        _sync_thread = threading.Thread(target=_sync_loop, args=(interval_seconds,), daemon=True)
        _sync_thread.start()


def get_vehicle_by_rfid(rfid_uid: str) -> Optional[Row]:
    """Look up a registered vehicle by its RFID tag UID."""
    logger.debug("DB query: vehicles WHERE rfid_uid=%s AND is_active=1", rfid_uid)
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM vehicles WHERE rfid_uid = ? AND is_active = 1",
            (rfid_uid,),
        )
        row = cursor.fetchone()
        logger.debug("DB result: %s", "vehicle_id=%s" % row["vehicle_id"] if row else "no match")
        return row


def get_vehicle_by_plate(plate_number: str) -> Optional[Row]:
    """Look up a registered vehicle by ANPR-detected plate number."""
    logger.debug("DB query: vehicles WHERE plate_number=%s AND is_active=1", plate_number)
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM vehicles WHERE plate_number = ? AND is_active = 1",
            (plate_number,),
        )
        row = cursor.fetchone()
        logger.debug("DB result: %s", "vehicle_id=%s" % row["vehicle_id"] if row else "no match")
        return row


def has_recent_transaction(vehicle_id: int, within_seconds: float) -> bool:
    """Whether `vehicle_id` already has a transaction logged in the last
    `within_seconds`, regardless of its payment outcome.

    Used by core/main.py's _charge_vehicle() to suppress charging the same
    vehicle twice for what's really one physical pass -- neither
    identification path (RFID or ANPR) has any other de-duplication.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT 1 FROM transactions
            WHERE vehicle_id = ?
              AND created_at >= strftime('%Y-%m-%d %H:%M:%f', 'now', ?)
            LIMIT 1
            """,
            (vehicle_id, f"-{within_seconds} seconds"),
        )
        recent = cursor.fetchone() is not None
        logger.debug(
            "has_recent_transaction(vehicle_id=%s, within=%ss) -> %s", vehicle_id, within_seconds, recent
        )
        return recent


def log_transaction(
    vehicle_id: Optional[int],
    identification_method: str,
    toll_amount: float,
    rfid_uid_scanned: Optional[str] = None,
    anpr_plate_detected: Optional[str] = None,
    anpr_confidence: Optional[float] = None,
    fallback_triggered: bool = False,
    payment_status: str = "PENDING",
    momo_reference: Optional[str] = None,
) -> int:
    """Insert a transaction record and return its transaction_id."""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO transactions (
                vehicle_id, identification_method, rfid_uid_scanned,
                anpr_plate_detected, anpr_confidence, fallback_triggered,
                toll_amount, payment_status, momo_reference
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vehicle_id,
                identification_method,
                rfid_uid_scanned,
                anpr_plate_detected,
                anpr_confidence,
                int(fallback_triggered),
                toll_amount,
                payment_status,
                momo_reference,
            ),
        )
        transaction_id = cursor.lastrowid
        logger.debug(
            "DB insert: transactions id=%s vehicle_id=%s method=%s amount=%.2f status=%s",
            transaction_id, vehicle_id, identification_method, toll_amount, payment_status,
        )
        return transaction_id


def log_audit_event(
    event_type: str,
    event_detail: Optional[str] = None,
    transaction_id: Optional[int] = None,
) -> None:
    """Insert an audit_log entry. Use for system-level events, not transactions."""
    logger.debug("DB insert: audit_log event_type=%s detail=%s transaction_id=%s", event_type, event_detail, transaction_id)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (transaction_id, event_type, event_detail)
            VALUES (?, ?, ?)
            """,
            (transaction_id, event_type, event_detail),
        )


def update_transaction_status(
    transaction_id: int,
    payment_status: str,
    momo_reference: Optional[str] = None,
) -> None:
    """Update a transaction's final payment outcome.

    Two callers today: core.main._charge_vehicle marks a transaction FAILED
    immediately if initialize_transaction() itself fails (no checkout link
    was ever issued, so there's nothing to poll); workers/charge's cron
    polling marks SUCCESS/FAILED once Paystack's verify endpoint resolves a
    transaction that did get a link (that path writes directly to Turso in
    TypeScript, not through this function -- this is the Python-only path).
    payment_status must be 'SUCCESS' or 'FAILED'.
    """
    if payment_status not in ("SUCCESS", "FAILED"):
        raise ValueError(f"Invalid payment_status: {payment_status!r}")

    logger.debug(
        "DB update: transactions id=%s -> payment_status=%s momo_reference=%s",
        transaction_id, payment_status, momo_reference,
    )
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE transactions
            SET payment_status = ?, momo_reference = ?
            WHERE transaction_id = ?
            """,
            (payment_status, momo_reference, transaction_id),
        )


def set_transaction_reference(transaction_id: int, momo_reference: str, checkout_url: str) -> None:
    """Record a fresh Paystack checkout link on a transaction, and stamp
    link_issued_at to now.

    Call this once initialize_transaction() returns a reference AND its
    authorization_url (core.main's _charge_vehicle does, right before
    sending the checkout-link SMS). Both get stored, not just the
    reference: workers/charge's cron polling (replaces the old webhook)
    looks transactions up by momo_reference to resolve them to
    SUCCESS/FAILED, and separately needs checkout_url verbatim to resend
    the *same* link in a reminder SMS -- the two values are independent in
    Paystack's response, so checkout_url can't be reconstructed from
    momo_reference alone.

    link_issued_at is distinct from created_at: it's when the *current*
    link was sent, so workers/charge's 2-hour reminder timer measures from
    here. This is the only place Python sets any of this (the initial
    send); a reissued link stamps all three again itself, directly in Turso.
    """
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE transactions
            SET momo_reference = ?, checkout_url = ?, link_issued_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
            WHERE transaction_id = ?
            """,
            (momo_reference, checkout_url, transaction_id),
        )


def get_toll_rate(vehicle_type: str) -> float:
    """Return the toll rate (GHS) for a vehicle type.

    Falls back to the 'car' rate if the type isn't found, rather than
    raising — a transaction should never be blocked by a missing rate
    row on a prototype system.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT rate_ghs FROM toll_rates WHERE vehicle_type = ?",
            (vehicle_type,),
        )
        row = cursor.fetchone()
        if row:
            return row["rate_ghs"]

        cursor = conn.execute(
            "SELECT rate_ghs FROM toll_rates WHERE vehicle_type = 'car'"
        )
        return cursor.fetchone()["rate_ghs"]


def register_vehicle(
    phone_number: str,
    rfid_uid: Optional[str] = None,
    plate_number: Optional[str] = None,
    owner_name: Optional[str] = None,
    ghana_card_id: Optional[str] = None,
    vehicle_type: str = "car",
) -> int:
    """Insert a new vehicle record and return its vehicle_id.

    At least one of rfid_uid / plate_number should be set, or the vehicle can
    never be matched by the identification pipeline — the DB doesn't enforce
    this (both are nullable, e.g. a plate-only record pending RFID tag
    issuance), so callers are responsible for supplying at least one.
    Raises sqlite3.IntegrityError on a duplicate rfid_uid/plate_number or an
    unrecognized vehicle_type (enforced by the toll_rates foreign key).
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO vehicles (
                rfid_uid, plate_number, owner_name, phone_number,
                ghana_card_id, vehicle_type
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (rfid_uid, plate_number, owner_name, phone_number, ghana_card_id, vehicle_type),
        )
        return cursor.lastrowid


def get_vehicle_by_id(vehicle_id: int) -> Optional[Row]:
    """Look up a vehicle by its internal vehicle_id."""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM vehicles WHERE vehicle_id = ? AND is_active = 1",
            (vehicle_id,),
        )
        return cursor.fetchone()