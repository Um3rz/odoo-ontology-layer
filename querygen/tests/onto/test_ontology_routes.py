"""Ontology Studio routes on the scratch database (plan 1.9, querygen half).

Odoo is stubbed: each bearer token maps to a whoami answer (a steward, a plain user, and another
tenant's steward). The six committed templates are the release under test. Proposals are rows
written straight into onto_proposal (the Builder that creates them is sub-phase 2.3).
Every route is steward-only, so the reads below are made by a steward too; A_VIEWER gets 404 everywhere.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db
from app.db.onto import OntoProposal, Tenant
from app.onto_routes import mount
from app.ontology import store
from app.ontology.loader import load_release
from app.utils import browser_auth as ba

STEWARD = "odoo_dashboards_agents.group_onto_steward"
ODOO_A, ODOO_B = "https://a.example.test", "https://b.example.test"
UUID_A, UUID_B = "uuid-a", "uuid-b"
WHOAMI = {
    "steward.sig": {"user": {"id": 7}, "groups": [STEWARD], "database": {"uuid": UUID_A}, "ai_enabled": True},
    "viewer.sig": {"user": {"id": 8}, "groups": [], "database": {"uuid": UUID_A}, "ai_enabled": True},
    "other.sig": {"user": {"id": 9}, "groups": [STEWARD], "database": {"uuid": UUID_B}, "ai_enabled": True},
}
A_STEWARD = {"Authorization": "Bearer steward.sig", "X-Odoo-Url": ODOO_A}
A_VIEWER = {"Authorization": "Bearer viewer.sig", "X-Odoo-Url": ODOO_A}
B_STEWARD = {"Authorization": "Bearer other.sig", "X-Odoo-Url": ODOO_B}
TEMPLATE_HASH = load_release().digest()
COUNTS = {"domain": 36, "object": 26, "rule": 21, "metric": 51, "org_role": 1, "link": 43}

LOTS_ON_HAND = {
    "key": "lots_on_hand", "label": "Lots on hand", "object": "stock_lot", "unit": "count",
    "additivity": "additive", "aggregation": "count",
    "synonyms": ["lots on hand", "lot count"],
}


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
    session.add(Tenant(odoo_base_url=ODOO_B, db_uuid=UUID_B))
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


def add_proposal(session, tenant_id, node_key, node, kind="metric"):
    row = OntoProposal(tenant_id=tenant_id, node_key=node_key, payload={"kind": kind, "node": node},
                       confidence=0.8, validation={"ok": True}, state="proposed")
    session.add(row)
    session.commit()
    return row.id


def publish(client, headers=A_STEWARD):
    r = client.post("/ontology/releases", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def current_ids(client, headers=A_STEWARD):
    return [r["release_id"] for r in client.get("/ontology/releases", headers=headers).json()["releases"]
            if r["current"]]


# --- auth and the no-release state -----------------------------------------------------------


def test_unauthenticated_calls_are_401(client):
    assert client.get("/ontology/search", params={"q": "stock"}, headers={"X-Odoo-Url": ODOO_A}).json() == {
        "error": "invalid_token"}
    assert client.post("/ontology/releases", headers={"X-Odoo-Url": ODOO_A}).status_code == 401


def test_reads_before_any_publish_say_so(client):
    r = client.get("/ontology/search", params={"q": "stock"}, headers=A_STEWARD)
    assert r.status_code == 404 and r.json() == {"detail": "no release published"}
    assert client.get("/ontology/taxonomy", headers=A_STEWARD).status_code == 404
    assert client.get("/ontology/releases", headers=A_STEWARD).json() == {"releases": []}


# --- reads on the six real templates ---------------------------------------------------------


def test_publish_stores_the_templates_and_reports_counts(client):
    out = publish(client)
    assert out["hash"] == TEMPLATE_HASH
    assert out["counts"] == COUNTS
    assert out["current"] is True and out["previous"] is None and out["drift"] is None
    assert out["proposals_applied"] == 0


def test_search_ranks_exact_synonym_matches_first(client):
    publish(client)
    r = client.get("/ontology/search", params={"q": "on hand"}, headers=A_STEWARD)
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["key"] for x in results[:2]] == ["on_hand_qty", "stock_position"]
    assert all(x["exact"] for x in results[:2])
    assert results[0]["trust"] == "Uncertified" and results[0]["kind"] == "metric"
    assert r.json()["release"] == TEMPLATE_HASH


def test_search_treats_hyphens_and_underscores_as_spaces(client):
    publish(client)
    keys = [x["key"] for x in client.get("/ontology/search", params={"q": "ON-HAND quantity"},
                                         headers=A_STEWARD).json()["results"]]
    assert "on_hand_qty" in keys


def test_search_parameters_are_checked(client):
    publish(client)
    assert client.get("/ontology/search", headers=A_STEWARD).status_code == 422  # q is required
    assert client.get("/ontology/search", params={"q": ""}, headers=A_STEWARD).status_code == 422
    assert client.get("/ontology/search", params={"q": "x", "limit": 0}, headers=A_STEWARD).status_code == 422


def test_metric_node_card_has_additivity_binding_and_trust(client):
    publish(client)
    card = client.get("/ontology/nodes/on_hand_qty", headers=A_STEWARD).json()
    assert card["kind"] == "metric" and card["domain"] == "inventory.stock"
    assert card["additivity"] == {"additivity": "semi_additive", "aggregation": "sum", "over_time": "last",
                                  "time_property": None}
    assert card["binding"] == {"measure": "quantity", "bindings": {"16": "quantity"}}
    assert card["trust"] == "Uncertified"
    assert card["provenance"]["source"] == "template"


def test_object_node_card_lists_properties_links_and_access(client):
    publish(client)
    card = client.get("/ontology/nodes/stock_position", headers=A_STEWARD).json()
    assert card["kind"] == "object" and card["trust"] is None and card["additivity"] is None
    assert card["definition"]["base_model"] == "stock.quant"
    template = next(o for o in load_release().objects if o.key == "stock_position")
    assert [p["key"] for p in card["properties"]] == [p.key for p in template.properties]
    assert "position_product" in [link["key"] for link in card["links"]["out"]]
    assert card["provenance"]["group_xmlids"] == ["stock.group_stock_user"]


def test_link_card_and_unknown_key(client):
    publish(client)
    link = client.get("/ontology/nodes/position_product", headers=A_STEWARD).json()
    assert link["kind"] == "link" and link["definition"]["target"] == "product"
    assert client.get("/ontology/nodes/no_such_node", headers=A_STEWARD).status_code == 404


def test_graph_walks_links_from_a_root_and_is_depth_limited(client):
    publish(client)
    one = client.get("/ontology/graph", params={"root": "stock_position", "depth": 1}, headers=A_STEWARD).json()
    ids = {n["id"] for n in one["nodes"]}
    assert {"stock_position", "product", "location", "stock_lot", "company"} <= ids
    assert "position_product" in {e["key"] for e in one["edges"]}
    zero = client.get("/ontology/graph", params={"root": "stock_position", "depth": 0}, headers=A_STEWARD).json()
    assert [n["id"] for n in zero["nodes"]] == ["stock_position"] and zero["edges"] == []
    three = client.get("/ontology/graph", params={"root": "stock_position", "depth": 3}, headers=A_STEWARD).json()
    assert ids <= {n["id"] for n in three["nodes"]}


def test_graph_refusals_and_the_whole_map(client):
    publish(client)
    assert client.get("/ontology/graph", params={"root": "stock_position", "depth": 4},
                      headers=A_STEWARD).status_code == 422
    assert client.get("/ontology/graph", params={"root": "no_such_node"}, headers=A_STEWARD).status_code == 404
    whole = client.get("/ontology/graph", headers=A_STEWARD).json()
    assert len([n for n in whole["nodes"] if n["category"] == "object"]) == COUNTS["object"]
    assert len(whole["edges"]) == COUNTS["link"]


def test_taxonomy_nests_domains_objects_and_metrics(client):
    publish(client)
    tree = client.get("/ontology/taxonomy", headers=A_STEWARD).json()["domains"]
    roots = {d["key"]: d for d in tree}
    assert {"inventory", "core", "org", "purchase", "pos", "sales"} <= set(roots)
    stock = next(d for d in roots["inventory"]["domains"] if d["key"] == "inventory.stock")
    position = next(o for o in stock["objects"] if o["key"] == "stock_position")
    assert "on_hand_qty" in {m["key"] for m in position["metrics"]}
    products = next(d for d in roots["core"]["domains"] if d["key"] == "core.products")
    assert {o["key"] for o in products["objects"]} == {"product", "product_category"}


def test_metrics_list_trust_filter_and_lineage(client):
    publish(client)
    everything = client.get("/ontology/metrics", headers=A_STEWARD).json()["metrics"]
    assert len(everything) == COUNTS["metric"]
    assert {m["trust"] for m in everything} == {"Uncertified"}  # no ground truth is stored yet
    assert client.get("/ontology/metrics", params={"trust": "Certified"}, headers=A_STEWARD).json()["metrics"] == []
    assert client.get("/ontology/metrics", params={"trust": "Gold"}, headers=A_STEWARD).status_code == 422
    by_key = {m["key"]: m for m in everything}
    assert by_key["count_accuracy"]["lineage"] == [
        {"name": "counted_lines", "kind": "metric"}, {"name": "matched_lines", "kind": "metric"}]
    assert by_key["cogs"]["lineage"] == [{"name": "value", "kind": "property"}]


def test_a_template_metric_with_a_passed_ground_truth_is_certified(client, session, tenant_a):
    """Nothing writes ground_truth yet (1.10 will). This checks the rule the badge follows."""
    store.publish(session, tenant_a.id, load_release(), provenance={"on_hand_qty": {"ground_truth": "passed"}})
    certified = client.get("/ontology/metrics", params={"trust": "Certified"}, headers=A_STEWARD).json()["metrics"]
    assert [m["key"] for m in certified] == ["on_hand_qty"]
    assert client.get("/ontology/nodes/on_hand_qty", headers=A_STEWARD).json()["trust"] == "Certified"


def test_access_summary_defaults_and_unset_objects(client):
    publish(client)
    objects = {o["key"]: o for o in client.get("/ontology/access", headers=A_STEWARD).json()["objects"]}
    assert objects["stock_position"]["group_xmlids"] == ["stock.group_stock_user"]
    assert objects["stock_position"]["access_source"] == "domain_default"
    assert objects["stock_valuation"]["group_xmlids"] == ["stock.group_stock_manager"]
    assert objects["pos_order"]["group_xmlids"] == ["point_of_sale.group_pos_user"]
    assert objects["product"]["group_xmlids"] == [] and objects["product"]["access_source"] == "unset"
    assert objects["product"]["engine_only"] is True  # no groups: engine only, the same as the provision payload (3.2a)
    assert objects["stock_position"]["engine_only"] is False


def test_schema_is_not_built_yet(client):
    assert client.get("/ontology/schema", headers=A_STEWARD).status_code == 501
    assert client.get("/ontology/schema", headers=A_VIEWER).status_code == 404
    assert client.get("/ontology/schema", headers={"X-Odoo-Url": ODOO_A}).status_code == 401


# --- stewards only: every route, for a signed-in non-steward ------------------------------------


def test_non_stewards_get_the_same_404_on_every_ontology_route(client, session, tenant_a):
    first = publish(client)
    pid = add_proposal(session, tenant_a.id, "lots_on_hand", LOTS_ON_HAND)
    routes = [
        ("GET", "/ontology/search?q=stock"),
        ("GET", "/ontology/nodes/on_hand_qty"),
        ("GET", "/ontology/graph?root=stock_position&depth=1"),
        ("GET", "/ontology/taxonomy"),
        ("GET", "/ontology/metrics"),
        ("GET", "/ontology/proposals"),
        ("POST", f"/ontology/proposals/{pid}/approve"),
        ("POST", f"/ontology/proposals/{pid}/revoke"),
        ("GET", "/ontology/releases"),
        ("POST", "/ontology/releases"),
        ("POST", f"/ontology/releases/{first['hash']}/activate"),
        ("GET", "/ontology/access"),
        ("GET", "/ontology/schema"),
    ]
    for method, path in routes:
        r = client.request(method, path, headers=A_VIEWER)
        assert r.status_code == 404 and r.json() == {"detail": "not found"}, (method, path)
    # Nothing moved: the proposal is still a draft and the publish was the only release.
    assert client.get("/ontology/proposals", headers=A_STEWARD).json()["proposals"][0]["state"] == "proposed"
    assert current_ids(client) == [first["release_id"]]


def test_steward_gets_404_for_a_proposal_that_does_not_exist(client):
    publish(client)
    assert client.post("/ontology/proposals/999/approve", headers=A_STEWARD).status_code == 404


def test_publishing_twice_gives_the_same_release(client):
    first = publish(client)
    second = publish(client)
    assert second["hash"] == first["hash"] == TEMPLATE_HASH
    assert second["release_id"] == first["release_id"]
    assert second["previous"] is None and second["drift"] is None
    releases = client.get("/ontology/releases", headers=A_STEWARD).json()["releases"]
    assert len(releases) == 1 and releases[0]["current"] is True and releases[0]["counts"] == COUNTS


# --- proposals: approve, publish, revoke ------------------------------------------------------


def test_approved_proposal_is_published_and_revoking_leaves_the_release_alone(client, session, tenant_a):
    first = publish(client)
    pid = add_proposal(session, tenant_a.id, "lots_on_hand", LOTS_ON_HAND)

    listed = client.get("/ontology/proposals", headers=A_STEWARD).json()["proposals"]
    assert [(p["id"], p["state"], p["trust"]) for p in listed] == [(pid, "proposed", "Uncertified")]

    approved = client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD).json()
    assert approved["state"] == "approved" and approved["trust"] == "Approved" and approved["approver_uid"] == 7
    again = client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD).json()
    assert again["approved_at"] == approved["approved_at"]  # idempotent

    releases_before = client.get("/ontology/releases", headers=A_STEWARD).json()["releases"]
    assert [r["release_id"] for r in releases_before] == [first["release_id"]]  # approve touched no release
    published = publish(client)
    assert published["hash"] != first["hash"]
    assert published["current"] is True
    assert published["proposals_applied"] == 1 and published["counts"]["metric"] == COUNTS["metric"] + 1
    assert published["previous"] == {"release_id": first["release_id"], "hash": first["hash"]}
    assert published["drift"] == {"added": ["lots_on_hand"], "removed": [], "changed": []}

    approved_metrics = client.get("/ontology/metrics", params={"trust": "Approved"}, headers=A_STEWARD).json()
    assert [m["key"] for m in approved_metrics["metrics"]] == ["lots_on_hand"]
    card = client.get("/ontology/nodes/lots_on_hand", headers=A_STEWARD).json()
    assert card["provenance"]["source"] == "proposal" and card["provenance"]["approver_uid"] == 7

    revoked = client.post(f"/ontology/proposals/{pid}/revoke", headers=A_STEWARD).json()
    assert revoked["state"] == "revoked" and revoked["trust"] == "Uncertified" and revoked["approved_at"] is None
    after = client.get("/ontology/releases", headers=A_STEWARD).json()["releases"]
    assert [(r["release_id"], r["counts"]["metric"], r["current"]) for r in after] == [
        (published["release_id"], COUNTS["metric"] + 1, True), (first["release_id"], COUNTS["metric"], False)]
    # Revoking changes the draft only: the published release stays current until the next publish.


def test_a_proposal_that_is_not_a_valid_node_cannot_be_approved(client, session, tenant_a):
    publish(client)
    broken = {**LOTS_ON_HAND}
    del broken["unit"]
    pid = add_proposal(session, tenant_a.id, "lots_on_hand", broken)
    r = client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD)
    assert r.status_code == 422 and "unit" in r.json()["detail"]
    assert client.get("/ontology/proposals", headers=A_STEWARD).json()["proposals"][0]["state"] == "proposed"


def test_a_proposal_must_carry_its_own_node_key(client, session, tenant_a):
    publish(client)
    pid = add_proposal(session, tenant_a.id, "other_key", LOTS_ON_HAND)
    assert client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD).status_code == 422


def test_a_bad_binding_stops_publishing_with_every_problem_listed(client, session, tenant_a):
    publish(client)
    bad = {
        "key": "bad_lot_name", "label": "Bad lot name", "domain": "inventory.lots",
        "base_model": "stock.lot", "synonyms": ["bad lot name"],
        "properties": [{"key": "lot_name", "label": "Lot name", "role": "dimension",
                        "bindings": {"16": "no_such_field"}, "synonyms": ["lot name"]}],
    }
    pid = add_proposal(session, tenant_a.id, "bad_lot_name", bad, kind="object")
    assert client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD).status_code == 200
    r = client.post("/ontology/releases", headers=A_STEWARD)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_release" and r.json()["detail"]["problems"]
    assert len(client.get("/ontology/releases", headers=A_STEWARD).json()["releases"]) == 1


def test_a_proposal_cannot_reuse_a_key_already_in_the_release(client, session, tenant_a):
    publish(client)
    clash = {**LOTS_ON_HAND, "key": "on_hand_qty"}
    pid = add_proposal(session, tenant_a.id, "on_hand_qty", clash)
    r = client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD)  # 2.12: refused at approval
    assert r.status_code == 409 and "already in the release templates" in r.json()["detail"]


# --- the current release: a pointer that publish and activate move ------------------------------


def test_republishing_an_older_release_makes_it_current_again(client, session, tenant_a):
    templates = publish(client)
    pid = add_proposal(session, tenant_a.id, "lots_on_hand", LOTS_ON_HAND)
    client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD)
    with_lots = publish(client)
    assert with_lots["current"] is True and with_lots["previous"]["release_id"] == templates["release_id"]

    client.post(f"/ontology/proposals/{pid}/revoke", headers=A_STEWARD)
    back = publish(client)  # the templates are stored already: returned, and made current again
    assert back["release_id"] == templates["release_id"] and back["current"] is True
    assert back["previous"] == {"release_id": with_lots["release_id"], "hash": with_lots["hash"]}
    assert back["drift"] == {"added": [], "removed": ["lots_on_hand"], "changed": []}
    assert current_ids(client) == [templates["release_id"]]
    assert client.get("/ontology/nodes/lots_on_hand", headers=A_STEWARD).status_code == 404
    assert client.get("/ontology/search", params={"q": "stock"}, headers=A_STEWARD).json()["release"] == TEMPLATE_HASH


def test_activate_points_current_at_a_stored_release_and_back(client, session, tenant_a):
    templates = publish(client)
    pid = add_proposal(session, tenant_a.id, "lots_on_hand", LOTS_ON_HAND)
    client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD)
    with_lots = publish(client)
    client.post(f"/ontology/proposals/{pid}/revoke", headers=A_STEWARD)
    publish(client)  # the templates are current again

    r = client.post(f"/ontology/releases/{with_lots['hash']}/activate", headers=A_STEWARD)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["release_id"] == with_lots["release_id"] and out["current"] is True
    assert out["previous"] == {"release_id": templates["release_id"], "hash": templates["hash"]}
    assert out["drift"] == {"added": ["lots_on_hand"], "removed": [], "changed": []}
    assert "proposals_applied" not in out
    assert client.get("/ontology/nodes/lots_on_hand", headers=A_STEWARD).json()["provenance"]["source"] == "proposal"
    assert current_ids(client) == [with_lots["release_id"]]

    again = client.post(f"/ontology/releases/{with_lots['hash']}/activate", headers=A_STEWARD).json()
    assert again["current"] is True and again["previous"] is None and again["drift"] is None

    assert client.post(f"/ontology/releases/{'0' * 64}/activate", headers=A_STEWARD).status_code == 404
    assert client.post(f"/ontology/releases/{templates['hash']}/activate", headers=A_VIEWER).status_code == 404
    assert current_ids(client) == [with_lots["release_id"]]


# --- tenant isolation -------------------------------------------------------------------------


def test_another_tenant_sees_nothing_of_this_map(client, session, tenant_a):
    publish(client)
    pid = add_proposal(session, tenant_a.id, "lots_on_hand", LOTS_ON_HAND)
    assert client.get("/ontology/search", params={"q": "stock"}, headers=B_STEWARD).status_code == 404
    assert client.get("/ontology/releases", headers=B_STEWARD).json() == {"releases": []}
    assert client.get("/ontology/proposals", headers=B_STEWARD).json() == {"proposals": []}
    assert client.post(f"/ontology/proposals/{pid}/approve", headers=B_STEWARD).status_code == 404

    own = publish(client, headers=B_STEWARD)
    assert own["current"] is True
    seen_b = client.get("/ontology/releases", headers=B_STEWARD).json()["releases"]
    seen_a = client.get("/ontology/releases", headers=A_STEWARD).json()["releases"]
    assert [r["release_id"] for r in seen_b] == [own["release_id"]]
    assert [r["release_id"] for r in seen_a] != [own["release_id"]]
    assert client.get("/ontology/proposals", headers=A_STEWARD).json()["proposals"][0]["state"] == "proposed"


def test_another_tenants_current_release_is_its_own(client, session, tenant_a):
    publish(client)
    pid = add_proposal(session, tenant_a.id, "lots_on_hand", LOTS_ON_HAND)
    client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD)
    with_lots = publish(client)  # A's current release now has lots_on_hand
    assert client.post(f"/ontology/releases/{with_lots['hash']}/activate", headers=B_STEWARD).status_code == 404

    own = publish(client, headers=B_STEWARD)  # B's first publish is its own current release
    assert own["current"] is True and own["previous"] is None
    assert current_ids(client, A_STEWARD) == [with_lots["release_id"]]  # A's pointer did not move
    assert current_ids(client, B_STEWARD) == [own["release_id"]]


# --- build-and-go: the Builder approves what passed validation --------------------------------

LOT_REGISTRY = {
    "key": "lot_registry", "label": "Lot registry", "domain": "inventory.lots", "base_model": "stock.lot",
    "synonyms": ["lot registry"],
    "properties": [{"key": "lot_label", "label": "Lot label", "role": "dimension", "bindings": {"16": "name"},
                    "synonyms": ["lot label"]}],
}
LOT_REGISTRY_COUNT = {"key": "lot_registry_count", "label": "Registered lots", "object": "lot_registry",
                      "unit": "count", "additivity": "additive", "aggregation": "count", "synonyms": ["registered lots"]}


def builder_row(session, tenant_id, node_key, node, kind="metric", confidence=0.8, ok=True):
    row = OntoProposal(tenant_id=tenant_id, node_key=node_key, payload={"kind": kind, "source": "llm", "node": node},
                       confidence=confidence, validation={"ok": ok, "problems": [] if ok else ["no_such_field"]},
                       state="proposed")
    session.add(row)
    session.commit()
    return row.id


def test_auto_approve_takes_objects_first_and_the_release_publishes_them_as_unreviewed(
        client, session, tenant_a, monkeypatch):
    from app.onto_routes import ontology
    from app.routes.native_bi import _unreviewed

    publish(client)
    metric = builder_row(session, tenant_a.id, "lot_registry_count", LOT_REGISTRY_COUNT)  # the lower id comes first
    obj = builder_row(session, tenant_a.id, "lot_registry", LOT_REGISTRY, kind="object")
    order, approve = [], ontology._approve
    monkeypatch.setattr(ontology, "_approve", lambda row, uid, auto=False: (order.append(row.id), approve(row, uid, auto)))

    out = ontology.auto_approve(session, tenant_a.id)
    assert order == [obj, metric]
    assert out == {"approved": {"object": 1, "metric": 1}, "skipped": [], "failed": []}
    listed = {p["id"]: p for p in client.get("/ontology/proposals", headers=A_STEWARD).json()["proposals"]}
    assert [(listed[i]["state"], listed[i]["auto"], listed[i]["approver_uid"]) for i in (obj, metric)] == [
        ("approved", True, None)] * 2

    published = publish(client)
    assert published["proposals_applied"] == 2 and published["drift"]["added"] == ["lot_registry", "lot_registry_count"]

    def unreviewed():
        release, provenance, _ = ontology._release_with_approved(session, tenant_a.id)
        return _unreviewed(next(m for m in release.metrics if m.key == "lot_registry_count"), provenance)

    assert unreviewed()
    # A steward's approval makes an auto proposal reviewed; the metric is reviewed once its object is too.
    reviewed = client.post(f"/ontology/proposals/{metric}/approve", headers=A_STEWARD).json()
    assert reviewed["auto"] is False and reviewed["approver_uid"] == 7
    assert unreviewed()
    client.post(f"/ontology/proposals/{obj}/approve", headers=A_STEWARD)
    assert not unreviewed()


def test_auto_approve_keeps_the_higher_confidence_on_a_clash_and_leaves_failures_pending(client, session, tenant_a):
    from app.onto_routes import ontology

    publish(client)
    low = builder_row(session, tenant_a.id, "lots_on_hand", LOTS_ON_HAND, confidence=0.6)
    high = builder_row(session, tenant_a.id, "lots_on_hand", {**LOTS_ON_HAND, "label": "Lots in stock"}, confidence=0.9)
    broken = builder_row(session, tenant_a.id, "lot_registry", LOT_REGISTRY, kind="object", ok=False)
    template = builder_row(session, tenant_a.id, "on_hand_qty", {**LOTS_ON_HAND, "key": "on_hand_qty"})

    out = ontology.auto_approve(session, tenant_a.id)
    assert out["approved"] == {"metric": 1} and out["failed"] == [broken]
    assert {item["id"] for item in out["skipped"]} == {low, template}
    rows = {row.id: row for row in session.exec(select(OntoProposal)).all()}
    assert rows[high].state == "approved" and rows[low].state == rows[broken].state == "proposed"
    assert f"kept proposal {high}" in rows[low].payload["auto_note"]
    assert rows[broken].validation["problems"] == ["no_such_field"] and "auto_note" not in rows[broken].payload

    # A key the steward revoked is not approved again by the next build.
    client.post(f"/ontology/proposals/{high}/revoke", headers=A_STEWARD)
    again = builder_row(session, tenant_a.id, "lots_on_hand", LOTS_ON_HAND, confidence=0.95)
    out = ontology.auto_approve(session, tenant_a.id)
    assert out["approved"] == {} and again in {item["id"] for item in out["skipped"]}
    assert "revoked by a steward" in session.get(OntoProposal, again).payload["auto_note"]
