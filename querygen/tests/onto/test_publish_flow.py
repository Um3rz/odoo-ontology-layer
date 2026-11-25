"""Batch approval, the validation refusal on approve, and the provision payload of a publish (plan 2.5, contract 8).

Written, not run in this sub-phase (owner rule). Odoo and the fixtures are those of test_ontology_routes: the steward
headers, the six templates, the proposal rows written straight into onto_proposal.
"""
from tests.onto.test_ontology_routes import (  # noqa: F401  (client and tenant_a are fixtures)
    A_STEWARD, A_VIEWER, COUNTS, LOTS_ON_HAND, ODOO_A, TEMPLATE_HASH, client, publish, tenant_a)
from app.db.onto import OntoProposal
from app.ontology import builder, store

SECOND = {"key": "lot_total", "label": "Lot total", "object": "stock_lot", "unit": "count",
          "additivity": "additive", "aggregation": "count", "synonyms": ["lot total"]}


def node(key):
    """A valid metric node, distinct per key (a count of lots on stock_lot)."""
    return dict(SECOND, key=key, label=key.replace("_", " "), synonyms=[key.replace("_", " ")])


def proposal(session, tenant_id, node, *, batch=True, ok=True, confidence=0.9, source="builder",
             state="proposed", run_id=None):
    payload = {"kind": "metric", "source": source, "batch_approvable": batch, "node": node}
    if run_id is not None:
        payload["run_id"] = run_id
    row = OntoProposal(tenant_id=tenant_id, node_key=node["key"], payload=payload, confidence=confidence,
                       validation={"ok": ok, "problems": [] if ok else ["a field is missing"]}, state=state)
    session.add(row)
    session.commit()
    return row.id


def states(client):
    return {p["id"]: p["state"] for p in client.get("/ontology/proposals", headers=A_STEWARD).json()["proposals"]}


def draft(location_id, confidence=0.9, ambiguous=False):
    """An OrgMap draft as the Builder makes it (plan 2.3). An ambiguous draft has no role and says why."""
    return {"location_id": location_id, "complete_name": f"WH/Stock/{location_id}",
            "proposed_role": None if ambiguous else "qc_hold", "confidence": confidence, "ambiguous": ambiguous,
            "candidates": [] if ambiguous else ["qc_hold"],
            "problem": "two rules match: damaged, qc_hold" if ambiguous else None}


TEMPLATE_FIT = {"template": "inventory", "confidence": 0.93, "proposed": True, "objects": []}


def stored_fits(session, tenant_id):
    """One OrgMap draft (confident) and one template fit, stored the way the Builder stores them: (org id, template id)."""
    org, template = store.replace_builder_proposals(
        session, tenant_id, builder._org_rows([draft(8)], "run-1") + builder._template_rows([TEMPLATE_FIT], "run-1"))
    return org, template


# --- batch approval -------------------------------------------------------------------------


def test_batch_approve_takes_only_the_eligible_rows(client, session, tenant_a):
    eligible = proposal(session, tenant_a.id, LOTS_ON_HAND)
    not_batch = proposal(session, tenant_a.id, node("lot_total_b"), batch=False)
    invalid = proposal(session, tenant_a.id, node("lot_total_c"), ok=False)
    low = proposal(session, tenant_a.id, node("lot_total_d"), confidence=0.5)
    drift_row = proposal(session, tenant_a.id, node("lot_total_e"), source="drift")
    revoked = proposal(session, tenant_a.id, node("lot_total_f"), state="revoked")

    r = client.post("/ontology/proposals/batch-approve", headers=A_STEWARD)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["approved"] == [eligible]
    assert {s["id"]: s["reason"] for s in out["skipped"]} == {
        not_batch: "not batch-approvable",
        invalid: "validation failed",
        low: "confidence below 0.85",
        drift_row: "not batch-approvable",  # drift is never batch, whatever its flag says
    }
    assert states(client) == {eligible: "approved", not_batch: "proposed", invalid: "proposed",
                              low: "proposed", drift_row: "proposed", revoked: "revoked"}


def test_the_list_says_which_rows_batch_approval_would_take(client, session, tenant_a):
    eligible = proposal(session, tenant_a.id, LOTS_ON_HAND)
    not_batch = proposal(session, tenant_a.id, node("lot_total_b"), batch=False)
    rows = {p["id"]: p["batch_approvable"] for p in client.get("/ontology/proposals", headers=A_STEWARD).json()["proposals"]}
    assert rows == {eligible: True, not_batch: False}


def test_batch_approve_can_be_limited_to_one_builder_run(client, session, tenant_a):
    mine = proposal(session, tenant_a.id, LOTS_ON_HAND, run_id="run-1")
    other = proposal(session, tenant_a.id, node("lot_total_b"), run_id="run-2")
    out = client.post("/ontology/proposals/batch-approve", json={"run_id": "run-1"}, headers=A_STEWARD).json()
    assert out == {"approved": [mine], "skipped": [], "org_locations": []}
    assert states(client)[other] == "proposed"  # another run's row is not looked at at all


def test_a_proposal_that_failed_validation_cannot_be_approved_one_by_one(client, session, tenant_a):
    pid = proposal(session, tenant_a.id, LOTS_ON_HAND, ok=False, batch=False)
    r = client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD)
    assert r.status_code == 422
    assert r.json() == {"detail": {"error": "validation_failed", "problems": ["a field is missing"]}}
    assert states(client)[pid] == "proposed"
    assert client.post(f"/ontology/proposals/{pid}/revoke", headers=A_STEWARD).json()["state"] == "revoked"


