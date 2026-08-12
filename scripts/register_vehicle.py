#!/usr/bin/env python3
"""
scripts/register_vehicle.py

Manual CLI to register a vehicle for the Smart Toll system. Stopgap for
the future dashboard (workers/dashboard) — inserts directly into the local
SQLite DB via core/db.py.

Usage:
    python3 -m scripts.register_vehicle \\
        --phone 0244000004 \\
        --rfid-uid D4E5F6A7 \\
        --plate GT-1234-24 \\
        --owner "Yaw Darko" \\
        --type car
"""

import argparse
import sqlite3
import sys

from core.db import init_db, register_vehicle
from core.config import TOLL_RATES


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", required=True, help="Mobile money number to charge (e.g. 0244000004)")
    parser.add_argument("--rfid-uid", help="RFID tag UID, hex string (e.g. D4E5F6A7)")
    parser.add_argument("--plate", help="Number plate (e.g. GT-1234-24)")
    parser.add_argument("--owner", help="Owner's name")
    parser.add_argument("--ghana-card-id", help="Owner's Ghana Card ID")
    parser.add_argument(
        "--type",
        dest="vehicle_type",
        default="car",
        choices=sorted(TOLL_RATES),
        help="Vehicle type, determines toll rate (default: car)",
    )
    args = parser.parse_args()

    if not args.rfid_uid and not args.plate:
        parser.error("at least one of --rfid-uid or --plate is required")

    init_db()

    try:
        vehicle_id = register_vehicle(
            phone_number=args.phone,
            rfid_uid=args.rfid_uid,
            plate_number=args.plate,
            owner_name=args.owner,
            ghana_card_id=args.ghana_card_id,
            vehicle_type=args.vehicle_type,
        )
    except sqlite3.IntegrityError as e:
        print(f"Could not register vehicle: {e}", file=sys.stderr)
        return 1

    print(f"Registered vehicle_id={vehicle_id} (rfid_uid={args.rfid_uid!r}, plate={args.plate!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
