#!/usr/bin/env python3
"""
scripts/test_arkesel_sms.py

Isolated live test for api_clients/arkesel.py — sends one real SMS via
Arkesel and prints the parsed SmsResult, so we can confirm send_sms()'s
response-shape assumptions against a real account (see arkesel.py's
module docstring: written from Arkesel's public docs, never verified
live before now). Touches nothing else — no DB, no Paystack.

Usage:
    python3 -m scripts.test_arkesel_sms --phone 0244000004
    python3 -m scripts.test_arkesel_sms --phone 0244000004 --message "custom test message"
"""

import argparse
import sys

from api_clients.arkesel import send_sms
from core.config import ARKESEL_API_KEY, ARKESEL_SENDER_ID


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", required=True, help="Real phone number to send the test SMS to")
    parser.add_argument(
        "--message",
        default="Smart Toll: this is a live test SMS from scripts/test_arkesel_sms.py.",
        help="Message body to send",
    )
    args = parser.parse_args()

    if not ARKESEL_API_KEY:
        print("ARKESEL_API_KEY is not set in .env — nothing to test.", file=sys.stderr)
        return 1

    print(f"Sending via sender ID {ARKESEL_SENDER_ID!r} to {args.phone!r}...")
    result = send_sms(args.phone, args.message)
    print(f"success={result['success']}")
    print(f"message={result['message']!r}")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
