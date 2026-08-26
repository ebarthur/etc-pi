#!/usr/bin/env python3
"""
scripts/test_paystack_charge.py

Isolated live test for api_clients/momo.py — creates one real Paystack
hosted-checkout transaction (against a test secret key, so no real money
moves) and prints the parsed InitializeResult, including the checkout URL.
Touches nothing else — no DB, no SMS, no vehicle registration, since
initialize_transaction() itself doesn't need any of that.

Usage:
    python3 -m scripts.test_paystack_charge --phone 0551234987 --amount 5.00
"""

import argparse
import sys

from api_clients.momo import initialize_transaction
from core.config import PAYSTACK_SECRET_KEY


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", required=True, help="Owner's number (e.g. 0551234987)")
    parser.add_argument("--amount", type=float, default=5.00, help="Amount in GHS (default: 5.00)")
    args = parser.parse_args()

    if not PAYSTACK_SECRET_KEY:
        print("PAYSTACK_SECRET_KEY is not set in .env — nothing to test.", file=sys.stderr)
        return 1
    if not PAYSTACK_SECRET_KEY.startswith("sk_test_"):
        print("PAYSTACK_SECRET_KEY does not look like a test key (sk_test_...) — refusing to run.", file=sys.stderr)
        return 1

    print(f"Initializing a GHS {args.amount:.2f} checkout for {args.phone!r}...")
    result = initialize_transaction(args.phone, args.amount)
    print(f"success={result['success']}")
    print(f"authorization_url={result['authorization_url']!r}")
    print(f"reference={result['reference']!r}")
    print(f"message={result['message']!r}")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
