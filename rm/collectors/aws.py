"""AWS: spend by service and by cost-allocation tag (Cost Explorer) and credit balances (Billing GetCredits).

Cost Explorer allows two group-bys per request, so spend is stored in separate views:
  gross_service  : usage cost by AWS service and by the product tag
  gross_purpose  : usage cost by the client tag and the product tag
  net_total      : cost after credits, all days
Credits applied each day = gross total minus net total.
Each Cost Explorer request is billed by AWS (see docs: AWS Cost Management pricing), so collect hourly at most.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from ..config import Settings
from ..db import log_sync, utcnow

log = logging.getLogger("rm.aws")


def _tag_value(key: str) -> str:
    """Cost Explorer returns tag groups as 'tagkey$value'. An empty value means untagged."""
    return key.split("$", 1)[1] if "$" in key else key


def _ce_pages(ce, **params):
    token = None
    while True:
        if token:
            params["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**params)
        yield from resp.get("ResultsByTime", [])
        token = resp.get("NextPageToken")
        if not token:
            return


def _store_groups(conn, dim: str, results, tag_cols: tuple[bool, bool]) -> int:
    n = 0
    for day in results:
        d = day["TimePeriod"]["Start"]
        groups = day.get("Groups") or []
        if not groups and day.get("Total"):
            amt = float(day["Total"].get("UnblendedCost", {}).get("Amount", 0))
            conn.execute("INSERT OR REPLACE INTO aws_cost(dim,day,k1,k2,usd) VALUES(?,?,?,?,?)", (dim, d, "", "", amt))
            n += 1
            continue
        for g in groups:
            keys = g["Keys"]
            k1 = _tag_value(keys[0]) if tag_cols[0] else keys[0]
            k2 = (_tag_value(keys[1]) if tag_cols[1] else keys[1]) if len(keys) > 1 else ""
            amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
            conn.execute("INSERT OR REPLACE INTO aws_cost(dim,day,k1,k2,usd) VALUES(?,?,?,?,?)", (dim, d, k1, k2, amt))
            n += 1
    conn.commit()
    return n


def collect_costs(conn, settings: Settings, ce, days: int = 35, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    start = (today - timedelta(days=days)).isoformat()
    end = (today + timedelta(days=1)).isoformat()  # end date is exclusive
    period = {"Start": start, "End": end}
    usage_only = {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}
    base = dict(TimePeriod=period, Granularity="DAILY", Metrics=["UnblendedCost"])
    rows = 0
    rows += _store_groups(conn, "gross_service", _ce_pages(ce, **base, Filter=usage_only, GroupBy=[
        {"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "TAG", "Key": settings.aws_tag_product}]), (False, True))
    rows += _store_groups(conn, "gross_purpose", _ce_pages(ce, **base, Filter=usage_only, GroupBy=[
        {"Type": "TAG", "Key": settings.aws_tag_client}, {"Type": "TAG", "Key": settings.aws_tag_product}]), (True, True))
    rows += _store_groups(conn, "net_total", _ce_pages(ce, **base), (False, False))
    return rows


def _amount(obj) -> float | None:
    try:
        return float(obj["currencyAmount"])
    except (KeyError, TypeError, ValueError):
        return None


def collect_credits(conn, settings: Settings, billing, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    start = int(datetime.combine(today - timedelta(days=364), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    resp = billing.get_credits(accountId=settings.aws_account_id, startDate=start, payerAccountFlag=False)
    credits = resp.get("credits", [])
    conn.execute("DELETE FROM aws_credits")
    for c in credits:
        conn.execute(
            "INSERT OR REPLACE INTO aws_credits VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.get("creditId"), c.get("description") or c.get("ruleName") or c.get("creditId"), c.get("creditType"),
             c.get("creditStatus"), (c.get("remainingAmount") or {}).get("currencyCode", "USD"),
             _amount(c.get("initialAmount")), _amount(c.get("remainingAmount")), _amount(c.get("estimatedAmount")),
             c.get("startDate"), c.get("endDate"), c.get("exhaustDate"),
             ", ".join(c.get("applicableProductNames") or []), utcnow()))
    conn.commit()
    return len(credits)


def _session(settings: Settings):
    import boto3

    kwargs = {"profile_name": settings.aws_profile} if settings.aws_profile else {}
    return boto3.Session(**kwargs)


def collect(conn, settings: Settings, days: int = 35) -> None:
    if not settings.aws_enabled:
        log.info("AWS_ACCOUNT_ID not set; skipping AWS")
        return
    try:
        sess = _session(settings)
        rows = collect_costs(conn, settings, sess.client("ce", region_name="us-east-1"), days=days)
        log_sync(conn, "aws", True, rows, "cost explorer ok")
    except Exception as exc:  # report, never crash the other collectors
        log.error("AWS cost collection failed: %s", type(exc).__name__)
        log_sync(conn, "aws", False, 0, f"cost explorer: {type(exc).__name__}: {exc}")
        return
    try:
        sess = _session(settings)
        n = collect_credits(conn, settings, sess.client("billing", region_name="us-east-1"))
        log_sync(conn, "aws_credits", True, n, "credits ok")
    except Exception as exc:
        log.error("AWS credits collection failed: %s", type(exc).__name__)
        log_sync(conn, "aws_credits", False, 0, f"get_credits: {type(exc).__name__}: {exc}")
