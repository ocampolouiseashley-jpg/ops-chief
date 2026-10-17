"""The agent organization: a chief of staff that delegates to three specialists.

Each agent has its own system prompt and its own small set of tools. A specialist
never sees the chief's transcript: it gets a task, today's date and the memories
retrieved for that task. Every proposed action goes through policy.submit().
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Callable

from . import db, memory, policy
from .llm import Model, ModelUnavailable
from .tracing import Tracer

MAX_STEPS = 8


@dataclass
class Ctx:
    conn: sqlite3.Connection
    tracer: Tracer
    model: Model


def _tool(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": props, "required": required or list(props),
                         "additionalProperties": False},
    }


NULLABLE_STR = {"type": ["string", "null"]}

SEARCH_MEMORY = _tool("search_memory", "Search company memory (policies, client and vendor notes) by keywords.",
                      {"query": {"type": "string"}})
FLAG_ISSUE = _tool("flag_issue", "Record an operational issue for today's brief.", {
    "ref": {"type": "string", "description": "Task, invoice or project id"},
    "kind": {"type": "string", "enum": ["stalled", "missed_handoff", "overdue", "po_mismatch", "no_po", "client_waiting"]},
    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
    "detail": {"type": "string"},
})

TOOLS = {
    "chief": [
        SEARCH_MEMORY,
        _tool("read_inbox", "Read today's unanswered inbox messages.", {}),
        _tool("delegate", "Give a task to a specialist agent and get its report back.", {
            "agent": {"type": "string", "enum": ["projects", "finance", "followup"]},
            "task": {"type": "string"},
        }),
        _tool("save_memory", "Save a durable fact (not a policy) for future runs.", {
            "key": {"type": "string", "description": "e.g. vendor.ridgeline_mechanical"},
            "value": {"type": "string"},
        }),
    ],
    "projects": [
        SEARCH_MEMORY,
        _tool("list_tasks", "List tasks with days since last update and whether they are overdue.", {
            "project_id": NULLABLE_STR, "status": NULLABLE_STR}),
        FLAG_ISSUE,
        _tool("mark_task_status", "Propose a task status change.", {
            "task_id": {"type": "string"}, "status": {"type": "string"}}),
    ],
    "finance": [
        SEARCH_MEMORY,
        _tool("list_invoices", "List invoices by status.", {"status": {"type": "string"}}),
        _tool("get_purchase_order", "Look up a purchase order.", {"po_number": {"type": "string"}}),
        FLAG_ISSUE,
        _tool("propose_payment", "Propose paying an invoice. Policy decides whether it waits for approval or is blocked.", {
            "invoice_id": {"type": "string"}, "note": {"type": "string"}}),
    ],
    "followup": [
        SEARCH_MEMORY,
        _tool("list_issues", "List the issues flagged in this run.", {}),
        _tool("lookup_contact", "Find a contact's email and role.", {"name": {"type": "string"}}),
        _tool("draft_email", "Draft an email. It is never sent automatically; a person approves it first.", {
            "to_name": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
            "reason": {"type": "string"}}),
    ],
}

SYSTEMS = {
    "chief": (
        "You are the chief of staff for {company}, a project-based construction business. Today is {today}.\n"
        "Run the daily operations review: read the inbox, check the company's policies in memory, then delegate. "
        "Send stalled or blocked work to the projects agent, invoices to the finance agent, and once both have "
        "reported, ask the followup agent to draft the messages that move things forward. Save any durable new "
        "fact you learn. You never send anything or approve anything yourself. Finish with a short brief for the "
        "owner: what needs a decision today, what is waiting on whom, and what you handled."
    ),
    "projects": (
        "You are the projects agent for {company}. Today is {today}. Find stalled tasks, missed handoffs and "
        "overdue work. Flag each with a clear one-line detail and mark stalled tasks as stalled. Report briefly."
    ),
    "finance": (
        "You are the finance agent for {company}. Today is {today}. Check unpaid invoices against purchase "
        "orders. Propose payment only for invoices you believe are correct; flag mismatches, missing POs and "
        "overdue invoices. You cannot pay anything; a person approves. Report briefly."
    ),
    "followup": (
        "You are the follow-up agent for {company}. Today is {today}. For each issue flagged today, draft one "
        "short, polite email to the person who can unblock it, and reply to clients who are waiting. Drafts go "
        "to a person for approval. Report what you drafted."
    ),
}


# --------------------------------------------------------------------------- tool implementations

def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def run_tool(ctx: Ctx, agent: str, name: str, args: dict) -> str:
    conn, run_id = ctx.conn, ctx.tracer.run_id
    today = db.today(conn)

    if name == "search_memory":
        return json.dumps(memory.search(conn, args["query"]))
    if name == "save_memory":
        return memory.write(conn, args["key"], args["value"], source=f"agent:{agent}")
    if name == "read_inbox":
        return json.dumps(db.rows(conn, "SELECT * FROM inbox ORDER BY received DESC"))
    if name == "delegate":
        return delegate(ctx, args["agent"], args["task"])
    if name == "list_tasks":
        sql, params = "SELECT * FROM tasks WHERE 1=1", []
        if args.get("project_id"):
            sql += " AND project_id = ?"; params.append(args["project_id"])
        if args.get("status"):
            sql += " AND status = ?"; params.append(args["status"])
        out = []
        for t in db.rows(conn, sql, tuple(params)):
            t["days_since_update"] = _days(today, t["last_update"])
            t["overdue"] = t["status"] != "done" and t["due"] < today
            out.append(t)
        return json.dumps(out)
    if name == "flag_issue":
        conn.execute("INSERT INTO issues (run_id, agent, ref, kind, severity, detail) VALUES (?, ?, ?, ?, ?, ?)",
                     (run_id, agent, args["ref"], args["kind"], args["severity"], args["detail"]))
        conn.commit()
        return f"Flagged {args['kind']} on {args['ref']}."
    if name == "list_issues":
        return json.dumps(db.rows(conn, "SELECT ref, kind, severity, detail, agent FROM issues WHERE run_id = ?", (run_id,)))
    if name == "list_invoices":
        return json.dumps(db.rows(conn, "SELECT * FROM invoices WHERE status = ?", (args["status"],)))
    if name == "get_purchase_order":
        po = db.rows(conn, "SELECT * FROM purchase_orders WHERE po_number = ?", (args["po_number"],))
        return json.dumps(po[0] if po else {"error": "not found"})
    if name == "lookup_contact":
        c = db.rows(conn, "SELECT * FROM contacts WHERE name = ?", (args["name"],))
        return json.dumps(c[0] if c else {"error": f"No contact named {args['name']}"})

    # Everything below proposes an action: the policy gate decides, not the model.
    if name == "mark_task_status":
        kind, payload = "update_task_status", {"task_id": args["task_id"], "status": args["status"]}
    elif name == "propose_payment":
        kind, payload = "approve_payment", {"invoice_id": args["invoice_id"], "note": args["note"]}
    elif name == "draft_email":
        c = db.rows(conn, "SELECT email FROM contacts WHERE name = ?", (args["to_name"],))
        kind = "send_email"
        payload = {"to": c[0]["email"] if c else "", "to_name": args["to_name"], "subject": args["subject"],
                   "body": args["body"], "reason": args["reason"]}
    else:
        raise KeyError(f"Unknown tool {name}")
    with ctx.tracer.span("gate", kind, agent) as rec:
        result = policy.submit(conn, run_id, agent, kind, payload)
        rec["decision"] = result["decision"]
    return json.dumps(result)


# --------------------------------------------------------------------------- the loop

def run_agent(ctx: Ctx, agent: str, task: str) -> str:
    company = ctx.conn.execute("SELECT value FROM meta WHERE key='company'").fetchone()[0]
    system = SYSTEMS[agent].format(company=company, today=db.today(ctx.conn))
    messages: list = [{"role": "user", "content": task}]
    tools = TOOLS[agent]

    for step in range(MAX_STEPS):
        with ctx.tracer.span("llm", f"step {step + 1}", agent) as rec:
            reply = ctx.model.respond(agent, system, messages, tools)
            rec.update(model=reply.model, tokens=reply.tokens, stop=reply.stop_reason)
        messages.append({"role": "assistant", "content": reply.assistant_content})
        calls = [b for b in reply.blocks if b["type"] == "tool_use"]
        if reply.stop_reason != "tool_use" or not calls:
            return "\n".join(b["text"] for b in reply.blocks if b["type"] == "text").strip()

        results = []
        for call in calls:
            with ctx.tracer.span("tool", call["name"], agent, input=call["input"]) as rec:
                try:
                    content, is_error = run_tool(ctx, agent, call["name"], call["input"]), False
                except (KeyError, ValueError, ModelUnavailable) as exc:
                    content, is_error = f"Error: {exc}", True
                    rec["status"] = "error"
                    rec["error"] = content
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})  # all results in one message

    # Ran out of steps: never loop forever; leave a visible trail for a person.
    ctx.conn.execute("INSERT INTO issues (run_id, agent, ref, kind, severity, detail) VALUES (?, ?, ?, ?, ?, ?)",
                     (ctx.tracer.run_id, agent, agent, "stalled", "high", f"{agent} hit the {MAX_STEPS}-step limit"))
    ctx.conn.commit()
    return f"{agent} stopped at the step limit; escalated to a person."


def delegate(ctx: Ctx, agent: str, task: str) -> str:
    notes = memory.search(ctx.conn, task + " " + agent)
    context = "\n".join(f"- {m['value']}" for m in notes) or "- (none)"
    brief = f"{task}\n\nRelevant company memory:\n{context}"
    with ctx.tracer.span("delegate", agent, "chief"):
        report = run_agent(ctx, agent, brief)
    actions = db.rows(ctx.conn, "SELECT id, kind, decision, status, reason FROM actions WHERE run_id = ? AND agent = ?",
                      (ctx.tracer.run_id, agent))
    issues = db.rows(ctx.conn, "SELECT ref, kind, severity, detail FROM issues WHERE run_id = ? AND agent = ?",
                     (ctx.tracer.run_id, agent))
    return json.dumps({"agent": agent, "report": report, "issues": issues, "actions": actions})


def daily_review(ctx: Ctx) -> str:
    with ctx.tracer.span("run", "daily review", "chief"):
        return run_agent(ctx, "chief", "Run today's operations review and write the owner's brief.")
