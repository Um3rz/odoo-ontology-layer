"""POST /onto/explore/compile and /onto/lookup/compile (plan 3.5; contracts 3.2 exploration, 3.5 lookup).

No database: the explore compile is pure, and the lookup route uses the same stubs as test_compile_route.py
(one registered tenant, a licence stub, no stored releases, the committed templates on a dev install).
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.onto_routes import compile as compile_mod
from app.onto_routes import mount
from app.ontology import explore, store
from app.ontology.loader import load_release
from app.ontology.spec import SpecError
from app.utils import auth
from app.utils import browser_auth as ba

ODOO = "https://odoo.example.test"
KEY = {"X-API-Key": "good-key", "X-Odoo-Url": ODOO}
TABLES = {"stock.picking": "stock_picking", "stock.picking.type": "stock_picking_type",
          "stock.warehouse": "stock_warehouse"}


@pytest.fixture(scope="module")
def templates():
    return load_release()


def _warehouse_hops():
    return [
        {"field": "picking_type_id", "type": "many2one", "model": "stock.picking", "comodel": "stock.picking.type"},
        {"field": "warehouse_id", "type": "many2one", "model": "stock.picking.type", "comodel": "stock.warehouse"},
        {"field": "code", "type": "char", "model": "stock.warehouse"},
    ]


def _type_name_hops():
    return [
        {"field": "picking_type_id", "type": "many2one", "model": "stock.picking", "comodel": "stock.picking.type"},
        {"field": "name", "type": "char", "model": "stock.picking.type"},
    ]


PATHS = {
    "picking_type_id.warehouse_id.code": _warehouse_hops(),
    "picking_type_id.name": _type_name_hops(),
    "state": [{"field": "state", "type": "selection", "model": "stock.picking"}],
}


def explore_body(**spec):
    base = {"model": "stock.picking", "fields": list(PATHS), "group_by": ["picking_type_id.warehouse_id.code"],
            "filters": [{"path": "picking_type_id.name", "op": "=", "value": "Delivery Orders"},
                        {"path": "state", "op": "=", "value": "done"}],
            "aggregate": {"op": "count", "path": None}, "limit": 100}
    base.update(spec)
    return {"spec": base, "tables": TABLES, "paths": PATHS}


# --- the explore SQL (pure) -----------------------------------------------------------------

def test_explore_is_one_select_with_its_joins_group_and_params():
    out = explore.compile_explore(explore_body())
    sql = out["sql"]
    assert sql.startswith("SELECT ") and "WITH" not in sql and ";" not in sql
    assert 'FROM "stock_picking" AS "b"' in sql
    assert 'LEFT JOIN "stock_picking_type" AS "j1" ON "j1"."id" = "b"."picking_type_id"' in sql
    assert 'LEFT JOIN "stock_warehouse" AS "j2" ON "j2"."id" = "j1"."warehouse_id"' in sql
    assert 'AS "picking_type_id.warehouse_id.code"' in sql and 'COUNT(*) AS "_aggregate"' in sql
    assert 'GROUP BY "j2"."code"' in sql and sql.endswith("LIMIT 100")
    assert '"b"."id" IN (SELECT id FROM onto_visible)' in sql
    assert out["params"] == {"f0": "Delivery Orders", "f1": "done"}


def test_filter_values_are_params_never_sql_text():
    hostile = "x'; DROP TABLE stock_picking; --"
    out = explore.compile_explore(explore_body(filters=[{"path": "state", "op": "=", "value": hostile}]))
    assert out["params"] == {"f0": hostile}
    assert "DROP" not in out["sql"] and hostile not in out["sql"]


def test_bindings_name_every_path_the_sql_reads():
    out = explore.compile_explore(explore_body())
    assert out["bindings"] == {
        "picking_type_id.warehouse_id.code": "picking_type_id.warehouse_id.code",  # group column: the requested path
        "j1": "picking_type_id",  # join prefixes are the paths Odoo must also check
        "j2": "picking_type_id.warehouse_id",
        "f0": "picking_type_id.name",
        "f1": "state",
    }


def test_an_in_list_becomes_one_param_per_value():
    out = explore.compile_explore(explore_body(filters=[{"path": "state", "op": "in", "value": ["done", "cancel"]}]))
    assert '"b"."state" IN (%(f0_0)s, %(f0_1)s)' in out["sql"]
    assert out["params"] == {"f0_0": "done", "f0_1": "cancel"}


def test_a_count_with_a_path_and_a_sum_on_a_number():
    paths = {**PATHS, "product_uom_qty": [{"field": "product_uom_qty", "type": "float", "model": "stock.picking"}]}
    body = explore_body(fields=list(paths), group_by=[], filters=[], aggregate={"op": "sum", "path": "product_uom_qty"})
    body["paths"] = paths
    out = explore.compile_explore(body)
    assert 'SUM("b"."product_uom_qty") AS "_aggregate"' in out["sql"] and "GROUP BY" not in out["sql"]


@pytest.mark.parametrize("tables", [
    {"stock.picking": "public.stock_picking", "stock.picking.type": "stock_picking_type", "stock.warehouse": "stock_warehouse"},
    {"stock.picking": "stock_picking; DROP TABLE x", "stock.picking.type": "stock_picking_type", "stock.warehouse": "stock_warehouse"},
])
def test_a_schema_qualified_or_hostile_table_is_refused(tables):
    body = explore_body()
    body["tables"] = tables
    with pytest.raises(SpecError):
        explore.compile_explore(body)


def test_a_hop_that_does_not_chain_into_the_next_model_is_refused():
    body = explore_body()
    body["paths"] = {**PATHS, "picking_type_id.warehouse_id.code": [
        {**_warehouse_hops()[0], "comodel": "stock.warehouse"},  # wrong comodel: the next hop is on stock.picking.type
        _warehouse_hops()[1], _warehouse_hops()[2]]}
    with pytest.raises(SpecError):
        explore.compile_explore(body)


def test_a_path_outside_the_requested_fields_is_refused():
    body = explore_body(group_by=["picking_type_id.name"], fields=["state"])
    with pytest.raises(SpecError):
        explore.compile_explore(body)


def test_sum_over_a_char_field_is_refused():
    with pytest.raises(SpecError):
        explore.compile_explore(explore_body(aggregate={"op": "sum", "path": "state"}))


def test_a_filter_with_no_value_is_refused():
    with pytest.raises(SpecError):
        explore.compile_explore(explore_body(filters=[{"path": "state", "op": "=", "value": None}]))


# --- the lookup SQL (pure) ------------------------------------------------------------------

def test_lookup_reads_the_view_with_the_visibility_predicate(templates):
    out = explore.compile_lookup(templates, "sales_order", "customer")
    assert out["sql"] == 'SELECT "customer" FROM "wad_onto_sales_order_view" WHERE _onto_id IN (SELECT id FROM onto_visible)'
    assert out["params"] == {} and out["bindings"] == {"customer": "partner_id"}


def test_lookup_refuses_personal_data_and_unknown_names(templates):
    with pytest.raises(SpecError):
        explore.compile_lookup(templates, "partner", "name")  # pii: true in core.yaml
    with pytest.raises(LookupError):
        explore.compile_lookup(templates, "no_such_object", "customer")
    with pytest.raises(LookupError):
        explore.compile_lookup(templates, "sales_order", "no_such_column")


# --- the routes -----------------------------------------------------------------------------

class FakeLicenseCache:
    async def validate_license_key(self, session, license_key, organization_id=None):
        return {"validated": license_key == "good-key"}


def _no_stored_release(session, tenant_id, digest):
    raise LookupError(digest)


@pytest.fixture()
def client(monkeypatch):
    """No database: one registered tenant, a licence stub, no stored releases, templates on a dev install."""
    monkeypatch.setenv("ENVIRONMENT", "DEV")
    monkeypatch.setattr(auth, "get_license_cache_service", FakeLicenseCache)
    monkeypatch.setattr(compile_mod, "tenant_by_url",
                        lambda session, url: SimpleNamespace(id=uuid.UUID(int=1)) if url == ODOO else None)
    monkeypatch.setattr(store, "load_release", _no_stored_release)
    monkeypatch.setattr(store, "open_drift", lambda session, tenant_id: {})
    monkeypatch.setattr(ba, "tenant_limiter", ba.RateLimiter(1000, 60))
    app = FastAPI()
    app.dependency_overrides[db.get_session] = lambda: None
    mount(app)
    return TestClient(app)


def test_explore_route_returns_sql_params_and_bindings(client):
    r = client.post("/onto/explore/compile", json=explore_body(), headers=KEY)
    assert r.status_code == 200
    assert set(r.json()) == {"sql", "params", "bindings"}
    assert r.json()["params"] == {"f0": "Delivery Orders", "f1": "done"}


def test_explore_route_refuses_a_bad_spec_with_422(client):
    body = explore_body(model="stock.picking; DROP")
    assert client.post("/onto/explore/compile", json=body, headers=KEY).status_code == 422


def test_explore_route_needs_the_server_key(client):
    assert client.post("/onto/explore/compile", json=explore_body(), headers={"X-Odoo-Url": ODOO}).status_code == 401


def test_lookup_route_returns_the_release_hash_and_sql(client, templates):
    r = client.post("/onto/lookup/compile", json={"release": templates.digest(), "object": "sales_order",
                                                  "column": "customer"}, headers=KEY)
    assert r.status_code == 200
    out = r.json()
    assert out["release"] == templates.digest() and out["bindings"] == {"customer": "partner_id"}
    assert out["sql"].startswith('SELECT "customer" FROM "wad_onto_sales_order_view"')


def test_lookup_route_refuses_pii_with_422_and_unknown_names_with_404(client, templates):
    pii = client.post("/onto/lookup/compile", json={"release": templates.digest(), "object": "partner",
                                                    "column": "name"}, headers=KEY)
    unknown = client.post("/onto/lookup/compile", json={"release": templates.digest(), "object": "sales_order",
                                                        "column": "no_such_column"}, headers=KEY)
    assert pii.status_code == 422
    assert unknown.status_code == 404
