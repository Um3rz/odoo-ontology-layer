"""Readiness gates (plan 2.6, querygen half): the pure gate functions, then POST /ontology/readiness.

The gate tests are plain functions with no database. The route tests use the Odoo stub and scratch
database of test_ontology_routes.py (copied here: test modules in tests/onto are not a package).
Steward gets a reply per feature; anyone else gets the same 404 as every /ontology route.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.db.onto import Tenant
from app.onto_routes import mount
from app.systems.health import GATES, gate, readiness
from app.utils import browser_auth as ba

STEWARD = "odoo_dashboards_agents.group_onto_steward"
ODOO_A, UUID_A = "https://a.example.test", "uuid-a"
WHOAMI = {
    "steward.sig": {"user": {"id": 7}, "groups": [STEWARD], "database": {"uuid": UUID_A}, "ai_enabled": True},
    "viewer.sig": {"user": {"id": 8}, "groups": [], "database": {"uuid": UUID_A}, "ai_enabled": True},
}
A_STEWARD = {"Authorization": "Bearer steward.sig", "X-Odoo-Url": ODOO_A}
A_VIEWER = {"Authorization": "Bearer viewer.sig", "X-Odoo-Url": ODOO_A}

# Every feature ready: stock history, expiry, POS and 3 warehouses, 200 storable products, no open checks.
FULL = {"history_days": {"stock.move": 412}, "expiry_in_use": True, "pos_in_use": True,
        "warehouse_count": 3, "storable_products": 200}


def row(key, severity="warning", count=1):
    """A Data Health row as Odoo sends it, with the fields the gates ignore."""
    return {"check_key": key, "severity": severity, "model": "product.product", "company_id": 1,
            "count": count, "sample_ids": [1, 2], "message": "plain English", "fix_url": "/web#model=x"}


def missing(feature, facts, checks=()):
    reply = gate(feature, facts, checks)
    assert reply is not None and reply["insufficient_data"] is True and reply["feature"] == feature
    return reply["missing"]


# --- ready and not ready, feature by feature -------------------------------------------------


def test_every_feature_is_ready_when_the_profile_allows_it():
    assert readiness(FULL, []) == {name: None for name in GATES}


def test_forecast_needs_90_days_of_stock_history():
    assert gate("forecast", {"history_days": {"stock.move": 90}}, []) is None
    assert gate("forecast", {"history_days": {"stock.move": 40}}, []) == {
        "insufficient_data": True, "feature": "forecast", "missing": ["90 days of stock history (have 40)"]}


def test_expiry_needs_expiry_dates_on_lots():
    assert gate("expiry", {"expiry_in_use": True}, []) is None
    assert missing("expiry", {"expiry_in_use": False}) == ["expiry dates on lots (off)"]


def test_replenishment_needs_a_supplier_on_most_products_and_no_blocking_issue():
    # 80 of 200 without a supplier is under half: ready.
    assert gate("replenishment", {"storable_products": 200}, [row("product_no_supplier", count=80)]) is None
    assert missing("replenishment", {"storable_products": 200}, [row("product_no_supplier", count=120)]) == [
        "a supplier on more than half of storable products (120 of 200 without a supplier)"]
    assert missing("replenishment", {"storable_products": 200}, [row("product_dup", "blocking")]) == [
        "no blocking health issues (open: product_dup)"]
    assert missing("replenishment", {"storable_products": 0}, []) == [
        "a supplier on more than half of storable products (nothing to compare)"]


def test_transfers_need_two_warehouses():
    assert gate("transfers", {"warehouse_count": 2}, []) is None
    assert missing("transfers", {"warehouse_count": 1}) == ["2 or more warehouses (have 1)"]


def test_pos_demand_needs_point_of_sale_in_use():
    assert gate("pos_demand", {"pos_in_use": True}, []) is None
    assert missing("pos_demand", {"pos_in_use": False}) == ["point of sale orders (off)"]


def test_valuation_is_blocked_only_by_an_open_stock_value_warning():
    assert gate("valuation", {}, []) is None
    assert gate("valuation", {}, [row("product_no_cost")]) is None  # another check does not block it
    assert missing("valuation", {}, [row("reconcile_stock_value")]) == [
        "stock value reconciled to valuation layers (open: reconcile_stock_value)"]


# --- unknown facts and bad shapes ------------------------------------------------------------


def test_facts_that_were_never_profiled_are_not_profiled_yet():
    assert missing("forecast", {}) == ["90 days of stock history (not profiled yet)"]
    assert missing("expiry", {}) == ["expiry dates on lots (not profiled yet)"]
    assert missing("replenishment", {}, []) == [
        "a supplier on more than half of storable products (not profiled yet)"]
    assert missing("transfers", {}) == ["2 or more warehouses (not profiled yet)"]
    assert missing("pos_demand", {}) == ["point of sale orders (not profiled yet)"]


def test_checks_that_were_never_read_make_check_gates_unknown():
    assert missing("valuation", {}, None) == ["stock value reconciled to valuation layers (not profiled yet)"]
    assert missing("replenishment", FULL, None) == [
        "a supplier on more than half of storable products (not profiled yet)",
        "no blocking health issues (not profiled yet)"]


def test_wrong_types_are_unknown_not_guessed():
    assert missing("forecast", {"history_days": {"stock.move": True}}) == [
        "90 days of stock history (not profiled yet)"]  # a bool is not a day count
    assert missing("expiry", {"expiry_in_use": "yes"}) == ["expiry dates on lots (not profiled yet)"]
    assert missing("transfers", {"warehouse_count": None}) == ["2 or more warehouses (not profiled yet)"]


def test_bad_shapes_never_raise():
    assert missing("forecast", "not a dict", "not a list") == ["90 days of stock history (not profiled yet)"]
    assert gate("valuation", {}, ["not a row", 3, None]) is None  # rows that are not dicts are ignored
    assert gate(["forecast"], {}, []) == {
        "insufficient_data": True, "feature": ["forecast"], "missing": ["unknown feature"]}
    assert gate("no_such_feature", {}, [])["missing"] == ["unknown feature"]


def test_readiness_answers_for_every_feature():
    assert set(readiness({}, [])) == set(GATES)


# --- POST /ontology/readiness: steward only -----------------------------------------------------


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


class FakeOdoo:
    """Stands in for the tenant's Odoo: whoami answers by token."""

    def __init__(self, base_url, token):
        self.token = token

    async def whoami(self):
        return WHOAMI[self.token]

    async def aclose(self):
        return None


