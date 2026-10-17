"""Approval gates. Agents only propose actions; this code decides what happens to them.

Three outcomes:
  auto     - low-risk, done immediately (e.g. noting a task as stalled)
  approval - waits in the approval queue for a person
  blocked  - not allowed as proposed; escalated with the reason

The rules live in plain code so they can be tested, and so a confused or
manipulated model can never talk its way past them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import db

OWNER_THRESHOLD = 5000.00
PO_TOLERANCE = 0.02
AUTO_TASK_STATUSES = {"stalled", "at_risk"}


@dataclass
class Decision:
    outcome: str  # "auto" | "approval" | "blocked"
    reason: str


def decide(conn: sqlite3.Connection, kind: str, payload: dict) -> Decision:
    if kind == "send_email":
        if not payload.get("to") or not payload.get("body"):
            return Decision("blocked", "An email needs a recipient and a body.")
        return Decision("approval", "Outbound email is always approved by a person before it goes out.")

    if kind == "approve_payment":
        inv = db.rows(conn, "SELECT * FROM invoices WHERE id = ?", (payload.get("invoice_id", ""),))
        if not inv:
            return Decision("blocked", "Unknown invoice.")
        inv = inv[0]
        if not inv["po_number"]:
            return Decision("blocked", "No purchase order on file; hold and ask the project manager.")
        po = db.rows(conn, "SELECT * FROM purchase_orders WHERE po_number = ?", (inv["po_number"],))
        if not po:
            return Decision("blocked", f"Purchase order {inv['po_number']} not found.")
        if abs(inv["amount"] - po[0]["amount"]) > po[0]["amount"] * PO_TOLERANCE:
            return Decision("blocked", f"Invoice {inv['amount']:.2f} does not match PO {po[0]['amount']:.2f} within 2%.")
        if inv["amount"] > OWNER_THRESHOLD:
            return Decision("approval", "Over 5,000 USD: needs the owner's approval.")
        return Decision("approval", "Every payment is approved by a person.")

    if kind == "update_task_status":
        if payload.get("status") in AUTO_TASK_STATUSES:
            return Decision("auto", "Marking a task stalled or at risk is informational.")
        return Decision("approval", "Other status changes are confirmed by the task owner.")

    return Decision("blocked", f"Unknown action type '{kind}'.")


def submit(conn: sqlite3.Connection, run_id: str, agent: str, kind: str, payload: dict) -> dict:
    d = decide(conn, kind, payload)
    status = {"auto": "done", "approval": "pending", "blocked": "escalated"}[d.outcome]
    if d.outcome == "auto" and kind == "update_task_status":
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (payload["status"], payload.get("task_id")))
    cur = conn.execute(
        "INSERT INTO actions (run_id, agent, kind, payload, decision, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, agent, kind, json.dumps(payload), d.outcome, d.reason, status),
    )
    conn.commit()
    return {"action_id": cur.lastrowid, "decision": d.outcome, "status": status, "reason": d.reason}


def resolve(conn: sqlite3.Connection, action_id: int, approve: bool, person: str) -> str:
    row = db.rows(conn, "SELECT * FROM actions WHERE id = ?", (action_id,))
    if not row:
        return "No such action."
    if row[0]["status"] != "pending":
        return f"Action {action_id} is {row[0]['status']}, not pending."
    status = "approved" if approve else "rejected"
    conn.execute("UPDATE actions SET status = ?, decided_by = ? WHERE id = ?", (status, person, action_id))
    conn.commit()
    note = " Nothing is sent from this demo; in production this is where the send would happen." if approve else ""
    return f"Action {action_id} {status} by {person}.{note}"
