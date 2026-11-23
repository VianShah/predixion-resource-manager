"""Optional helper for your voice-agent code: report each Sarvam call to this dashboard.

    from rm.sarvam_logger import log_usage
    log_usage(api="tts", model="bulbul:v3", units={"chars": 420}, workspace="kollect-prod")

Fire and forget: it never raises and never slows the call (short timeout, background thread).
Needs RM_DASHBOARD_URL and RM_INGEST_TOKEN in the agent's environment.
"""
from __future__ import annotations

import logging
import os
import threading

import requests

log = logging.getLogger("rm.sarvam_logger")


def _post(url: str, token: str, event: dict) -> None:
    try:
        requests.post(url, json=event, headers={"Authorization": f"Bearer {token}"}, timeout=2)
    except Exception as exc:
        log.debug("usage log not sent: %s", type(exc).__name__)


def log_usage(api: str, units: dict | None = None, model: str = "", workspace: str = "", http_status: int = 200,
              error_code: str | None = None, ref: str | None = None) -> None:
    base, token = os.environ.get("RM_DASHBOARD_URL", ""), os.environ.get("RM_INGEST_TOKEN", "")
    if not base or not token:
        return
    event = {"api": api, "model": model, "units": units or {}, "workspace": workspace,
             "http_status": http_status, "error_code": error_code, "ref": ref}
    threading.Thread(target=_post, args=(base.rstrip("/") + "/api/ingest/sarvam", token, event), daemon=True).start()
