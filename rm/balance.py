"""Enter a balance that the vendor does not expose by API.

    python -m rm.balance sarvam credits 21000
    python -m rm.balance interakt wallet 6200
"""
from __future__ import annotations

import argparse

from .config import load_settings
from .db import connect, set_balance

ALLOWED = {("sarvam", "credits"), ("interakt", "wallet")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vendor", choices=["sarvam", "interakt"])
    ap.add_argument("key", choices=["credits", "wallet"])
    ap.add_argument("amount_inr", type=float)
    a = ap.parse_args()
    if (a.vendor, a.key) not in ALLOWED:
        ap.error("use: sarvam credits <amount> | interakt wallet <amount>")
    conn = connect(load_settings().db_path)
    set_balance(conn, a.vendor, a.key, a.amount_inr, "INR", source="manual")
    print(f"{a.vendor} {a.key} set to ₹{a.amount_inr:,.2f}")


if __name__ == "__main__":
    main()
