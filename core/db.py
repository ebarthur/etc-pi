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

import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import libsql

from core.config import TOLL_RATES, TURSO_AUTH_TOKEN, TURSO_DATABASE_URL, TURSO_SYNC_INTERVAL_SECONDS

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
    created_at                 TEXT NOT NULL DEFAULT (datetime('now'))
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
        # See _has_replica_metadata()'s docstring for why this first-ever
        # open has to be a synced one, before any plain connection can touch
        # DB_PATH. Deliberately the plain (non-offline) synced connect, not
        # _open_offline_connection() below — confirmed empirically that
        # offline=True on a truly fresh file, if anything gets written to it
        # before an explicit sync(), can hit "server returned a conflict"
        # against a Turso DB that already has data on it from elsewhere;
        # this bootstrap only pulls, writing no data itself.
        #
        # The explicit .sync() call here (not just connect()) is required,
        # not optional: confirmed empirically that skipping it leaves the
        # local replica's frame baseline stale, so the *next* push (whether
        # from this process's own _sync_once() or the background thread)
        # gets rejected with "server returned a conflict" against a Turso DB
        # that already has other data on it — connect() alone creates the
        # metadata sidecar but does not itself pull the remote's current
        # state into it.
        #
        # Best-effort: if this box has never had connectivity (e.g. a Pi's
        # very first boot with no signal yet), this raises and we fall back
        # to a fully local plain file for now — get_connection() below then
        # stays plain (no metadata sidecar => no offline=True) until db/ is
        # wiped and re-initialized while online. Not auto-retried later;
        # this matches the project's existing "db/ is disposable scratch
        # data, delete anytime" convention rather than adding a
        # retry-to-upgrade-a-plain-file mechanism.
        try:
            bootstrap_conn = _open_synced_connection()
            try:
                bootstrap_conn.sync()
            finally:
                bootstrap_conn.close()
        except Exception as e:
            print(f"Turso replica bootstrap failed, starting local-only: {e}", file=sys.stderr)
    with get_connection() as conn:
        conn.executescript(SCHEMA)
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


def _open_synced_connection() -> _Connection:
    """Non-offline synced connection. Only for init_db()'s one-time bootstrap
    of a brand-new DB_PATH — see its docstring for why. Everything else that
    needs a synced connection uses _open_offline_connection() below."""
    return _Connection(
        libsql.connect(str(DB_PATH), sync_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    )


def _open_offline_connection() -> _Connection:
    return _Connection(
        libsql.connect(
            str(DB_PATH), sync_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN, offline=True
        )
    )


def _sync_once(log_failures: bool) -> bool:
    """Best-effort single sync attempt. Returns whether it succeeded.

    No-op (returns True) when Turso isn't configured. Failures — including
    just failing to connect, which is where a sync attempt actually fails
    when offline — are expected on a Pi with intermittent connectivity, so
    this never raises.
    """
    global _last_sync_ok
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
        return False

    if log_failures and _last_sync_ok is False:
        log_audit_event("TURSO_SYNC_RESTORED")
    _last_sync_ok = True
    return True


def _sync_loop(interval_seconds: float) -> None:
    while True:
        time.sleep(interval_seconds)
        _sync_once(log_failures=True)


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
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM vehicles WHERE rfid_uid = ? AND is_active = 1",
            (rfid_uid,),
        )
        return cursor.fetchone()


def get_vehicle_by_plate(plate_number: str) -> Optional[Row]:
    """Look up a registered vehicle by ANPR-detected plate number."""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM vehicles WHERE plate_number = ? AND is_active = 1",
            (plate_number,),
        )
        return cursor.fetchone()


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
        return cursor.lastrowid


def log_audit_event(
    event_type: str,
    event_detail: Optional[str] = None,
    transaction_id: Optional[int] = None,
) -> None:
    """Insert an audit_log entry. Use for system-level events, not transactions."""
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
    """Update a transaction's payment outcome after the MoMo /charge call returns.

    Call this once, after api_clients/momo.py gets a response — not before.
    payment_status must be 'SUCCESS' or 'FAILED'.
    """
    if payment_status not in ("SUCCESS", "FAILED"):
        raise ValueError(f"Invalid payment_status: {payment_status!r}")

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE transactions
            SET payment_status = ?, momo_reference = ?
            WHERE transaction_id = ?
            """,
            (payment_status, momo_reference, transaction_id),
        )


def set_transaction_reference(transaction_id: int, momo_reference: str) -> None:
    """Record a Paystack reference on a transaction still awaiting resolution.

    Call this when charge_toll() comes back 'pending' (main.py's
    handle_uid does). workers/charge's webhook (Phase 3) looks transactions
    up by momo_reference to resolve them to SUCCESS/FAILED once Paystack's
    charge.success/charge.failed event arrives — without this, a pending
    transaction has no reference on it for the webhook to match against.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE transactions SET momo_reference = ? WHERE transaction_id = ?",
            (momo_reference, transaction_id),
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