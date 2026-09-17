#!/usr/bin/env python3
"""
scripts/recover_missing_sync.py

One-off recovery: push a transactions row that exists locally but never
reached Turso (silent push-sync gap — see plan.md / the 2026-08-27
investigation). Only inserts if the row is genuinely absent remotely and
its content is byte-for-byte what's in the local DB; never overwrites.

Usage:
    python3 -m scripts.recover_missing_sync --transaction-id 14
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import libsql

from core.config import TURSO_AUTH_TOKEN, TURSO_DATABASE_URL
from core.db import DB_PATH

COLUMNS = [
    "transaction_id", "vehicle_id", "identification_method", "rfid_uid_scanned",
    "anpr_plate_detected", "anpr_confidence", "fallback_triggered", "toll_amount",
    "payment_status", "momo_reference", "checkout_url", "created_at",
    "link_issued_at", "reminder_sent_at", "reissue_count",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transaction-id", type=int, required=True)
    args = parser.parse_args()

    if not (TURSO_DATABASE_URL and TURSO_AUTH_TOKEN):
        print("Turso not configured.", file=sys.stderr)
        return 1

    local = sqlite3.connect(str(DB_PATH))
    local.row_factory = sqlite3.Row
    row = local.execute(
        f"SELECT {', '.join(COLUMNS)} FROM transactions WHERE transaction_id = ?",
        (args.transaction_id,),
    ).fetchone()
    local.close()
    if row is None:
        print(f"transaction_id={args.transaction_id} not found locally.", file=sys.stderr)
        return 1

    # A file-backed scratch path, not ":memory:" -- confirmed empirically that an
    # in-memory embedded-replica target raises "ValueError: wal_insert_begin failed"
    # in this libsql build. Matches core/db.py's own convention (_open_synced_connection,
    # _repair_replica_conflict, _check_sync_gap all use a throwaway file path for exactly
    # this reason), which this script predates and hadn't caught up to.
    scratch_path = DB_PATH.parent / "tolling.recover-scratch.db"

    def _clear_scratch() -> None:
        for suffix in ("", "-wal", "-shm", "-info"):
            Path(str(scratch_path) + suffix).unlink(missing_ok=True)

    _clear_scratch()
    conn = libsql.connect(str(scratch_path), sync_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    try:
        conn.sync()

        existing = conn.execute(
            "SELECT transaction_id FROM transactions WHERE transaction_id = ?", (args.transaction_id,)
        ).fetchall()
        if existing:
            print(f"transaction_id={args.transaction_id} already present remotely — nothing to do.")
            return 0

        placeholders = ", ".join("?" for _ in COLUMNS)
        conn.execute(
            f"INSERT INTO transactions ({', '.join(COLUMNS)}) VALUES ({placeholders})",
            tuple(row[c] for c in COLUMNS),
        )
        conn.commit()

        verify = conn.execute(
            "SELECT transaction_id, vehicle_id, payment_status, momo_reference "
            "FROM transactions WHERE transaction_id = ?",
            (args.transaction_id,),
        ).fetchone()
        print(f"Pushed: {tuple(verify)}")
        return 0
    finally:
        conn.close()
        _clear_scratch()


if __name__ == "__main__":
    sys.exit(main())
