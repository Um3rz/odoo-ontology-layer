"""Plan 3.6: the Inventory Agent skeleton and the user_request playbook.

The model, the Decider and Odoo are all fakes, so nothing here reaches OpenAI, Odoo or Redis. Run `async def`
tests through the hook in conftest.py. Written at the 3.6 test pass; not run in this session (owner rule).
"""
import pytest

from app.agents.inventory import supervisor
from app.agents.inventory.playbooks import PLAYBOOKS
from app.agents.inventory.playbooks.base import Turn
from app.odoo.client import OdooError

RUN = "a" * 32
UPSERT = "/onto/v1/store/upsert"
READ = "/onto/v1/store/read"
USER_REQUEST = PLAYBOOKS["user_request"]
STATUS_ROW = {"product_id": [11, "Widget"], "location_id": [3, "Main"], "status": "ok",
              "refreshed_at": "2026-11-09T06:00:00"}
ANSWER = {"answer": "Product 11 is ok at 06:00.",
          "options": [{"option_key": "review", "action_type": "activity",
                       "reason": "Stock is ok, so a review is enough."}]}
COMPANY = 1
OWNER = 7  # the requester's Odoo user id: the activity goes on their res.users record
ROW = {"id": 3, "company_id": [1, "My Company"], "ai_enabled": False, "agents_paused": False}
ODOO_LINE_KEYS = frozenset(("option_key", "line_type", "action_type", "params", "reason"))  # Odoo's line allowlist


class FakeOdoo:
    """Answers whoami and the three store routes the run uses. Every call is recorded."""

    def __init__(self, setting=None, status_rows=None, setting_error=None, whoami_error=None):
        self.calls = []
        self.setting = [ROW] if setting is None else setting
        self.status_rows = [STATUS_ROW] if status_rows is None else status_rows
        self.setting_error = setting_error
        self.whoami_error = whoami_error

    async def whoami(self):
        self.calls.append(("GET", "/onto/v1/whoami", None))
        if self.whoami_error is not None:
            raise self.whoami_error
        return {"active_company_id": COMPANY, "user": {"id": 7}, "groups": [], "ai_enabled": True}

    async def request(self, method, path, json=None, *, want=dict):
        self.calls.append((method, path, json))
        if path == READ and json["model"] == "onto.agent.setting":
            if self.setting_error is not None:
                raise self.setting_error
            return self.setting
        if path == READ and json["model"] == "onto.status":
            return self.status_rows
        if path == UPSERT:
            return {"ok": True}
        raise AssertionError(path)

    def paths(self, path):
        return [c for c in self.calls if c[1] == path]


class FakeDecider:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    async def __call__(self, question, options, state, *, decision_type, run_id):
        self.calls += 1
        return {"answer": self.answer, "confidence": 0.9, "provider": "fake", "probability": None}


class ScriptedModel:
    """Returns the scripted turns in order, then `repeat` forever if it is set."""

    def __init__(self, *turns, repeat=None):
        self.turns, self.repeat, self.calls = list(turns), repeat, 0

    async def __call__(self, *, system, payload, tools, output_schema):
        self.calls += 1
        return self.turns.pop(0) if self.turns else self.repeat


def _ctx(odoo, model, decider=None):
    return supervisor.Ctx(run_id=RUN, odoo=odoo, bot=odoo, model=model,
                          decider=decider or FakeDecider("user_request"), owner_uid=OWNER)


