"""Deterministic policies for the offline demo and the tests.

They do what a well-behaved model would do with these tools, turn by turn. One
deliberate exception: the finance policy also tries to pay a mismatched invoice
(the vendor's email makes it sound fine), so the demo shows the policy gate
blocking an action the model wanted to take.
"""

from __future__ import annotations

import json

from .llm import ScriptedModel


def _turn(messages: list) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def _last(results: list, n: int = 1) -> list:
    return [json.loads(r["content"]) if r["content"][:1] in "[{" else r["content"] for r in results[-n:]]


def call(tool: str, /, **args) -> dict:
    return {"type": "tool_use", "name": tool, "input": args}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def chief(messages, results):
    t = _turn(messages)
    if t == 0:
        return [text("Starting the daily review with the inbox."), call("read_inbox")]
    if t == 1:
        return [call("search_memory", query="policy payment approval outbound email stalled")]
    if t == 2:
        return [call("delegate", agent="projects",
                     task="Find stalled or blocked work and missed handoffs across all projects. Flag each one and mark stalled tasks.")]
    if t == 3:
        return [call("delegate", agent="finance",
                     task="Check every unpaid invoice against its purchase order. Propose payment only where it matches; flag mismatches, missing POs and overdue invoices.")]
    if t == 4:
        return [call("delegate", agent="followup",
                     task="Draft follow-ups for every issue flagged today, and reply to the Kettle & Crumb owner who is waiting on the walkthrough.")]
    if t == 5:
        return [call("save_memory", key="vendor.ridgeline_mechanical",
                     value="2026-10-15: Ridgeline Mechanical said HVAC shop drawings are late because their engineer is out.")]
    reports = [json.loads(r["content"]) for r in results if r["content"].startswith('{"agent"')]
    pending = [a for r in reports for a in r["actions"] if a["status"] == "pending"]
    blocked = [a for r in reports for a in r["actions"] if a["status"] == "escalated"]
    issues = [i for r in reports for i in r["issues"]]
    high = [i for i in issues if i["severity"] == "high"]
    lines = ["Owner brief", "",
             f"Needs your decision today: {len(pending)} item(s) in the approval queue "
             f"({sum(a['kind'] == 'send_email' for a in pending)} email drafts, "
             f"{sum(a['kind'] == 'approve_payment' for a in pending)} payments).",
             f"Held by policy: {len(blocked)} action(s). " + (blocked[0]["reason"] if blocked else ""),
             f"Issues found: {len(issues)}, {len(high)} high priority:"]
    lines += [f"  - {i['ref']}: {i['detail']}" for i in high]
    lines += ["", "Handled: stalled tasks marked, follow-ups drafted, and a note saved about Ridgeline Mechanical's delay."]
    return [text("\n".join(lines))]


def projects(messages, results):
    t = _turn(messages)
    if t == 0:
        return [call("list_tasks", project_id=None, status=None)]
    if t == 1:
        tasks = _last(results)[0]
        by_id = {x["id"]: x for x in tasks}
        out = []
        for x in tasks:
            if x["status"] == "done":
                continue
            dep = by_id.get(x["depends_on"]) if x["depends_on"] else None
            if dep and dep["status"] != "done" and x["status"] == "blocked":
                out.append(call("flag_issue", ref=x["id"], kind="missed_handoff", severity="high",
                                detail=f"'{x['title']}' is blocked waiting on {dep['id']} ({dep['owner']}), which is {dep['days_since_update']} days without an update."))
            elif x["overdue"] or x["days_since_update"] >= 7:
                sev = "high" if x["overdue"] else "medium"
                why = "overdue" if x["overdue"] else f"no update for {x['days_since_update']} days"
                out.append(call("flag_issue", ref=x["id"], kind="stalled", severity=sev,
                                detail=f"'{x['title']}' ({x['owner']}) is {why}."))
                out.append(call("mark_task_status", task_id=x["id"], status="stalled"))
        return out
    return [text("Projects review done: stalled and blocked work flagged; stalled tasks marked.")]


