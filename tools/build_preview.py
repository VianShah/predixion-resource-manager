"""Build a single-file demo preview (synthetic data) at preview.html. Not for real data.

    python tools/build_preview.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rm.config import load_json, load_settings  # noqa: E402
from rm.db import connect  # noqa: E402
from rm.demo import seed  # noqa: E402
from rm.summary import build_summary  # noqa: E402

s = load_settings()
conn = connect(":memory:")
rules, rates = load_json("purposes.json"), load_json("rates.json")
seed(conn, rules, rates)
boot = {"summary": build_summary(conn, s, rates, demo=True), "runbooks": load_json("runbooks.json")}
html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
payload = json.dumps(boot).replace("</", "<\\/")
out = html.replace("<script>\n", f"<script>\nwindow.__BOOT__={payload};\n", 1)
(ROOT / "preview.html").write_text(out, encoding="utf-8")
print("wrote preview.html", len(out) // 1024, "KB")
