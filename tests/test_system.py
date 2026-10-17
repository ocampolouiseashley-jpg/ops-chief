"""End to end with the scripted model: delegation, context isolation, gates, tracing, evals."""

import json

from ops_chief import db, evals, scripted, tracing
from ops_chief.agents import MAX_STEPS, Ctx, daily_review, run_agent
from ops_chief.llm import Reply


def run(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.seed(conn)
    tracer = tracing.Tracer(tmp_path / "trace.jsonl")
    brief = daily_review(Ctx(conn, tracer, scripted.model()))
    return conn, tracer, brief


def test_eval_suite_passes():
    assert evals.run_all()


def test_brief_names_the_decisions(tmp_path):
    _, _, brief = run(tmp_path)
    assert "approval queue" in brief and "INV-9002" in brief


def test_trace_has_delegations_and_gate_decisions(tmp_path):
    _, tracer, _ = run(tmp_path)
    spans = tracing.load(tracer.path)
    assert {s["name"] for s in spans if s["kind"] == "delegate"} == {"projects", "finance", "followup"}
    assert {s.get("decision") for s in spans if s["kind"] == "gate"} == {"auto", "approval", "blocked"}
    assert "daily review" in tracing.render(spans)


def test_specialists_get_their_task_and_memory_not_the_chief_transcript(tmp_path):
    seen = {}
    base = scripted.model()

    class Spy:
        def respond(self, agent, system, messages, tools):
            if agent != "chief" and agent not in seen:
                seen[agent] = messages[0]["content"]
            return base.respond(agent, system, messages, tools)

    conn = db.connect(tmp_path / "t.db")
    db.seed(conn)
    daily_review(Ctx(conn, tracing.Tracer(tmp_path / "trace.jsonl"), Spy()))
    assert "Relevant company memory" in seen["finance"]
    assert "purchase order" in seen["finance"].lower()
    assert "Shop drawings" not in seen["finance"]  # the chief's inbox read never leaks into finance's context


def test_agent_stops_at_step_limit_and_escalates(tmp_path):
    class Looping:
        def respond(self, agent, system, messages, tools):
            block = {"type": "tool_use", "id": f"t{len(messages)}", "name": "list_tasks",
                     "input": {"project_id": None, "status": None}}
            return Reply("tool_use", [block], [block], "loop")

    conn = db.connect(tmp_path / "t.db")
    db.seed(conn)
    out = run_agent(Ctx(conn, tracing.Tracer(tmp_path / "trace.jsonl"), Looping()), "projects", "go")
    assert "step limit" in out
    assert db.rows(conn, "SELECT detail FROM issues")[0]["detail"].endswith(f"{MAX_STEPS}-step limit")


def test_tool_errors_go_back_to_the_model_not_crash(tmp_path):
    calls = []

    class BadTool:
        def respond(self, agent, system, messages, tools):
            if len(messages) == 1:
                b = {"type": "tool_use", "id": "x1", "name": "no_such_tool", "input": {}}
                return Reply("tool_use", [b], [b], "bad")
            calls.append(messages[-1]["content"][0])
            return Reply("end_turn", [{"type": "text", "text": "ok"}], [], "bad")

    conn = db.connect(tmp_path / "t.db")
    db.seed(conn)
    assert run_agent(Ctx(conn, tracing.Tracer(tmp_path / "trace.jsonl"), BadTool()), "projects", "go") == "ok"
    assert calls[0]["is_error"] is True


def test_no_agent_action_is_executed_without_gate(tmp_path):
    conn, _, _ = run(tmp_path)
    for a in db.rows(conn, "SELECT * FROM actions"):
        assert a["decision"] in ("auto", "approval", "blocked")
        if a["kind"] in ("send_email", "approve_payment"):
            assert a["status"] in ("pending", "escalated")
        json.loads(a["payload"])
