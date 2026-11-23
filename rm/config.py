"""Settings from the environment (.env). Secrets are read here and nowhere else."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("rm")


def mask(value: str | None, keep: int = 4) -> str:
    """Mask an identifier so only the last few characters show. Used for phone numbers and keys."""
    if not value:
        return ""
    value = str(value)
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def _f(name: str, default: float | None = None) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s is not a number; using default", name)
        return default


@dataclass
class Settings:
    db_path: Path
    ingest_token: str
    usd_inr: float
    host: str
    port: int
    aws_account_id: str
    aws_region: str
    aws_profile: str
    aws_tag_client: str
    aws_tag_product: str
    aws_tag_env: str
    aws_budget_usd: float | None
    vobiz_auth_id: str
    vobiz_auth_token: str
    vobiz_base_url: str
    vobiz_currency: str
    interakt_secret: str
    sarvam_plan: str
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    collect_interval_min: float = 0.0

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")

    @property
    def aws_enabled(self) -> bool:
        return bool(self.aws_account_id)

    @property
    def vobiz_enabled(self) -> bool:
        return bool(self.vobiz_auth_id and self.vobiz_auth_token)

    @property
    def interakt_enabled(self) -> bool:
        return bool(self.interakt_secret)

    def public_status(self) -> dict:
        """What is configured, with no secret values."""
        return {
            "aws": self.aws_enabled,
            "vobiz": self.vobiz_enabled,
            "interakt": self.interakt_enabled,
            "sarvam": bool(self.ingest_token),
        }


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env")
    logging.basicConfig(
        level=os.environ.get("RM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db = Path(os.environ.get("RM_DB_PATH", "data/rm.sqlite"))
    if not db.is_absolute():
        db = ROOT / db
    return Settings(
        db_path=db,
        ingest_token=os.environ.get("RM_INGEST_TOKEN", "").strip(),
        usd_inr=_f("USD_INR", 88.0) or 88.0,
        host=os.environ.get("RM_HOST", "127.0.0.1"),
        # Render (and most PaaS) inject PORT; RM_PORT wins if both are set.
        port=int(_f("RM_PORT", None) or _f("PORT", 8080) or 8080),
        dashboard_user=os.environ.get("RM_DASHBOARD_USER", "admin").strip() or "admin",
        dashboard_password=os.environ.get("RM_DASHBOARD_PASSWORD", "").strip(),
        collect_interval_min=_f("RM_COLLECT_INTERVAL_MIN", 0.0) or 0.0,
        aws_account_id=os.environ.get("AWS_ACCOUNT_ID", "").strip(),
        aws_region=os.environ.get("AWS_REGION", "ap-south-1").strip(),
        aws_profile=os.environ.get("AWS_PROFILE", "").strip(),
        aws_tag_client=os.environ.get("AWS_TAG_CLIENT", "client").strip(),
        aws_tag_product=os.environ.get("AWS_TAG_PRODUCT", "product").strip(),
        aws_tag_env=os.environ.get("AWS_TAG_ENV", "env").strip(),
        aws_budget_usd=_f("AWS_MONTHLY_BUDGET_USD"),
        vobiz_auth_id=os.environ.get("VOBIZ_AUTH_ID", "").strip(),
        vobiz_auth_token=os.environ.get("VOBIZ_AUTH_TOKEN", "").strip(),
        vobiz_base_url=os.environ.get("VOBIZ_BASE_URL", "https://api.vobiz.ai").rstrip("/"),
        vobiz_currency=os.environ.get("VOBIZ_CURRENCY", "INR").strip() or "INR",
        interakt_secret=os.environ.get("INTERAKT_WEBHOOK_SECRET", "").strip(),
        sarvam_plan=(os.environ.get("SARVAM_PLAN", "payg").strip().lower() or "payg"),
    )


def load_json(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))