async def test_user_request_runs_within_budget():
    odoo = FakeOdoo()
    model = ScriptedModel(
        Turn(tool="status_lookup", args={"product_id": 11}, cost=0.01, input_tokens=120, output_tokens=15,
             model="scripted"),
        Turn(output=ANSWER, cost=0.01, input_tokens=200, output_tokens=60, model="scripted"),
    )
    result = await supervisor.run(_ctx(odoo, model), "Is product 11 ok?")

    assert result["stop_reason"] == "done"
    assert model.calls == 2 <= USER_REQUEST.budget.max_steps
    assert result["answer"] == "Product 11 is ok at 06:00. A proposal is waiting for approval in Odoo."
    assert result["usage"]["input_tokens"] == 320
    assert result["usage"]["cost"] == pytest.approx(0.02)

    (read,) = [c for c in odoo.paths(READ) if c[2]["model"] == "onto.status"]
    assert read[2]["domain"] == [["product_id", "=", 11]]

    assert result["proposal"]["written"] is True
    (upsert,) = odoo.paths(UPSERT)
    body = upsert[2]
    assert body["model"] == "onto.recommendation" and body["key"] == ["external_ref"]
    (row,) = body["rows"]
    assert row["external_ref"] == f"{RUN}:user_request" and row["state"] == "proposed"
    assert [line["option_key"] for line in row["lines"]] == ["review", "do_nothing"]
    review, do_nothing = row["lines"]
    assert review["line_type"] == "action" and review["action_type"] == "activity"
    assert review["params"] == {"res_model": "res.users", "res_id": OWNER,
                                "summary": "Stock is ok, so a review is enough."}
    assert do_nothing["line_type"] == "do_nothing" and do_nothing["params"] == {}
    assert all(set(line) <= ODOO_LINE_KEYS for line in row["lines"])
    assert row["preferred_option_key"] == "do_nothing"
    assert result["refs"] == [f"{RUN}:user_request"]


async def test_an_activity_with_no_owner_is_not_written():
    odoo = FakeOdoo()
    ctx = supervisor.Ctx(run_id=RUN, odoo=odoo, bot=odoo, model=ScriptedModel(repeat=Turn(output=ANSWER)),
                         decider=FakeDecider("user_request"))
    result = await supervisor.run(ctx, "Is product 11 ok?")

    assert result["proposal"] == {"written": False, "reason": "no_target"}
    assert odoo.paths(UPSERT) == []


async def test_the_activity_summary_is_capped_at_the_odoo_limit():
    long = {"answer": "Check it.", "options": [{"option_key": "review", "action_type": "activity", "reason": "x" * 300}]}
    odoo = FakeOdoo()
    await supervisor.run(_ctx(odoo, ScriptedModel(Turn(output=long))), "Is product 11 ok?")

    (upsert,) = odoo.paths(UPSERT)
    assert len(upsert[2]["rows"][0]["lines"][0]["params"]["summary"]) == supervisor.SUMMARY_MAX == 255


async def test_the_model_cannot_choose_the_record_an_activity_is_on():
    smuggled = {"answer": "Check it.", "options": [{"option_key": "review", "action_type": "activity",
                                                    "reason": "Check.", "params": {"res_model": "product.product",
                                                                                    "res_id": 11}}]}
    odoo = FakeOdoo()
    result = await supervisor.run(_ctx(odoo, ScriptedModel(Turn(output=smuggled))), "Is product 11 ok?")

    assert result["stop_reason"] == "invalid_output" and odoo.paths(UPSERT) == []


async def test_kill_switch_stops_the_run_before_any_work():
    odoo = FakeOdoo(setting=[{**ROW, "agents_paused": True}])
    model = ScriptedModel(repeat=Turn(output=ANSWER))
    decider = FakeDecider("user_request")
    result = await supervisor.run(_ctx(odoo, model, decider), "Is product 11 ok?")

    assert result["stop_reason"] == "kill_switch"
    assert model.calls == 0 and decider.calls == 0
    assert odoo.paths(UPSERT) == []
    assert odoo.paths(READ) and odoo.paths(READ)[0][2]["model"] == "onto.agent.setting"


async def test_unreadable_kill_switch_fails_closed():
    odoo = FakeOdoo(setting_error=OdooError(422, "bad_model"))
    model = ScriptedModel(repeat=Turn(output=ANSWER))
    result = await supervisor.run(_ctx(odoo, model), "Is product 11 ok?")

    assert result["stop_reason"] == "kill_switch_unreadable"
    assert model.calls == 0 and odoo.paths(UPSERT) == []


