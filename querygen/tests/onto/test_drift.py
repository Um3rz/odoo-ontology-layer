"""Drift (plan 2.5, contract 9): a changed Odoo breaks bindings, suspends what reads them and proposes a fix; restoring the
field resolves it. Written, not run in this sub-phase (owner rule).

The Odoo stub answers whoami by bearer token (the Onto bot, a steward, a viewer) and the introspect (2.1). The introspect
is built from the Odoo 16 fixture, which holds only the fields the templates bind: a healthy Odoo until a test drops one.
The pure tests at the top need no database.
"""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db
from app.db.onto import JobRun, OntoDrift, Tenant
from app.odoo.client import OdooClient
from app.onto_routes import mount, parity
from app.ontology.builder.profile import Introspect
from app.ontology.drift import compare, metric_props
from app.ontology.loader import load_release
from app.ontology.schema import ODOO_16
from app.ontology.spec import fields_for
from app.utils import auth
from app.utils import browser_auth as ba

ODOO = "https://odoo.example.test"
DB_UUID = "33a1b60e-0000-0000-0000-000000000025"
BOT_GROUP = "odoo_dashboards_agents.group_onto_bot"
STEWARD_GROUP = "odoo_dashboards_agents.group_onto_steward"
WHO = {
    "bot.sig": {"database": {"uuid": DB_UUID}, "groups": [BOT_GROUP], "ai_enabled": True, "user": {"id": 9}},
    "steward.sig": {"database": {"uuid": DB_UUID}, "groups": [STEWARD_GROUP], "ai_enabled": True, "user": {"id": 7}},
    "viewer.sig": {"database": {"uuid": DB_UUID}, "groups": [], "ai_enabled": True, "user": {"id": 8}},
}
BOT = {"Authorization": "Bearer bot.sig", "X-Odoo-Url": ODOO}
STEWARD = {"Authorization": "Bearer steward.sig", "X-Odoo-Url": ODOO}
VIEWER = {"Authorization": "Bearer viewer.sig", "X-Odoo-Url": ODOO}
KEY = {"X-API-Key": "good-key", "X-Odoo-Url": ODOO}
TEMPLATE_HASH = load_release().digest()
QUANTITY = ("stock.quant", "quantity")  # on_hand_qty, available_qty and expired_qty read it through stock_position


def healthy_introspect(drop=frozenset(), absent=frozenset(), type_of=None):
    """An Odoo 16 introspect (2.1) with every bound field, except the (model, field) pairs in `drop`.
    `absent` models are left out of the reply (a module that is not installed). `type_of` overrides one field's type."""
    models = []
    for model, fields in fields_for(ODOO_16).items():
        if model in absent:
            continue
        rows = []
        for name, meta in fields.items():
            if (model, name) in drop:
                continue
            kind = "char" if type_of == (model, name) else meta["type"]
            rows.append({"name": name, "string": name, "type": kind, "relation": meta.get("relation"),
                         "store": bool(meta.get("store")), "custom": False, "selection": None})
        models.append({"model": model, "name": model, "custom": False, "fields": rows})
    return {"odoo_version": "16.0", "models": models, "warehouses": [], "locations": []}


# --- pure: the comparison and the metric walk (no database) -----------------------------------


def test_a_healthy_odoo_breaks_no_binding():
    checked, broken, unchecked = compare(load_release(), Introspect(healthy_introspect()))
    assert broken == [] and unchecked == set() and len(checked) > 100


def test_a_missing_field_and_a_changed_type_are_both_broken():
    release = load_release()
    _, missing, _ = compare(release, Introspect(healthy_introspect(drop={QUANTITY})))
    node = {b["node"]: b for b in missing}["stock_position.quantity"]
    assert node["binding"] == "quantity" and "stock.quant.quantity is missing or not stored" in node["problem"]
    _, changed, _ = compare(release, Introspect(healthy_introspect(type_of=QUANTITY)))
    problem = {b["node"]: b["problem"] for b in changed}["stock_position.quantity"]
    assert "changed type from float to char" in problem


def test_a_module_that_is_not_installed_is_not_compared_or_resolved():
    release = load_release()
    pos_nodes = {f"{o.key}.{p.key}" for o in release.objects if o.base_model == "pos.order" for p in o.properties}
    assert pos_nodes
    checked, broken, unchecked = compare(release, Introspect(healthy_introspect(absent={"pos.order"})))
    assert pos_nodes <= unchecked
    assert not pos_nodes & checked
    assert not any(b["node"] in pos_nodes for b in broken)


def test_a_metric_reads_the_properties_its_leaves_read():
    release = load_release()
    assert metric_props(release, "on_hand_qty") == {"quantity"}
    assert {"quantity", "reserved_quantity"} <= metric_props(release, "available_qty")
    assert "reserved_quantity" not in metric_props(release, "on_hand_qty")


# --- database: the job, the steward route, suspension and resolution --------------------------


class FakeLicenseCache:
    async def validate_license_key(self, session, license_key, organization_id=None):
        return {"validated": license_key == "good-key"}


