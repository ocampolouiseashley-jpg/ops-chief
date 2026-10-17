"""Model layer: one interface, two implementations.

AnthropicModel  - the Claude API, with a fallback chain, a circuit breaker and refusal handling.
ScriptedModel   - deterministic stand-in that drives the same tools; used for the offline demo and tests.

Both return a Reply whose blocks are plain dicts ({"type": "text"|"tool_use", ...}), so the
agent loop never depends on which one is running.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import anthropic

CHIEF_MODEL = "claude-opus-5-5"
SPECIALIST_MODEL = "claude-haiku-5-5"
FALLBACKS = {CHIEF_MODEL: ["claude-sonnet-5-5"], SPECIALIST_MODEL: ["claude-sonnet-5-5"]}


@dataclass
class Reply:
    stop_reason: str
    blocks: list[dict]
    assistant_content: Any  # what to append to the transcript (SDK blocks keep thinking intact)
    model: str
    tokens: int = 0


class Model(Protocol):
    def respond(self, agent: str, system: str, messages: list, tools: list[dict]) -> Reply: ...


class ModelUnavailable(Exception):
    pass


@dataclass
class CircuitBreaker:
    """Opens for a model when 3 of its last 10 calls failed; calls then go straight to the fallback."""
    window: int = 10
    max_failures: int = 3
    history: dict[str, deque] = field(default_factory=dict)

    def record(self, model: str, ok: bool) -> None:
        self.history.setdefault(model, deque(maxlen=self.window)).append(ok)

    def is_open(self, model: str) -> bool:
        return list(self.history.get(model, [])).count(False) >= self.max_failures


class AnthropicModel:
    RETRYABLE = (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)

    def __init__(self, models: dict[str, str], client: anthropic.Anthropic | None = None, effort: str = "low"):
        self.client = client or anthropic.Anthropic(max_retries=2)  # the SDK retries 429/5xx with backoff first
        self.models = models  # agent name -> primary model
        self.effort = effort
        self.breaker = CircuitBreaker()

    def _call(self, model: str, system: str, messages: list, tools: list[dict]):
        return self.client.messages.create(
            model=model, max_tokens=16000, system=system, tools=tools, messages=messages,
            output_config={"effort": self.effort},
        )

    def respond(self, agent: str, system: str, messages: list, tools: list[dict]) -> Reply:
        primary = self.models.get(agent, SPECIALIST_MODEL)
        chain = [primary] + FALLBACKS.get(primary, [])
        errors = []
        for model in chain:
            if self.breaker.is_open(model) and model != chain[-1]:
                errors.append(f"{model}: circuit open")
                continue
            try:
                resp = self._call(model, system, messages, tools)
            except self.RETRYABLE as exc:
                self.breaker.record(model, False)
                errors.append(f"{model}: {type(exc).__name__}")
                continue
            if resp.stop_reason == "refusal":
                self.breaker.record(model, False)
                errors.append(f"{model}: refusal")
                continue
            self.breaker.record(model, True)
            blocks = []
            for b in resp.content:
                if b.type == "text":
                    blocks.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
            return Reply(resp.stop_reason, blocks, resp.content, model, tokens)
        raise ModelUnavailable("; ".join(errors))


# --------------------------------------------------------------------------- scripted stand-in

Policy = Callable[[list, list[dict]], list[dict]]


class ScriptedModel:
    """Each agent gets a policy: f(messages, tool_results_so_far) -> blocks for the next turn.

    The policies call the same tools a real model would, in a fixed order, so the whole
    system (delegation, memory, gates, tracing) runs end to end without an API key.
    """

    def __init__(self, policies: dict[str, Policy]):
        self.policies = policies
        self.counter = 0

    def respond(self, agent: str, system: str, messages: list, tools: list[dict]) -> Reply:
        results = [c for m in messages if m["role"] == "user" and isinstance(m["content"], list)
                   for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_result"]
        blocks = self.policies[agent](messages, results)
        for b in blocks:
            if b["type"] == "tool_use" and "id" not in b:
                self.counter += 1
                b["id"] = f"toolu_scripted_{self.counter}"
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return Reply(stop, blocks, blocks, "scripted")
