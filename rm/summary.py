"""Turn stored rows into the numbers the dashboard shows. One function per service, all read-only."""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timedelta, timezone

from .alerts import build_alerts
from .config import Settings
from .db import get_balances, last_sync

IST = timezone(timedelta(hours=5, minutes=30))
CODE_NAMES = {402: "insufficient_quota_error", 403: "invalid_api_key_error", 429: "rate limit", 500: "internal_server_error",
              502: "model_call_failed", 503: "model_overloaded"}


def rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _pct(a: float, b: float) -> float:
    return round(100 * a / b, 1) if b else 0.0


def _sync(conn, vendor: str) -> dict | None:
    s = last_sync(conn, vendor)
    return {"ts": s["ts"], "ok": bool(s["ok"]), "message": s["message"]} if s else None


def _bal(b: dict, key: str):
    return b[key]["value"] if key in b else None


def _purposes(items: list[dict]) -> list[dict]:
    return [dict(i, label=" · ".join(x for x in (i.get("client"), i.get("product"), i.get("purpose")) if x)) for i in items]


def aws_section(conn, s: Settings, today: date) -> dict:
    ms = today.replace(day=1).isoformat()
    d30 = (today - timedelta(days=29)).isoformat()
    d7 = (today - timedelta(days=6)).isoformat()
    by_service = rows(conn, "SELECT k1 AS name, SUM(usd) AS usd FROM aws_cost WHERE dim='gross_service' AND day>=? GROUP BY k1 ORDER BY usd DESC LIMIT 12", (ms,))
    mtd = (conn.execute("SELECT COALESCE(SUM(usd),0) v FROM aws_cost WHERE dim='gross_service' AND day>=?", (ms,)).fetchone()["v"])
    net = (conn.execute("SELECT COALESCE(SUM(usd),0) v FROM aws_cost WHERE dim='net_total' AND day>=?", (ms,)).fetchone()["v"])
    purpose = rows(conn, "SELECT k1 AS client, k2 AS product, SUM(usd) AS usd FROM aws_cost WHERE dim='gross_purpose' AND day>=? GROUP BY k1,k2 ORDER BY usd DESC", (ms,))
    for p in purpose:
        p["client"] = p["client"] or "Unassigned"
        p["product"] = p["product"] or "Unassigned"
        p["purpose"] = "Infrastructure"
    untagged = sum(p["usd"] for p in purpose if p["client"] == "Unassigned")
    daily = rows(conn, "SELECT day, SUM(usd) AS usd FROM aws_cost WHERE dim='gross_service' AND day>=? GROUP BY day ORDER BY day", (d30,))
    last7 = conn.execute("SELECT COALESCE(SUM(usd),0) v, COUNT(DISTINCT day) n FROM aws_cost WHERE dim='gross_service' AND day>=?", (d7,)).fetchone()
    avg7 = last7["v"] / last7["n"] if last7["n"] else 0
    credits = []
    for c in rows(conn, "SELECT * FROM aws_credits ORDER BY end_ts"):
        end = datetime.fromtimestamp(c["end_ts"], timezone.utc).date() if c["end_ts"] else None
        bal = c["estimated"] if c["estimated"] is not None else c["remaining"]
        credits.append({"name": c["name"], "status": c["status"], "initial": c["initial"], "remaining": bal,
                        "expires": end.isoformat() if end else None, "days_to_expiry": (end - today).days if end else None,
                        "products": c["products"]})
    remaining = sum(c["remaining"] or 0 for c in credits if (c["days_to_expiry"] is None or c["days_to_expiry"] >= 0))
    dim = calendar.monthrange(today.year, today.month)[1]
    forecast = mtd / today.day * dim if today.day else mtd
    return {
        "configured": s.aws_enabled, "last_sync": _sync(conn, "aws"), "credits_sync": _sync(conn, "aws_credits"),
        "kpis": {"mtd_usd": round(mtd, 2), "mtd_inr": round(mtd * s.usd_inr), "credits_applied_usd": round(max(mtd - net, 0), 2),
                 "cash_usd": round(max(net, 0), 2), "forecast_usd": round(forecast, 2), "avg7_usd": round(avg7, 2),
                 "credits_remaining_usd": round(remaining, 2),
                 "runway_days": round(remaining / avg7) if avg7 > 0 and remaining > 0 else None,
                 "untagged_pct": _pct(untagged, mtd), "budget_usd": s.aws_budget_usd, "usd_inr": s.usd_inr},
        "by_service": by_service, "by_purpose": _purposes(purpose), "daily": daily, "credits": credits,
    }


