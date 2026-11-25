"""The Decider (plan 3.4): Decisions API first, the structured-output fallback, NONE, the shadow run, the agreement metric.

Every provider and every OpenAI client is a fake, so no test reaches OpenAI or Jev. Run `async def` tests through the
hook in conftest.py.
"""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from app.agents import decider
from app.agents.decider import DecisionRecord, MemorySink, agreement_rate, decide
from app.ontology.builder import llm as builder_llm

Q = "Which playbook fits this case?"
OPTIONS = ["stock_risk", "late_po"]
STATE = {"sku": "A-100", "on_hand": 3}
DECISION_ENV = ("ONTO_DECIDER_PRIMARY", "ONTO_DECIDER_TIMEOUT_MS", "ONTO_DECIDER_THRESHOLD",
                "ONTO_DECIDER_FALLBACK_MODEL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in DECISION_ENV:
        monkeypatch.delenv(name, raising=False)


class Fake:
    """A provider stand-in: answers (answer, confidence, probability), or raises, after an optional delay."""

    def __init__(self, answer=None, confidence=0.9, probability=None, error=None, delay=0.0):
        self.answer, self.confidence, self.probability = answer, confidence, probability
        self.error, self.delay = error, delay
        self.calls = 0

    async def __call__(self, question, options, state):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.answer, self.confidence, self.probability


async def run(primary, fallback, rng=lambda: 0.99, options=OPTIONS):
    """primary None means the real Decisions API provider, reached through the fake client."""
    sink = MemorySink()
    result = await decide(Q, options, STATE, decision_type="playbook", run_id="r1",
                          primary=primary, fallback=fallback, sink=sink, rng=rng)
    return result, sink


def decision(answer_type="choice", choice="stock_risk", confidence=0.9, probability=0.8):
    """What client.decisions.create returns for one choice question named `decision`."""
    if answer_type == "refusal":
        return SimpleNamespace(answers=[SimpleNamespace(type="refusal", name=decider.QUESTION_NAME)])
    answer = SimpleNamespace(type="choice", name=decider.QUESTION_NAME, choice=choice, confidence=confidence,
                             probabilities=[SimpleNamespace(value=choice, probability=probability)])
    return SimpleNamespace(answers=[answer])


def fake_client(monkeypatch, decisions_result=None, fallback_args=None):
    """Install one fake OpenAI client behind the Builder's helper. decisions_result is returned, or raised when it is
    an exception. Returns the kwargs each endpoint received, keyed by "decisions" and "fallback"."""
    seen = {}

    def decisions_create(**kwargs):
        seen["decisions"] = kwargs
        if isinstance(decisions_result, Exception):
            raise decisions_result
        return decisions_result

    def chat_create(**kwargs):
        seen["fallback"] = kwargs
        args = fallback_args or {"answer": "late_po", "confidence": 0.8}
        call = SimpleNamespace(type="function",
                               function=SimpleNamespace(name=decider.TOOL_NAME, arguments=json.dumps(args)))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])

    client = SimpleNamespace(decisions=SimpleNamespace(create=decisions_create),
                             chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)))
    monkeypatch.setattr(builder_llm, "_openai_client", lambda: client)
    monkeypatch.setattr(decider, "_openai", None)
    return seen


async def test_decisions_choice_is_the_primary_answer(monkeypatch):
    seen = fake_client(monkeypatch, decision(choice="stock_risk", confidence=0.9, probability=0.8))
    fallback = Fake("late_po", 0.8)
    result, sink = await run(None, fallback)
    assert result == {"answer": "stock_risk", "confidence": 0.9, "provider": "openai_decisions",
                      "model": "gpt-6-luna", "probability": 0.8}
    assert fallback.calls == 0
    call = seen["decisions"]
    assert call["model"] == "gpt-6-luna" and json.loads(call["input"]) == STATE
    (question,) = call["questions"]
    assert question["type"] == "choice" and question["instructions"] == Q
    assert [c["value"] for c in question["choices"]] == OPTIONS + ["NONE"]
    assert all(c["description"] for c in question["choices"])
    assert sink.rows[0].provider == "openai_decisions" and sink.rows[0].agreed_with_claude is None


async def test_refusal_falls_through_to_the_fallback(monkeypatch):
    fake_client(monkeypatch, decision("refusal"))
    result, _ = await run(None, Fake("late_po", 0.8))
    assert result == {"answer": "late_po", "confidence": 0.8, "provider": "openai_fallback",
                      "model": "gpt-5.4-mini", "probability": None}


async def test_invalid_choice_falls_through(monkeypatch):
    fake_client(monkeypatch, decision(choice="made_up"))
    result, _ = await run(None, Fake("late_po", 0.8))
    assert result["provider"] == "openai_fallback"


async def test_low_confidence_falls_through(monkeypatch):
    fake_client(monkeypatch, decision(confidence=0.3))
    fallback = Fake("late_po", 0.8)
    result, _ = await run(None, fallback)
    assert result["provider"] == "openai_fallback" and fallback.calls == 1


async def test_timeout_falls_through(monkeypatch):
    monkeypatch.setenv("ONTO_DECIDER_TIMEOUT_MS", "10")
    result, _ = await run(Fake("stock_risk", 0.99, delay=5), Fake("late_po", 0.8))
    assert result["provider"] == "openai_fallback"


