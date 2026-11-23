"""Dashboard server.  Run:  python server.py   (binds 127.0.0.1 unless RM_HOST is set)

Login: set RM_DASHBOARD_PASSWORD (HTTP Basic). Mandatory when RM_HOST is not loopback (e.g. Render);
the server refuses to start otherwise. Without a password the dashboard is open, so keep it on 127.0.0.1.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from rm.balance import ALLOWED
from rm.collectors import aws, interakt, sarvam, vobiz
from rm.config import ROOT, load_json, load_settings
from rm.db import connect, set_balance
from rm.summary import build_summary

log = logging.getLogger("rm.server")
IST = timezone(timedelta(hours=5, minutes=30))


def create_app(settings=None, demo: bool | None = None) -> FastAPI:
    s = settings or load_settings()
    demo = bool(os.environ.get("RM_DEMO")) if demo is None else demo
    app = FastAPI(title="Resource Manager", docs_url=None, redoc_url=None)
    conn = connect(":memory:" if demo else s.db_path)
    lock = threading.Lock()
    rules, rates = load_json("purposes.json"), load_json("rates.json")
    if demo:
        from rm.demo import seed
        seed(conn, rules, rates)

    def auth(authorization: str = Header(default="")) -> None:
        token = authorization.removeprefix("Bearer ").strip()
        if not s.ingest_token or not hmac.compare_digest(token, s.ingest_token):
            raise HTTPException(401, "missing or wrong bearer token (set RM_INGEST_TOKEN)")

    def view_auth(authorization: str = Header(default="")) -> None:
        """HTTP Basic on the read routes. Off only when no password is set (loopback use)."""
        if not s.dashboard_password:
            return
        ok = False
        if authorization.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(authorization[6:]).decode("utf-8").partition(":")
                ok = hmac.compare_digest(user, s.dashboard_user) & hmac.compare_digest(pw, s.dashboard_password)
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            raise HTTPException(401, "login required", headers={"WWW-Authenticate": 'Basic realm="Resource Manager"'})

    @app.get("/api/summary", dependencies=[Depends(view_auth)])
    def summary():
        with lock:
            return build_summary(conn, s, rates, demo=demo)

    @app.get("/api/runbooks", dependencies=[Depends(view_auth)])
    def runbooks():
        return load_json("runbooks.json")

    @app.post("/api/collect", dependencies=[Depends(auth)])
    def collect_now():
        with lock:
            aws.collect(conn, s)
            vobiz.collect(conn, s, rules)
        return {"ok": True}

    @app.post("/api/ingest/sarvam", dependencies=[Depends(auth)])
    async def ingest_sarvam(request: Request):
        event = await request.json()
        try:
            with lock:
                rid = sarvam.record(conn, rules, rates, s.sarvam_plan, event)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc))
        return {"id": rid}

    @app.post("/api/balance", dependencies=[Depends(auth)])
    async def balance(request: Request):
        b = await request.json()
        key = (b.get("vendor"), b.get("key"))
        if key not in ALLOWED:
            raise HTTPException(422, "vendor/key must be sarvam/credits or interakt/wallet")
        try:
            amount = float(b["amount_inr"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "amount_inr must be a number")
        with lock:
            set_balance(conn, key[0], key[1], amount, "INR", source="manual")
        return {"ok": True}

    @app.post("/webhooks/interakt")
    async def interakt_webhook(request: Request):
        body = await request.body()
        if s.interakt_secret and not interakt.verify_signature(s.interakt_secret, body, request.headers.get("Interakt-Signature")):
            raise HTTPException(401, "bad signature")
        if not s.interakt_secret:
            raise HTTPException(503, "INTERAKT_WEBHOOK_SECRET not set")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "not JSON")
        with lock:
            interakt.ingest(conn, rules, rates, payload)
        return {"ok": True}

    @app.get("/", dependencies=[Depends(view_auth)])
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}  # no data; for Render's health check

    def collect_loop() -> None:
        interval = s.collect_interval_min * 60
        while not stop.wait(interval):
            try:
                with lock:
                    if s.aws_enabled:
                        aws.collect(conn, s)
                    if s.vobiz_enabled:
                        vobiz.collect(conn, s, rules)
                log.info("scheduled collect finished")
            except Exception:  # keep the loop alive; never log secrets
                log.exception("scheduled collect failed")

    stop = threading.Event()

    @app.on_event("startup")
    def _start():
        if s.collect_interval_min > 0 and not demo and (s.aws_enabled or s.vobiz_enabled):
            threading.Thread(target=collect_loop, name="rm-collect", daemon=True).start()
            log.info("scheduled collection every %s min", s.collect_interval_min)

    @app.on_event("shutdown")
    def _stop():
        stop.set()

    return app


def assert_safe_bind(cfg) -> None:
    """Refuse to expose the dashboard beyond loopback without a login (project rule)."""
    if not cfg.is_loopback and not cfg.dashboard_password:
        raise SystemExit("Refusing to start: RM_HOST is not loopback and RM_DASHBOARD_PASSWORD is empty.")


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app


if __name__ == "__main__":
    import uvicorn

    cfg = load_settings()
    assert_safe_bind(cfg)
    uvicorn.run(get_app(), host=cfg.host, port=cfg.port)