async def test_no_setting_row_means_the_switch_is_off():
    odoo = FakeOdoo(setting=[])
    model = ScriptedModel(Turn(output=ANSWER))
    result = await supervisor.run(_ctx(odoo, model), "Is product 11 ok?")

    assert result["stop_reason"] == "done"
    assert model.calls == 1
    (read,) = [c for c in odoo.paths(READ) if c[2]["model"] == "onto.agent.setting"]
    assert read[2] == {"model": "onto.agent.setting", "fields": ["company_id", "ai_enabled", "agents_paused"],
                       "domain": [["company_id", "=", COMPANY]], "limit": 1}


async def test_a_non_boolean_switch_fails_closed():
    odoo = FakeOdoo(setting=[{**ROW, "agents_paused": "yes"}])
    model = ScriptedModel(repeat=Turn(output=ANSWER))
    result = await supervisor.run(_ctx(odoo, model), "Is product 11 ok?")

    assert result["stop_reason"] == "kill_switch_unreadable"
    assert model.calls == 0 and odoo.paths(UPSERT) == []


async def test_a_failed_company_lookup_fails_closed():
    odoo = FakeOdoo(whoami_error=OdooError(503))
    model = ScriptedModel(repeat=Turn(output=ANSWER))
    result = await supervisor.run(_ctx(odoo, model), "Is product 11 ok?")

    assert result["stop_reason"] == "kill_switch_unreadable"
    assert model.calls == 0 and odoo.paths(READ) == []


async def test_step_budget_stops_a_run_that_never_finishes():
    odoo = FakeOdoo()
    model = ScriptedModel(repeat=Turn(tool="status_lookup", args={"product_id": 11}))
    result = await supervisor.run(_ctx(odoo, model), "Is product 11 ok?")

    assert result["stop_reason"] == "step_budget"
    assert model.calls == USER_REQUEST.budget.max_steps
    assert odoo.paths(UPSERT) == []


async def test_cost_budget_stops_before_the_tool_runs():
    odoo = FakeOdoo()
    model = ScriptedModel(Turn(tool="status_lookup", args={"product_id": 11}, cost=1.0))
    result = await supervisor.run(_ctx(odoo, model), "Is product 11 ok?")

    assert result["stop_reason"] == "cost_budget"
    assert [c for c in odoo.paths(READ) if c[2]["model"] == "onto.status"] == []
    assert odoo.paths(UPSERT) == []


async def test_output_over_the_option_limit_is_not_proposed():
    odoo = FakeOdoo()
    options = [{"option_key": f"a{i}", "action_type": "activity", "reason": "Check."} for i in range(4)]
    model = ScriptedModel(Turn(output={"answer": "Check it.", "options": options}))
    result = await supervisor.run(_ctx(odoo, model), "Is product 11 ok?")

    assert result["stop_reason"] == "invalid_output"
    assert odoo.paths(UPSERT) == []


async def test_no_playbook_means_no_model_call():
    odoo = FakeOdoo()
    model = ScriptedModel(repeat=Turn(output=ANSWER))
    result = await supervisor.run(_ctx(odoo, model, FakeDecider("NONE")), "Tell me a joke.")

    assert result["stop_reason"] == "no_playbook"
    assert model.calls == 0


async def test_the_decision_trace_names_provider_model_and_confidence_but_not_the_question():
    async def decider(question, options, state, *, decision_type, run_id):
        return {"answer": "NONE", "confidence": 0.42, "provider": "openai_fallback", "model": "gpt-5.4-mini",
                "probability": None}

    odoo = FakeOdoo()
    result = await supervisor.run(_ctx(odoo, ScriptedModel(), decider), "Tell me a joke.")

    [entry] = [e for e in result["trace"] if e["type"] == "decision"]
    assert (entry["provider"], entry["model"], entry["confidence"]) == ("openai_fallback", "gpt-5.4-mini", 0.42)
    assert "Tell me a joke" not in repr(result["trace"]) and supervisor.TRIAGE_QUESTION not in repr(result["trace"])
