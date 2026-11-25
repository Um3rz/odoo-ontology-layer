"""The Builder's match and propose step (plan 2.3, D52) on the hand-written seed fixture (fixtures/builder_seed.json).

Pure tests call the Builder directly. Route tests run on the scratch database: the steward stores proposals, a viewer
gets 404. The model is always a fake client, so no test reaches the Anthropic or the OpenAI API.
"""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db
from app.db.onto import OntoProposal, Tenant
from app.db.tenant import scoped
from app.onto_routes import mount
from app.onto_routes import ontology as onto_routes
from app.ontology import builder, loader, store
from app.ontology.builder import infer, llm, match
from app.ontology.builder.profile import InputError, Introspect, Profile
from app.ontology.schema import Template
from app.utils import browser_auth as ba

SEED = json.loads((Path(__file__).parent / "fixtures" / "builder_seed.json").read_text(encoding="utf-8"))
BODY = {"introspect": SEED["introspect"], "profile": SEED["profile"]}
STEWARD = "odoo_dashboards_agents.group_onto_steward"
ODOO_A = "https://a.example.test"
UUID_A = "uuid-a"
WHOAMI = {
    "steward.sig": {"user": {"id": 7}, "groups": [STEWARD], "database": {"uuid": UUID_A}, "ai_enabled": True},
    "viewer.sig": {"user": {"id": 8}, "groups": [], "database": {"uuid": UUID_A}, "ai_enabled": True},
}
A_STEWARD = {"Authorization": "Bearer steward.sig", "X-Odoo-Url": ODOO_A}
A_VIEWER = {"Authorization": "Bearer viewer.sig", "X-Odoo-Url": ODOO_A}


def intro() -> Introspect:
    return Introspect(SEED["introspect"])


def prof() -> Profile:
    return Profile(SEED["profile"])


def custom_field(key: str, field: str, role: str = "dimension", confidence: float = 0.7) -> dict:
    return {"kind": "custom_field", "confidence": confidence, "payload": {
        "object": "stock_position",
        "property": {"key": key, "label": key.title(), "role": role, "synonyms": [key], "bindings": {"16": field}}}}


GOOD = custom_field("grade", "x_grade")
BAD_ROLE = custom_field("shelf", "x_grade", role="banana")
MISSING = custom_field("ghost", "x_missing")


class FakeMessages:
    def __init__(self, proposals):
        self.proposals = proposals
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="tool_use", name=llm.TOOL_NAME, input={"proposals": self.proposals})
        return SimpleNamespace(content=[block])


@pytest.fixture()
def fake_model(monkeypatch):
    """Install a fake Anthropic client that answers every call with these proposals. Returns the recorder."""
    def install(proposals):
        messages = FakeMessages(proposals)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(messages=messages))
        return messages
    return install


class FakeResponses:
    def __init__(self, proposals):
        self.proposals = proposals
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        call = SimpleNamespace(type="function_call", name=llm.TOOL_NAME,
                               arguments=json.dumps({"proposals": self.proposals}))
        return SimpleNamespace(output=[call])


