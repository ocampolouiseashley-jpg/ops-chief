"""SQLite store: the business data the agents work on, plus their memory, actions and issues."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SEED = Path(__file__).resolve().parent.parent / "data" / "seed.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT, client TEXT, phase TEXT, pm TEXT, due TEXT);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, owner TEXT, status TEXT,
                                  due TEXT, last_update TEXT, depends_on TEXT);
CREATE TABLE IF NOT EXISTS purchase_orders (po_number TEXT PRIMARY KEY, vendor TEXT, project_id TEXT, amount REAL);
CREATE TABLE IF NOT EXISTS invoices (id TEXT PRIMARY KEY, vendor TEXT, project_id TEXT, po_number TEXT, amount REAL,
                                     due TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS contacts (name TEXT PRIMARY KEY, role TEXT, email TEXT, company TEXT);
CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, sender TEXT, subject TEXT, body TEXT, received TEXT);
CREATE TABLE IF NOT EXISTS memory (key TEXT PRIMARY KEY, value TEXT, source TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS issues (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, agent TEXT, ref TEXT, kind TEXT,
                                   severity TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, agent TEXT, kind TEXT,
                                    payload TEXT, decision TEXT, reason TEXT, status TEXT, decided_by TEXT);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def seed(conn: sqlite3.Connection, seed_path: Path = SEED) -> None:
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    for table in ("projects", "tasks", "purchase_orders", "invoices", "contacts", "inbox", "memory",
                  "issues", "actions", "meta"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.execute("INSERT INTO meta VALUES ('today', ?)", (data["today"],))
    conn.execute("INSERT INTO meta VALUES ('company', ?)", (data["company"],))
    for table in ("projects", "tasks", "purchase_orders", "invoices", "contacts", "inbox"):
        for row in data[table]:
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
    for m in data["memory"]:
        conn.execute("INSERT INTO memory VALUES (?, ?, ?, ?)", (m["key"], m["value"], m["source"], data["today"]))
    conn.commit()


def today(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT value FROM meta WHERE key = 'today'").fetchone()[0]


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
