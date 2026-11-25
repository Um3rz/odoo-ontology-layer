"""Plan 3.5, the BI Agent: the graph, the tools, the numbers guard, the answer card and the record.

No database, Redis or network. The release is the committed templates, the checkpointer is LangGraph's in-memory
saver, and the model and Odoo are scripted fakes. Each test names the rule it checks (plan 3.5 items 1-7, 6a).
"""
import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents import bi, runs
from app.agents.tools import ontology as tools
from app.odoo.client import OdooError
from app.ontology.loader import load_release

RELEASE = load_release()
DIGEST = RELEASE.digest()


class FakeModel:
    """Replies in order. `seen` keeps the messages each turn was given."""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    async def turn(self, system, messages, tool_list):
        self.seen.append(list(messages))
        return self.replies.pop(0)


def reply(text="", calls=()):
    return bi.Reply(text, [{"id": f"call{i}", "name": n, "args": a} for i, (n, a) in enumerate(calls)],
                    10, 5, "fake-1")


class FakeOdoo:
    """`answer(path, body)` returns the Odoo reply, or raises the OdooError to simulate a refusal."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def request(self, method, path, json=None):
        self.calls.append((path, json))
        out = self.answer(path, json)
        if isinstance(out, Exception):
            raise out
        return out


def query_reply(columns, rows, release=DIGEST):
    return {"columns": columns, "rows": rows, "release": release, "read_at": "2026-10-08T06:00:00Z",
            "truncated": False, "next_page": None}


def ontology(odoo, trust=None):
    return tools.Ontology(release=RELEASE, digest=DIGEST, trust=trust or {"revenue": "certified"},
                          suspended=set(), odoo=odoo)


async def run_turn(question, model, odoo, trust=None):
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    deps = bi.Deps(ontology=ontology(odoo, trust), model=model, emit=emit)
    graph = bi.build_graph(deps, MemorySaver())
    final = await graph.ainvoke(bi.new_state(question), {"configurable": {"thread_id": uuid.uuid4().hex}})
    return final, events


REVENUE_TOTAL = [{"key": "revenue", "label": "Revenue", "role": "measure", "unit": "money"}]


# --- numbers guard (item 3) -------------------------------------------------------------

def test_the_guard_reads_rounding_and_skips_dates_and_years():
    figures = bi.numbers_in("Revenue is 2.6K, 1,234.50 EUR, on 2026-09-01 and in August 2026")
    assert any(abs(v - 2600) < 1e-6 and abs(t - 50) < 1e-6 for v, t in figures)  # 2.6K: +/- 50
    assert any(abs(v - 1234.5) < 1e-9 and abs(t - 0.005) < 1e-9 for v, t in figures)
    assert all(v != 2026.0 for v, _ in figures)  # years and dates are not figures


def test_a_rounded_figure_passes_and_an_invented_one_is_flagged():
    assert bi.invented("About 2.6K.", [{"v": 2550.0}], "") == []  # 2.6K is 2,600 +/- 50
    assert bi.invented("About 2.6K.", [{"v": 2400.0}], "") == ["2600"]


def test_a_percent_may_be_the_fraction_the_tool_returned_and_a_change_may_be_stated_by_size():
    assert bi.invented("Up 25%.", [{"change_pct": 0.25}], "") == []
    assert bi.invented("Down 100.", [{"revenue_change": -100.0}], "") == []


# --- the message shapes sent to each provider (item 1) --------------------------------------

def test_tool_results_of_one_turn_share_one_anthropic_user_message():
    messages = [
        {"role": "user", "text": "How much?"},
        {"role": "assistant", "text": "", "calls": [{"id": "a", "name": "measure", "args": {"metric": "revenue"}},
                                                    {"id": "b", "name": "describe", "args": {"key": "revenue"}}]},
        {"role": "tool", "id": "a", "name": "measure", "content": "{}"},
        {"role": "tool", "id": "b", "name": "describe", "content": "{}"},
    ]
    out = bi.to_anthropic(messages)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["type"] for b in out[2]["content"]] == ["tool_result", "tool_result"]
    openai = bi.to_openai(messages)
    assert openai[1]["tool_calls"][0]["function"]["arguments"] == '{"metric": "revenue"}'
    assert openai[2] == {"role": "tool", "tool_call_id": "a", "content": "{}"}


# --- the graph: answer, card, events (items 1, 4) ----------------------------------------

async def test_a_total_is_measured_passes_the_guard_and_is_badged_certified():
    odoo = FakeOdoo(lambda path, body: query_reply(REVENUE_TOTAL, [[1234.5]]))
    model = FakeModel([reply(calls=[("measure", {"metric": "revenue"})]), reply("Revenue is 1,234.50 EUR.")])
    final, events = await run_turn("What was revenue?", model, odoo)

    card = final["card"]
    assert card["kind"] == "answer" and card["guard"] == {"ok": True, "flagged": []}
    assert card["numbers"] == [{"label": "Sales order value before discounts and tax", "value": 1234.5, "unit": "money"}]
    assert card["badge"] == "Certified" and card["trust"] == "certified"
    assert card["source"]["kind"] == "metric" and card["source"]["release"] == DIGEST
    assert card["definition"]["formula"] == "quantity * price"
    assert odoo.calls[0][0] == "/onto/v1/query" and odoo.calls[0][1]["metric"] == "revenue"
    tool_events = [data for kind, data in events if kind == "tool"]
    assert tool_events[0]["ok"] is True and tool_events[0]["summary"] == "Sales order value before discounts and tax: 1 rows"
    assert [kind for kind, _ in events][-1] == "answer"


async def test_an_invented_figure_gets_one_retry_then_is_flagged():
    odoo = FakeOdoo(lambda path, body: query_reply(REVENUE_TOTAL, [[1234.5]]))
    model = FakeModel([reply(calls=[("measure", {"metric": "revenue"})]),
                       reply("Revenue is 999,999."), reply("Revenue is 999,999.")])
    final, _ = await run_turn("What was revenue?", model, odoo)

    assert final["card"]["guard"] == {"ok": False, "flagged": ["999999"]}
    assert final["answer"].endswith("Not verified: 999999 does not appear in the query results.")
    assert len(model.seen) == 3  # the first reply, the retry, and no further calls
    assert "Check failed" in model.seen[2][-1]["text"]


async def test_a_bad_spec_is_refused_before_any_odoo_call():
    odoo = FakeOdoo(lambda path, body: query_reply(REVENUE_TOTAL, [[1.0]]))
    model = FakeModel([reply(calls=[("measure", {"metric": "revenue", "dims": ["no_such_dim"]})]),
                       reply("I could not measure that.")])
    final, _ = await run_turn("Revenue by nothing?", model, odoo)

    assert odoo.calls == []  # check_spec refused it locally
    assert final["trace"][0]["summary"].startswith("measure: invalid_spec")
    assert final["card"]["numbers"] == []


async def test_top_n_ranks_by_the_queried_metric_whatever_the_model_names():
    odoo = FakeOdoo(lambda path, body: query_reply(REVENUE_TOTAL, [[1.0]]))
    await tools.measure(ontology(odoo), {"metric": "revenue", "dims": ["customer"],
                                         "top_n": {"n": "5", "by": "value", "dir": "asc"}})
    top_n = odoo.calls[0][1]["top_n"]
    assert (top_n["n"], top_n["by"], top_n["dir"]) == (5, "revenue", "asc")


async def test_an_odoo_refusal_keeps_its_code_and_is_never_an_answer():
    odoo = FakeOdoo(lambda path, body: OdooError(403, code="forbidden"))
    model = FakeModel([reply(calls=[("measure", {"metric": "revenue"})]), reply("You cannot read revenue.")])
    final, _ = await run_turn("Revenue?", model, odoo)

    assert final["trace"][0]["summary"] == "measure: forbidden"
    assert final["card"]["numbers"] == [] and final["card"]["kind"] == "answer"


async def test_a_transport_failure_maps_to_odoo_unavailable():
    odoo = FakeOdoo(lambda path, body: OdooError(0, "ConnectError"))
    model = FakeModel([reply(calls=[("measure", {"metric": "revenue"})]), reply("Odoo is unreachable.")])
    final, _ = await run_turn("Revenue?", model, odoo)

    assert final["trace"][0]["summary"] == "measure: odoo_unavailable"


# --- decompose: parts add up to the total change (item 6a) -------------------------------

def _decompose_odoo():
    def answer(path, body):
        metric, dims = body["metric"], body.get("dims")
        if metric == "revenue" and not dims:  # 900 now, 1000 before
            return query_reply([{"key": "revenue"}, {"key": "revenue_previous"}], [[900.0, 1000.0]])
        if metric == "units_sold":  # 30 units now, 40 before: the volume side of revenue = quantity * price
            return query_reply([{"key": "units_sold"}, {"key": "units_sold_previous"}], [[30.0, 40.0]])
        if metric == "revenue" and dims == ["product"]:
            return query_reply([{"key": "product", "role": "dimension"}, {"key": "revenue"},
                                {"key": "revenue_previous"}],
                               [["Tea", 500.0, 600.0], ["Coffee", 400.0, 400.0]])
        raise AssertionError(body)
    return FakeOdoo(answer)


async def test_decompose_splits_the_change_into_volume_price_and_dimension_that_add_up():
    odoo = _decompose_odoo()
    model = FakeModel([reply(calls=[("decompose", {"metric": "revenue", "dims": ["product"],
                                                   "period": {"from": "2026-10-01", "to": "2026-10-07"}})]),
                       reply("Revenue fell by 100, from 1,000 to 900.")])
    final, _ = await run_turn("Why did sales drop last week?", model, odoo)

    split = final["card"]["decomposition"]
    assert split["total"]["change"] == -100.0
    assert split["volume"]["effect"] == -250.0 and split["price"]["effect"] == 150.0
    assert split["checks"]["volume_plus_price_equals_total"] is True
    dim = split["dimensions"][0]
    assert dim["sums_to_total"] is True
    assert sum(p["change"] for p in dim["parts"]) == -100.0
    assert final["card"]["guard"]["ok"] is True and final["card"]["badge"] == "Certified"


# --- refusals and exploration (items 5, 6) -----------------------------------------------

async def test_a_forecast_is_refused_with_a_request_action_and_no_query():
    odoo = FakeOdoo(lambda path, body: query_reply(REVENUE_TOTAL, [[1.0]]))
    model = FakeModel([reply(calls=[("refuse", {"reason": "forecast", "message": "No forecasts here."})]),
                       reply("I do not forecast.")])
    final, _ = await run_turn("What will revenue be next month?", model, odoo)

    card = final["card"]
    assert card["kind"] == "refusal" and card["refusal"]["reason"] == "forecast"
    assert card["actions"] == [{"type": "request_metric", "label": "Request this metric",
                                "question": "What will revenue be next month?"}]
    assert odoo.calls == []


async def test_an_exploration_is_badged_uncertified_and_offers_propose_as_metric():
    def answer(path, body):
        assert path == "/onto/v1/explore" and body["model"] == "stock.picking"
        return {"columns": [{"key": "state", "label": "Status", "role": "dimension"},
                            {"key": "_aggregate", "label": "Count", "role": "measure"}],
                "rows": [["done", 3], ["draft", 2]], "release": None, "read_at": "2026-10-08T06:00:00Z",
                "truncated": False, "next_page": None, "trust": "uncertified"}
    odoo = FakeOdoo(answer)
    model = FakeModel([reply(calls=[("explore", {"model": "stock.picking", "fields": ["state"],
                                                 "group_by": ["state"]})]),
                       reply("There are 3 done transfers.")])
    final, _ = await run_turn("How many transfers are done?", model, odoo)

    card = final["card"]
    assert card["badge"] == "Uncertified" and card["source"]["kind"] == "exploration"
    assert card["table"]["rows"] == [["done", 3], ["draft", 2]]
    assert card["actions"][0]["type"] == "propose_metric"


# --- limits (item 7's turn cap) ----------------------------------------------------------

async def test_the_step_limit_answers_instead_of_looping():
    looping = reply(calls=[("search_ontology", {"query": "revenue"})])
    odoo = FakeOdoo(lambda path, body: None)
    final, _ = await run_turn("Anything?", FakeModel([looping] * bi.MAX_TURNS), odoo)

    assert final["answer"] == bi.STEP_LIMIT_TEXT
    assert odoo.calls == []


# --- the record (contract 3.10) ------------------------------------------------------------

def test_a_chat_turn_is_recorded_with_its_card_trace_and_usage_and_no_tool_rows():
    card = {"v": 1, "kind": "answer", "text": "Revenue is 1."}
    trace = [{"seq": 1, "type": "tool", "tool": "measure", "ms": 12, "summary": "Revenue: 1 rows"}]
    outcome = {"answer": "Revenue is 1.", "card": card, "trace": trace,
               "usage": {"input_tokens": 3, "output_tokens": 4}, "model": "fake-1"}
    rec = runs._record("run1", "chat", "done", 7, outcome, {"text": "Revenue?", "agent": "bi"})

    assert rec["usage"]["model"] == "fake-1" and rec["usage"]["input_tokens"] == 3
    assert rec["run"]["trace"] == trace and rec["run"]["outcome"] == {}
    assistant = rec["chat"]["messages"][1]
    assert assistant["text"] == "Revenue is 1." and assistant["card"] == card


def test_a_failed_turn_says_so_in_the_record():
    rec = runs._record("run2", "chat", "failed", 7, {}, {"text": "Revenue?", "agent": "bi"})
    assert rec["chat"]["messages"][1]["text"] == runs.FAILED_TEXT
    assert rec["usage"]["model"] == "none"


# --- providers --------------------------------------------------------------------------

def test_make_model_follows_the_provider_switch(monkeypatch):
    monkeypatch.delenv("ONTO_LLM_PROVIDER", raising=False)
    assert isinstance(bi.make_model(), bi.OpenAIModel)
    monkeypatch.setenv("ONTO_LLM_PROVIDER", "anthropic")
    assert isinstance(bi.make_model(), bi.AnthropicModel)
    monkeypatch.setenv("ONTO_LLM_PROVIDER", "gemini")
    with pytest.raises(RuntimeError):
        bi.make_model()
