"""Pull data from the vendors that have an API (AWS, Vobiz). Run hourly from cron or EventBridge.

    python -m rm.collect --vendor all
    python -m rm.collect --vendor aws --days 60
"""
from __future__ import annotations

import argparse

from .collectors import aws, vobiz
from .config import load_json, load_settings
from .db import connect


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vendor", choices=["aws", "vobiz", "all"], default="all")
    ap.add_argument("--days", type=int, default=None, help="how far back to pull (default: 35 for AWS, 2 for Vobiz)")
    a = ap.parse_args()
    s = load_settings()
    conn = connect(s.db_path)
    if a.vendor in ("aws", "all"):
        aws.collect(conn, s, a.days or 35)
    if a.vendor in ("vobiz", "all"):
        vobiz.collect(conn, s, load_json("purposes.json"), a.days or 2)


if __name__ == "__main__":
    main()
