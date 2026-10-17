"""Tracing: every model call, tool call, delegation and gate decision becomes a span in a JSONL file."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Tracer:
    def __init__(self, path: Path, run_id: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self._stack: list[str] = []

    def _write(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    @contextmanager
    def span(self, kind: str, name: str, agent: str, **attrs):
        span_id = uuid.uuid4().hex[:8]
        parent = self._stack[-1] if self._stack else None
        self._stack.append(span_id)
        start = time.perf_counter()
        record = {"run_id": self.run_id, "span": span_id, "parent": parent, "kind": kind, "name": name, "agent": agent, **attrs}
        try:
            yield record
            record.setdefault("status", "ok")
        except Exception as exc:  # recorded, then re-raised
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["ms"] = round((time.perf_counter() - start) * 1000, 1)
            self._stack.pop()
            self._write(record)


def load(path: Path, run_id: str | None = None) -> list[dict]:
    if not Path(path).exists():
        return []
    spans = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if run_id is None and spans:
        run_id = spans[-1]["run_id"]
    return [s for s in spans if s["run_id"] == run_id]


def render(spans: list[dict]) -> str:
    children: dict[str | None, list[dict]] = {}
    for s in spans:
        children.setdefault(s["parent"], []).append(s)
    lines: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for s in children.get(parent, []):
            extra = ""
            if s.get("tokens"):
                extra += f" tokens={s['tokens']}"
            if s.get("decision"):
                extra += f" -> {s['decision']}"
            if s.get("status") == "error":
                extra += f" ERROR {s.get('error')}"
            lines.append(f"{'  ' * depth}{s['kind']:<8} {s['agent']:<9} {s['name']} ({s['ms']} ms){extra}")
            walk(s["span"], depth + 1)

    walk(None, 0)
    return "\n".join(lines)