def test_steward_gets_a_reply_per_feature_without_a_published_release(client):
    body = {"facts": {"history_days": {"stock.move": 40}, "expiry_in_use": True},
            "checks": [row("product_no_supplier", count=3)]}
    r = client.post("/ontology/readiness", json=body, headers=A_STEWARD)
    assert r.status_code == 200
    features = r.json()["features"]
    assert set(features) == set(GATES)
    assert features["forecast"]["missing"] == ["90 days of stock history (have 40)"]
    assert features["expiry"] is None
    assert features["transfers"]["missing"] == ["2 or more warehouses (not profiled yet)"]
    assert features["replenishment"]["missing"] == [
        "a supplier on more than half of storable products (not profiled yet)"]
    assert features["valuation"] is None


def test_non_stewards_get_the_same_404_and_signed_out_callers_401(client):
    body = {"facts": {}, "checks": []}
    r = client.post("/ontology/readiness", json=body, headers=A_VIEWER)
    assert r.status_code == 404 and r.json() == {"detail": "not found"}
    assert client.post("/ontology/readiness", json=body, headers={"X-Odoo-Url": ODOO_A}).status_code == 401


def test_the_body_is_checked_at_the_route(client):
    too_many_checks = [row("product_duplicate") for _ in range(201)]
    too_many_facts = {f"k{i}": i for i in range(201)}
    assert client.post("/ontology/readiness", json={"checks": too_many_checks}, headers=A_STEWARD).status_code == 422
    assert client.post("/ontology/readiness", json={"facts": too_many_facts}, headers=A_STEWARD).status_code == 422
    assert client.post("/ontology/readiness", json={"checks": [row("x", "urgent")]},
                       headers=A_STEWARD).status_code == 422
    assert client.post("/ontology/readiness", json={"checks": [row("x", count=-1)]},
                       headers=A_STEWARD).status_code == 422