class StubOdoo:
    """The tenant's Odoo as the job and the browser see it. Counts every call; answers whoami per token."""

    def __init__(self):
        self.who = copy.deepcopy(WHO)
        self.who_status = 200
        self.introspect = healthy_introspect()
        self.introspect_status = 200
        self.calls = []

    def handler(self, req: httpx.Request):
        token = (req.headers.get("authorization") or "").partition(" ")[2]
        self.calls.append((req.method, req.url.path))
        if req.url.path == "/onto/v1/whoami":
            if self.who_status != 200 or token not in self.who:
                return httpx.Response(401)
            return httpx.Response(200, json=self.who[token])
        if req.url.path == "/onto/v1/introspect":
            return httpx.Response(self.introspect_status, json=self.introspect)
        return httpx.Response(404)

    def count(self, path):
        return sum(1 for _, p in self.calls if p == path)


@pytest.fixture()
def env(engine, session, clean_tenants, monkeypatch):
    stub = StubOdoo()

    def factory(base, token):
        return OdooClient(base, token, transport=httpx.MockTransport(stub.handler), backoff=0, retries=0)

    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(parity, "odoo_client_factory", factory)  # the bot job
    monkeypatch.setattr(ba, "odoo_client_factory", factory)  # the browser routes and /onto/compile
    monkeypatch.setattr(auth, "get_license_cache_service", FakeLicenseCache)
    monkeypatch.setattr(ba, "token_limiter", ba.RateLimiter(1000, 60))
    monkeypatch.setattr(ba, "tenant_limiter", ba.RateLimiter(1000, 60))
    ba._whoami_cache.clear()
    tenant = Tenant(odoo_base_url=ODOO, db_uuid=DB_UUID)
    session.add(tenant)
    session.commit()
    app = FastAPI()
    app.dependency_overrides[db.get_session] = lambda: session
    mount(app)
    return SimpleNamespace(client=TestClient(app), stub=stub, session=session, tenant_id=tenant.id)


def publish(env):
    r = env.client.post("/ontology/releases", headers=STEWARD)
    assert r.status_code == 200, r.text
    return r.json()


def run_job(env, introspect=None):
    if introspect is not None:
        env.stub.introspect = introspect
    return env.client.post("/onto/jobs/drift", headers=BOT)


def compile_on_hand(env):
    body = {"release": TEMPLATE_HASH, "spec": {"release": TEMPLATE_HASH, "object": "stock_position",
                                              "metric": "on_hand_qty"}}
    return env.client.post("/onto/compile", json=body, headers=KEY)


def drift_proposals(env):
    rows = env.client.get("/ontology/proposals", headers=STEWARD).json()["proposals"]
    return [p for p in rows if p["node_key"].startswith("drift:")]


def test_a_dropped_field_breaks_its_binding_suspends_its_metrics_and_proposes_a_fix(env):
    publish(env)
    r = run_job(env, healthy_introspect(drop={QUANTITY}))
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "done"
    assert ("stock_position.quantity", "quantity") in {(b["node"], b["binding"]) for b in r.json()["broken"]}

    [row] = env.session.exec(select(OntoDrift).where(OntoDrift.node_key == "stock_position.quantity")).all()
    assert row.resolved_at is None and row.binding == "quantity" and row.release_hash == TEMPLATE_HASH

    [proposal] = drift_proposals(env)
    assert proposal["state"] == "proposed" and proposal["batch_approvable"] is False
    assert proposal["payload"]["source"] == "drift"

    metrics = {m["key"]: m for m in env.client.get("/ontology/metrics", headers=STEWARD).json()["metrics"]}
    assert metrics["on_hand_qty"]["suspended"] is True
    assert metrics["on_hand_qty"]["suspended_reason"].startswith("stock_position.quantity reads quantity")
    assert metrics["available_qty"]["suspended"] is True  # derived from on_hand_qty
    assert metrics["reserved_qty"]["suspended"] is False  # reads reserved_quantity, not quantity
    card = env.client.get("/ontology/nodes/on_hand_qty", headers=STEWARD).json()
    assert card["suspended"] is True and card["suspended_reason"] == metrics["on_hand_qty"]["suspended_reason"]

    refused = compile_on_hand(env)
    assert refused.status_code == 422
    assert refused.json() == {"detail": {"error": "suspended_by_drift", "binding": "quantity"}}


def test_a_drift_proposal_is_never_batch_approved_and_refuses_one_by_one(env):
    publish(env)
    run_job(env, healthy_introspect(drop={QUANTITY}))
    [proposal] = drift_proposals(env)
    out = env.client.post("/ontology/proposals/batch-approve", headers=STEWARD).json()
    assert out["approved"] == [] and {s["id"] for s in out["skipped"]} >= {proposal["id"]}
    r = env.client.post(f"/ontology/proposals/{proposal['id']}/approve", headers=STEWARD)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "validation_failed"


