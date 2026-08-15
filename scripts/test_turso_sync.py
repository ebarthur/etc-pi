#!/usr/bin/env python3
"""
scripts/test_turso_sync.py

Isolated live test for core/db.py's Phase 4 Turso sync path — never tested
against a real Turso database before (see plan.md Phase 9). No Paystack,
no SMS, no core.main involved.

What it does:
    1. init_db() against the real local DB_PATH (db/tolling.db), which
       Turso-configured .env makes do a real sync pull + starts the
       background sync thread.
    2. Registers a throwaway, uniquely-named test vehicle locally.
    3. Forces one push sync (db._sync_once) rather than waiting out
       TURSO_SYNC_INTERVAL_SECONDS.
    4. Opens a *second*, independent local replica file (a temp path) with
       the same Turso credentials and syncs (pulls) it — if the test
       vehicle shows up there, the round trip (local write -> push ->
       Turso -> pull -> a totally different local file) is proven, not
       just "the API call didn't error."
    5. Deletes the test vehicle (locally, then a push sync propagates the
       delete) and removes the temp replica file, so nothing test-related
       is left behind in the real Turso DB or on disk.

Usage:
    python3 -m scripts.test_turso_sync
"""

import sys
import tempfile
import time
from pathlib import Path

import core.db as db
from core.config import TURSO_AUTH_TOKEN, TURSO_DATABASE_URL


def main() -> int:
    if not (TURSO_DATABASE_URL and TURSO_AUTH_TOKEN):
        print("TURSO_DATABASE_URL / TURSO_AUTH_TOKEN not set in .env — nothing to test.", file=sys.stderr)
        return 1

    test_uid = f"TURSOSYNCTEST-{int(time.time())}"
    print(f"1. init_db() — schema + initial sync against {TURSO_DATABASE_URL}")
    db.init_db()

    print(f"2. Registering throwaway vehicle rfid_uid={test_uid!r} locally...")
    vehicle_id = db.register_vehicle(
        phone_number="0000000000",
        rfid_uid=test_uid,
        plate_number=f"TEST-{test_uid}",
        owner_name="Turso Sync Test",
        vehicle_type="car",
    )
    print(f"   local vehicle_id={vehicle_id}")

    print("3. Forcing a push sync...")
    ok = db._sync_once(log_failures=False)
    if not ok:
        print("   sync FAILED — see stderr above for the error.", file=sys.stderr)
        return 1
    print("   sync ok")

    print("4. Verifying via a completely independent replica file...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        verify_path = Path(tmp_dir) / "turso_verify.db"
        import libsql

        conn = libsql.connect(str(verify_path), sync_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        conn.sync()
        cursor = conn.execute("SELECT vehicle_id, owner_name FROM vehicles WHERE rfid_uid = ?", (test_uid,))
        row = cursor.fetchone()
        conn.close()

        if row is None:
            print("   FAIL: test vehicle not found in the independently-synced replica.", file=sys.stderr)
            print("   This means the push sync did not actually land the row in Turso.", file=sys.stderr)
            cleanup(test_uid)
            return 1

        print(f"   OK: found remotely — vehicle_id={row[0]}, owner_name={row[1]!r}")

    print("5. Cleaning up test vehicle...")
    cleanup(test_uid)
    print("   done. Turso round-trip sync confirmed working.")
    return 0


def cleanup(test_uid: str) -> None:
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vehicles WHERE rfid_uid = ?", (test_uid,))
    db._sync_once(log_failures=False)


if __name__ == "__main__":
    sys.exit(main())
