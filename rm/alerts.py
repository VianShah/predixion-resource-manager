"""Alert rules. Each alert names the fix guide entry (runbook id) that explains how to resolve it.

Rules read only the summary dict, so they are easy to test. Thresholds are constants at the top: change them here.
"""
from __future__ import annotations

from datetime import datetime, timezone

CREDIT_EXPIRY_WARN_DAYS = 30
CREDIT_EXPIRY_CRIT_DAYS = 7
RUNWAY_WARN_DAYS, RUNWAY_CRIT_DAYS = 45, 21
UNTAGGED_WARN_PCT = 15
SARVAM_RATE_LIMIT_WARN = 5
SARVAM_5XX_WARN = 5
BALANCE_WARN_DAYS, BALANCE_CRIT_DAYS = 7, 3
BALANCE_STALE_HOURS = 48
VOBIZ_CONCURRENCY_WARN_PCT = 90
VOBIZ_ANSWER_DROP_POINTS = 10
VOBIZ_QUALITY_WARN_PCT = 5
VOBIZ_POSTPAID_WARN_PCT = 70  # assumption, VERIFY with finance: share of credit limit used before we warn
VOBIZ_POSTPAID_CRIT_PCT = 90  # assumption, VERIFY
INTERAKT_FAIL_WARN_PCT, INTERAKT_FAIL_CRIT_PCT = 3, 8
INTERAKT_SILENCE_HOURS = 12

# WhatsApp error code to the fix guide entry that covers it
META_CODE_RUNBOOK = {
    "131026": "meta-delivery", "131047": "meta-delivery", "131056": "meta-delivery",
    "130429": "meta-throughput",
    "131048": "meta-quality", "131049": "meta-quality", "131050": "meta-quality",
    "132000": "meta-template", "132001": "meta-template", "132012": "meta-template", "132015": "meta-template", "132016": "meta-template",
    "131042": "meta-account", "131031": "meta-account", "368": "meta-account", "190": "meta-account", "100": "meta-account", "133010": "meta-account",
}


def _alert(service, sev, title, detail, runbook):
    return {"service": service, "severity": sev, "title": title, "detail": detail, "runbook": runbook}


