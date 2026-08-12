"""
core/db.py

SQLite persistence layer for the Smart Cashless Tolling System.

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
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from core.config import TOLL_RATES

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "tolling.db"

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


def init_db() -> None:
    """Create the database file, schema, and seed default toll rates.

    Idempotent: CREATE ... IF NOT EXISTS handles schema, and the seed
    step uses INSERT OR IGNORE so re-running never overwrites rates
    you've since edited by hand.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO toll_rates (vehicle_type, rate_ghs) VALUES (?, ?)",
            list(TOLL_RATES.items()),
        )


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection configured for this project's access pattern.

    Opens a fresh connection per call rather than holding one long-lived
    connection across the process. SQLite connections are cheap to open
    and this avoids cross-thread sharing issues if main.py later runs
    RFID/ANPR handling on separate threads.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
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


def get_vehicle_by_rfid(rfid_uid: str) -> Optional[sqlite3.Row]:
    """Look up a registered vehicle by its RFID tag UID."""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM vehicles WHERE rfid_uid = ? AND is_active = 1",
            (rfid_uid,),
        )
        return cursor.fetchone()


def get_vehicle_by_plate(plate_number: str) -> Optional[sqlite3.Row]:
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


def get_vehicle_by_id(vehicle_id: int) -> Optional[sqlite3.Row]:
    """Look up a vehicle by its internal vehicle_id."""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM vehicles WHERE vehicle_id = ? AND is_active = 1",
            (vehicle_id,),
        )
        return cursor.fetchone()