def finance(messages, results):
    t = _turn(messages)
    if t == 0:
        return [call("list_invoices", status="unpaid")]
    invoices = next(json.loads(r["content"]) for r in results if r["content"].startswith("[{\"id\": \"INV"))
    if t == 1:
        return [call("get_purchase_order", po_number=i["po_number"]) for i in invoices if i["po_number"]] or [text("No POs to check.")]
    if t == 2:
        pos = {p["po_number"]: p for p in (json.loads(r["content"]) for r in results) if isinstance(p, dict) and "po_number" in p}
        out = []
        for i in invoices:
            if not i["po_number"]:
                out.append(call("flag_issue", ref=i["id"], kind="no_po", severity="medium",
                                detail=f"{i['vendor']} invoice for {i['amount']:.2f} has no purchase order."))
                continue
            po = pos[i["po_number"]]
            if abs(i["amount"] - po["amount"]) > po["amount"] * 0.02:
                out.append(call("flag_issue", ref=i["id"], kind="po_mismatch", severity="high",
                                detail=f"{i['vendor']} billed {i['amount']:.2f} against PO {po['amount']:.2f}; extras need a change order."))
                # The vendor's email explains the extra; a model could be tempted. The gate decides.
                out.append(call("propose_payment", invoice_id=i["id"], note="Vendor says the difference is an added pump truck day."))
                continue
            if i["due"] < "2026-10-16":
                out.append(call("flag_issue", ref=i["id"], kind="overdue", severity="medium",
                                detail=f"{i['vendor']} invoice was due {i['due']}."))
            out.append(call("propose_payment", invoice_id=i["id"], note="Matches its purchase order."))
        return out
    return [text("Finance review done: matching invoices proposed for approval; mismatches and missing POs flagged.")]


def followup(messages, results):
    t = _turn(messages)
    if t == 0:
        return [call("list_issues")]
    issues = json.loads(results[0]["content"])
    if t == 1:
        names = ["Ridgeline Mechanical", "Ridgeline Concrete", "Jordan Lee", "Maya Torres"]
        return [call("lookup_contact", name=n) for n in names]
    if t == 2:
        refs = {i["ref"] for i in issues}
        out = []
        if "T-202" in refs or "T-201" in refs:
            out.append(call("draft_email", to_name="Ridgeline Mechanical", subject="HVAC shop drawings: new date?",
                            body="Hi, thanks for letting us know about the delay. Could you send a firm date for the shop drawings? Approval and the rooftop unit order are waiting on them.",
                            reason="T-201 is overdue and blocks T-202 and T-203."))
        if "INV-9002" in refs:
            out.append(call("draft_email", to_name="Ridgeline Concrete", subject="INV-9002 and the pump truck day",
                            body="Hi, INV-9002 is above PO-5501. Could you send the approved change order for the added pump truck day? We'll process the invoice once it matches.",
                            reason="Invoice does not match its PO."))
        if "T-206" in refs:
            out.append(call("draft_email", to_name="Jordan Lee", subject="Lien waivers for Harbor Street café",
                            body="Hi Jordan, the lien waivers for the café closeout haven't started and are due 10/23. Can you start collecting them this week?",
                            reason="T-206 has had no update for 16 days."))
        if "T-205" in refs:
            out.append(call("draft_email", to_name="Maya Torres", subject="Kettle & Crumb is asking about the walkthrough",
                            body="Hi Maya, the Kettle & Crumb owner asked whether the punch walkthrough is still on this week. They prefer a short email update. Can you confirm a time?",
                            reason="Client is waiting; T-205 is due tomorrow with no update for 13 days."))
        return out
    return [text("Drafted follow-ups for the open issues; all are waiting for approval.")]


def model() -> ScriptedModel:
    return ScriptedModel({"chief": chief, "projects": projects, "finance": finance, "followup": followup})
