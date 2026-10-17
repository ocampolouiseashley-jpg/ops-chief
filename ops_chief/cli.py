"""ops-chief command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db, policy, scripted, tracing
from .agents import Ctx, daily_review
from .llm import CHIEF_MODEL, SPECIALIST_MODEL, AnthropicModel

HOME = Path.cwd() / ".ops-chief"
DB_PATH = HOME / "ops.db"
TRACE_PATH = HOME / "trace.jsonl"


def cmd_run(args) -> int:
    conn = db.connect(DB_PATH)
    if args.fresh or not db.rows(conn, "SELECT 1 FROM meta"):
        db.seed(conn)
    if args.offline:
        model = scripted.model()
    else:
        model = AnthropicModel({"chief": CHIEF_MODEL, "projects": SPECIALIST_MODEL, "finance": SPECIALIST_MODEL,
                                "followup": SPECIALIST_MODEL}, effort=args.effort)
    tracer = tracing.Tracer(TRACE_PATH)
    print(f"Run {tracer.run_id} ({'offline' if args.offline else 'Claude API'})\n")
    print(daily_review(Ctx(conn, tracer, model)))
    print(f"\nApprovals waiting: {len(db.rows(conn, "SELECT id FROM actions WHERE status='pending'"))}. "
          "See them with: python -m ops_chief approvals")
    return 0


def cmd_approvals(args) -> int:
    conn = db.connect(DB_PATH)
    rows = db.rows(conn, "SELECT * FROM actions ORDER BY id")
    if not rows:
        print("No actions yet. Run: python -m ops_chief run --offline")
    for a in rows:
        p = json.loads(a["payload"])
        what = p.get("subject") or p.get("invoice_id") or f"{p.get('task_id')} -> {p.get('status')}"
        print(f"#{a['id']:<3} {a['status']:<9} {a['kind']:<18} {what}")
        print(f"     {a['agent']} | {a['reason']}")
    return 0


def cmd_decide(args, approve: bool) -> int:
    print(policy.resolve(db.connect(DB_PATH), args.id, approve, args.by))
    return 0


def cmd_trace(args) -> int:
    spans = tracing.load(TRACE_PATH, args.run)
    print(tracing.render(spans) if spans else "No trace yet.")
    return 0


def cmd_eval(args) -> int:
    from .evals import run_all
    return 0 if run_all(verbose=True) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ops-chief", description="A chief-of-staff agent that runs a daily operations review.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Run the daily review")
    r.add_argument("--offline", action="store_true", help="Use the scripted model (no API key needed)")
    r.add_argument("--fresh", action="store_true", help="Reset the demo data first")
    r.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    sub.add_parser("approvals", help="Show the approval queue")
    for name in ("approve", "reject"):
        d = sub.add_parser(name, help=f"{name.title()} a pending action")
        d.add_argument("id", type=int)
        d.add_argument("--by", default="owner")
    t = sub.add_parser("trace", help="Show the trace of a run (latest by default)")
    t.add_argument("run", nargs="?")
    sub.add_parser("eval", help="Run the evaluation scenarios")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    HOME.mkdir(exist_ok=True)
    return {
        "run": cmd_run, "approvals": cmd_approvals, "trace": cmd_trace, "eval": cmd_eval,
        "approve": lambda a: cmd_decide(a, True), "reject": lambda a: cmd_decide(a, False),
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
