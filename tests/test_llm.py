"""The Claude model layer with a fake client: fallback, refusals and the circuit breaker."""

from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from ops_chief.llm import AnthropicModel, CircuitBreaker, ModelUnavailable


def ok_response(text="done"):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def connection_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


class FakeMessages:
    def __init__(self, script):
        self.script = script  # model -> list of outcomes (Exception or response), consumed in order
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw["model"])
        outcome = self.script[kw["model"]].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(script):
    fake = FakeMessages(script)
    return AnthropicModel({"projects": "claude-haiku-5-5"}, client=SimpleNamespace(messages=fake)), fake


def test_primary_model_answers():
    m, fake = make({"claude-haiku-5-5": [ok_response("hello")]})
    r = m.respond("projects", "sys", [], [])
    assert r.model == "claude-haiku-5-5" and r.blocks == [{"type": "text", "text": "hello"}] and r.tokens == 15


def test_falls_back_after_connection_error():
    m, fake = make({"claude-haiku-5-5": [connection_error()], "claude-sonnet-5-5": [ok_response()]})
    assert m.respond("projects", "sys", [], []).model == "claude-sonnet-5-5"
    assert fake.calls == ["claude-haiku-5-5", "claude-sonnet-5-5"]


def test_refusal_moves_to_fallback():
    refused = SimpleNamespace(stop_reason="refusal", content=[], usage=SimpleNamespace(input_tokens=1, output_tokens=0))
    m, fake = make({"claude-haiku-5-5": [refused], "claude-sonnet-5-5": [ok_response()]})
    assert m.respond("projects", "sys", [], []).model == "claude-sonnet-5-5"


def test_all_models_failing_raises():
    m, _ = make({"claude-haiku-5-5": [connection_error()], "claude-sonnet-5-5": [connection_error()]})
    with pytest.raises(ModelUnavailable):
        m.respond("projects", "sys", [], [])


def test_circuit_opens_and_skips_the_failing_model():
    script = {"claude-haiku-5-5": [connection_error()] * 3, "claude-sonnet-5-5": [ok_response()] * 4}
    m, fake = make(script)
    for _ in range(3):
        m.respond("projects", "sys", [], [])
    fake.calls.clear()
    m.respond("projects", "sys", [], [])
    assert fake.calls == ["claude-sonnet-5-5"]  # haiku skipped while its circuit is open


def test_breaker_window():
    b = CircuitBreaker(window=10, max_failures=3)
    for ok in [False, False, True]:
        b.record("m", ok)
    assert not b.is_open("m")
    b.record("m", False)
    assert b.is_open("m")