def _hours_since(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 3600


def _inr(v) -> str:
    return f"₹{v:,.0f}"


def build_alerts(summary: dict, now: datetime) -> list[dict]:
    out: list[dict] = []
    sv = summary["services"]

    # ---- AWS
    a = sv["aws"]
    k = a["kpis"]
    if a["configured"] and a["last_sync"] and not a["last_sync"]["ok"]:
        out.append(_alert("aws", "warn", "AWS cost data is not updating", a["last_sync"]["message"], "aws-access"))
    if a["configured"] and a.get("credits_sync") and not a["credits_sync"]["ok"]:
        out.append(_alert("aws", "warn", "AWS credit balances are not updating", a["credits_sync"]["message"], "aws-access"))
    for c in a["credits"]:
        d = c["days_to_expiry"]
        if d is not None and 0 <= d <= CREDIT_EXPIRY_WARN_DAYS and (c["remaining"] or 0) > 0:
            out.append(_alert("aws", "crit" if d <= CREDIT_EXPIRY_CRIT_DAYS else "warn",
                              f"Credit \"{c['name']}\" expires in {d} days", f"${c['remaining']:,.0f} left.", "aws-credits"))
    if k["runway_days"] is not None and k["runway_days"] < RUNWAY_WARN_DAYS:
        out.append(_alert("aws", "crit" if k["runway_days"] < RUNWAY_CRIT_DAYS else "warn",
                          f"AWS credits cover about {k['runway_days']} more days",
                          f"${k['credits_remaining_usd']:,.0f} left at ${k['avg7_usd']:,.0f} a day (last 7 days).", "aws-credits"))
    if k["untagged_pct"] > UNTAGGED_WARN_PCT:
        out.append(_alert("aws", "warn", f"{k['untagged_pct']}% of AWS spend has no client tag",
                          "Spend without a client tag cannot be assigned to a client or purpose.", "aws-untagged"))
    if k["budget_usd"] and k["forecast_usd"] > k["budget_usd"]:
        out.append(_alert("aws", "crit", "AWS is forecast to pass its monthly budget",
                          f"Forecast ${k['forecast_usd']:,.0f} against ${k['budget_usd']:,.0f}.", "aws-budget"))

    # ---- Sarvam
    s = sv["sarvam"]
    sk = s["kpis"]
    for e in s["errors"]:
        st = e["status"]
        if st == 402:
            out.append(_alert("sarvam", "crit", f"Sarvam is rejecting calls with 402 ({e['n']} in 24h)",
                              "The organisation's credit balance is empty. Every workspace and key is affected.", "sarvam-402"))
        elif st == 403:
            out.append(_alert("sarvam", "crit", f"Sarvam rejected the API key {e['n']} times in 24h", e["code"], "sarvam-403"))
        elif st == 429 and e["n"] >= SARVAM_RATE_LIMIT_WARN:
            out.append(_alert("sarvam", "warn", f"Sarvam rate limit hit {e['n']} times in 24h", e["code"] or "429", "sarvam-429"))
        elif st >= 500 and e["n"] >= SARVAM_5XX_WARN:
            out.append(_alert("sarvam", "warn", f"Sarvam server errors ({st}) {e['n']} times in 24h", e["code"], "sarvam-5xx"))
    if sk["balance_days_left"] is not None and sk["balance_days_left"] < BALANCE_WARN_DAYS:
        out.append(_alert("sarvam", "crit" if sk["balance_days_left"] < BALANCE_CRIT_DAYS else "warn",
                          f"Sarvam credits cover about {sk['balance_days_left']} days",
                          f"{_inr(sk['balance_inr'])} at the last 7 days' spend. Add credits or turn on Auto-Recharge.", "sarvam-402"))
    stale = _hours_since(sk["balance_ts"], now)
    if sk["balance_inr"] is None:
        out.append(_alert("sarvam", "info", "Sarvam credit balance is not tracked", "Sarvam has no balance API. Enter it from Settings > Billing.", "sarvam-balance"))
    elif stale is not None and stale > BALANCE_STALE_HOURS:
        out.append(_alert("sarvam", "info", "Sarvam balance entry is out of date", f"Last entered {stale / 24:.0f} days ago.", "sarvam-balance"))

    # ---- Vobiz
    v = sv["vobiz"]
    vk = v["kpis"]
    if v["configured"] and v["last_sync"] and not v["last_sync"]["ok"]:
        out.append(_alert("vobiz", "warn", "Vobiz call records are not updating", v["last_sync"]["message"], "vobiz-auth"))
    if vk.get("is_postpaid") and vk["available_balance"] is not None and vk["credit_limit"]:
        # Postpaid: negative balance is normal (money owed). Alert on how much of the credit limit is used.
        used = vk["credit_used_pct"]
        if used >= VOBIZ_POSTPAID_WARN_PCT:
            out.append(_alert("vobiz", "crit" if used >= VOBIZ_POSTPAID_CRIT_PCT else "warn",
                              f"Vobiz postpaid credit {used:.0f}% used",
                              f"{_inr(vk['amount_owed'])} owed against a {_inr(vk['credit_limit'])} limit ({vk['credit_limit_type'] or 'limit type unknown'}). "
                              f"{_inr(vk['credit_headroom'])} headroom left.", "vobiz-low-balance"))
    elif vk["available_balance"] is not None:
        low = vk["low_balance_threshold"]
        if (low is not None and vk["available_balance"] <= low) or (vk["balance_days_left"] is not None and vk["balance_days_left"] < 5):
            out.append(_alert("vobiz", "crit", "Vobiz balance is low",
                              f"{_inr(vk['available_balance'])} available" + (f", about {vk['balance_days_left']} days of calling." if vk["balance_days_left"] else "."), "vobiz-low-balance"))
    if vk["concurrency_pct"] is not None and vk["concurrency_pct"] >= VOBIZ_CONCURRENCY_WARN_PCT:
        out.append(_alert("vobiz", "warn", f"Vobiz channels are {vk['concurrency_pct']:.0f}% full",
                          f"{vk['concurrent_calls']:.0f} of {vk['max_concurrent']:.0f} concurrent calls. New calls will be rejected with 429.", "vobiz-429"))
    if vk["today_calls"] >= 100 and vk["prior7_calls"] >= 300 and vk["prior7_answer_rate"] - vk["today_answer_rate"] >= VOBIZ_ANSWER_DROP_POINTS:
        out.append(_alert("vobiz", "warn", "Answer rate dropped today",
                          f"{vk['today_answer_rate']}% today against {vk['prior7_answer_rate']}% over the previous 7 days.", "vobiz-answer-rate"))
    if vk["quality_bad_pct"] > VOBIZ_QUALITY_WARN_PCT or vk["media_timeout_pct"] > 2:
        out.append(_alert("vobiz", "warn", "Call quality problems on Vobiz",
                          f"{vk['quality_bad_pct']}% of answered calls below the quality line; {vk['media_timeout_pct']}% media timeouts.", "vobiz-quality"))
    if vk["outside_window_calls"] > 0:
        out.append(_alert("vobiz", "crit", f"{vk['outside_window_calls']} Kollect calls started outside 8am to 7pm IST in the last 7 days",
                          "Collections calls are allowed only between 8am and 7pm. Fix the campaign schedule.", "vobiz-calling-window"))

    # ---- Interakt
    i = sv["interakt"]
    ik = i["kpis"]
    silent = _hours_since(i["last_event"], now)
    if i["configured"] and silent is not None and silent > INTERAKT_SILENCE_HOURS:
        out.append(_alert("interakt", "warn", f"No Interakt webhook events for {silent:.0f} hours",
                          "Interakt disables the webhook after five failures in ten minutes and does not replay missed events.", "interakt-webhook"))
    if ik["last24_sent"] >= 50 and ik["last24_failure_rate"] >= INTERAKT_FAIL_WARN_PCT:
        top = i["errors"][0] if i["errors"] else None
        rb = META_CODE_RUNBOOK.get(str(top["code"]), "interakt-unknown-error") if top else "interakt-unknown-error"
        out.append(_alert("interakt", "crit" if ik["last24_failure_rate"] >= INTERAKT_FAIL_CRIT_PCT else "warn",
                          f"{ik['last24_failure_rate']}% of WhatsApp messages failed in the last 24h",
                          f"Most common error: {top['code']} {top['reason'] or ''}".strip() if top else "No error code recorded.", rb))
    if ik["wallet_inr"] is None:
        out.append(_alert("interakt", "info", "Interakt wallet balance is not tracked", "Interakt documents no wallet API. Enter the balance from the dashboard.", "interakt-wallet"))
    else:
        if ik["wallet_days_left"] is not None and ik["wallet_days_left"] < BALANCE_WARN_DAYS:
            out.append(_alert("interakt", "crit" if ik["wallet_days_left"] < BALANCE_CRIT_DAYS else "warn",
                              f"Interakt wallet covers about {ik['wallet_days_left']} days", f"{_inr(ik['wallet_inr'])} left. API sends stop when it goes below zero.", "interakt-wallet"))
        st = _hours_since(ik["wallet_ts"], now)
        if st is not None and st > BALANCE_STALE_HOURS:
            out.append(_alert("interakt", "info", "Interakt wallet entry is out of date", f"Last entered {st / 24:.0f} days ago.", "interakt-wallet"))

    # ---- Purpose coverage, all services
    for name, pct in summary["unassigned_pct"].items():
        if pct > UNTAGGED_WARN_PCT and name != "aws":
            out.append(_alert(name, "warn", f"{pct}% of {name.capitalize()} spend has no purpose",
                              "Add a rule in purposes.json so this spend is attributed to a client and purpose.", "unassigned-purpose"))
    order = {"crit": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda x: order[x["severity"]])