def vobiz_section(conn, s: Settings, today: date) -> dict:
    ms = today.replace(day=1).isoformat()
    d30 = (today - timedelta(days=29)).isoformat()
    tot = conn.execute("SELECT COUNT(*) calls, COALESCE(SUM(billsec>0),0) answered, COALESCE(SUM(billsec),0) talk, COALESCE(SUM(cost_inr),0) cost FROM calls WHERE day>=?", (ms,)).fetchone()
    purpose = rows(conn, "SELECT client, product, purpose, COUNT(*) AS calls, SUM(billsec>0) AS answered, ROUND(SUM(billsec)/60.0,1) AS talk_min, ROUND(SUM(cost_inr),2) AS cost FROM calls WHERE day>=? GROUP BY client,product,purpose ORDER BY cost DESC", (ms,))
    hangups = rows(conn, "SELECT hangup_cause AS name, COUNT(*) AS n FROM calls WHERE day>=? GROUP BY hangup_cause ORDER BY n DESC LIMIT 8", (ms,))
    daily = rows(conn, "SELECT day, COUNT(*) AS calls, SUM(billsec>0) AS answered, ROUND(SUM(cost_inr),2) AS cost FROM calls WHERE day>=? GROUP BY day ORDER BY day", (d30,))
    numbers = rows(conn, "SELECT from_number AS number, COUNT(*) AS calls, SUM(billsec>0) AS answered FROM calls WHERE day>=? GROUP BY from_number ORDER BY calls DESC LIMIT 10", (ms,))
    q = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(mos<3.5 OR jitter>30 OR packet_loss>1),0) bad, COALESCE(SUM(hangup_cause='MEDIA_TIMEOUT'),0) mt FROM calls WHERE day>=? AND billsec>0 AND mos IS NOT NULL", (ms,)).fetchone()
    d1, d7 = today.isoformat(), (today - timedelta(days=7)).isoformat()
    y = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(billsec>0),0) a FROM calls WHERE day>=?", (d1,)).fetchone()
    p = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(billsec>0),0) a FROM calls WHERE day>=? AND day<?", (d7, d1)).fetchone()
    window = 0
    window_total = 0
    for r in rows(conn, "SELECT start_ts, product FROM calls WHERE day>=? AND direction='outbound' AND product='Kollect'", (d7,)):
        try:
            h = datetime.fromisoformat(r["start_ts"].replace("Z", "+00:00")).astimezone(IST).hour
        except ValueError:
            continue
        window_total += 1
        window += 1 if (h < 8 or h >= 19) else 0
    b = get_balances(conn, "vobiz")
    avg7 = (conn.execute("SELECT COALESCE(SUM(cost_inr),0) v FROM calls WHERE day>=?", (d7,)).fetchone()["v"]) / 7
    avail = _bal(b, "available_balance")
    return {
        "configured": s.vobiz_enabled, "last_sync": _sync(conn, "vobiz"), "account_sync": _sync(conn, "vobiz_account"),
        "kpis": {"calls": tot["calls"], "answered": tot["answered"], "answer_rate": _pct(tot["answered"], tot["calls"]),
                 "talk_min": round(tot["talk"] / 60, 1), "cost_inr": round(tot["cost"], 2),
                 "cost_per_answered": round(tot["cost"] / tot["answered"], 2) if tot["answered"] else None,
                 "available_balance": avail, "low_balance_threshold": _bal(b, "low_balance_threshold"),
                 "is_postpaid": bool(_bal(b, "is_postpaid")), "credit_limit": _bal(b, "credit_limit"),
                 # Postpaid: a negative balance is money owed, and the runway is the headroom left under the credit limit.
                 "amount_owed": round(-avail, 2) if avail is not None and avail < 0 else 0.0,
                 "credit_headroom": round(_bal(b, "credit_limit") + avail, 2) if bool(_bal(b, "is_postpaid")) and avail is not None and _bal(b, "credit_limit") is not None else None,
                 "credit_used_pct": round(-avail / _bal(b, "credit_limit") * 100, 1) if bool(_bal(b, "is_postpaid")) and avail is not None and avail < 0 and _bal(b, "credit_limit") else 0.0,
                 "balance_days_left": round(avail / avg7, 1) if avail is not None and avg7 > 0 else None,
                 "concurrency_pct": _bal(b, "utilization_pct"), "concurrent_calls": _bal(b, "concurrent_calls"),
                 "max_concurrent": _bal(b, "max_concurrent"), "credit_limit_type": (b.get("credit_limit_type") or {}).get("text"),
                 "quality_bad_pct": _pct(q["bad"], q["n"]), "media_timeout_pct": _pct(q["mt"], q["n"]),
                 "today_calls": y["n"], "today_answer_rate": _pct(y["a"], y["n"]), "prior7_answer_rate": _pct(p["a"], p["n"]),
                 "prior7_calls": p["n"], "outside_window_calls": window, "outside_window_pct": _pct(window, window_total)},
        "by_purpose": _purposes(purpose), "by_hangup": hangups, "daily": daily, "by_number": numbers,
    }


