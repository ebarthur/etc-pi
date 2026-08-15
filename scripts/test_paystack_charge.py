#!/usr/bin/env python3
"""
scripts/test_paystack_charge.py

Isolated live test for api_clients/momo.py — fires one real Paystack
mobile money charge (against a test secret key, so no real money moves)
and prints the parsed ChargeResult. Touches nothing else — no DB, no SMS,
no vehicle registration, since charge_toll() itself doesn't need any of
that.

Usage:
    python3 -m scripts.test_paystack_charge --phone 0551234987 --amount 5.00
    python3 -m scripts.test_paystack_charge --phone 0551234987 --provider mtn
"""

import argparse
import sys

from api_clients.momo import DEFAULT_PROVIDER, MOMO_PROVIDER_MAP, charge_toll
from core.config import PAYSTACK_SECRET_KEY


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", required=True, help="Mobile money number to charge (e.g. 0551234987)")
    parser.add_argument("--amount", type=float, default=5.00, help="Amount in GHS (default: 5.00)")
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        choices=sorted(MOMO_PROVIDER_MAP),
        help=f"Mobile money provider (default: {DEFAULT_PROVIDER})",
    )
    args = parser.parse_args()

    if not PAYSTACK_SECRET_KEY:
        print("PAYSTACK_SECRET_KEY is not set in .env — nothing to test.", file=sys.stderr)
        return 1
    if not PAYSTACK_SECRET_KEY.startswith("sk_test_"):
        print("PAYSTACK_SECRET_KEY does not look like a test key (sk_test_...) — refusing to run.", file=sys.stderr)
        return 1

    print(f"Charging GHS {args.amount:.2f} to {args.phone!r} via {args.provider!r}...")
    result = charge_toll(args.phone, args.amount, provider=args.provider)
    print(f"status={result['status']!r}")
    print(f"success={result['success']}")
    print(f"reference={result['reference']!r}")
    print(f"message={result['message']!r}")
    return 0 if result["status"] in ("success", "pending") else 1


if __name__ == "__main__":
    sys.exit(main())
