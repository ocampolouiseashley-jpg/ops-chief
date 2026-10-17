"""Organizational memory: persistent facts and rules the agents retrieve by relevance.

Retrieval is deliberately simple (keyword overlap with a small boost for policy
entries) so it is easy to inspect and test. Each agent gets only the memories
relevant to its task instead of the whole store: that is the context
engineering part.
"""

from __future__ import annotations

import re
import sqlite3

from . import db

WORD = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "is", "are", "with", "by", "be", "it", "its"}


def _terms(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 2}


def search(conn: sqlite3.Connection, query: str, limit: int = 4) -> list[dict]:
    q = _terms(query)
    scored = []
    for m in db.rows(conn, "SELECT key, value, source FROM memory"):
        terms = _terms(m["key"].replace(".", " ").replace("_", " ") + " " + m["value"])
        score = len(q & terms) + (0.5 if m["key"].startswith("policy.") and q & terms else 0)
        if score:
            scored.append((score, m))
    scored.sort(key=lambda s: (-s[0], s[1]["key"]))
    return [m for _, m in scored[:limit]]


def write(conn: sqlite3.Connection, key: str, value: str, source: str) -> str:
    if key.startswith("policy."):
        # Agents may learn facts, but only people change policy.
        return f"Refused: '{key}' is a policy. Policies are changed by a person, not an agent."
    conn.execute(
        "INSERT INTO memory VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "source=excluded.source, updated=excluded.updated",
        (key, value, source, db.today(conn)),
    )
    conn.commit()
    return f"Saved memory '{key}'."
