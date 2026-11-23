"""Sarvam: usage recorded by our own voice agents.

Sarvam documents usage and spend only in the dashboard (Settings > Usage, per workspace) and no usage API, so we
record each request ourselves and price it from rates.json. Errors are recorded too, so a 402 or 429 shows up here
and links to its fix. Credit balance is not available by API; enter it with rm.balance.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from ..purpose import resolve

log = logging.getLogger("rm.sarvam")
IST = timezone(timedelta(hours=5, minutes=30))
APIS = {"stt", "stt_diarize", "tts", "translate", "lid", "llm", "other"}


def cost_inr(rates: dict, plan: str, api: str, model: str, units: dict) -> float:
    s = rates["sarvam"]
    p = s["plans"].get(plan) or s["plans"]["payg"]
    if api == "stt":
        return float(units.get("seconds", 0)) / 60 * p["stt_per_min"]
    if api == "stt_diarize":
        return float(units.get("seconds", 0)) / 60 * p["stt_diarize_per_min"]
    if api == "tts":
        rate = p["tts_per_1k_chars"].get(model) or p["tts_per_1k_chars"]["default"]
        return float(units.get("chars", 0)) / 1000 * rate
    if api == "translate":
        return float(units.get("chars", 0)) / 1000 * p["translate_per_1k_chars"]
    if api == "lid":
        return float(units.get("chars", 0)) / 1000 * p["lid_per_1k_chars"]
    if api == "llm":
        m = s["llm_per_1m_tokens"].get(model) or s["llm_per_1m_tokens"]["default"]
        return (float(units.get("input_tokens", 0)) * m["input"] + float(units.get("cached_input_tokens", 0)) * m["cached_input"]
                + float(units.get("output_tokens", 0)) * m["output"]) / 1e6
    return 0.0


def record(conn, rules: dict, rates: dict, plan: str, event: dict) -> int:
    """Store one usage or error event. Returns the row id (existing id when 'ref' repeats)."""
    api = event.get("api") or "other"
    if api not in APIS:
        raise ValueError(f"api must be one of {sorted(APIS)}")
    ts = event.get("ts") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        day = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST).date().isoformat()
    except ValueError as exc:
        raise ValueError("ts must be ISO 8601") from exc
    status = int(event.get("http_status") or 200)
    units = event.get("units") or {}
    model = event.get("model") or ""
    cost = cost_inr(rates, plan, api, model, units) if status < 400 else 0.0
    p = resolve(rules, "sarvam", {"workspace": event.get("workspace"), "api": api})
    ref = event.get("ref")
    cur = conn.execute(
        "INSERT OR IGNORE INTO sarvam_usage(ref,ts,day,workspace,api,model,units,cost_inr,http_status,error_code,client,product,purpose) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ref, ts, day, event.get("workspace"), api, model, json.dumps(units), cost, status, event.get("error_code"),
         p["client"], p["product"], p["purpose"]))
    conn.commit()
    if cur.lastrowid and cur.rowcount:
        return cur.lastrowid
    row = conn.execute("SELECT id FROM sarvam_usage WHERE ref=?", (ref,)).fetchone()
    return row["id"] if row else 0
