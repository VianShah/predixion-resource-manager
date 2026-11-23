"""Interakt (WhatsApp): delivery-status webhooks.

Interakt returns template status only through webhooks, not in the send response, and documents no wallet-balance
API. So this module receives webhooks and the wallet balance is entered by hand (rm.balance).
Source: Interakt "Message Webhook Format" (read 23-Nov-2026). Webhooks need the Growth or Advanced plan; there are no
retries; five failures in ten minutes disable the webhook.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

from ..config import mask
from ..db import utcnow
from ..purpose import resolve

log = logging.getLogger("rm.interakt")
IST = timezone(timedelta(hours=5, minutes=30))
RANK = {"Sent": 1, "Delivered": 2, "Read": 3}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Header is 'sha256=' plus the hex HMAC-SHA256 of the raw body, keyed with the Secret Key."""
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def template_name(raw) -> str:
    try:
        obj = raw if isinstance(raw, dict) else json.loads(raw)
        return str(obj.get("name") or "")
    except (TypeError, ValueError, AttributeError):
        return ""


def category_for(rates: dict, template: str) -> str:
    for rule in rates.get("interakt", {}).get("template_categories", []):
        if template.startswith(rule["prefix"]):
            return rule["category"]
    return "unknown"


def ingest(conn, rules: dict, rates: dict, payload: dict) -> str:
    """Store one webhook event. Returns the message id, or '' when the event is not a message status."""
    etype = str(payload.get("type") or "")
    if not (etype.startswith("message_api_") or etype.startswith("message_campaign_")):
        return ""
    msg = (payload.get("data") or {}).get("message") or {}
    mid = str(msg.get("id") or "")
    if not mid:
        return ""
    status = msg.get("message_status") or etype.rsplit("_", 1)[-1].capitalize()
    tmpl = template_name(msg.get("raw_template"))
    p = resolve(rules, "interakt", {"template": tmpl, "campaign_id": msg.get("campaign_id")})
    ts = payload.get("timestamp") or utcnow()
    try:
        day = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(IST).date().isoformat()
    except ValueError:
        day = str(ts)[:10]
    row = conn.execute("SELECT status FROM messages WHERE id=?", (mid,)).fetchone()
    old = row["status"] if row else None
    if status == "Failed":
        new = "Failed"
    elif old == "Failed":
        new = "Failed"
    else:
        new = status if RANK.get(status, 0) >= RANK.get(old or "", 0) else old
    phone = mask((payload.get("data") or {}).get("customer", {}).get("channel_phone_number"))
    conn.execute(
        "INSERT INTO messages(id,first_ts,day,template,category,status,error_code,error_reason,campaign_id,phone,"
        "sent_at,delivered_at,read_at,client,product,purpose) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
        "error_code=COALESCE(excluded.error_code,messages.error_code),error_reason=COALESCE(excluded.error_reason,messages.error_reason),"
        "sent_at=COALESCE(excluded.sent_at,messages.sent_at),delivered_at=COALESCE(excluded.delivered_at,messages.delivered_at),"
        "read_at=COALESCE(excluded.read_at,messages.read_at),"
        "template=CASE WHEN excluded.template!='' THEN excluded.template ELSE messages.template END",
        (mid, ts, day, tmpl, category_for(rates, tmpl), new,
         (str(msg["channel_error_code"]) if msg.get("channel_error_code") else None), msg.get("channel_failure_reason"),
         msg.get("campaign_id"), phone, msg.get("received_at_utc"), msg.get("delivered_at_utc"), msg.get("seen_at_utc"),
         p["client"], p["product"], p["purpose"]))
    conn.commit()
    return mid
