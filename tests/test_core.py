import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from rm.alerts import build_alerts
from rm.collectors import aws, interakt, sarvam, vobiz
from rm.config import load_json, load_settings, mask
from rm.db import connect, get_balances, set_balance
from rm.demo import seed
from rm.purpose import UNASSIGNED, resolve
from rm.summary import build_summary

IST = timezone(timedelta(hours=5, minutes=30))
RULES, RATES = load_json("purposes.json"), load_json("rates.json")


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def settings(monkeypatch, tmp_path):
    for k in ("AWS_ACCOUNT_ID", "VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN", "INTERAKT_WEBHOOK_SECRET", "RM_INGEST_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(tmp_path / "none.env")
    s.ingest_token = "tok"
    s.interakt_secret = "sec"
    return s


# ---- purpose
def test_purpose_exact_prefix_and_default():
    assert resolve(RULES, "vobiz", {"campaign_id": "SMFG-X"})["client"] == "SMFG"
    assert resolve(RULES, "sarvam", {"workspace": "qa-1"})["product"] == "QA"
    assert resolve(RULES, "vobiz", {"campaign_id": "nope"}) == UNASSIGNED or resolve(RULES, "vobiz", {"campaign_id": "nope"})["client"] == "Unassigned"


def test_mask_hides_digits():
    assert "9876543210" not in mask("+919876543210") and mask("+919876543210").endswith("3210")


# ---- vobiz
def test_vobiz_parse_masks_and_ist_day():
    r = vobiz.parse_cdr_row({"uuid": "u1", "start_time": "2026-11-22T20:00:00Z", "caller_id_number": "+910000000001",
                             "destination_number": "+919876543210", "billsec": 30, "total_cost": 0.9, "campaign_id": "SMFG-1"}, RULES)
    assert r["day"] == "2026-11-23"  # 01:30 IST next day
    assert "9876543210" not in r["to_number"] and r["client"] == "SMFG" and r["cost_inr"] == 0.9


def test_vobiz_collect_with_fake_client(conn, settings):
    class Fake:
        def cdr_rows(self, a, b):
            yield {"uuid": "a", "start_time": "2026-11-23T05:00:00Z", "billsec": 10, "total_cost": 1, "hangup_cause": "NORMAL_CLEARING"}
            yield {"uuid": "a", "start_time": "2026-11-23T05:00:00Z", "billsec": 10, "total_cost": 1, "hangup_cause": "NORMAL_CLEARING"}

        def balance(self, c):
            return {"available_balance": 100, "low_balance_threshold": 50, "credit_limit_type": "prepaid"}

        def concurrency(self):
            return {"concurrent_calls": 9, "max_concurrent": 10, "utilization_pct": 90}

    vobiz.collect(conn, settings, RULES, client=Fake())
    assert conn.execute("select count(*) from calls").fetchone()[0] == 1  # idempotent
    b = get_balances(conn, "vobiz")
    assert b["available_balance"]["value"] == 100 and b["utilization_pct"]["value"] == 90


def test_vobiz_collect_records_failure(conn, settings):
    class Bad:
        def cdr_rows(self, a, b):
            raise RuntimeError("401")

        def balance(self, c):
            raise RuntimeError("401")

    vobiz.collect(conn, settings, RULES, client=Bad())
    assert conn.execute("select ok from sync_log where vendor='vobiz'").fetchone()[0] == 0


# ---- interakt
def _payload(kind, mid="m1", **msg):
    return {"type": f"message_api_{kind}", "timestamp": "2026-11-23T05:00:00Z",
            "data": {"customer": {"channel_phone_number": "919876543210"},
                     "message": {"id": mid, "raw_template": json.dumps({"name": "collect_reminder_v2"}), **msg}}}


def test_interakt_signature():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    assert interakt.verify_signature("sec", body, good)
    assert not interakt.verify_signature("sec", body, "sha256=bad")
    assert not interakt.verify_signature("sec", body, None)


def test_interakt_status_progression_and_failed_sticky(conn):
    for kind, st in (("sent", "Sent"), ("read", "Read"), ("delivered", "Delivered")):
        interakt.ingest(conn, RULES, RATES, _payload(kind, message_status=st))
    assert conn.execute("select status from messages").fetchone()[0] == "Read"  # late Delivered does not downgrade
    interakt.ingest(conn, RULES, RATES, _payload("failed", message_status="Failed", channel_error_code=131026))
    interakt.ingest(conn, RULES, RATES, _payload("delivered", message_status="Delivered"))
    r = conn.execute("select status,category,client,phone from messages").fetchone()
    assert r["status"] == "Failed" and r["category"] == "utility" and r["client"] == "Kollect clients"
    assert "9876543210" not in r["phone"]


def test_interakt_ignores_other_events(conn):
    assert interakt.ingest(conn, RULES, RATES, {"type": "workflow_x"}) == ""


# ---- sarvam
def test_sarvam_costing_and_errors_free():
    assert sarvam.cost_inr(RATES, "payg", "stt", "", {"seconds": 60}) == RATES["sarvam"]["plans"]["payg"]["stt_per_min"]
    assert sarvam.cost_inr(RATES, "payg", "other", "", {}) == 0


def test_sarvam_record_idempotent_and_402_zero_cost(conn):
    e = {"api": "stt", "units": {"seconds": 120}, "workspace": "kollect-prod", "ref": "r1"}
    a = sarvam.record(conn, RULES, RATES, "payg", e)
    b = sarvam.record(conn, RULES, RATES, "payg", e)
    assert a == b and conn.execute("select count(*) from sarvam_usage").fetchone()[0] == 1
    sarvam.record(conn, RULES, RATES, "payg", {"api": "tts", "units": {"chars": 500}, "http_status": 402, "ref": "r2"})
    assert conn.execute("select cost_inr from sarvam_usage where ref='r2'").fetchone()[0] == 0
    with pytest.raises(ValueError):
        sarvam.record(conn, RULES, RATES, "payg", {"api": "bogus"})


# ---- aws
def test_aws_costs_and_credits_with_fake_client(conn, settings):
    settings.aws_account_id = "123456789012"
    today = datetime(2026, 11, 23, tzinfo=timezone.utc).date()

    class CE:
        calls = []

        def get_cost_and_usage(self, **p):
            CE.calls.append(p)
            if "GroupBy" not in p:
                return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-22"}, "Total": {"UnblendedCost": {"Amount": "5"}}, "Groups": []}]}
            keys = ["Amazon EC2", "product$Kollect"] if p["GroupBy"][0]["Type"] == "DIMENSION" else ["client$Snapmint", "product$Kollect"]
            return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-22"}, "Groups": [{"Keys": keys, "Metrics": {"UnblendedCost": {"Amount": "10"}}}]}]}

    class Billing:
        def get_credits(self, **p):
            return {"credits": [{"creditId": "c1", "description": "Promo", "remainingAmount": {"amount": "100", "currencyCode": "USD"},
                                 "endDate": 1800000000}]}

    aws.collect_costs(conn, settings, CE(), today=today)
    assert all(len(c.get("GroupBy", [])) <= 2 for c in CE.calls)  # Cost Explorer limit
    row = conn.execute("select k1,k2 from aws_cost where dim='gross_purpose'").fetchone()
    assert (row[0], row[1]) == ("Snapmint", "Kollect")
    assert aws.collect_credits(conn, settings, Billing(), today=today) == 1


# ---- summary and alerts on demo data
@pytest.fixture
def demo(conn, settings):
    seed(conn, RULES, RATES)
    return build_summary(conn, settings, RATES, demo=True)


def test_summary_shape(demo):
    assert set(demo["services"]) == {"aws", "sarvam", "interakt", "vobiz"}
    assert all(v > 0 for v in demo["spend_inr"].values())
    assert 0 <= demo["unassigned_pct"]["vobiz"] <= 100
    assert demo["services"]["vobiz"]["by_purpose"]


def test_every_alert_points_at_a_runbook(demo):
    ids = {r["id"] for r in load_json("runbooks.json")["runbooks"]}
    now = datetime.now(IST)
    s = json.loads(json.dumps(demo))
    s["services"]["sarvam"]["errors"] = [{"status": 402, "code": "", "n": 3}, {"status": 429, "code": "", "n": 9}, {"status": 403, "code": "", "n": 1}, {"status": 503, "code": "", "n": 9}]
    s["services"]["vobiz"]["kpis"].update(available_balance=100, low_balance_threshold=500, concurrency_pct=95, concurrent_calls=48,
                                         max_concurrent=50, today_calls=200, prior7_calls=900, today_answer_rate=20, prior7_answer_rate=50,
                                         quality_bad_pct=30, outside_window_calls=3)
    s["services"]["interakt"]["kpis"].update(last24_sent=100, last24_failure_rate=9, wallet_inr=None)
    s["services"]["interakt"]["last_event"] = "2020-01-01T00:00:00Z"
    s["services"]["aws"]["kpis"].update(untagged_pct=40, runway_days=10, budget_usd=1, forecast_usd=5)
    s["unassigned_pct"]["sarvam"] = 50
    alerts = build_alerts(s, now)
    assert len(alerts) >= 14
    assert {a["runbook"] for a in alerts} <= ids
    assert alerts[0]["severity"] == "crit"
    for code, rb in __import__("rm.alerts", fromlist=["x"]).META_CODE_RUNBOOK.items():
        assert rb in ids


def test_calling_window_alert_detects_after_hours(conn, settings):
    seed(conn, RULES, RATES)
    t = datetime.now(IST).replace(hour=21, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    vobiz.store_calls(conn, [vobiz.parse_cdr_row({"uuid": "late", "start_time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "campaign_id": "SMFG-1",
                                                 "call_direction": "outbound", "billsec": 5}, RULES)])
    s = build_summary(conn, settings, RATES)
    assert s["services"]["vobiz"]["kpis"]["outside_window_calls"] >= 1
    assert any(a["runbook"] == "vobiz-calling-window" for a in s["alerts"])


# ---- server
def test_server_auth_and_webhook(settings, monkeypatch):
    from server import create_app

    c = TestClient(create_app(settings, demo=True))
    assert c.get("/api/summary").status_code == 200
    assert c.get("/api/runbooks").json()["runbooks"]
    assert c.post("/api/collect").status_code == 401
    assert c.post("/api/balance", json={"vendor": "sarvam", "key": "credits", "amount_inr": 5}).status_code == 401
    h = {"Authorization": "Bearer tok"}
    assert c.post("/api/balance", json={"vendor": "sarvam", "key": "credits", "amount_inr": 5}, headers=h).status_code == 200
    assert c.post("/api/balance", json={"vendor": "aws", "key": "x", "amount_inr": 5}, headers=h).status_code == 422
    assert c.post("/api/ingest/sarvam", json={"api": "bogus"}, headers=h).status_code == 422
    assert c.post("/api/ingest/sarvam", json={"api": "tts", "units": {"chars": 10}, "ref": "z"}, headers=h).status_code == 200
    body = json.dumps(_payload("sent", mid="w1", message_status="Sent")).encode()
    assert c.post("/webhooks/interakt", content=body, headers={"Interakt-Signature": "sha256=bad"}).status_code == 401
    sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    assert c.post("/webhooks/interakt", content=body, headers={"Interakt-Signature": sig}).status_code == 200
    assert "Resource Manager" in c.get("/").text


# ---- login + safe bind (Render exposure)
def test_dashboard_login_required_when_password_set(settings):
    import base64
    from server import create_app

    settings.dashboard_password = "pw-123"
    c = TestClient(create_app(settings, demo=True))
    for path in ("/", "/api/summary", "/api/runbooks"):
        r = c.get(path)
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    assert c.get("/api/summary", headers=bad).status_code == 401
    assert c.get("/api/summary", headers={"Authorization": "Basic !!!"}).status_code == 401
    good = {"Authorization": "Basic " + base64.b64encode(b"admin:pw-123").decode()}
    assert c.get("/api/summary", headers=good).status_code == 200
    assert c.get("/healthz").status_code == 200  # carries no data
    assert c.post("/api/collect").status_code == 401  # bearer-token routes unchanged


def test_server_refuses_public_bind_without_password(settings):
    from server import assert_safe_bind

    settings.host, settings.dashboard_password = "0.0.0.0", ""
    with pytest.raises(SystemExit):
        assert_safe_bind(settings)
    settings.dashboard_password = "x"
    assert_safe_bind(settings)
    settings.host, settings.dashboard_password = "127.0.0.1", ""
    assert_safe_bind(settings)


def test_port_env_for_render(monkeypatch, tmp_path):
    monkeypatch.delenv("RM_PORT", raising=False)
    monkeypatch.setenv("PORT", "10000")
    assert load_settings(tmp_path / "none.env").port == 10000


# ---- Vobiz postpaid: negative balance is money owed, not an empty wallet
def _vobiz_balances(conn, available, limit, postpaid=1.0):
    for k, v in (("available_balance", available), ("credit_limit", limit), ("is_postpaid", postpaid), ("low_balance_threshold", 50.0)):
        set_balance(conn, "vobiz", k, v, "INR", source="api")


def _vobiz_alerts(conn, settings):
    return [a for a in build_summary(conn, settings, RATES)["alerts"] if a["runbook"] == "vobiz-low-balance"]


def test_postpaid_negative_balance_within_limit_does_not_alert(conn, settings):
    _vobiz_balances(conn, -46277.6, 500000.0)
    k = build_summary(conn, settings, RATES)["services"]["vobiz"]["kpis"]
    assert k["is_postpaid"] and k["amount_owed"] == 46277.6 and k["credit_headroom"] == 453722.4 and k["credit_used_pct"] == 9.3
    assert _vobiz_alerts(conn, settings) == []


def test_postpaid_alerts_as_limit_is_used(conn, settings):
    _vobiz_balances(conn, -400000.0, 500000.0)
    assert [a["severity"] for a in _vobiz_alerts(conn, settings)] == ["warn"]
    set_balance(conn, "vobiz", "available_balance", -460000.0, "INR", source="api")
    a = _vobiz_alerts(conn, settings)
    assert [x["severity"] for x in a] == ["crit"] and "limit" in a[0]["detail"]


def test_prepaid_zero_balance_still_alerts(conn, settings):
    _vobiz_balances(conn, 10.0, 0.0, postpaid=0.0)
    assert [a["severity"] for a in _vobiz_alerts(conn, settings)] == ["crit"]
