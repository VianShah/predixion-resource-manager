"""Vobiz: call detail records (CDR), account balance and live concurrency.

Endpoints and field names are from https://www.vobiz.ai/docs/cdr, /docs/account/balance and /docs/account/concurrency
(read 23-Nov-2026). Fields marked VERIFY in the docs are handled defensively.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from ..config import Settings, mask
from ..db import log_sync, set_balance
from ..purpose import resolve

log = logging.getLogger("rm.vobiz")
IST = timezone(timedelta(hours=5, minutes=30))


class VobizError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class VobizClient:
    def __init__(self, auth_id: str, auth_token: str, base_url: str = "https://api.vobiz.ai",
                 session: requests.Session | None = None, sleep=time.sleep):
        self.auth_id, self.base_url = auth_id, base_url.rstrip("/")
        self.http = session or requests.Session()
        self.sleep = sleep
        self.headers = {"X-Auth-ID": auth_id, "X-Auth-Token": auth_token, "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/api/v1/Account/{self.auth_id}{path}"
        for attempt in range(5):
            resp = self.http.get(url, headers=self.headers, params=params, timeout=20)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After") or 2 ** attempt)
                log.warning("Vobiz %s on %s, retrying in %.0fs", resp.status_code, path, wait)
                self.sleep(min(wait, 30))
                continue
            if resp.status_code >= 400:
                raise VobizError(resp.status_code, resp.text[:200].replace(self.headers["X-Auth-Token"], "***"))
            return resp.json()
        raise VobizError(resp.status_code, "gave up after retries")

    def cdr_rows(self, start_date: str, end_date: str):
        page = 1
        while True:
            body = self._get("/cdr", {"start_date": start_date, "end_date": end_date, "page": page, "per_page": 100})
            yield from body.get("data", [])
            if not (body.get("pagination") or {}).get("has_next"):
                return
            page += 1

    def balance(self, currency: str) -> dict:
        return self._get(f"/balance/{currency}")

    def concurrency(self) -> dict:
        return self._get("/concurrency")


def parse_cdr_row(row: dict, rules: dict) -> dict:
    """Map one CDR record to our table. Phone numbers are masked before storage."""
    start = row.get("start_time") or ""
    try:
        day = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(IST).date().isoformat()
    except ValueError:
        day = start[:10]
    total = row.get("total_cost")
    cost = float(total if total is not None else (row.get("cost") or 0))
    frm = row.get("caller_id_number") or ""
    fields = {"campaign_id": row.get("campaign_id"), "from_number": frm, "direction": row.get("call_direction")}
    p = resolve(rules, "vobiz", fields)
    return {
        "uuid": row.get("uuid") or str(row.get("id")), "start_ts": start, "day": day,
        "direction": row.get("call_direction"), "from_number": mask(frm), "to_number": mask(row.get("destination_number")),
        "duration": int(row.get("duration") or 0), "billsec": int(row.get("billsec") or 0), "cost_inr": cost,
        "hangup_cause": row.get("hangup_cause") or "UNKNOWN", "hangup_source": row.get("hangup_source"),
        "campaign_id": row.get("campaign_id"), "mos": row.get("mos"), "jitter": row.get("jitter"),
        "packet_loss": row.get("packet_loss"), **p,
    }


def store_calls(conn, rows: list[dict]) -> int:
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO calls(uuid,start_ts,day,direction,from_number,to_number,duration,billsec,cost_inr,"
            "hangup_cause,hangup_source,campaign_id,mos,jitter,packet_loss,client,product,purpose) "
            "VALUES(:uuid,:start_ts,:day,:direction,:from_number,:to_number,:duration,:billsec,:cost_inr,:hangup_cause,"
            ":hangup_source,:campaign_id,:mos,:jitter,:packet_loss,:client,:product,:purpose)", r)
    conn.commit()
    return len(rows)


def collect(conn, settings: Settings, rules: dict, days: int = 2, client: VobizClient | None = None) -> None:
    if not settings.vobiz_enabled and client is None:
        log.info("Vobiz credentials not set; skipping Vobiz")
        return
    client = client or VobizClient(settings.vobiz_auth_id, settings.vobiz_auth_token, settings.vobiz_base_url)
    today = datetime.now(IST).date()
    start, end = (today - timedelta(days=days)).isoformat(), today.isoformat()
    try:
        n = store_calls(conn, [parse_cdr_row(r, rules) for r in client.cdr_rows(start, end)])
        log_sync(conn, "vobiz", True, n, f"cdr {start}..{end}")
    except Exception as exc:
        log.error("Vobiz CDR failed: %s", exc)
        log_sync(conn, "vobiz", False, 0, f"cdr: {exc}")
    try:
        b = client.balance(settings.vobiz_currency)
        for key in ("balance", "reserved_funds", "available_balance", "promotional_balance", "credit_limit", "low_balance_threshold"):
            if b.get(key) is not None:
                set_balance(conn, "vobiz", key, float(b[key]), settings.vobiz_currency)
        for key in ("credit_limit_type", "status"):
            if b.get(key) is not None:
                set_balance(conn, "vobiz", key, None, "", text=str(b[key]))
        set_balance(conn, "vobiz", "is_postpaid", 1.0 if b.get("is_postpaid") else 0.0)
        c = client.concurrency()
        for key in ("concurrent_calls", "max_concurrent", "utilization_pct"):
            if c.get(key) is not None:
                set_balance(conn, "vobiz", key, float(c[key]))
        log_sync(conn, "vobiz_account", True, 1, "balance and concurrency ok")
    except Exception as exc:
        log.error("Vobiz account calls failed: %s", exc)
        log_sync(conn, "vobiz_account", False, 0, f"balance: {exc}")