def sarvam_section(conn, s: Settings, today: date) -> dict:
    ms = today.replace(day=1).isoformat()
    d30 = (today - timedelta(days=29)).isoformat()
    ok = "http_status<400"
    tot = conn.execute(f"SELECT COUNT(*) n, COALESCE(SUM(cost_inr),0) cost FROM sarvam_usage WHERE day>=? AND {ok}", (ms,)).fetchone()
    purpose = rows(conn, f"SELECT client, product, purpose, COUNT(*) AS requests, ROUND(SUM(cost_inr),2) AS cost FROM sarvam_usage WHERE day>=? AND {ok} GROUP BY client,product,purpose ORDER BY cost DESC", (ms,))
    by_api = rows(conn, f"SELECT api AS name, COUNT(*) AS requests, ROUND(SUM(cost_inr),2) AS cost FROM sarvam_usage WHERE day>=? AND {ok} GROUP BY api ORDER BY cost DESC", (ms,))
    by_ws = rows(conn, f"SELECT workspace AS name, COUNT(*) AS requests, ROUND(SUM(cost_inr),2) AS cost FROM sarvam_usage WHERE day>=? AND {ok} GROUP BY workspace ORDER BY cost DESC", (ms,))
    daily = rows(conn, f"SELECT day, ROUND(SUM(cost_inr),2) AS cost FROM sarvam_usage WHERE day>=? AND {ok} GROUP BY day ORDER BY day", (d30,))
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    errs = rows(conn, "SELECT http_status AS status, COALESCE(error_code,'') AS code, COUNT(*) AS n FROM sarvam_usage WHERE ts>=? AND http_status>=400 GROUP BY http_status,error_code ORDER BY n DESC", (since,))
    for e in errs:
        e["code"] = e["code"] or CODE_NAMES.get(e["status"], "")
    secs = chars = tin = tout = 0.0
    for r in conn.execute(f"SELECT api, units FROM sarvam_usage WHERE day>=? AND {ok}", (ms,)):
        u = json.loads(r["units"] or "{}")
        secs += float(u.get("seconds", 0)) if r["api"].startswith("stt") else 0
        chars += float(u.get("chars", 0)) if r["api"] == "tts" else 0
        tin += float(u.get("input_tokens", 0)) + float(u.get("cached_input_tokens", 0))
        tout += float(u.get("output_tokens", 0))
    b = get_balances(conn, "sarvam")
    avg7 = (conn.execute(f"SELECT COALESCE(SUM(cost_inr),0) v FROM sarvam_usage WHERE day>=? AND {ok}", ((today - timedelta(days=6)).isoformat(),)).fetchone()["v"]) / 7
    bal = _bal(b, "credits")
    last = conn.execute("SELECT MAX(ts) t FROM sarvam_usage").fetchone()["t"]
    return {
        "configured": bool(s.ingest_token), "last_event": last,
        "kpis": {"requests": tot["n"], "cost_inr": round(tot["cost"], 2), "stt_min": round(secs / 60, 1), "tts_chars": int(chars),
                 "llm_tokens_in": int(tin), "llm_tokens_out": int(tout), "plan": s.sarvam_plan,
                 "balance_inr": bal, "balance_ts": b["credits"]["ts"] if "credits" in b else None,
                 "balance_days_left": round(bal / avg7, 1) if bal is not None and avg7 > 0 else None, "errors_24h": sum(e["n"] for e in errs)},
        "by_purpose": _purposes(purpose), "by_api": by_api, "by_workspace": by_ws, "daily": daily, "errors": errs,
    }


