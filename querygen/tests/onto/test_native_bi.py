"""Redis-free protocol tests using a scripted model and Odoo tool returns."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.agents import native_bi
from app.ontology import loader
from app.routes import native_bi as routes


TENANT = uuid.UUID(int=1)
KEY = "odi_test"


def begin(**kwargs):
    return native_bi.Step(run_id="run-1", step=0, question="Compare sales",
                          catalog={"metrics": [{"key": "revenue"}]}, **kwargs)


def test_catalog_explains_the_published_usable_stock_filter():
    release = loader.load_release()
    catalog = {"metrics": [{"key": "usable_qty", "object": "stock_position"}]}
    enriched = routes._catalog_with_metric_rules(catalog, release)
    rule, = enriched["metrics"][0]["built_in_filters"]
    assert rule["key"] == "usable_stock"
    assert rule["when"] == [{"prop": "location_role", "op": "not in", "value": ["damaged", "qc_hold", "expired"]}]
    assert len(enriched["metrics"]) == len(catalog["metrics"])


def needs_query(state):
    return [
        {"id": "tool-1", "name": "compare", "arguments": {"metric": "revenue"}},
    ], "", {"model": "test", "input_tokens": 4, "output_tokens": 2}


def test_one_turn_uses_exact_dimensions_and_numeric_evidence():
    first = native_bi.run_step(begin(), TENANT, KEY, needs_query)
    assert first["status"] == "tools"
    assert first["tools"][0]["name"] == "compare"

    result = {
        "source_kind": "ontology", "source_id": "revenue", "trust": "approved",
        "period": {"from": "2026-01-01", "to": "2026-01-31"},
        "columns": [
            {"key": "branch", "label": "Branch", "role": "dimension"},
            {"key": "sales", "label": "Sales", "role": "measure", "unit": "SAR"},
        ],
        "rows": [["North", 25], ["South", 50]],
    }

    def finish(state):
        assert state["evidence"][0]["id"] == "tool-1"
        return [{"id": "final", "name": "finish_answer", "arguments": {
            "summary": "The stated total was 1234.",
            "claims": [
                {"evidence_id": "tool-1", "row": 1, "column": "sales"},
                {"evidence_id": "tool-1", "row": 4, "column": "sales"},
            ],
        }}], "", {}

    second = native_bi.Step(run_id="run-1", step=1, state=first["state"],
                            tool_results=[{"id": "tool-1", "ok": True, "result": result}])
    card = native_bi.run_step(second, TENANT, KEY, finish)["card"]
    assert card["numbers"] == []  # a written answer carries its own figures: no number chips
    assert card["table"]["rows"] == result["rows"]
    assert card["chart"] is None  # two rows: the table is enough
    assert card["guard"]["ok"] is False  # 1234 is not in the evidence: the text is kept and flagged
    assert card["sources"][0]["source_id"] == "revenue"


def test_continuation_is_bound_to_tenant_run_step_and_tool_ids():
    state = native_bi.run_step(begin(), TENANT, KEY, needs_query)["state"]
    results = [{"id": "tool-1", "ok": False, "result": {"error": "forbidden"}}]
    for overrides, tenant in [
        ({}, uuid.UUID(int=2)),
        ({"run_id": "different"}, TENANT),
        ({"step": 2}, TENANT),
        ({"tool_results": [{"id": "wrong", "ok": True}]}, TENANT),
        ({"state": {**state, "step": 2}}, TENANT),
    ]:
        body = native_bi.Step.model_validate({
            "run_id": "run-1", "step": 1, "state": state, "tool_results": results, **overrides,
        })
        with pytest.raises(native_bi.StepError):
            native_bi.run_step(body, tenant, KEY, needs_query)


def test_a_call_that_already_failed_is_not_sent_to_odoo_again():
    first = native_bi.run_step(begin(), TENANT, KEY, needs_query)
    failed = native_bi.Step(run_id="run-1", step=1, state=first["state"],
                            tool_results=[{"id": "tool-1", "ok": True, "result": {"error": "invalid_spec"}}])
    assert native_bi.run_step(failed, TENANT, KEY, needs_query)["status"] == "done"  # same call, same error: stop


def test_new_follow_up_accepts_history_without_reusing_pending_tools():
    request = native_bi.Step(run_id="new", step=0, question="And which team?",
                             history=[{"role": "assistant", "text": "Prior conversation"}])

    def model(state):
        assert state["pending"] == []
        assert state["history"] == [{"role": "assistant", "text": "Prior conversation"}]
        assert state["question"] == "And which team?"
        return [], "Which sales period should I use?", {}

    reply = native_bi.run_step(request, TENANT, KEY, model)
    assert reply["status"] == "done"
    assert reply["card"]["text"] == "Which sales period should I use?"


def test_decomposition_reuses_scoped_comparisons_and_calculates_the_effects():
    period = {"from": "2026-02-01", "to": "2026-02-28"}
    catalog = {"metrics": [
        {"key": "revenue", "object": "sales", "derived_from": "qty * price", "dimensions": ["branch"],
         "properties": [{"key": "qty", "unit": "qty"}]},
        {"key": "volume", "object": "sales", "aggregation": "sum", "measure": "qty"},
    ]}

    def model(state):
        return [{"id": "split", "name": "decompose", "arguments": {"metric": "revenue", "period": period}}], "", {}

    first = native_bi.run_step(native_bi.Step(
        run_id="r", step=0, question="Why did revenue change?", catalog=catalog,
    ), TENANT, KEY, model)
    assert [tool["name"] for tool in first["tools"]] == ["compare", "compare", "compare"]
    assert [tool["arguments"]["dims"] for tool in first["tools"]] == [[], [], ["branch"]]
    data = [
        (["revenue", "revenue_previous"], [[360, 200]]),
        (["volume", "volume_previous"], [[30, 20]]),
        (["branch", "revenue", "revenue_previous"], [["North", 300, 200], ["South", 60, 0]]),
    ]
    replies = [{
        "id": tool["id"], "ok": True,
        "result": {"columns": [{"key": key, "role": "measure"} for key in columns],
                   "rows": rows, "source_kind": "ontology", "trust": "approved"},
    } for tool, (columns, rows) in zip(first["tools"], data)]
    second = native_bi.Step(run_id="r", step=1, state=first["state"], tool_results=replies)
    card = native_bi.run_step(second, TENANT, KEY, lambda _: ([], "Revenue changed.", {}))["card"]
    assert card["text"] == "Revenue changed."
    assert card["decomposition"]["total"]["change"] == 160
    assert card["decomposition"]["dimensions"][0]["sums_to_total"] is True


def test_access_denial_stops_model_before_fallback():
    first = native_bi.run_step(begin(), TENANT, KEY, needs_query)
    denied = native_bi.Step(run_id="run-1", step=1, state=first["state"], tool_results=[
        {"id": "tool-1", "ok": False, "result": {"error": "forbidden"}},
    ])

    def unused_model(_):
        raise AssertionError("A different tool must not be tried after an authorization denial")

    assert native_bi.run_step(denied, TENANT, KEY, unused_model)["status"] == "refused"


def test_native_route_refuses_a_token_bound_to_another_tenant(monkeypatch):
    session = SimpleNamespace(is_modified=lambda _: False)
    install = SimpleNamespace(uid="db-one")
    principal = SimpleNamespace(kind="install", install=install)
    tenant = SimpleNamespace(id=TENANT, odoo_base_url="https://odoo.example", db_uuid="db-two")
    monkeypatch.setattr(routes.installs, "resolve_principal", lambda session, key: principal)
    monkeypatch.setattr(routes, "tenant_by_url", lambda session, url: tenant)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[db.get_session] = lambda: session
    with TestClient(app) as client:
        url = "/native-bi/v1/step"
        body = begin().model_dump()
        assert client.post(url, json=body).status_code == 401
        response = client.post(url, json=body, headers={
            "x-api-key": KEY, "x-odoo-instance": "db-one", "x-odoo-url": "https://odoo.example",
        })
        assert response.status_code == 403


def test_activated_install_registers_only_its_own_origin(monkeypatch):
    seen = []
    session = SimpleNamespace(is_modified=lambda _: False, exec=lambda _: SimpleNamespace(first=lambda: None),
                              add=seen.append, commit=lambda: None)
    install = SimpleNamespace(uid="db-one", base_url="https://ours.example/web", license_id=1, status_override=None)
    principal = SimpleNamespace(kind="install", install=install)
    monkeypatch.setattr(routes.installs, "resolve_principal", lambda session, key: principal)
    monkeypatch.setattr(routes, "tenant_by_url", lambda session, url: None)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[db.get_session] = lambda: session
    monkeypatch.setattr(native_bi, "openai_turn", needs_query)
    with TestClient(app) as client:
        body = begin().model_dump()
        url = "/native-bi/v1/step"
        headers = {"x-api-key": KEY, "x-odoo-instance": "db-one"}
        assert client.post(url, json=body, headers={**headers, "x-odoo-url": "https://others.example"}).status_code == 403
        assert client.post(url, json=body, headers={**headers, "x-odoo-url": "https://ours.example"}).status_code == 200
    assert [(tenant.db_uuid, tenant.odoo_base_url) for tenant in seen] == [("db-one", "https://ours.example")]


def test_candidate_can_publish_supported_sales_without_optional_cost_fields(monkeypatch):
    release = loader.load_release().model_copy(deep=True)
    provenance = {obj.key: {"source": "template", "group_xmlids": ["sales_team.group_sale_salesman"]}
                  for obj in release.objects}
    objects = [{"object": obj.key, "properties": [
        {"property": prop.key, "status": "matched"} for prop in obj.properties if not prop.optional
    ]} for obj in release.objects if obj.base_model in {"sale.order", "sale.order.line"}]
    monkeypatch.setattr(routes.ontology, "_release_with_approved", lambda *_: (release, provenance, 0))
    monkeypatch.setattr(routes, "_proposal_rows", lambda *_: [
        SimpleNamespace(payload={"kind": "template", "objects": objects}),
    ])
    candidate, _, provision = routes._candidate(None, TENANT)
    keys = {metric.key for metric in candidate.metrics}
    assert {"order_count", "revenue", "net_revenue", "units_sold"} <= keys
    assert not {"margin", "margin_pct", "cost_of_sales"} & keys
    assert {item["key"] for item in provision["objects"]} == {"sales_order", "sales_order_line"}
    assert provision["release"]["hash"] == candidate.digest()