@pytest.fixture()
def fake_openai(monkeypatch):
    """Install a fake OpenAI client that answers every call with a forced function tool call. Returns the recorder."""
    def install(proposals):
        responses = FakeResponses(proposals)
        monkeypatch.setenv("ONTO_BUILDER_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(llm, "_openai_client", lambda: SimpleNamespace(responses=responses))
        return responses
    return install


@pytest.fixture(autouse=True)
def no_llm_env(monkeypatch):
    """No test reads the developer's provider, model or key settings. Each test sets the ones it needs."""
    for name in ("ONTO_BUILDER_PROVIDER", "ONTO_LLM_PROVIDER", "ONTO_BUILDER_MODEL", "ONTO_LLM_MODEL",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# --- templates: deterministic matching ---------------------------------------------------------


def test_template_confidence_and_property_statuses():
    results = {t["template"]: t for t in match.match_templates(
        loader._read_templates(loader.TEMPLATE_DIR, []), loader.load_release(), intro(), prof())}
    assert set(results) == {"core", "inventory", "purchase", "pos", "sales", "org", "account", "crm"}
    inventory = results["inventory"]
    assert inventory["confidence"] == 0.29 and inventory["proposed"] is False
    objects = {o["object"]: o for o in inventory["objects"]}
    assert objects["stock_position"]["confidence"] == 0.93
    assert objects["stock_lot"]["confidence"] == 0.5
    assert objects["stock_move"]["confidence"] == 0.57
    assert objects["stock_valuation"]["confidence"] == 0.0
    props = {p["property"]: p for p in objects["stock_position"]["properties"]}
    assert props["lot"] == {"property": "lot", "binding": "lot_id", "status": "matched", "fill_rate": 0.53}
    assert props["product_category"]["binding"] == "product_id.product_tmpl_id.categ_id"
    assert props["product_category"]["status"] == "matched"
    assert props["location_role"]["binding"] == "location_id" and props["location_role"]["status"] == "matched"
    # Profiled on 1201 rows with no value set: a valid binding, not "holds no data" (2.9f).
    assert props["count_diff"]["status"] == "matched" and props["count_diff"]["fill_rate"] == 0.0
    moves = {p["property"]: p for p in objects["stock_move"]["properties"]}
    assert moves["operation_code"]["status"] == "missing_field"
    assert moves["done_quantity"]["status"] == "matched"


def test_a_measure_bound_to_a_date_is_a_type_mismatch():
    status, _, problem = match.check_binding(intro(), prof(), "stock.quant", "in_date", "measure")
    assert status == "type_mismatch" and "datetime" in problem


def reply(model: str, *fields: tuple) -> Introspect:
    """A minimal 2.1 reply: one model with (name, type, stored) fields."""
    return Introspect({"odoo_version": "16.0", "warehouses": [], "locations": [], "models": [{"model": model, "fields": [
        {"name": n, "type": t, "store": s} for n, t, s in fields]}]})


def profile_of(model: str, row_count: int, **fill_rates: float) -> Profile:
    """A minimal 2.2 reply: one model with its row count and each field's fill rate."""
    return Profile({"models": [{"model": model, "row_count": row_count,
                                "fields": [{"name": n, "fill_rate": r} for n, r in fill_rates.items()]}]})


def test_a_flag_or_date_with_no_values_set_is_matched_not_no_data():
    intro_ = reply("stock.quant", ("inventory_quantity_set", "boolean", True), ("in_date", "datetime", True))
    prof_ = profile_of("stock.quant", 1201, inventory_quantity_set=0.0, in_date=0.0)
    assert match.check_binding(intro_, prof_, "stock.quant", "inventory_quantity_set", "status") == ("matched", 0.0, None)
    assert match.check_binding(intro_, prof_, "stock.quant", "in_date", "time") == ("matched", 0.0, None)


def test_only_a_known_empty_model_holds_no_data():
    intro_ = reply("stock.quant", ("in_date", "datetime", True))
    status, _, problem = match.check_binding(intro_, profile_of("stock.quant", 0, in_date=0.0), "stock.quant", "in_date", "time")
    assert status == "no_data" and problem == "stock.quant has no rows"
    assert match.check_binding(intro_, Profile({"models": []}), "stock.quant", "in_date", "time") == ("matched", None, None)
    assert match.check_binding(intro_, profile_of("stock.quant", 10), "stock.quant", "in_date", "time") == ("matched", None, None)


def test_an_optional_property_the_odoo_does_not_store_is_left_out_of_the_match():
    sales = next(t for t in loader._read_templates(loader.TEMPLATE_DIR, []) if t.name == "sales")
    line = next(o for o in sales.objects if o.key == "sales_order_line")
    unit_cost = next(p for p in line.properties if p.key == "unit_cost")
    assert unit_cost.optional is True and "optional" not in unit_cost.model_dump(mode="json")  # not in the digest
    # Odoo without sale_margin: purchase_price is not stored, price_unit is.
    intro_ = reply("sale.order.line", ("price_unit", "float", True), ("purchase_price", "float", False))
    rows = {r["property"]: r for r in match._property_rows(line, loader.load_release(), intro_, profile_of(
        "sale.order.line", 10, price_unit=1.0))}
    assert "unit_cost" not in rows and rows["price"]["status"] == "matched"
    assert rows["net_amount"]["status"] == "missing_field"  # a required field that is absent is still a gap


def test_a_template_whose_objects_all_match_is_proposed():
    tiny = Template.model_validate({"name": "tiny", "objects": [{
        "key": "location", "label": "Location", "domain": "inventory.stock", "base_model": "stock.location",
        "synonyms": ["location"],
        "properties": [{"key": "usage", "label": "Usage", "role": "status", "bindings": {"16": "usage"},
                        "synonyms": ["usage"]}]}]})
    [result] = match.match_templates([tiny], loader.load_release(), intro(), prof())
    assert result["confidence"] == 1.0 and result["proposed"] is True


def test_only_odoo_16_is_read():
    with pytest.raises(InputError):
        builder.run({**SEED["introspect"], "odoo_version": "17.0"}, SEED["profile"])


# --- OrgMap draft ---------------------------------------------------------------------------------


def test_the_qc_location_is_drafted_as_qc_hold():
    drafts = {d["location_id"]: d for d in match.orgmap_draft(intro())}
    assert drafts[8]["proposed_role"] == "qc_hold"
    assert drafts[8]["confidence"] == 0.9 and drafts[8]["ambiguous"] is False
    assert drafts[7]["proposed_role"] == "branch"  # the warehouse's main stock location
    assert 3 not in drafts  # a customer location is not stock, so it is not drafted


def test_ambiguous_locations_are_flagged_for_the_steward():
    drafts = {d["location_id"]: d for d in match.orgmap_draft(intro())}
    shelf = drafts[9]  # no rule matches
    assert shelf["ambiguous"] is True and shelf["proposed_role"] is None and shelf["candidates"] == []
    both = drafts[10]  # two rules match: qc and damaged
    assert both["ambiguous"] is True and both["proposed_role"] is None
    assert both["candidates"] == ["damaged", "qc_hold"]


def test_an_org_row_is_batch_approvable_only_at_085_and_unambiguous():
    def draft(confidence, ambiguous):
        return {"location_id": 8, "complete_name": "WH/Stock/QC", "proposed_role": None if ambiguous else "qc_hold",
                "confidence": confidence, "ambiguous": ambiguous, "candidates": [],
                "problem": "no rule matches" if ambiguous else None}
    rows = builder._org_rows([draft(0.9, False), draft(0.8, False), draft(0.9, True)], "run-1")
    assert [row["batch_approvable"] for row in rows] == [True, False, False]
    assert [row["payload"]["batch_approvable"] for row in rows] == [True, False, False]
    assert rows[2]["validation"] == {"ok": False, "problems": ["no rule matches"]}


def test_a_template_fit_is_never_batch_approvable():
    [row] = builder._template_rows([{"template": "core", "confidence": 0.9, "proposed": True, "objects": []}], "run-1")
    assert row["batch_approvable"] is False and row["payload"]["batch_approvable"] is False


# --- LLM step (fake client only) -----------------------------------------------------------------


def test_a_custom_field_gets_an_llm_proposal_that_is_not_batch_approvable(fake_model):
    messages = fake_model([GOOD])
    body, _ = builder.run(SEED["introspect"], SEED["profile"])
    assert body["llm"] == "ok (anthropic)"
    assert len(messages.calls) == 1  # only inventory is asked: sales has no models here, and accounting and crm have no templates
    [proposal] = body["llm_proposals"]
    assert proposal["node_key"] == "stock_position.grade" and proposal["domain"] == "inventory"
    assert proposal["validation"] == {"ok": True, "problems": []}
    assert proposal["batch_approvable"] is False


def test_without_an_api_key_the_llm_step_is_skipped(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: pytest.fail("the model must not be called without a key"))
    body, rows = builder.run(SEED["introspect"], SEED["profile"])
    assert body["llm"] == "skipped: no API key (ANTHROPIC_API_KEY)" and body["llm_proposals"] == []
    assert {row["payload"]["source"] for row in rows} == {"builder", "infer"}  # infer runs without the LLM
    assert body["inferred_proposals"] and all(row in rows for row in body["inferred_proposals"])


def test_openai_is_chosen_by_env_and_gets_the_same_forced_tool_call(fake_openai):
    completions = fake_openai([GOOD])
    body, _ = builder.run(SEED["introspect"], SEED["profile"])
    assert body["llm"] == "ok (openai)"
    [proposal] = body["llm_proposals"]
    assert proposal["node_key"] == "stock_position.grade" and proposal["validation"]["ok"] is True
    [call] = completions.calls
    assert call["model"] == "gpt-5.4-mini" and call["max_output_tokens"] == llm.MAX_OUTPUT and call["store"] is False
    assert call["tool_choice"] == {"type": "function", "name": llm.TOOL_NAME}
    assert call["tools"] == [{"type": "function", "name": llm.TOOL_NAME, "description": llm.TOOL["description"],
                              "parameters": llm.tool_for(json.loads(call["input"]))["input_schema"], "strict": False}]
    assert call["instructions"] == llm.SYSTEM
    assert "x_grade" in call["input"]


@pytest.mark.parametrize("env, provider, model", [
    ({}, "anthropic", "claude-sonnet-5-5"),
    ({"ONTO_LLM_MODEL": "gpt-x"}, "anthropic", "claude-sonnet-5-5"),  # ONTO_LLM_MODEL applies only to openai
    ({"ONTO_LLM_PROVIDER": "openai"}, "openai", "gpt-5.4-mini"),
    ({"ONTO_LLM_PROVIDER": "openai", "ONTO_LLM_MODEL": "gpt-x"}, "openai", "gpt-x"),
    ({"ONTO_BUILDER_PROVIDER": "openai", "ONTO_BUILDER_MODEL": "gpt-y", "ONTO_LLM_MODEL": "gpt-x"}, "openai", "gpt-y"),
    ({"ONTO_BUILDER_PROVIDER": "anthropic", "ONTO_LLM_PROVIDER": "openai", "ONTO_LLM_MODEL": "gpt-x"},
     "anthropic", "claude-sonnet-5-5"),
    ({"ONTO_BUILDER_PROVIDER": "anthropic", "ONTO_BUILDER_MODEL": "claude-x"}, "anthropic", "claude-x"),
])
def test_provider_and_model_come_from_the_env(monkeypatch, env, provider, model):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert llm.provider() == provider and llm.model_name() == model


def test_an_unknown_provider_is_an_error_not_a_fallback(monkeypatch):
    monkeypatch.setenv("ONTO_BUILDER_PROVIDER", "openia")
    with pytest.raises(ValueError):
        llm.provider()


def test_the_key_checked_belongs_to_the_chosen_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    assert llm.api_key_present() is True
    monkeypatch.setenv("ONTO_BUILDER_PROVIDER", "openai")
    assert llm.api_key_present() is False and llm.key_env() == "OPENAI_API_KEY"
    monkeypatch.setattr(llm, "_openai_client", lambda: pytest.fail("the model must not be called without a key"))
    body, _ = builder.run(SEED["introspect"], SEED["profile"])
    assert body["llm"] == "skipped: no API key (OPENAI_API_KEY)" and body["llm_proposals"] == []


def test_no_record_values_reach_the_model(fake_model):
    messages = fake_model([GOOD])
    builder.run(SEED["introspect"], SEED["profile"])
    call = messages.calls[0]
    content = call["messages"][0]["content"]
    sent = content + json.dumps({k: v for k, v in call.items() if k != "messages"}, default=str)
    for value in ("2026-02-14", "2026-10-30", "2026-01-03", "2026-11-01", "2026-03-09", "2027-06-30"):
        assert value not in sent  # the profile's min and max dates are never sent
    assert "x_grade" in content and '"fill_rate": 0.4' in content  # metadata does go


def test_an_invalid_llm_proposal_is_kept_with_ok_false_but_a_guessed_field_is_dropped(fake_model):
    fake_model([GOOD, BAD_ROLE, MISSING])
    body, _ = builder.run(SEED["introspect"], SEED["profile"])
    by_key = {p["node_key"]: p for p in body["llm_proposals"]}
    assert set(by_key) == {"stock_position.grade", "stock_position.shelf"}  # the guessed x_missing is dropped (2.11)
    assert by_key["stock_position.grade"]["validation"]["ok"] is True
    shelf = by_key["stock_position.shelf"]["validation"]
    assert shelf["ok"] is False and shelf["problems"][0].startswith("role:")


def test_the_tool_schema_states_the_rules_the_checks_enforce():
    schema = llm.TOOL["input_schema"]
    defs = schema["$defs"]
    assert defs["Property"]["properties"]["key"]["pattern"] == "^[a-z][a-z0-9_]*$"
    assert set(defs["Property"]["properties"]["role"]["enum"]) == {"id", "dimension", "measure", "time", "status", "link"}
    assert set(defs["Metric"]["properties"]["unit"]["enum"]) == {"qty", "money", "count", "days", "ratio", "pct"}
    for name in ("Object", "Property", "Metric"):
        assert "synonyms" in defs[name]["required"]
    assert "count" in defs["Metric"]["properties"]["measure"]["description"]
    assert schema["properties"]["proposals"]["maxItems"] == llm.MAX_PROPOSALS


def test_a_count_metric_with_a_measure_is_kept_with_the_problem(fake_model):
    bad = {"kind": "metric", "confidence": 0.8, "payload": {
        "key": "bad_count", "label": "Bad count", "object": "stock_position", "unit": "count",
        "additivity": "additive", "aggregation": "count", "measure": "quantity", "synonyms": ["bad count"]}}
    fake_model([bad])
    body, _ = builder.run(SEED["introspect"], SEED["profile"])
    [proposal] = body["llm_proposals"]
    assert proposal["validation"]["ok"] is False
    assert "a count takes no measure" in proposal["validation"]["problems"][0]


# --- metric measures are release measure keys (2.10a) --------------------------------------------


def metric_payload(obj: str, measure: str) -> dict:
    return {"key": "moved", "label": "Moved", "object": obj, "unit": "qty", "additivity": "additive",
            "aggregation": "sum", "measure": measure, "synonyms": ["moved"]}


def test_a_metric_measure_is_a_measure_key_or_the_odoo_field_it_binds():
    intro_ = odoo({"stock.move": [("quantity_done", "float", True, False), ("product_uom_qty", "float", True, False)],
                   "sale.order": [("amount_total", "monetary", True, False)]})
    prof_ = Profile({"models": [
        {"model": "stock.move", "row_count": 4, "fields": [{"name": "quantity_done", "fill_rate": 0.5},
                                                           {"name": "product_uom_qty", "fill_rate": 1.0}]},
        {"model": "sale.order", "row_count": 4, "fields": [{"name": "amount_total", "fill_rate": 1.0}]}]})
    stored, problems = match._check_metric(metric_payload("stock_move", "quantity_done"), RELEASE, intro_, prof_)
    assert problems == [] and stored["measure"] == "done_quantity"
    stored, problems = match._check_metric(metric_payload("stock_move", "product_uom_qty"), RELEASE, intro_, prof_)
    assert problems == [] and stored["measure"] == "demand_quantity"
    stored, problems = match._check_metric(metric_payload("sales_order", "amount_total"), RELEASE, intro_, prof_)
    assert problems == [] and stored["measure"] == "total"
    _, problems = match._check_metric(metric_payload("stock_move", "subtotal"), RELEASE, intro_, prof_)
    assert len(problems) == 1 and "'subtotal' is not a measure of stock_move" in problems[0]
    assert "demand_quantity, done_quantity" in problems[0]  # the reason names the measures the object has


def test_the_tool_offers_only_measure_keys_for_a_metric_measure():
    tool = llm.tool_for({"models": [], "measures": {"stock_move": ["done_quantity"], "sales_order": ["total"]}})
    assert tool["input_schema"]["$defs"]["Metric"]["properties"]["measure"]["anyOf"] == [
        {"enum": ["done_quantity", "total"]}, {"type": "null"}]


def test_a_metric_whose_measure_is_an_odoo_field_is_stored_under_the_measure_key(fake_model):
    fake_model([{"kind": "metric", "confidence": 0.9, "payload": metric_payload("stock_move", "quantity_done")}])
    body, _ = builder.run(SEED["introspect"], SEED["profile"])
    [proposal] = body["llm_proposals"]
    assert proposal["validation"] == {"ok": True, "problems": []}
    assert proposal["payload"]["node"]["measure"] == "done_quantity"


def test_a_proposal_whose_key_is_taken_or_whose_domain_is_not_allowed_is_dropped(fake_model):
    taken = {"kind": "object", "confidence": 0.9, "payload": {
        "key": "partner", "label": "Partner", "domain": "inventory.stock", "base_model": "stock.quant",
        "synonyms": ["partner"]}}
    unknown_domain = {"kind": "object", "confidence": 0.9, "payload": {
        "key": "quant_rogue", "label": "Rogue", "domain": "nowhere", "base_model": "stock.quant", "synonyms": ["rogue"]}}
    other_model = {"kind": "object", "confidence": 0.9, "payload": {
        "key": "journal_line", "label": "Journal line", "domain": "inventory.stock", "base_model": "account.move.line",
        "synonyms": ["journal line"]}}
    fake_model([taken, unknown_domain, other_model, GOOD])
    body, _ = builder.run(SEED["introspect"], SEED["profile"])
    assert [p["node_key"] for p in body["llm_proposals"]] == ["stock_position.grade"]


def test_a_domain_without_templates_is_not_asked(fake_model):
    messages = fake_model([GOOD])
    body, _ = builder.run(SEED["introspect"], SEED["profile"], ["accounting", "crm"])
    assert messages.calls == [] and body["llm_proposals"] == [] and body["llm"] == "ok (anthropic)"


def test_the_call_names_the_objects_already_in_the_release(fake_model):
    messages = fake_model([GOOD])
    builder.run(SEED["introspect"], SEED["profile"])
    payload = json.loads(messages.calls[0]["messages"][0]["content"])
    assert "partner" in payload["covered"] and "stock_position" in payload["covered"]
    assert "stock_position" in payload["matched_template_objects"]
    assert "partner" not in payload["matched_template_objects"]  # the seed has no res.partner, so it did not match


# --- what the model is offered and what is dropped (2.9b, 2.9c, 2.9d) ------------------------------

FIXTURE = loader.load_fields()
RELEASE = loader.load_release()


def odoo(models: dict) -> Introspect:
    """A minimal introspect: model -> [(field, type, stored, custom)]."""
    return Introspect({"odoo_version": "16.0", "models": [
        {"model": model, "name": model, "custom": False, "fields": [
            {"name": name, "string": name, "type": kind, "store": stored, "custom": custom}
            for name, kind, stored, custom in fields]}
        for model, fields in models.items()]})


def prop(key: str, path: str) -> dict:
    return {"key": key, "label": key, "role": "dimension", "synonyms": [key], "bindings": {"16": path}}


def object_entry(key: str, base: str, properties: tuple = ()) -> dict:
    return {"kind": "object", "confidence": 0.9, "payload": {
        "key": key, "label": key.title(), "domain": "inventory.stock", "base_model": base,
        "synonyms": [key], "properties": list(properties)}}


def test_a_model_with_no_rows_is_not_offered_and_an_unprofiled_one_is():
    intro_ = odoo({"stock.quant": [("quantity", "float", True, False)],
                   "stock.location": [("complete_name", "char", True, False)],
                   "stock.warehouse": [("name", "char", True, False)]})
    prof_ = Profile({"models": [{"model": "stock.quant", "row_count": 0, "fields": []},
                                {"model": "stock.location", "row_count": 14, "fields": []}]})
    payload = llm.domain_input("inventory", intro_, prof_, [], [], [], FIXTURE)
    assert [m["model"] for m in payload["models"]] == ["stock.location", "stock.warehouse"]


def test_only_models_and_fields_the_compiler_fixture_holds_are_offered():
    intro_ = odoo({
        "stock.package_level": [("company_id", "many2one", True, False)],  # not in the fixture
        "stock.location": [("complete_name", "char", True, False),  # in the fixture
                           ("name", "char", True, False),  # stored, not in the fixture
                           ("display_name", "char", False, False),  # not stored
                           ("x_zone", "char", True, True)],  # custom: offered, checked against introspect
    })
    payload = llm.domain_input("inventory", intro_, Profile({"models": []}), [], [], [], FIXTURE)
    [location] = payload["models"]
    assert location["model"] == "stock.location"
    assert [f["name"] for f in location["fields"]] == ["x_zone", "complete_name"]


def test_the_tool_lets_the_model_name_only_the_models_it_was_offered(fake_model):
    messages = fake_model([GOOD])
    builder.run(SEED["introspect"], SEED["profile"])
    call = messages.calls[0]
    offered = [m["model"] for m in json.loads(call["messages"][0]["content"])["models"]]
    base_model = call["tools"][0]["input_schema"]["$defs"]["Object"]["properties"]["base_model"]
    assert base_model["enum"] == sorted(offered)
    assert "enum" not in llm.TOOL["input_schema"]["$defs"]["Object"]["properties"]["base_model"]  # the shared tool is untouched


def test_an_object_on_a_base_model_a_template_object_already_maps_is_dropped():
    reason = match.out_of_scope(object_entry("quant_summary", "stock.quant"), "inventory", RELEASE, FIXTURE)
    assert reason == "model stock.quant is already covered by stock_position"


def test_an_object_the_compiler_cannot_read_is_dropped_with_the_reason():
    package_level = object_entry("package_level", "stock.package_level")
    assert match.out_of_scope(package_level, "inventory", RELEASE, FIXTURE) == \
        "model stock.package_level is not in the Odoo 16 fixture"
    barcode = object_entry("product_barcode", "product.template", [prop("barcode", "barcode")])
    assert "product.template.barcode is not in the Odoo 16 fixture" in \
        match.out_of_scope(barcode, "inventory", RELEASE, FIXTURE)
    via_uom = object_entry("unit", "product.template", [prop("unit_name", "uom_id.name")])
    assert match.out_of_scope(via_uom, "inventory", RELEASE, FIXTURE) == "model uom.uom is not in the Odoo 16 fixture"


def test_a_field_missing_from_the_fixture_or_not_stored_is_named_by_the_fixture_check():
    problem = match._outside_fixture({"base_model": "product.product", "properties": [prop("name", "display_name")]},
                                     FIXTURE)
    assert problem == "product.product.display_name is not in the Odoo 16 fixture (bound fields only; re-export to add it)"


def test_an_uncovered_object_on_a_fixture_model_is_kept():
    kept = object_entry("transfer_type", "stock.picking.type", [prop("code", "code")])
    assert match.out_of_scope(kept, "inventory", RELEASE, FIXTURE) is None


def test_a_custom_field_is_checked_against_introspect_not_the_fixture():
    assert match.out_of_scope(GOOD, "inventory", RELEASE, FIXTURE) is None


# --- metrics on a dropped object follow it to the release object (2.10b) ----------------------------


def metric_on(key: str, obj: str) -> dict:
    return {"kind": "metric", "confidence": 0.9, "payload": {
        "key": key, "label": key, "object": obj, "unit": "qty", "additivity": "additive", "aggregation": "sum",
        "measure": "quantity", "synonyms": [key]}}


def test_a_metric_on_a_dropped_object_moves_to_the_release_object_with_its_model():
    entries = [object_entry("quants", "stock.quant"), object_entry("transfers", "stock.picking"),
               object_entry("valuation_layer", "stock.valuation.layer"),
               metric_on("quant_qty", "quants"), metric_on("transfer_count", "transfers"),
               metric_on("layer_count", "valuation_layer")]
    settled, reasons = match.settle(entries, "inventory", RELEASE, FIXTURE, intro())
    assert reasons[:3] == ["model stock.quant is already covered by stock_position",
                           "model stock.picking is already covered by transfer",
                           "model stock.valuation.layer is already covered by stock_valuation"]
    assert reasons[3:] == [None, None, None]
    assert [entry["payload"]["object"] for entry in settled[3:]] == ["stock_position", "transfer", "stock_valuation"]


def test_a_metric_with_no_release_object_for_its_object_is_dropped_with_the_reason():
    entries = [object_entry("package_level", "stock.package_level"), metric_on("line_qty", "package_level"),
               metric_on("ghost_qty", "ghosts")]
    _, reasons = match.settle(entries, "inventory", RELEASE, FIXTURE, intro())
    assert reasons == ["model stock.package_level is not in the Odoo 16 fixture",
                       "object 'package_level' is not in the release, and no release object has its base model",
                       "object 'ghosts' is not in the release, and no release object has its base model"]


def test_a_metric_on_an_object_kept_in_the_same_call_is_left_to_the_checks():
    entries = [object_entry("transfer_type", "stock.picking.type"), metric_on("type_count", "transfer_type")]
    settled, reasons = match.settle(entries, "inventory", RELEASE, FIXTURE, intro())
    assert reasons == [None, None] and settled[1]["payload"]["object"] == "transfer_type"


def test_a_metric_on_a_dropped_object_is_stored_on_the_release_object(fake_model):
    fake_model([object_entry("quants", "stock.quant"), metric_on("quant_qty", "quants")])
    body, _ = builder.run(SEED["introspect"], SEED["profile"])
    [proposal] = body["llm_proposals"]
    assert proposal["node_key"] == "quant_qty" and proposal["validation"] == {"ok": True, "problems": []}
    assert proposal["payload"]["node"]["object"] == "stock_position"


# --- custom fields name a release object (2.10c) --------------------------------------------------


def custom_field_on(key: str, obj: str, field: str) -> dict:
    return {"kind": "custom_field", "confidence": 0.8, "payload": {"object": obj, "property": {
        "key": key, "label": key, "role": "dimension", "synonyms": [key], "bindings": {"16": field}}}}


def test_a_custom_field_names_the_release_object_for_its_model_or_for_its_one_field():
    intro_ = odoo({"res.partner": [("x_contract_type", "char", True, True)],
                   "stock.quant": [("x_grade", "char", True, True)], "stock.lot": [("x_grade", "char", True, True)]})
    entries = [custom_field_on("contract", "res.partner", "x_contract_type"),  # a model name
               custom_field_on("contract_kind", "contact", "x_contract_type"),  # an unknown key, one field owner
               custom_field_on("grade_tag", "buyer", "x_grade"),  # an unknown key, two owners: ambiguous
               custom_field_on("zone", "contact_zone", "x_zone")]  # no owner at all
    settled, reasons = match.settle(entries, "contacts", RELEASE, FIXTURE, intro_)
    assert reasons == [None, None, "object 'buyer' is not in the release, and no release object has its base model",
                       "object 'contact_zone' is not in the release, and no release object has its base model"]
    assert [entry["payload"]["object"] for entry in settled[:2]] == ["partner", "partner"]


def test_the_tool_offers_only_release_objects_for_a_metric_or_a_custom_field():
    tool = llm.tool_for({"models": [], "covered": ["stock_position", "partner"], "measures": {}})
    defs = tool["input_schema"]["$defs"]
    assert defs["CustomField"]["properties"]["object"]["enum"] == ["partner", "stock_position"]
    assert defs["Metric"]["properties"]["object"]["enum"] == ["partner", "stock_position"]


def test_every_proposal_requires_a_confidence_from_0_to_1():
    defs = llm.TOOL["input_schema"]["$defs"]
    for name in ("ObjectProposal", "MetricProposal", "TermProposal", "CustomFieldProposal"):
        assert "confidence" in defs[name]["required"]
        assert defs[name]["properties"]["confidence"]["minimum"] == 0 and defs[name]["properties"]["confidence"]["maximum"] == 1


def test_a_missing_or_out_of_range_confidence_is_defaulted_or_clamped(fake_model):
    no_confidence = {k: v for k, v in GOOD.items() if k != "confidence"}
    fake_model([no_confidence, {**GOOD, "confidence": 1.7}, {**GOOD, "confidence": -0.2},
                {**GOOD, "confidence": "high"}, {**GOOD, "confidence": True}, {**GOOD, "confidence": 0.8}])
    assert [p["confidence"] for p in llm.ask({})["proposals"]] == [0.5, 1.0, 0.0, 0.5, 0.5, 0.8]


def test_a_custom_field_remapped_to_its_release_object_keeps_its_confidence(fake_model):
    intro_ = odoo({"res.partner": [("x_contract_type", "char", True, True)]})
    fake_model([{"kind": "custom_field", "payload": {"object": "res.partner",
                                                     "property": prop("contract_type", "x_contract_type")}}])
    [entry] = llm.ask({})["proposals"]  # the reply gave no confidence: 0.5 (2.11)
    settled, reasons = match.settle([entry], "contacts", RELEASE, FIXTURE, intro_)
    assert reasons == [None] and settled[0]["payload"]["object"] == "partner" and settled[0]["confidence"] == 0.5


def test_a_custom_field_whose_field_the_introspect_does_not_hold_is_dropped_with_its_reason():
    guessed = custom_field_on("temperature_class", "product", "x_studio_temperature_zone")
    held = custom_field_on("temperature_class", "product", "x_studio_temperature_class")
    settled, reasons = match.settle([guessed, held], "inventory", RELEASE, FIXTURE,
                                    odoo({"product.product": [("x_studio_temperature_class", "char", True, True)]}))
    assert reasons == ["product.product.x_studio_temperature_zone is not a stored field in the Odoo introspect, "
                       "so the LLM guessed it", None]
    [reason] = match.settle([held], "inventory", RELEASE, FIXTURE,
                            odoo({"product.product": [("x_studio_temperature_class", "char", False, True)]}))[1]
    assert reason == "product.product.x_studio_temperature_class is not a stored field in the Odoo introspect, " \
                     "so the LLM guessed it"


def test_a_custom_field_the_seed_introspect_lacks_is_dropped_by_settle():
    [reason] = match.settle([MISSING], "inventory", RELEASE, FIXTURE, intro())[1]
    assert reason == "stock.quant.x_missing is not a stored field in the Odoo introspect, so the LLM guessed it"


# --- a steward's role for an ambiguous org row (2.9e) --------------------------------------------


def org_row(role: str | None = None, state: str = "proposed") -> OntoProposal:
    """An ambiguous org_location draft as builder._org_rows stores it: no role, validation failed."""
    return OntoProposal(tenant_id=uuid.uuid4(), node_key="org_location.7", state=state, confidence=0.0,
                        payload={"kind": "org_location", "source": "builder", "location_id": 7,
                                 "complete_name": "WH/Expired", "proposed_role": role, "ambiguous": role is None,
                                 "candidates": [], "problem": None if role else "no rule matches",
                                 "batch_approvable": False},
                        validation={"ok": role is not None, "problems": [] if role else ["no rule matches"]})


def test_a_steward_role_makes_an_ambiguous_org_row_approvable():
    row = org_row()
    with pytest.raises(HTTPException) as refused:
        onto_routes._approvable(row)
    assert refused.value.status_code == 422
    onto_routes._choose_role(row, "expired")
    onto_routes._approvable(row)  # no longer refused
    assert row.payload["proposed_role"] == "expired" and row.payload["ambiguous"] is False
    assert row.validation == {"ok": True, "problems": []}


def test_an_unknown_role_is_a_422_that_lists_the_release_roles():
    with pytest.raises(HTTPException) as caught:
        onto_routes._choose_role(org_row(), "banana")
    assert caught.value.status_code == 422 and caught.value.detail["error"] == "unknown_role"
    assert caught.value.detail["roles"] == ["central", "factory", "branch", "transit", "damaged", "qc_hold",
                                            "expired", "scrap"]


def test_a_role_applies_to_an_org_location_only():
    not_org = OntoProposal(tenant_id=uuid.uuid4(), node_key="sales_order", confidence=0.9,
                           payload={"kind": "object", "node": {"key": "sales_order"}}, validation={"ok": True})
    with pytest.raises(HTTPException) as caught:
        onto_routes._choose_role(not_org, "expired")
    assert caught.value.status_code == 422


def test_an_approved_row_keeps_the_role_it_was_approved_with():
    row = org_row(role="expired", state="approved")
    onto_routes._choose_role(row, "expired")  # the same role again is fine
    with pytest.raises(HTTPException) as caught:
        onto_routes._choose_role(row, "scrap")
    assert caught.value.status_code == 409 and row.payload["proposed_role"] == "expired"


# --- approved org rows are not offered again (2.9a) -----------------------------------------------


def test_approval_covers_the_approved_role_or_an_ambiguous_draft_only():
    assert builder.approval_covers({"proposed_role": "branch"}, "branch")
    assert not builder.approval_covers({"proposed_role": "branch"}, "damaged")
    assert builder.approval_covers({"proposed_role": None}, "expired")
    assert not builder.approval_covers({"proposed_role": "branch"}, None)


def test_a_location_approved_with_the_drafted_role_is_not_offered_again():
    body, rows = builder.run(SEED["introspect"], SEED["profile"], approved={7: "branch"})
    assert [d["location_id"] for d in body["orgmap_draft"]] == [8, 9, 10]
    assert "org_location.7" not in {row["node_key"] for row in rows}


def test_an_approved_location_is_offered_again_when_the_draft_names_another_role():
    body, rows = builder.run(SEED["introspect"], SEED["profile"], approved={7: "damaged"})
    assert 7 in [d["location_id"] for d in body["orgmap_draft"]]
    assert "org_location.7" in {row["node_key"] for row in rows}


def test_an_ambiguous_location_the_steward_chose_a_role_for_is_not_offered_again():
    body, _ = builder.run(SEED["introspect"], SEED["profile"], approved={9: "expired"})
    assert 9 not in [d["location_id"] for d in body["orgmap_draft"]]


# --- legacy duplicate approved org rows (2.10e) ----------------------------------------------------


def proposal_row(id_: int, node_key: str, state: str = "approved", kind: str = "org_location",
                 role: str | None = "branch") -> OntoProposal:
    return OntoProposal(id=id_, tenant_id=uuid.uuid4(), node_key=node_key, state=state,
                        payload={"kind": kind, "proposed_role": role}, confidence=0.9,
                        validation={"ok": True, "problems": []})


class FakeSession:
    """Answers every query with the rows it was given, and records the deletes and commits."""

    def __init__(self, rows):
        self.rows, self.deleted, self.commits = rows, [], 0

    def exec(self, statement):
        return SimpleNamespace(all=lambda: list(self.rows))

    def delete(self, row):
        self.deleted.append(row.id)

    def add(self, row):
        pass

    def commit(self):
        self.commits += 1


def test_the_list_shows_the_newest_approved_org_row_of_each_location():
    rows = [proposal_row(1, "org_location.7"), proposal_row(2, "org_location.7"),
            proposal_row(3, "org_location.7", state="proposed"), proposal_row(4, "org_location.8"),
            proposal_row(5, "sales_order", kind="object")]
    assert [row.id for row in onto_routes._newest_org_rows(rows)] == [2, 3, 4, 5]


def test_the_cleanup_deletes_the_older_approved_copy_of_the_same_location_and_role():
    rows = [proposal_row(1, "org_location.7"), proposal_row(2, "org_location.7"),
            proposal_row(3, "org_location.7", role="damaged"), proposal_row(4, "org_location.8"),
            proposal_row(5, "org_location.7", state="proposed"), proposal_row(6, "sales_order", kind="object"),
            proposal_row(7, "org_location.7")]
    session = FakeSession(rows)
    assert store.dedupe_approved_org_locations(session, uuid.uuid4()) == [1, 2]
    assert session.deleted == [1, 2] and session.commits == 1


def test_the_cleanup_leaves_a_location_approved_once_alone():
    session = FakeSession([proposal_row(1, "org_location.7"), proposal_row(2, "org_location.7", role="damaged")])
    assert store.dedupe_approved_org_locations(session, uuid.uuid4()) == []
    assert session.deleted == [] and session.commits == 1


# --- a release key approved again (2.12) ------------------------------------------------------------


def metric_node(key: str, label: str = "Scrap") -> dict:
    """A metric as the release reads it: a committed template metric under another key."""
    base = loader.load_release().metrics[0]
    return {**base.model_dump(mode="json"), "key": key, "label": label}


def release_row(id_: int, node_key: str, node: dict, state: str = "approved") -> OntoProposal:
    return OntoProposal(id=id_, tenant_id=uuid.uuid4(), node_key=node_key, state=state,
                        payload={"kind": "metric", "source": "llm", "node": node}, confidence=0.9,
                        validation={"ok": True, "problems": []})


class ReplaceSession(FakeSession):
    """Answers the two queries of replace_builder_proposals in turn: the proposed rows, then the approved ones."""

    def __init__(self, proposed, approved):
        super().__init__([])
        self.answers, self.added = [proposed, approved], []

    def exec(self, statement):
        rows = self.answers.pop(0)
        return SimpleNamespace(all=lambda: list(rows))

    def add_all(self, rows):
        self.added += rows


def test_identical_approved_copies_of_a_key_publish_once_from_the_newest():
    node = metric_node("scrap_total")
    session = FakeSession([release_row(1, "scrap_total", node), release_row(2, "scrap_total", node)])
    _, provenance, applied = onto_routes._release_with_approved(session, uuid.uuid4())
    assert applied == 1 and provenance["scrap_total"]["proposal_id"] == 2


def test_approved_copies_of_a_key_that_differ_are_refused_naming_both_ids():
    session = FakeSession([release_row(1, "scrap_total", metric_node("scrap_total")),
                           release_row(2, "scrap_total", metric_node("scrap_total", label="Scrap qty"))])
    with pytest.raises(HTTPException) as refused:
        onto_routes._release_with_approved(session, uuid.uuid4())
    problems = " ".join(refused.value.detail["problems"])
    assert "'scrap_total'" in problems and "1, 2" in problems and "revoke one" in problems


def test_the_cleanup_revokes_the_older_identical_copy_and_leaves_differing_copies():
    node = metric_node("scrap_total")
    same = FakeSession([release_row(1, "scrap_total", node), release_row(3, "scrap_total", node)])
    assert store.dedupe_approved_duplicate_keys(same, uuid.uuid4()) == [1]
    assert [row.state for row in same.rows] == ["revoked", "approved"] and same.commits == 1
    differ = FakeSession([release_row(1, "scrap_total", node),
                          release_row(2, "scrap_total", metric_node("scrap_total", label="Other"))])
    assert store.dedupe_approved_duplicate_keys(differ, uuid.uuid4()) == []
    assert [row.state for row in differ.rows] == ["approved", "approved"]


def test_approval_refuses_a_key_another_approved_proposal_or_the_templates_hold():
    node = metric_node("scrap_total")
    row = release_row(2, "scrap_total", node, state="proposed")
    held = onto_routes._held_by_other(FakeSession([release_row(1, "scrap_total", node), row]), uuid.uuid4(), row)
    assert held == "key 'scrap_total' is already held by approved proposal 1"
    assert onto_routes._held_by_other(FakeSession([row]), uuid.uuid4(), row) is None
    template = release_row(3, "on_hand_qty", node, state="proposed")
    assert onto_routes._held_by_other(FakeSession([]), uuid.uuid4(), template) == \
        "key 'on_hand_qty' is already in the release templates"


def test_the_builder_does_not_store_a_key_an_approved_proposal_holds():
    rows = [{"node_key": key, "payload": {"kind": "metric", "node": metric_node(key)}, "confidence": 0.9,
             "validation": {"ok": True, "problems": []}} for key in ("scrap_total", "other_total")]
    session = ReplaceSession([], [release_row(5, "scrap_total", metric_node("scrap_total"))])
    ids = store.replace_builder_proposals(session, uuid.uuid4(), rows)
    assert ids[0] == 5 and [row.node_key for row in session.added] == ["other_total"]


def test_the_builder_drops_a_metric_the_release_or_an_approved_proposal_already_has():
    release = loader.load_release()
    taken = match._release_keys(release)
    assert match._already_held({"kind": "metric", "payload": {"key": "on_hand_qty"}}, taken, set()) == \
        "key 'on_hand_qty' is already in the release or approved"
    fresh = {"kind": "metric", "payload": {"key": "new_total"}}
    assert match._already_held(fresh, taken, {"new_total"}) is not None
    assert match._already_held(fresh, taken, set()) is None


def test_a_metric_filter_must_be_a_rule_of_its_own_object():
    rule = loader.load_release().rules[0]
    metric = {"kind": "metric", "payload": {"key": "done_total", "object": "stock_move", "filters": ["status = 'done'"]}}
    assert "is not a rule of stock_move" in match._foreign_filter(metric, loader.load_release())
    own = {"kind": "metric", "payload": {"key": "own_total", "object": rule.object, "filters": [rule.key]}}
    assert match._foreign_filter(own, loader.load_release()) is None


# --- route and database (steward only) -----------------------------------------------------------


class FakeOdoo:
    """Stands in for the tenant's Odoo: whoami answers by token."""

    def __init__(self, base_url, token):
        self.token = token

    async def whoami(self):
        return WHOAMI[self.token]

    async def aclose(self):
        return None


@pytest.fixture()
def client(engine, session, clean_tenants, monkeypatch):
    monkeypatch.setattr(ba, "odoo_client_factory", FakeOdoo)
    monkeypatch.setattr(ba, "token_limiter", ba.RateLimiter(1000, 60))
    monkeypatch.setattr(ba, "tenant_limiter", ba.RateLimiter(1000, 60))
    ba._whoami_cache.clear()
    session.add(Tenant(odoo_base_url=ODOO_A, db_uuid=UUID_A))
    session.commit()

    def session_override():
        yield session

    app = FastAPI()
    app.dependency_overrides[db.get_session] = session_override
    mount(app)
    return TestClient(app)


@pytest.fixture()
def tenant_a(session):
    return session.exec(select(Tenant).where(Tenant.odoo_base_url == ODOO_A)).one()


def test_each_stored_row_is_named_in_the_reply():
    """Studio reads proposal_id off the reply: an LLM proposal and a proposed template carry the id of their row."""
    llm_row = {"node_key": "stock_position.grade", "confidence": 0.8, "payload": {}, "validation": {}}
    template_row = {"node_key": "template.core", "confidence": 0.9, "payload": {}, "validation": {}}
    body = {"template_proposals": [{"template": "core", "confidence": 0.9, "proposed": True},
                                   {"template": "pos", "confidence": 0.1, "proposed": False}],
            "llm_proposals": [llm_row]}
    builder.attach_proposal_ids(body, [template_row, llm_row], [11, 14])
    assert body["template_proposals"][0] == {"template": "core", "confidence": 0.9, "proposed": True, "proposal_id": 11}
    assert body["template_proposals"][1]["proposal_id"] is None
    assert body["llm_proposals"][0]["proposal_id"] == 14 and body["llm_proposals"][0]["confidence"] == 0.8


def test_only_stewards_can_run_the_builder(client):
    r = client.post("/ontology/builder/run", json=BODY, headers=A_VIEWER)
    assert r.status_code == 404 and r.json() == {"detail": "not found"}


def test_the_run_stores_unapproved_builder_rows_and_a_rerun_replaces_them(client, session, tenant_a, monkeypatch):
    r = client.post("/ontology/builder/run", json=BODY, headers=A_STEWARD)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["llm"] == "skipped: no API key (ANTHROPIC_API_KEY)" and out["odoo_version"] == "16.0"
    assert {t["template"] for t in out["template_proposals"]} == set(loader.TEMPLATE_NAMES)
    inferred = len(out["inferred_proposals"])
    assert len(out["proposal_ids"]) == 4 + inferred  # one draft per location 7, 8, 9 and 10, and the inferred rows
    rows = session.exec(scoped(OntoProposal, tenant_a.id)).all()
    assert sorted(row.node_key for row in rows if row.node_key.startswith("org_location.")) == [
        "org_location.10", "org_location.7", "org_location.8", "org_location.9"]
    assert {row.payload["source"] for row in rows} == {"builder", "infer"}
    assert all(row.state == "proposed" for row in rows)
    again = client.post("/ontology/builder/run", json=BODY, headers=A_STEWARD)
    assert len(again.json()["proposal_ids"]) == 4 + inferred
    assert len(session.exec(scoped(OntoProposal, tenant_a.id)).all()) == 4 + inferred
    stored = {row.id for row in session.exec(scoped(OntoProposal, tenant_a.id)).all()}
    assert all(result["proposal_id"] is None for result in again.json()["template_proposals"]
               if not result["proposed"])
    assert all(result["proposal_id"] in stored for result in again.json()["template_proposals"]
               if result["proposed"])


def test_an_approved_org_row_is_kept_and_not_offered_again_on_a_rerun(client, session, tenant_a):
    client.post("/ontology/builder/run", json=BODY, headers=A_STEWARD)
    [org] = session.exec(scoped(OntoProposal, tenant_a.id).where(OntoProposal.node_key == "org_location.7")).all()
    assert client.post(f"/ontology/proposals/{org.id}/approve", headers=A_STEWARD).status_code == 200
    again = client.post("/ontology/builder/run", json=BODY, headers=A_STEWARD).json()
    assert 7 not in [d["location_id"] for d in again["orgmap_draft"]]
    stored = session.exec(scoped(OntoProposal, tenant_a.id).where(OntoProposal.node_key == "org_location.7")).all()
    assert [(row.id, row.state) for row in stored] == [(org.id, "approved")]


# --- link properties and as_of metrics validate -----------------------------------------------------


TEMPLATE_OBJECT = {"kind": "object", "confidence": 0.9, "payload": {
    "key": "product_template", "label": "Product template", "domain": "inventory.stock",
    "base_model": "product.template", "synonyms": ["template"], "properties": [
        {"key": "name", "label": "Name", "role": "dimension", "synonyms": ["name"], "bindings": {"16": "name"}},
        {"key": "category", "label": "Category", "role": "link", "synonyms": ["category"],
         "bindings": {"16": "categ_id"}}]}}


def test_a_many2one_link_property_gets_a_link_that_publishes_with_the_object():
    row = match.llm_row(TEMPLATE_OBJECT, "inventory", "run", RELEASE, intro(), prof())
    assert row["validation"] == {"ok": True, "problems": []}
    [link] = row["payload"]["links"]
    assert (link["source"], link["target"], link["via"], link["cardinality"]) == \
        ("product_template", "product_category", "category", "m:1")
    assert "links" not in row["payload"]["node"]
    approved = OntoProposal(id=1, tenant_id=uuid.uuid4(), node_key=row["node_key"], state="approved",
                            payload=row["payload"], confidence=0.9, validation=row["validation"])
    release, provenance, applied = onto_routes._release_with_approved(FakeSession([approved]), uuid.uuid4())
    assert applied == 1 and provenance[link["key"]]["proposal_id"] == 1
    assert any(item.key == link["key"] for item in release.links)


@pytest.mark.parametrize("template", ["expired_qty", "near_expiry_qty"])
def test_a_metric_whose_rule_compares_to_as_of_validates(template):
    node = next(m for m in RELEASE.metrics if m.key == template).model_dump(mode="json")
    entry = {"kind": "metric", "confidence": 0.9, "payload": {**node, "key": f"{template}_again"}}
    row = match.llm_row(entry, "inventory", "run", RELEASE, intro(), prof())
    assert row["validation"] == {"ok": True, "problems": []}


def test_a_term_its_target_already_has_is_dropped():
    target = RELEASE.objects[0]
    entries = [{"kind": "term", "confidence": 0.9, "payload": {"target": target.key, "synonym": synonym}}
               for synonym in (target.synonyms[0].upper(), "brand new synonym")]
    _, reasons = match.settle(entries, "inventory", RELEASE, FIXTURE, intro())
    assert reasons[0] == f"{target.synonyms[0].upper()!r} is already a synonym of {target.key}" and reasons[1] is None


def test_approval_refuses_an_object_whose_link_key_is_already_held():
    node = metric_node("scrap_total")
    row = release_row(2, "scrap_total", node, state="proposed")
    row.node_key = "my_template"
    row.payload = {**row.payload, "links": [{"key": "my_template_categ_id"}]}
    holder = release_row(1, "other", node)
    holder.payload = {**holder.payload, "links": [{"key": "my_template_categ_id"}]}
    # first query: approved rows with this node key (none); second: every approved row
    assert onto_routes._held_by_other(ReplaceSession([], [holder, row]), uuid.uuid4(), row) == \
        "link key 'my_template_categ_id' is already held by approved proposal 1"
    assert onto_routes._held_by_other(FakeSession([row]), uuid.uuid4(), row) is None
    row.payload = {**row.payload, "links": [{"key": "on_hand_qty"}]}
    assert onto_routes._held_by_other(FakeSession([]), uuid.uuid4(), row) == \
        "link key 'on_hand_qty' is already in the release templates"


# --- full schema coverage: inferred candidates (ONTOLOGY_BUILDER_PLAN phases A-C) -------------------


VALUATION = {  # stock.valuation.layer reaches a warehouse only through its stock move
    "stock.valuation.layer": [("product_id", "many2one", "product.product"), ("stock_move_id", "many2one", "stock.move"),
                              ("company_id", "many2one", "res.company"), ("quantity", "float", None),
                              ("value", "monetary", None), ("remaining_qty", "float", None),
                              ("remaining_value", "monetary", None), ("unit_cost", "monetary", None),
                              ("create_date", "datetime", None), ("price_diff_value", "float", None),
                              ("account_move_id", "many2one", "account.move")],
    "stock.move": [("warehouse_id", "many2one", "stock.warehouse"), ("scrapped", "boolean", None),
                   ("product_id", "many2one", "product.product"),
                   ("state", "selection", None), ("product_uom_qty", "float", None), ("date", "datetime", None)],
    "stock.warehouse": [("name", "char", None)],
    "product.product": [("default_code", "char", None)],
    "res.company": [("name", "char", None)],
}


def valuation_odoo() -> tuple[Introspect, Profile]:
    selection = [["draft", "New"], ["done", "Done"], ["cancel", "Cancelled"]]
    intro_ = Introspect({"odoo_version": "16.0", "models": [
        {"model": model, "fields": [{"name": n, "string": n.replace("_", " ").title(), "type": t, "relation": r,
                                     "store": True, **({"selection": selection} if n == "state" else {})}
                                    for n, t, r in fields]} for model, fields in VALUATION.items()]})
    prof_ = Profile({"models": [
        {"model": "stock.valuation.layer", "row_count": 100, "fields": [
            {"name": n, "fill_rate": 0.01 if n == "price_diff_value" else 1.0}
            for n, _, _ in VALUATION["stock.valuation.layer"]]},
        {"model": "stock.move", "row_count": 80, "fields": [
            {"name": "warehouse_id", "fill_rate": 0.9}, {"name": "scrapped", "fill_rate": 0.01},
            {"name": "state", "fill_rate": 1.0}, {"name": "date", "fill_rate": 1.0},
            {"name": "product_uom_qty", "fill_rate": 1.0}, {"name": "product_id", "fill_rate": 1.0}]}]})
    return intro_, prof_


def valuation_rows(held=frozenset()):
    intro_, prof_ = valuation_odoo()
    caveats = {k: v for t in loader._read_templates(loader.TEMPLATE_DIR, []) for k, v in t.caveats.items()}
    return infer.run(list(RELEASE.objects), RELEASE, intro_, prof_, "run", set(held), caveats)


def test_stock_valuation_gets_a_warehouse_through_its_stock_move():
    rows, _, _ = valuation_rows()
    by_key = {row["node_key"]: row for row in rows}
    warehouse = by_key["property.stock_valuation.warehouse"]
    payload = warehouse["payload"]
    assert set(payload) == {"kind", "source", "run_id", "object", "key", "label", "role", "unit", "binding",
                            "synonyms", "fill_rate", "notes", "batch_approvable"}  # the Studio row contract
    assert (payload["binding"], payload["role"], payload["source"]) == ("stock_move_id.warehouse_id", "dimension", "infer")
    assert payload["fill_rate"] == 0.9 and "branch" in payload["synonyms"]
    assert warehouse["batch_approvable"] is True and payload["batch_approvable"] is True and warehouse["confidence"] >= 0.85
    assert any("receiving warehouse" in note for note in payload["notes"])  # the template caveat rides along
    assert "property.stock_valuation.product" not in by_key  # the template binds product_id already


def test_a_count_on_a_non_snapshot_object_is_additive_and_on_a_snapshot_is_semi_additive():
    rows, _, _ = valuation_rows()
    nodes = {row["node_key"]: row["payload"]["node"] for row in rows if row["kind"] == "metric"}
    move = nodes["stock_move_count"]
    assert (move["additivity"], move["over_time"], move["aggregation"]) == ("additive", None, "count")
    layer = nodes["stock_valuation_count"]
    assert (layer["additivity"], layer["over_time"]) == ("semi_additive", "last")
    assert nodes["stock_valuation_unit_cost_avg"]["additivity"] == "non_additive"  # a unit price is averaged
    assert all(row["payload"]["source"] == "infer" and row["validation"]["ok"] for row in rows)


def test_an_empty_field_is_dropped_with_a_note_and_a_held_key_is_not_offered():
    rows, notes, _ = valuation_rows(held={"property.stock_valuation.warehouse"})
    keys = {row["node_key"] for row in rows}
    assert "property.stock_valuation.warehouse" not in keys
    assert any("price_diff_value" in note and "under 5%" in note for note in notes)


def test_an_llm_rule_and_label_curate_the_inferred_rows(fake_model):
    intro_, prof_ = valuation_odoo()
    rule = {"kind": "rule", "confidence": 0.8, "payload": {
        "key": "done_move_only", "label": "Done move", "object": "stock_move", "synonyms": ["done"],
        "when": [{"prop": "move_status", "op": "=", "value": "done"}]}}
    bad = {**rule, "payload": {**rule["payload"], "key": "shipped_move", "when": [
        {"prop": "move_status", "op": "=", "value": "shipped"}]}}
    messages = fake_model([rule, bad])
    introspect = {"odoo_version": "16.0", "models": [
        {"model": m, "fields": [{"name": n, "type": t, "relation": r, "store": True, "string": n,
                                 **({"selection": [["draft", "New"], ["done", "Done"]]} if n == "state" else {})}
                                for n, t, r in f]} for m, f in VALUATION.items()]}
    body, _ = builder.run(introspect, {"models": [
        {"model": "stock.valuation.layer", "row_count": 100, "fields": []},
        {"model": "stock.move", "row_count": 80, "fields": [{"name": "warehouse_id", "fill_rate": 0.9}]}]},
        ["inventory"])
    by_key = {row["node_key"]: row for row in body["llm_proposals"]}
    good = by_key["rule.done_move_only"]
    assert good["validation"]["ok"] is True, good["validation"]
    assert set(good["payload"]) == {"kind", "source", "run_id", "object", "key", "label", "when", "synonyms", "notes",
                                    "batch_approvable"}
    assert good["batch_approvable"] is False and good["payload"]["source"] == "llm"
    assert "'shipped' is not a value of stock.move.state" in by_key["rule.shipped_move"]["validation"]["problems"][0]
    sent = json.loads(messages.calls[0]["messages"][0]["content"])
    move = next(o for o in sent["objects"] if o["object"] == "stock_move")
    assert any(c["node_key"] == "property.stock_move.warehouse" for c in move["candidates"])


def test_curate_relabels_and_drops_but_never_a_batch_approvable_row():
    rows, _, _ = valuation_rows()
    rows, notes = infer.curate(rows, [{"node_key": "property.stock_valuation.warehouse", "label": "Branch",
                                       "synonyms": ["branch", "shop"]}],
                               [{"node_key": "property.stock_valuation.warehouse", "reason": "noise"},
                                {"node_key": "property.stock_valuation.account_move", "reason": "technical link"}])
    keys = {row["node_key"]: row for row in rows}
    assert keys["property.stock_valuation.warehouse"]["payload"]["label"] == "Branch"
    assert "shop" in keys["property.stock_valuation.warehouse"]["payload"]["synonyms"]
    assert "property.stock_valuation.account_move" not in keys
    assert notes == ["property.stock_valuation.account_move: dropped by the LLM: technical link"]


def property_proposal(id_: int, key: str = "warehouse", state: str = "approved", obj: str = "stock_valuation",
                      binding: str = "stock_move_id.warehouse_id") -> OntoProposal:
    payload = {"kind": "property", "source": "infer", "run_id": "run", "object": obj, "key": key, "label": "Warehouse",
               "role": "dimension", "unit": None, "binding": binding, "synonyms": ["warehouse", "branch"],
               "fill_rate": 0.9, "notes": [], "batch_approvable": True}
    return OntoProposal(id=id_, tenant_id=uuid.uuid4(), node_key=f"property.{obj}.{key}", state=state,
                        payload=payload, confidence=0.9, validation={"ok": True, "problems": []})


def test_an_approved_property_lands_in_the_template_object_and_a_template_key_supersedes_it():
    release, provenance, applied = onto_routes._release_with_approved(
        FakeSession([property_proposal(1)]), uuid.uuid4())
    layer = next(o for o in release.objects if o.key == "stock_valuation")
    assert applied == 1 and layer.properties[-1].bindings == {"16": "stock_move_id.warehouse_id"}
    # the template gained the key since approval: the template's property wins, the overlay entry is superseded
    release, provenance, applied = onto_routes._release_with_approved(
        FakeSession([property_proposal(2, key="product", binding="stock_move_id.product_id")]), uuid.uuid4())
    layer = next(o for o in release.objects if o.key == "stock_valuation")
    assert applied == 0 and [p.bindings["16"] for p in layer.properties if p.key == "product"] == ["product_id"]
    assert provenance["stock_valuation"]["superseded"] == [{"proposal_id": 2, "property": "product"}]


def test_approval_refuses_a_property_or_rule_key_that_clashes():
    pending = property_proposal(2, state="proposed")
    assert onto_routes._held_by_other(FakeSession([property_proposal(1), pending]), uuid.uuid4(), pending) == \
        "key 'stock_valuation.warehouse' is already held by approved proposal 1"
    assert onto_routes._held_by_other(FakeSession([pending]), uuid.uuid4(), pending) is None
    template = property_proposal(3, key="product", state="proposed")
    assert onto_routes._held_by_other(FakeSession([]), uuid.uuid4(), template) == \
        "key 'stock_valuation.product' is already in the release templates"
    rule = OntoProposal(id=4, tenant_id=uuid.uuid4(), node_key="rule.usable_stock", state="proposed", confidence=0.6,
                        validation={"ok": True, "problems": []},
                        payload={"kind": "rule", "source": "llm", "key": "usable_stock", "object": "stock_position"})
    assert onto_routes._held_by_other(FakeSession([]), uuid.uuid4(), rule) == \
        "key 'usable_stock' is already in the release templates"  # a rule key is global


def test_a_batch_approvable_property_passes_the_batch_check_and_parses():
    row = property_proposal(1, state="proposed")
    assert onto_routes._batch_problem(row) is None
    kind, (obj, prop) = onto_routes._parse(row)
    assert (kind, obj, prop.key) == ("property", "stock_valuation", "warehouse")
