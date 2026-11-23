"""SQLite storage. One file, created on first run."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS aws_cost (
  dim TEXT NOT NULL, day TEXT NOT NULL, k1 TEXT NOT NULL DEFAULT '', k2 TEXT NOT NULL DEFAULT '',
  usd REAL NOT NULL, PRIMARY KEY (dim, day, k1, k2));
CREATE TABLE IF NOT EXISTS aws_credits (
  credit_id TEXT PRIMARY KEY, name TEXT, type TEXT, status TEXT, currency TEXT,
  initial REAL, remaining REAL, estimated REAL, start_ts INTEGER, end_ts INTEGER, exhaust_ts INTEGER,
  products TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS calls (
  uuid TEXT PRIMARY KEY, start_ts TEXT, day TEXT, direction TEXT, from_number TEXT, to_number TEXT,
  duration INTEGER, billsec INTEGER, cost_inr REAL, hangup_cause TEXT, hangup_source TEXT,
  campaign_id TEXT, mos REAL, jitter REAL, packet_loss REAL, client TEXT, product TEXT, purpose TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, first_ts TEXT, day TEXT, template TEXT, category TEXT, status TEXT,
  error_code TEXT, error_reason TEXT, campaign_id TEXT, phone TEXT,
  sent_at TEXT, delivered_at TEXT, read_at TEXT, client TEXT, product TEXT, purpose TEXT);
CREATE TABLE IF NOT EXISTS sarvam_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, ts TEXT, day TEXT, workspace TEXT, api TEXT,
  model TEXT, units TEXT, cost_inr REAL, http_status INTEGER, error_code TEXT,
  client TEXT, product TEXT, purpose TEXT);
CREATE TABLE IF NOT EXISTS balances (
  vendor TEXT NOT NULL, key TEXT NOT NULL, value REAL, text TEXT, unit TEXT, source TEXT, ts TEXT NOT NULL,
  PRIMARY KEY (vendor, key));
CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, vendor TEXT, ts TEXT, ok INTEGER, rows INTEGER, message TEXT);
CREATE INDEX IF NOT EXISTS idx_calls_day ON calls(day);
CREATE INDEX IF NOT EXISTS idx_msgs_day ON messages(day);
CREATE INDEX IF NOT EXISTS idx_sarvam_day ON sarvam_usage(day);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def set_balance(conn, vendor: str, key: str, value: float | None, unit: str = "", source: str = "api", text: str = "") -> None:
    conn.execute(
        "INSERT INTO balances(vendor,key,value,text,unit,source,ts) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(vendor,key) DO UPDATE SET value=excluded.value,text=excluded.text,unit=excluded.unit,"
        "source=excluded.source,ts=excluded.ts",
        (vendor, key, value, text, unit, source, utcnow()),
    )
    conn.commit()


def get_balances(conn, vendor: str) -> dict:
    rows = conn.execute("SELECT * FROM balances WHERE vendor=?", (vendor,)).fetchall()
    return {r["key"]: dict(r) for r in rows}


def log_sync(conn, vendor: str, ok: bool, rows: int, message: str = "") -> None:
    conn.execute(
        "INSERT INTO sync_log(vendor,ts,ok,rows,message) VALUES(?,?,?,?,?)",
        (vendor, utcnow(), 1 if ok else 0, rows, message[:500]),
    )
    conn.commit()


def last_sync(conn, vendor: str) -> dict | None:
    r = conn.execute("SELECT * FROM sync_log WHERE vendor=? ORDER BY id DESC LIMIT 1", (vendor,)).fetchone()
    return dict(r) if r else None