# --- the provision payload (contract 8, Part D 3.2a) ----------------------------------------


def test_the_publish_reply_carries_the_provision_payload(client):
    prov = publish(client)["provision"]
    assert prov["release"] == {"hash": TEMPLATE_HASH}
    assert len(prov["objects"]) == COUNTS["object"]
    for obj in prov["objects"]:
        assert set(obj) == {"key", "label", "base_model", "sql", "columns", "group_xmlids", "engine_only", "spec_hash"}
        assert len(obj["spec_hash"]) == 64 and obj["engine_only"] is (not obj["group_xmlids"])
    stock = next(o for o in prov["objects"] if o["key"] == "stock_position")
    assert stock["group_xmlids"] == ["stock.group_stock_user"]  # inventory.stock, from DOMAIN_GROUPS
    assert stock["engine_only"] is False and stock["sql"].startswith("SELECT")
    product = next(o for o in prov["objects"] if o["key"] == "product")
    assert product["group_xmlids"] == [] and product["engine_only"] is True  # core has no groups: Odoo needs engine_only


def test_the_spec_hash_is_stable_and_the_get_route_returns_the_same_payload(client):
    first = publish(client)["provision"]
    assert publish(client)["provision"] == first  # the same content publishes the same hashes
    got = client.get(f"/ontology/releases/{TEMPLATE_HASH}/provision", headers=A_STEWARD)
    assert got.status_code == 200 and got.json() == first
    assert client.get(f"/ontology/releases/{'0' * 64}/provision", headers=A_STEWARD).status_code == 404


def test_the_new_routes_are_steward_only(client, session, tenant_a):
    publish(client)
    pid = proposal(session, tenant_a.id, LOTS_ON_HAND)
    for method, path in (("POST", "/ontology/proposals/batch-approve"),
                         ("GET", f"/ontology/releases/{TEMPLATE_HASH}/provision")):
        r = client.request(method, path, headers=A_VIEWER)
        assert r.status_code == 404 and r.json() == {"detail": "not found"}, path
    assert client.post("/ontology/proposals/batch-approve", headers={"X-Odoo-Url": ODOO_A}).status_code == 401
    assert states(client)[pid] == "proposed"  # nothing was approved by the refused calls


# --- D54: OrgMap drafts are applied in Odoo; template fits are informational -----------------


def test_an_org_row_approves_and_the_release_does_not_change(client, session, tenant_a):
    first = publish(client)
    [pid] = store.replace_builder_proposals(session, tenant_a.id, builder._org_rows([draft(8)], "run-1"))
    r = client.post(f"/ontology/proposals/{pid}/approve", headers=A_STEWARD)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "approved" and r.json()["payload"]["applied_in"] == "odoo"
    published = publish(client)
    assert published["hash"] == first["hash"] and published["proposals_applied"] == 0  # the release is unchanged


def test_batch_approve_lists_confident_org_rows_and_leaves_them_pending(client, session, tenant_a):
    first = publish(client)
    good, low, ambiguous = store.replace_builder_proposals(session, tenant_a.id, builder._org_rows(
        [draft(8), draft(7, confidence=0.8), draft(9, ambiguous=True)], "run-1"))
    out = client.post("/ontology/proposals/batch-approve", headers=A_STEWARD).json()
    assert out["approved"] == []  # batch never approves an org_location
    assert out["org_locations"] == [{"proposal_id": good, "location_id": 8, "role": "qc_hold"}]
    assert {s["id"]: s["reason"] for s in out["skipped"]} == {low: "not batch-approvable", ambiguous: "not batch-approvable"}
    assert set(states(client).values()) == {"proposed"}  # every org row is still pending
    listed = {p["id"]: p for p in client.get("/ontology/proposals", headers=A_STEWARD).json()["proposals"]}
    assert "applied_in" not in listed[good]["payload"]  # only the single approve marks it applied in Odoo
    one = client.post(f"/ontology/proposals/{good}/approve", headers=A_STEWARD).json()  # the browser, once Odoo accepted it
    assert one["state"] == "approved" and one["payload"]["applied_in"] == "odoo"
    assert publish(client)["hash"] == first["hash"]  # neither step changed a release


def test_a_template_fit_cannot_be_approved(client, session, tenant_a):
    _, template = stored_fits(session, tenant_a.id)
    r = client.post(f"/ontology/proposals/{template}/approve", headers=A_STEWARD)
    assert r.status_code == 422 and r.json() == {"detail": {"error": "informational"}}
    assert states(client)[template] == "proposed"


def test_template_fits_are_left_out_of_batch_approval_and_not_listed_as_skipped(client, session, tenant_a):
    org, template = stored_fits(session, tenant_a.id)
    out = client.post("/ontology/proposals/batch-approve", headers=A_STEWARD).json()
    assert out["approved"] == [] and out["skipped"] == []  # the org row is listed, not approved; the template is not listed
    assert [c["proposal_id"] for c in out["org_locations"]] == [org]
    assert states(client) == {org: "proposed", template: "proposed"}


def test_the_list_flags_template_fits_as_informational(client, session, tenant_a):
    org, template = stored_fits(session, tenant_a.id)
    listed = {p["id"]: p for p in client.get("/ontology/proposals", headers=A_STEWARD).json()["proposals"]}
    assert listed[template]["informational"] is True and listed[template]["batch_approvable"] is False
    assert listed[org]["informational"] is False and listed[org]["batch_approvable"] is True