def test_restoring_the_field_resolves_the_row_and_lifts_the_suspension(env):
    publish(env)
    run_job(env, healthy_introspect(drop={QUANTITY}))
    r = run_job(env, healthy_introspect())
    assert r.status_code == 200, r.text
    assert r.json()["broken"] == [] and r.json()["resolved"] == 1

    [row] = env.session.exec(select(OntoDrift).where(OntoDrift.node_key == "stock_position.quantity")).all()
    assert row.resolved_at is not None
    assert drift_proposals(env) == []  # the pending proposal for a fine binding goes

    metrics = {m["key"]: m for m in env.client.get("/ontology/metrics", headers=STEWARD).json()["metrics"]}
    assert metrics["on_hand_qty"]["suspended"] is False and metrics["on_hand_qty"]["suspended_reason"] is None
    assert compile_on_hand(env).status_code == 200


def test_a_healthy_run_over_a_healthy_odoo_breaks_and_resolves_nothing(env):
    publish(env)
    r = run_job(env)
    assert r.status_code == 200, r.text
    assert r.json()["broken"] == [] and r.json()["resolved"] == 0 and r.json()["checked"] > 100
    assert compile_on_hand(env).status_code == 200


def test_the_steward_runs_the_same_comparison_from_the_browser(env):
    assert env.client.post("/ontology/drift", json={"introspect": healthy_introspect()}, headers=STEWARD).status_code == 404
    publish(env)
    body = {"introspect": healthy_introspect(drop={QUANTITY})}
    r = env.client.post("/ontology/drift", json=body, headers=STEWARD)
    assert r.status_code == 200, r.text
    assert ("stock_position.quantity", "quantity") in {(b["node"], b["binding"]) for b in r.json()["broken"]}
    assert env.session.exec(select(JobRun).where(JobRun.job == "drift")).all()  # one job run row, counts only
    assert env.client.post("/ontology/drift", json={**body, "extra": 1}, headers=STEWARD).status_code == 422


def test_a_live_run_blocks_another_and_odoo_is_not_asked(env):
    publish(env)
    env.session.add(JobRun(tenant_id=env.tenant_id, job="drift", state="running",
                           heartbeat_at=datetime.now(timezone.utc)))
    env.session.commit()
    r = run_job(env)
    assert (r.status_code, r.json()) == (409, {"error": "already_running"})
    assert env.stub.count("/onto/v1/introspect") == 0


def test_an_odoo_failure_or_a_wrong_reply_is_a_failed_run_and_not_retried(env):
    publish(env)
    env.stub.introspect_status = 503
    r = run_job(env)
    assert (r.status_code, r.json()) == (502, {"error": "odoo_unavailable"})
    assert env.stub.count("/onto/v1/introspect") == 1
    env.stub.introspect_status = 200
    r = run_job(env, {"odoo_version": 16})  # a reply of the wrong shape
    assert (r.status_code, r.json()) == (502, {"error": "odoo_unexpected"})
    r = run_job(env, healthy_introspect() | {"odoo_version": "17.0"})  # not Odoo 16
    assert (r.status_code, r.json()) == (502, {"error": "odoo_unexpected"})
    states = [row.state for row in env.session.exec(select(JobRun).where(JobRun.job == "drift")).all()]
    assert states == ["failed", "failed", "failed"]


def test_only_the_onto_bot_of_the_registered_database_may_run_the_drift_job(env):
    publish(env)
    no_bearer = env.client.post("/onto/jobs/drift", headers={"X-Odoo-Url": ODOO})
    api_key = env.client.post("/onto/jobs/drift", headers={"Authorization": "Bearer " + "ab" * 16, "X-Odoo-Url": ODOO})
    unknown = env.client.post("/onto/jobs/drift", headers={"Authorization": "Bearer bot.sig",
                                                           "X-Odoo-Url": "https://other.example.test"})
    assert [no_bearer.status_code, api_key.status_code, unknown.status_code] == [401, 401, 403]
    assert env.stub.count("/onto/v1/introspect") == 0  # refused before Odoo is asked anything

    refusals = {
        "not_the_bot": {"groups": [STEWARD_GROUP]},  # a steward's token, not the bot's
        "tenant_mismatch": {"database": {"uuid": "some-other-database"}},
        "ai_features_off": {"ai_enabled": False},
    }
    for error, change in refusals.items():
        env.stub.who["bot.sig"] = dict(copy.deepcopy(WHO["bot.sig"]), **change)
        r = env.client.post("/onto/jobs/drift", headers=BOT)
        assert (r.status_code, r.json()) == (403, {"error": error}), error
    env.stub.who["bot.sig"] = copy.deepcopy(WHO["bot.sig"])
    env.stub.who_status = 401  # Odoo itself refuses the token
    r = env.client.post("/onto/jobs/drift", headers=BOT)
    assert (r.status_code, r.json()) == (401, {"error": "invalid_token"})
    assert env.stub.count("/onto/v1/introspect") == 0


def test_the_steward_drift_route_is_steward_only(env):
    publish(env)
    body = {"introspect": healthy_introspect()}
    viewer = env.client.post("/ontology/drift", json=body, headers=VIEWER)
    assert (viewer.status_code, viewer.json()) == (404, {"detail": "not found"})
    anonymous = env.client.post("/ontology/drift", json=body, headers={"X-Odoo-Url": ODOO})
    assert anonymous.status_code == 401
    assert env.session.exec(select(OntoDrift)).all() == []  # the refused calls changed nothing