def interakt_section(conn, s: Settings, rates: dict, today: date) -> dict:
    ms = today.replace(day=1).isoformat()
    d30 = (today - timedelta(days=29)).isoformat()
    rate = rates.get("interakt", {}).get("rate_per_delivered_message_inr", {})
    tot = conn.execute("SELECT COUNT(*) sent, COALESCE(SUM(status IN ('Delivered','Read') OR delivered_at IS NOT NULL),0) delivered, "
                       "COALESCE(SUM(status='Read' OR read_at IS NOT NULL),0) read, COALESCE(SUM(status='Failed'),0) failed FROM messages WHERE day>=?", (ms,)).fetchone()
    cat = rows(conn, "SELECT category, COALESCE(SUM(status IN ('Delivered','Read') OR delivered_at IS NOT NULL),0) AS delivered FROM messages WHERE day>=? GROUP BY category", (ms,))
    cost = sum(c["delivered"] * rate.get(c["category"], 0) for c in cat)
    purpose = rows(conn, "SELECT client, product, purpose, category, COUNT(*) AS sent, SUM(status IN ('Delivered','Read') OR delivered_at IS NOT NULL) AS delivered, SUM(status='Failed') AS failed FROM messages WHERE day>=? GROUP BY client,product,purpose,category", (ms,))
    for p in purpose:
        p["cost"] = round(p["delivered"] * rate.get(p["category"], 0), 2)
    purpose.sort(key=lambda p: -p["cost"])
    templates = rows(conn, "SELECT template AS name, category, COUNT(*) AS sent, SUM(status='Failed') AS failed FROM messages WHERE day>=? GROUP BY template,category ORDER BY sent DESC LIMIT 12", (ms,))
    errors = rows(conn, "SELECT error_code AS code, MAX(error_reason) AS reason, COUNT(*) AS n FROM messages WHERE day>=? AND status='Failed' GROUP BY error_code ORDER BY n DESC LIMIT 8", (ms,))
    daily = rows(conn, "SELECT day, COUNT(*) AS sent, SUM(status='Failed') AS failed FROM messages WHERE day>=? GROUP BY day ORDER BY day", (d30,))
    h24 = (today - timedelta(days=1)).isoformat()
    r24 = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(status='Failed'),0) f FROM messages WHERE day>=?", (h24,)).fetchone()
    last = conn.execute("SELECT MAX(COALESCE(read_at,delivered_at,sent_at,first_ts)) t FROM messages").fetchone()["t"]
    b = get_balances(conn, "interakt")
    avg7 = sum(c["delivered"] * rate.get(c["category"], 0) for c in rows(conn, "SELECT category, COALESCE(SUM(status IN ('Delivered','Read') OR delivered_at IS NOT NULL),0) AS delivered FROM messages WHERE day>=? GROUP BY category", ((today - timedelta(days=6)).isoformat(),))) / 7
    bal = _bal(b, "wallet")
    return {
        "configured": s.interakt_enabled, "last_event": last,
        "kpis": {"sent": tot["sent"], "delivered": tot["delivered"], "delivery_rate": _pct(tot["delivered"], tot["sent"]),
                 "read_rate": _pct(tot["read"], tot["delivered"]), "failed": tot["failed"], "failure_rate": _pct(tot["failed"], tot["sent"]),
                 "est_cost_inr": round(cost, 2), "wallet_inr": bal, "wallet_ts": b["wallet"]["ts"] if "wallet" in b else None,
                 "wallet_days_left": round(bal / avg7, 1) if bal is not None and avg7 > 0 else None,
                 "last24_sent": r24["n"], "last24_failure_rate": _pct(r24["f"], r24["n"])},
        "rate_status": rates.get("interakt", {}).get("_status", ""),
        "by_purpose": _purposes(purpose), "by_template": templates, "errors": errors, "daily": daily,
    }


def build_summary(conn, settings: Settings, rates: dict, now: datetime | None = None, demo: bool = False) -> dict:
    now = now or datetime.now(IST)
    today = now.astimezone(IST).date()
    services = {
        "aws": aws_section(conn, settings, today),
        "sarvam": sarvam_section(conn, settings, today),
        "interakt": interakt_section(conn, settings, rates, today),
        "vobiz": vobiz_section(conn, settings, today),
    }
    # Spend in rupees for the overview, with how much of it has no purpose yet
    spend = {
        "aws": services["aws"]["kpis"]["mtd_inr"], "sarvam": services["sarvam"]["kpis"]["cost_inr"],
        "interakt": services["interakt"]["kpis"]["est_cost_inr"], "vobiz": services["vobiz"]["kpis"]["cost_inr"],
    }
    unassigned = {
        "aws": services["aws"]["kpis"]["untagged_pct"],
        "sarvam": _share_unassigned(services["sarvam"]["by_purpose"], "cost"),
        "interakt": _share_unassigned(services["interakt"]["by_purpose"], "cost"),
        "vobiz": _share_unassigned(services["vobiz"]["by_purpose"], "cost"),
    }
    summary = {"generated_at": now.isoformat(timespec="seconds"), "demo": demo, "month": today.strftime("%B %Y"),
               "today": today.isoformat(), "spend_inr": spend, "unassigned_pct": unassigned, "services": services,
               "configured": settings.public_status()}
    summary["alerts"] = build_alerts(summary, now)
    return summary


def _share_unassigned(items: list[dict], key: str) -> float:
    total = sum(i.get(key) or 0 for i in items)
    un = sum(i.get(key) or 0 for i in items if i.get("client") == "Unassigned")
    return _pct(un, total)
