"""Synthetic data for a demo or preview. Goes through the same code paths as live data. Nothing here is real."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .collectors import interakt, sarvam, vobiz
from .db import set_balance, log_sync, utcnow

IST = timezone(timedelta(hours=5, minutes=30))


def seed(conn, rules: dict, rates: dict, now: datetime | None = None, seed_value: int = 7) -> None:
    rnd = random.Random(seed_value)
    now = (now or datetime.now(IST)).astimezone(IST)
    today = now.date()

    # ---- AWS
    services = {"Amazon EC2": 38, "Amazon Bedrock": 22, "Amazon RDS": 14, "Amazon S3": 6, "AWS Lambda": 4, "Amazon CloudWatch": 5, "Data Transfer": 7}
    products = ["Kollect", "LeadX", "QA", ""]
    clients = {"Kollect": ["Snapmint", "SMFG", "OneCard"], "LeadX": ["EKL"], "QA": ["Internal"], "": [""]}
    for d in range(35):
        day = (today - timedelta(days=d)).isoformat()
        growth = 1 + (34 - d) * 0.008
        total = 0.0
        for svc, base in services.items():
            prod = rnd.choices(products, weights=[5, 3, 1, 1])[0]
            usd = round(base * growth * rnd.uniform(0.85, 1.15), 2)
            total += usd
            conn.execute("INSERT OR REPLACE INTO aws_cost VALUES('gross_service',?,?,?,?)", (day, svc, prod, usd))
        for prod in products:
            for cl in clients[prod]:
                usd = round(total * rnd.uniform(0.04, 0.2), 2)
                conn.execute("INSERT OR REPLACE INTO aws_cost VALUES('gross_purpose',?,?,?,?)",
                             (day, cl, prod, usd))
        conn.execute("INSERT OR REPLACE INTO aws_cost VALUES('net_total',?,?,?,?)", (day, "", "", round(max(total - 78, 0) * 0.15, 2)))
    end = int((now + timedelta(days=24)).timestamp())
    conn.execute("DELETE FROM aws_credits")
    conn.execute("INSERT INTO aws_credits VALUES('demo-1','Startup program credit','PROMOTIONAL','ACTIVE','USD',25000,5600,5600,?,?,NULL,'All services',?)",
                 (int((now - timedelta(days=300)).timestamp()), end, utcnow()))
    conn.execute("INSERT INTO aws_credits VALUES('demo-2','Bedrock credit','PROMOTIONAL','ACTIVE','USD',5000,3200,3200,?,?,NULL,'Amazon Bedrock',?)",
                 (int((now - timedelta(days=60)).timestamp()), int((now + timedelta(days=200)).timestamp()), utcnow()))
    conn.commit()
    log_sync(conn, "aws", True, 35 * 12, "demo")
    log_sync(conn, "aws_credits", True, 2, "demo")

    # ---- Vobiz
    camps = [("SMFG-DPD1", "+910000000011", 0.34), ("EXAMPLE-SNAPMINT-DPD1", "+910000000012", 0.30),
             ("EKL-NURTURE", "+910000000013", 0.20), ("", "+910000000001", 0.12), ("MISC-77", "+910000000099", 0.04)]
    causes = ["NORMAL_CLEARING"] * 12 + ["NO_ANSWER"] * 7 + ["USER_BUSY"] * 3 + ["UNALLOCATED_NUMBER"] + ["CALL_REJECTED"] * 2
    rows = []
    n = 0
    for d in range(30):
        date = today - timedelta(days=d)
        for _ in range(rnd.randint(260, 340)):
            cid, frm, _w = rnd.choices(camps, weights=[c[2] for c in camps])[0]
            hour = rnd.choices(range(8, 19), weights=[4, 8, 9, 8, 6, 5, 6, 8, 9, 7, 3])[0]
            start = datetime(date.year, date.month, date.day, hour, rnd.randint(0, 59), tzinfo=IST)
            cause = rnd.choice(causes)
            answered = cause == "NORMAL_CLEARING"
            bill = rnd.randint(8, 140) if answered else 0
            n += 1
            rows.append({"uuid": f"demo-{n}", "start_time": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "call_direction": "outbound", "caller_id_number": frm, "destination_number": f"+9198{rnd.randint(10000000, 99999999)}",
                         "duration": bill + (rnd.randint(8, 30) if not answered else 0), "billsec": bill,
                         "total_cost": round(max(bill, 0) / 60 * 1.1 + 0.05, 2), "hangup_cause": cause, "hangup_source": "callee",
                         "campaign_id": cid, "mos": round(rnd.uniform(3.55, 4.4), 2) if answered else None,
                         "jitter": round(rnd.uniform(2, 22), 1) if answered else None, "packet_loss": round(rnd.uniform(0, 0.6), 2) if answered else None})
    vobiz.store_calls(conn, [vobiz.parse_cdr_row(r, rules) for r in rows])
    log_sync(conn, "vobiz", True, len(rows), "demo")
    log_sync(conn, "vobiz_account", True, 1, "demo")
    for key, val in (("balance", 18400.0), ("reserved_funds", 300.0), ("available_balance", 18100.0), ("low_balance_threshold", 5000.0),
                     ("concurrent_calls", 38.0), ("max_concurrent", 50.0), ("utilization_pct", 76.0)):
        set_balance(conn, "vobiz", key, val, "INR")
    set_balance(conn, "vobiz", "credit_limit_type", None, "", text="prepaid")

    # ---- Interakt
    templates = ["collect_reminder_v2", "collect_ptp_followup", "settle_offer_v1", "lead_welcome_v3", "otp_login", "misc_notice"]
    codes = [("131026", "Message undeliverable"), ("131049", "Not delivered to maintain healthy ecosystem engagement"), ("132000", "Number of parameters does not match")]
    for d in range(30):
        date = today - timedelta(days=d)
        for _ in range(rnd.randint(150, 260)):
            t = rnd.choices(templates, weights=[8, 4, 3, 5, 3, 1])[0]
            ts = datetime(date.year, date.month, date.day, rnd.randint(8, 20), rnd.randint(0, 59), tzinfo=IST).astimezone(timezone.utc)
            mid = f"demo-m-{d}-{rnd.randint(0, 10**9)}"
            fail = rnd.random() < (0.02 if d > 0 else 0.045)
            base = {"timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "data": {"customer": {"channel_phone_number": f"91{rnd.randint(7000000000, 9999999999)}"},
                             "message": {"id": mid, "raw_template": {"name": t}}}}
            def send(kind, **extra):
                p = {"type": f"message_api_{kind}", **base}
                p["data"] = {**base["data"], "message": {**base["data"]["message"], **extra}}
                interakt.ingest(conn, rules, rates, p)
            send("sent", message_status="Sent", received_at_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if fail:
                code, why = rnd.choice(codes)
                send("failed", message_status="Failed", channel_error_code=code, channel_failure_reason=why)
                continue
            send("delivered", message_status="Delivered", delivered_at_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if rnd.random() < 0.55:
                send("read", message_status="Read", seen_at_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
    set_balance(conn, "interakt", "wallet", 6200.0, "INR", source="manual")

    # ---- Sarvam (self-logged)
    for d in range(30):
        date = today - timedelta(days=d)
        for i in range(rnd.randint(180, 260)):
            ws = rnd.choices(["kollect-prod", "leadx-prod", "qa-audit", "dev-sandbox", "legacy-key"], weights=[10, 5, 2, 1, 1])[0]
            api = rnd.choices(["stt", "tts", "llm"], weights=[5, 4, 5])[0]
            units = ({"seconds": rnd.randint(20, 120)} if api == "stt" else {"chars": rnd.randint(200, 900)} if api == "tts"
                     else {"input_tokens": rnd.randint(800, 3000), "cached_input_tokens": rnd.randint(0, 1500), "output_tokens": rnd.randint(40, 300)})
            ts = datetime(date.year, date.month, date.day, rnd.randint(8, 19), rnd.randint(0, 59), tzinfo=IST).astimezone(timezone.utc)
            status = 200
            code = None
            if d == 0 and rnd.random() < 0.01:
                status, code = 429, "rate_limit_exceeded_error"
            sarvam.record(conn, rules, rates, "payg", {
                "api": api, "model": {"stt": "saaras", "tts": "bulbul:v3", "llm": "sarvam-105b"}[api], "units": units, "workspace": ws,
                "http_status": status, "error_code": code, "ref": f"demo-s-{d}-{i}", "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")})
    set_balance(conn, "sarvam", "credits", 21000.0, "INR", source="manual")
