"""Work out what a unit of usage is for: client, product and purpose.

Rules live in purposes.json so a non-engineer can edit them. A rule matches when every key in "match" matches.
A key ending in "_prefix" matches the start of that field; any other key must be equal.
The first matching rule wins. Nothing matched means "Unassigned", and the dashboard shows how much is unassigned.
"""
from __future__ import annotations

UNASSIGNED = {"client": "Unassigned", "product": "Unassigned", "purpose": "Unassigned"}


def _match(match: dict, fields: dict) -> bool:
    for key, want in match.items():
        if key.endswith("_prefix"):
            have = str(fields.get(key[: -len("_prefix")]) or "")
            if not have.startswith(str(want)):
                return False
        else:
            if str(fields.get(key) or "") != str(want):
                return False
    return True


def resolve(rules: dict, vendor: str, fields: dict) -> dict:
    default = rules.get("default", UNASSIGNED)
    for rule in rules.get("rules", {}).get(vendor, []):
        if _match(rule.get("match", {}), fields):
            return {k: rule.get(k, default[k]) for k in ("client", "product", "purpose")}
    return dict(default)