async def test_none_is_always_an_option_and_a_valid_answer(monkeypatch):
    seen = fake_client(monkeypatch, decision(choice="NONE", confidence=0.9))
    fallback = Fake("late_po", 0.8)
    sink = MemorySink()
    result = await decide(Q, ["stock_risk"], STATE, decision_type="playbook", run_id="r1", fallback=fallback,
                          sink=sink, rng=lambda: 0.99)
    assert result == {"answer": "NONE", "confidence": 0.9, "provider": "openai_decisions",
                      "model": "gpt-6-luna", "probability": 0.8}
    assert fallback.calls == 0
    assert [c["value"] for c in seen["decisions"]["questions"][0]["choices"]] == ["stock_risk", "NONE"]
    assert sink.rows[0].option_ids == ["stock_risk", "NONE"]


async def test_when_both_paths_fail_the_answer_is_none(monkeypatch):
    fake_client(monkeypatch, decisions_result=RuntimeError("boom"))
    result, sink = await run(None, Fake(error=RuntimeError("boom")))
    assert result == {"answer": "NONE", "confidence": 0.0, "provider": "none", "model": None, "probability": None}
    assert sink.rows[0].chosen == "NONE"


async def test_missing_sdk_falls_back_with_jev_primary(monkeypatch):
    monkeypatch.setenv("ONTO_DECIDER_PRIMARY", "jev")
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)  # makes the import raise ImportError
    result, _ = await run(None, Fake("late_po", 0.8))
    assert result["provider"] == "openai_fallback"


async def test_jev_primary_is_recorded_as_jev(monkeypatch):
    monkeypatch.setenv("ONTO_DECIDER_PRIMARY", "jev")
    result, _ = await run(Fake("stock_risk", 0.9, probability=0.7), Fake("late_po", 0.8))
    assert result == {"answer": "stock_risk", "confidence": 0.9, "provider": "jev", "model": "jev-latest",
                      "probability": 0.7}


@pytest.mark.parametrize(("env", "name"), [
    ({}, "openai"),
    ({"ONTO_DECIDER_PRIMARY": "jev"}, "jev"),
    ({"ONTO_DECIDER_PRIMARY": " OpenAI "}, "openai"),
])
def test_primary_switch(monkeypatch, env, name):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert decider._primary() == name


def test_unknown_primary_raises(monkeypatch):
    monkeypatch.setenv("ONTO_DECIDER_PRIMARY", "banana")
    with pytest.raises(ValueError):
        decider._primary()


async def test_fallback_is_forced_to_one_option(monkeypatch):
    seen = fake_client(monkeypatch, fallback_args={"answer": "late_po", "confidence": 0.8})
    assert await decider.openai_fallback_provider(Q, OPTIONS, STATE) == ("late_po", 0.8, None)
    tool = seen["fallback"]["tools"][0]["function"]
    assert tool["parameters"]["properties"]["answer"]["enum"] == OPTIONS
    assert seen["fallback"]["tool_choice"] == {"type": "function", "function": {"name": decider.TOOL_NAME}}


@pytest.mark.parametrize(("env", "model"), [
    ({}, "gpt-5.4-mini"),
    ({"ONTO_DECIDER_FALLBACK_MODEL": "gpt-y"}, "gpt-y"),
])
def test_fallback_model_comes_from_env(monkeypatch, env, model):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seen = fake_client(monkeypatch)
    asyncio.run(decider.openai_fallback_provider(Q, OPTIONS, STATE))
    assert seen["fallback"]["model"] == model


async def test_shadow_runs_the_fallback_and_records_agreement_without_changing_the_answer():
    agree_fallback = Fake("stock_risk")
    agree = await run(Fake("stock_risk", 0.9, probability=0.7), agree_fallback, rng=lambda: 0.05)
    differ = await run(Fake("stock_risk", 0.9), Fake("late_po"), rng=lambda: 0.05)
    assert agree_fallback.calls == 1
    assert agree[0]["provider"] == differ[0]["provider"] == "openai_decisions"
    assert differ[0]["answer"] == "stock_risk"
    assert agree[1].rows[0].agreed_with_claude is True
    assert differ[1].rows[0].agreed_with_claude is False


async def test_failed_shadow_keeps_the_primary_answer():
    primary, fallback = Fake("stock_risk", 0.9), Fake(error=RuntimeError("boom"))
    result, sink = await run(primary, fallback, rng=lambda: 0.05)
    assert result["provider"] == "openai_decisions" and result["answer"] == "stock_risk"
    assert sink.rows[0].agreed_with_claude is None


async def test_logged_row_holds_no_question_or_state():
    _, sink = await run(Fake("stock_risk", 0.9), Fake("stock_risk"), rng=lambda: 0.05)
    text = repr(sink.rows[0])
    assert Q not in text and "A-100" not in text


def test_agreement_rate_counts_only_sampled_rows():
    def row(agreed):
        return DecisionRecord(run_id="r", decision_type="t", option_ids=["NONE"], chosen="NONE", confidence=0.0,
                              provider="none", ms=1, agreed_with_claude=agreed)

    assert agreement_rate([row(True), row(False), row(None)]) == 0.5
    assert agreement_rate([row(None)]) is None


async def test_the_installed_sdk_decision_type_is_what_the_provider_reads(monkeypatch):
    # The real model from the pinned openai SDK, not a stand-in: a renamed field or answer type fails here, offline.
    from openai.types.decision import Decision

    real = Decision.model_validate({
        "answers": [{"type": "choice", "name": decider.QUESTION_NAME, "choice": "stock_risk", "confidence": 0.9,
                     "probabilities": [{"value": "stock_risk", "probability": 0.8}]}],
        "model": decider.DECISIONS_MODEL,
        "usage": {"input_tokens": 1, "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
                  "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2},
    })
    fake_client(monkeypatch, real)
    assert await decider.decisions_provider(Q, OPTIONS, STATE) == ("stock_risk", 0.9, 0.8)
