"""Ontology Studio routes, querygen half (plan 1.9; contract: scratchpad p19_studio_contract.md).

Every route is steward-only: anyone else gets 404 (the Studio is the only audience, and object-group
filtering cannot be done in querygen). Reads serve the tenant's current release, the one its pointer
row names (store.current_release). Publishing builds the release from the committed templates plus
the approved proposals, checks it, and stores it with store.publish, which also makes it current.
Activate points the current release back at an earlier one. Trust (D23) is derived in _trust.
Nothing here reads or writes customer data, and nothing here calls Odoo beyond the auth check.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session

from app import db
from app.db.onto import JobRun, OntoDrift, OntoEdge, OntoNode, OntoProposal, OntoRelease
from app.db.tenant import scoped
from app.ontology import builder, loader, store
from app.ontology import drift as drift_check
from app.ontology import provision as provisioning
from app.ontology.builder.profile import InputError
from app.ontology.schema import RESERVED, Domain, Link, Metric, Object, OrgRole, Property, Release, Rule, formula_names
from app.ontology.spec import SpecError
from app.systems import health
from app.utils.browser_auth import OntoAuth, browser_auth

STEWARD_GROUP = "odoo_dashboards_agents.group_onto_steward"
KINDS = {  # node kind -> (model, the Release field that holds it)
    "domain": (Domain, "domains"),
    "object": (Object, "objects"),
    "rule": (Rule, "rules"),
    "metric": (Metric, "metrics"),
    "org_role": (OrgRole, "org_roles"),
    "link": (Link, "links"),
}
# The domain -> Odoo group table (plan A2.2 layer 2; contract 8, Part D 3.2a). It is the only place a domain maps to
# security groups: an object's group_xmlids are stored at publish from its domain, and the provision payload carries
# them. The longest matching domain prefix wins. core and org have no entry, so their objects are "unset" (never open to
# everyone: empty means unset). account has no template yet; its row is here for when an account object is added.
DOMAIN_GROUPS = {
    "core.contacts": ["base.group_user"],
    "inventory": ["stock.group_stock_user"],
    "inventory.valuation": ["stock.group_stock_manager"],
    "purchase": ["purchase.group_purchase_user"],
    "sales": ["sales_team.group_sale_salesman"],
    "pos": ["point_of_sale.group_pos_user"],
    "account": ["account.group_account_readonly", "account.group_account_invoice"],
    "crm": ["sales_team.group_sale_salesman"],
}
LINK_FIELDS = ("key", "label", "source", "target", "via", "cardinality", "aggregate")


def _steward_only(auth: OntoAuth = Depends(browser_auth)) -> None:
    """Every /ontology route. Signed-in non-stewards get 404, never 403; unauthenticated callers get 401 first."""
    if STEWARD_GROUP not in (auth.whoami.get("groups") or []):
        raise HTTPException(404, "not found")


router = APIRouter(prefix="/ontology", tags=["ontology"], dependencies=[Depends(_steward_only)])


@dataclass
class _Item:
    kind: str
    key: str
    payload: dict
    provenance: dict


def _by_key(item: _Item) -> str:
    return item.key


# ---------------------------------------------------------------- who, which release, what it holds


def _uid(auth: OntoAuth) -> int | None:
    """The steward's Odoo user id (approver, publisher, activator). Only called after _steward_only."""
    return (auth.whoami.get("user") or {}).get("id")


def _current(session: Session, tenant_id) -> OntoRelease:
    row = store.current_release(session, tenant_id)
    if row is None:
        raise HTTPException(404, "no release published")
    return row


def _items(session: Session, tenant_id, row: OntoRelease) -> tuple[dict[str, _Item], dict[str, _Item]]:
    """The nodes and the links of one stored release, each by key."""
    # ponytail: reads the whole release on every request; cache by hash the way graph.py does when this shows up in a profile.
    nodes = {n.key: _Item(n.kind, n.key, n.payload or {}, n.provenance or {})
             for n in session.exec(scoped(OntoNode, tenant_id).where(OntoNode.release_id == row.id))}
    links = {e.payload["key"]: _Item("link", e.payload["key"], e.payload or {}, {})
             for e in session.exec(scoped(OntoEdge, tenant_id).where(OntoEdge.release_id == row.id))}
    return nodes, links


def _trust(kind: str, provenance: dict) -> str | None:
    """Trust badge for metrics (D23, A3.2); null for other kinds.

    Approved: a steward approved the proposal before the release was published.
    Certified: a template metric whose stored provenance says ground_truth "passed". Nothing writes
    that yet (1.10 and 2.4 will), so no metric is Certified today.
    Uncertified: everything else.
    """
    if kind != "metric":
        return None
    if provenance.get("source") == "proposal":
        return "Approved"
    if provenance.get("ground_truth") == "passed":
        return "Certified"
    return "Uncertified"


def _domain_of(item: _Item, nodes: dict[str, _Item]) -> str | None:
    if item.kind == "object":
        return item.payload["domain"]
    if item.kind == "metric" and item.payload["object"] in nodes:
        return nodes[item.payload["object"]].payload["domain"]
    return None


# ---------------------------------------------------------------- reads


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("_", " ").replace("-", " ").split())


def _match(query: str, item: _Item) -> tuple[int, str] | None:
    """(0, field) for an exact match, (1, field) for a substring, None for no match. Key, label, then synonyms."""
    fields = [("key", item.key), ("label", item.payload.get("label", "")),
              *(("synonym", s) for s in item.payload.get("synonyms", []))]
    found = None
    for name, text in fields:
        value = _norm(text)
        if value == query:
            return 0, name
        if found is None and query in value:
            found = (1, name)
    return found


@router.get("/search")
def search(
    session: db.SessionType,
    auth: OntoAuth = Depends(browser_auth),
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    row = _current(session, auth.tenant_id)
    nodes, links = _items(session, auth.tenant_id, row)
    query = _norm(q)
    hits = []
    for item in [*nodes.values(), *links.values()]:
        found = _match(query, item) if query else None
        if found:
            hits.append((found[0], item.key, item, found[1]))
    hits.sort(key=lambda hit: (hit[0], hit[1]))
    return {
        "release": row.hash,
        "q": q,
        "results": [
            {"key": item.key, "kind": item.kind, "label": item.payload.get("label"),
             "domain": _domain_of(item, nodes), "trust": _trust(item.kind, item.provenance),
             "matched": field, "exact": score == 0}
            for score, _, item, field in hits[:limit]
        ],
    }


def _suspension_fields(row: OntoDrift | None) -> dict:
    """What a metric or node card says about drift (plan 2.5): suspended (always present), and the reason in one line."""
    return {"suspended": row is not None,
            "suspended_reason": None if row is None else f"{row.node_key} reads {row.binding}: {row.problem}"}


def _suspensions(session: Session, tenant_id, row: OntoRelease, nodes: dict[str, _Item]) -> dict[str, OntoDrift]:
    """Node key -> the open drift row that suspends it. A metric is suspended when a property it reads is broken;
    an object when one of its own properties is. Other kinds are never suspended."""
    open_rows = store.open_drift(session, tenant_id)
    if not open_rows:
        return {}
    release = store.load_release(session, tenant_id, row.hash)
    found: dict[str, OntoDrift] = {}
    for item in nodes.values():
        if item.kind == "object":
            read = [f"{item.key}.{p['key']}" for p in item.payload.get("properties", [])]
        elif item.kind == "metric":
            read = [f"{item.payload['object']}.{name}" for name in drift_check.metric_props(release, item.key)]
        else:
            continue
        hits = [open_rows[node] for node in read if node in open_rows]
        if hits:
            found[item.key] = hits[0]
    return found


def _link_view(item: _Item) -> dict:
    return {field: item.payload.get(field) for field in LINK_FIELDS}


def _binding(item: _Item, nodes: dict[str, _Item]) -> dict | None:
    if item.kind == "object":
        return {"base_model": item.payload["base_model"]}
    measure = item.payload.get("measure") if item.kind == "metric" else None
    if measure:
        obj = nodes.get(item.payload["object"])
        props = {p["key"]: p for p in (obj.payload.get("properties", []) if obj else [])}
        return {"measure": measure, "bindings": props.get(measure, {}).get("bindings")}
    return None


def _lineage(item: _Item, nodes: dict[str, _Item]) -> list[dict]:
    """Direct inputs of derived_from, one level, sorted by name. A name is a property of the object, or a metric."""
    formula = item.payload.get("derived_from")
    if not formula:
        return []
    obj = nodes.get(item.payload["object"])
    props = {p["key"] for p in (obj.payload.get("properties", []) if obj else [])}
    out = []
    for name in sorted(formula_names(formula)):
        if name in RESERVED:
            kind = "days"
        elif name in props:
            kind = "property"
        else:
            kind = "metric"
        out.append({"name": name, "kind": kind})
    return out


def _card(item: _Item, nodes: dict[str, _Item], links: dict[str, _Item]) -> dict:
    p = item.payload
    is_object = item.kind == "object"
    return {
        "key": item.key,
        "kind": item.kind,
        "label": p.get("label"),
        "synonyms": p.get("synonyms", []),
        "domain": _domain_of(item, nodes),
        "definition": {k: v for k, v in p.items() if k not in ("key", "label", "synonyms", "properties")},
        "properties": p.get("properties", []) if is_object else [],
        "links": {
            "out": [_link_view(link) for link in links.values() if is_object and link.payload["source"] == item.key],
            "in": [_link_view(link) for link in links.values() if is_object and link.payload["target"] == item.key],
        },
        "additivity": ({k: p.get(k) for k in ("additivity", "aggregation", "over_time", "time_property")}
                       if item.kind == "metric" else None),
        "binding": _binding(item, nodes),
        "trust": _trust(item.kind, item.provenance),
        "provenance": item.provenance,
    }


@router.get("/nodes/{key}")
def node_card(key: str, session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    row = _current(session, auth.tenant_id)
    nodes, links = _items(session, auth.tenant_id, row)
    item = nodes.get(key) or links.get(key)  # node keys are unique across kinds; a link key can be shadowed
    if item is None:
        raise HTTPException(404, "not found")
    suspended = _suspensions(session, auth.tenant_id, row, {item.key: item})
    return {"release": row.hash, **_card(item, nodes, links), **_suspension_fields(suspended.get(item.key))}


@router.get("/graph")
def graph_view(
    session: db.SessionType,
    auth: OntoAuth = Depends(browser_auth),
    root: str | None = Query(None),
    depth: int = Query(2, ge=0, le=3),
) -> dict:
    row = _current(session, auth.tenant_id)
    nodes, links = _items(session, auth.tenant_id, row)
    if root is None:
        keys = {key for key, item in nodes.items() if item.kind == "object"}
        edges = list(links.values())
    else:
        if root not in nodes:
            raise HTTPException(404, "not found")
        keys = store.load_graph(session, auth.tenant_id, row.hash).within(root, depth)
        edges = [link for link in links.values()
                 if link.payload["source"] in keys and link.payload["target"] in keys]
    return {
        "release": row.hash,
        "root": root,
        "depth": depth,
        "nodes": [{"id": key, "name": nodes[key].payload.get("label"), "category": nodes[key].kind,
                   "domain": _domain_of(nodes[key], nodes)} for key in sorted(keys)],
        "edges": [{"source": e.payload["source"], "target": e.payload["target"], "key": e.key,
                   "name": e.payload.get("label"), "via": e.payload.get("via"),
                   "cardinality": e.payload.get("cardinality")} for e in sorted(edges, key=_by_key)],
    }


@router.get("/taxonomy")
def taxonomy(session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    row = _current(session, auth.tenant_id)
    nodes, _ = _items(session, auth.tenant_id, row)
    children: dict[str | None, list[_Item]] = {}
    objects: dict[str, list[_Item]] = {}
    metrics: dict[str, list[_Item]] = {}
    for item in nodes.values():
        if item.kind == "domain":
            children.setdefault(item.payload.get("parent"), []).append(item)
        elif item.kind == "object":
            objects.setdefault(item.payload["domain"], []).append(item)
        elif item.kind == "metric":
            metrics.setdefault(item.payload["object"], []).append(item)

    def object_view(obj: _Item) -> dict:
        return {"key": obj.key, "label": obj.payload.get("label"), "metrics": [
            {"key": m.key, "label": m.payload.get("label"), "trust": _trust(m.kind, m.provenance),
             "additivity": m.payload.get("additivity")}
            for m in sorted(metrics.get(obj.key, []), key=_by_key)]}

    def domain_view(domain: _Item) -> dict:
        return {"key": domain.key, "label": domain.payload.get("label"),
                "synonyms": domain.payload.get("synonyms", []),
                "domains": [domain_view(child) for child in sorted(children.get(domain.key, []), key=_by_key)],
                "objects": [object_view(obj) for obj in sorted(objects.get(domain.key, []), key=_by_key)]}

    return {"release": row.hash,
            "domains": [domain_view(d) for d in sorted(children.get(None, []), key=_by_key)]}


@router.get("/metrics")
def metrics(
    session: db.SessionType,
    auth: OntoAuth = Depends(browser_auth),
    trust: Literal["Certified", "Approved", "Uncertified"] | None = Query(None),
) -> dict:
    row = _current(session, auth.tenant_id)
    nodes, _ = _items(session, auth.tenant_id, row)
    suspended = _suspensions(session, auth.tenant_id, row, nodes)
    out = []
    for item in sorted((n for n in nodes.values() if n.kind == "metric"), key=_by_key):
        badge = _trust(item.kind, item.provenance)
        if trust and badge != trust:
            continue
        p = item.payload
        out.append({
            "key": item.key, "label": p.get("label"), "object": p["object"],
            "domain": _domain_of(item, nodes), "unit": p["unit"], "additivity": p["additivity"],
            "aggregation": p.get("aggregation"), "over_time": p.get("over_time"),
            "derived_from": p.get("derived_from"), "lineage": _lineage(item, nodes), "trust": badge,
            **_suspension_fields(suspended.get(item.key)),
        })
    return {"release": row.hash, "metrics": out}


@router.get("/access")
def access(session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    row = _current(session, auth.tenant_id)
    nodes, _ = _items(session, auth.tenant_id, row)
    objects = sorted((n for n in nodes.values() if n.kind == "object"), key=_by_key)
    return {"release": row.hash, "objects": [{
        "key": o.key, "label": o.payload.get("label"), "domain": o.payload["domain"],
        "base_model": o.payload["base_model"],
        "engine_only": provisioning.engine_only(o.provenance.get("group_xmlids", []), o.provenance.get("engine_only", False)),
        "group_xmlids": o.provenance.get("group_xmlids", []),
        "access_source": o.provenance.get("access_source", "unset"),
    } for o in objects]}


@router.get("/schema")
def schema(auth: OntoAuth = Depends(browser_auth)) -> None:
    """The Odoo schema graph (Phase 3). Not built in this sub-phase: a clear 501 for signed-in callers."""
    raise HTTPException(501, "the Odoo schema graph is not built yet (Phase 3)")


# ---------------------------------------------------------------- proposals (steward only)

# D54: an OrgMap draft is applied in Odoo, and a template fit is informational. Neither is ever a release node.
ORG_KIND = "org_location"
TEMPLATE_KIND = "template"
TERM_KIND = "term"
CUSTOM_FIELD_KIND = "custom_field"
PROPERTY_KIND = "property"  # a property for an existing object; its row holds the fields, no node (Builder plan 2)
RULE_FIELDS = ("key", "label", "object", "when", "synonyms")  # a Builder rule row holds these, no node
NOT_RELEASE_KINDS = {ORG_KIND, TEMPLATE_KIND}


def _proposal_view(row: OntoProposal) -> dict:
    payload = row.payload or {}
    node = payload.get("node") or {}
    return {
        "id": row.id,
        "node_key": row.node_key,
        "kind": payload.get("kind"),
        "label": node.get("label") or payload.get("label"),
        "state": row.state,
        "trust": "Approved" if row.state == "approved" else "Uncertified",
        "confidence": row.confidence,
        "validation": row.validation,
        "batch_approvable": payload.get("batch_approvable") is True,
        "informational": payload.get("kind") == TEMPLATE_KIND,
        "approver_uid": row.approver_uid,
        "approved_at": row.approved_at,
        "auto": row.state == "approved" and payload.get("approved_by") == AUTO_APPROVER,
        "payload": payload,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _parse(row: OntoProposal) -> tuple[str, object]:
    """The proposal's node as its model. 422 when it is not a valid node of its kind under its node_key.
    A term is (target key, synonym): it adds a synonym to a node of the release instead of adding a node."""
    payload = row.payload or {}
    kind, node = payload.get("kind"), payload.get("node")
    if kind == CUSTOM_FIELD_KIND:
        # (object key, property, field types): the field joins its object, and the object records the field's type,
        # read from this database when the draft was built, since no compiler field list holds an x_ field.
        if not isinstance(node, dict) or not isinstance(node.get("object"), str) or "odoo_fields" not in node:
            raise HTTPException(422, f"proposal {row.id}: build the ontology draft again to approve this custom field")
        try:
            prop = Property.model_validate(node.get("property"))
        except ValidationError as exc:
            raise HTTPException(422, f"proposal {row.id}: {exc.errors()[0]['msg']}") from exc
        if f"{node['object']}.{prop.key}" != row.node_key:
            raise HTTPException(422, f"proposal {row.id}: node key must be {row.node_key!r}")
        return kind, (node["object"], prop, node.get("odoo_fields") or {})
    if kind == PROPERTY_KIND:  # (object key, property): the property joins its object, a template one as an overlay
        try:
            prop = Property.model_validate({**{k: payload.get(k) for k in ("key", "label", "role", "unit", "synonyms")},
                                            "bindings": {"16": payload.get("binding")}})
        except ValidationError as exc:
            raise HTTPException(422, f"proposal {row.id}: {exc.errors()[0]['msg']}") from exc
        if not isinstance(payload.get("object"), str) or f"property.{payload['object']}.{prop.key}" != row.node_key:
            raise HTTPException(422, f"proposal {row.id}: node key must be property.<object>.<key>")
        return kind, (payload["object"], prop)
    if kind == "rule" and node is None:
        node = {k: payload.get(k) for k in RULE_FIELDS}
        if row.node_key != f"rule.{node['key']}":
            raise HTTPException(422, f"proposal {row.id}: node key must be rule.<key>")
        try:
            return kind, Rule.model_validate(node)
        except ValidationError as exc:
            raise HTTPException(422, f"proposal {row.id}: {exc.errors()[0]['msg']}") from exc
    if kind == TERM_KIND:
        target = node.get("target") if isinstance(node, dict) else None
        synonym = node.get("synonym") if isinstance(node, dict) else None
        if not isinstance(target, str) or not isinstance(synonym, str) or not synonym.strip():
            raise HTTPException(422, f"proposal {row.id}: a term needs a target and a synonym")
        return kind, (target, synonym.strip())
    if kind not in KINDS or not isinstance(node, dict):
        raise HTTPException(422, f"proposal {row.id}: payload needs a kind and a node")
    if node.get("key") != row.node_key:
        raise HTTPException(422, f"proposal {row.id}: node.key must be {row.node_key!r}")
    try:
        return kind, KINDS[kind][0].model_validate(node)
    except ValidationError as exc:
        error = exc.errors()[0]
        raise HTTPException(
            422, f"proposal {row.id}: {'.'.join(map(str, error['loc']))}: {error['msg']}") from exc


def _newest_org_rows(rows: list[OntoProposal]) -> list[OntoProposal]:
    """2.10e: an approved org_location repeats its node_key when an earlier run approved it again (before 2.9a). Only
    the newest approved row of each node_key is listed. Rows come in id order, so the last row of a key is the newest."""
    def approved_org(row: OntoProposal) -> bool:
        return row.state == "approved" and (row.payload or {}).get("kind") == ORG_KIND

    newest = {row.node_key: row for row in rows if approved_org(row)}
    return [row for row in rows if not approved_org(row) or newest[row.node_key] is row]


@router.get("/proposals")
def proposals(session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    rows = session.exec(scoped(OntoProposal, auth.tenant_id).order_by(OntoProposal.id)).all()
    return {"proposals": [_proposal_view(r) for r in _newest_org_rows(rows)]}


def _approvable(row: OntoProposal) -> None:
    """Refuse a template fit (D54), a draft that failed validation, or one that would not publish (422). None is approved."""
    if (row.payload or {}).get("kind") == TEMPLATE_KIND:
        raise HTTPException(422, {"error": "informational"})
    validation = row.validation or {}
    if validation.get("ok") is False:
        raise HTTPException(422, {"error": "validation_failed", "problems": validation.get("problems", [])})
    if (row.payload or {}).get("kind") != ORG_KIND:  # an org row has no node to parse; Odoo applies its role
        _parse(row)


def _org_roles() -> list[str]:
    """The location roles the release declares (org.yaml, location_role): the only roles a steward may pick (2.9e)."""
    return next(role.values for role in loader.load_release().org_roles if role.key == "location_role")


def _choose_role(row: OntoProposal, role: str) -> None:
    """The steward's pick for an org_location proposal (2.9e). Odoo has applied the role (orgmap/apply), and the stored
    row takes it and is validated again: an unknown role is a 422, and an approved row keeps the role it was approved
    with (409). The caller approves it and commits."""
    payload = row.payload or {}
    if payload.get("kind") != ORG_KIND:
        raise HTTPException(422, "a role applies to an org_location proposal only")
    roles = _org_roles()
    if role not in roles:
        raise HTTPException(422, {"error": "unknown_role", "roles": roles})
    if row.state == "approved" and payload.get("proposed_role") != role:
        raise HTTPException(409, "already approved with another role")
    row.payload = {**payload, "proposed_role": role, "ambiguous": False, "problem": None}
    row.validation = {"ok": True, "problems": []}


def _approve(row: OntoProposal, uid: int | None, auto: bool = False) -> None:
    """Mark a draft approved; approving it again keeps the first decision, except that a steward approving a proposal
    the Builder approved (auto) makes it reviewed. The caller commits.
    An org_location is approved once its role is applied in Odoo, so approval records applied_in (D54)."""
    payload = dict(row.payload or {})
    if row.state == "approved" and (auto or payload.get("approved_by") != AUTO_APPROVER):
        return
    payload.pop("approved_by", None)
    if auto:
        payload["approved_by"] = AUTO_APPROVER
    if payload.get("kind") == ORG_KIND:
        payload["applied_in"] = "odoo"
    row.payload = payload
    row.state, row.approver_uid, row.approved_at = "approved", uid, datetime.now(timezone.utc)


def _template_keys() -> set[str]:
    """Every node key of the templates, and each template property as object.property (a custom field's key)."""
    release = loader.load_release()
    return ({node.key for _, field in KINDS.values() for node in getattr(release, field)} |
            {f"{obj.key}.{prop.key}" for obj in release.objects for prop in obj.properties})


def _release_key(row: OntoProposal) -> str:
    """The key a proposal takes in the release: a property's object.key (unique within its object, like a custom
    field's), a Builder rule's own key (global, like a metric's), else its node_key."""
    payload = row.payload or {}
    if payload.get("kind") == PROPERTY_KIND:
        return f"{payload.get('object')}.{payload.get('key')}"
    if payload.get("kind") == "rule" and payload.get("node") is None:
        return str(payload.get("key"))
    return row.node_key


def _held_by_other(session: Session, tenant_id, row: OntoProposal) -> str | None:
    """2.12: why approving this proposal would repeat a release key, or None. The key is in the release templates, or
    another approved proposal holds it. An org_location and a template fit are not release nodes, so they never repeat
    one, and a row that is already approved is not a new approval."""
    if (row.payload or {}).get("kind") in NOT_RELEASE_KINDS or row.state == "approved":
        return None
    key = _release_key(row)
    if key in _template_keys():
        return f"key {key!r} is already in the release templates"
    held = session.exec(scoped(OntoProposal, tenant_id).where(OntoProposal.state == "approved")).all()
    other = next((r for r in held if r.id != row.id and _release_key(r) == key), None)
    if other is not None:
        return f"key {key!r} is already held by approved proposal {other.id}"
    return _link_held(session, tenant_id, row)


def _link_keys(row: OntoProposal) -> list[str]:
    return [link["key"] for link in (row.payload or {}).get("links") or [] if isinstance(link, dict) and link.get("key")]


def _link_held(session: Session, tenant_id, row: OntoProposal) -> str | None:
    """The links an object proposal brings repeat a template key or a key another approved proposal holds."""
    keys = _link_keys(row)
    if not keys:
        return None
    template = _template_keys()
    approved = [r for r in session.exec(scoped(OntoProposal, tenant_id).where(
        OntoProposal.state == "approved")).all() if r.id != row.id]
    holders = {r.node_key: r.id for r in approved} | {k: r.id for r in approved for k in _link_keys(r)}
    for key in keys:
        if key in template:
            return f"link key {key!r} is already in the release templates"
        if key in holders:
            return f"link key {key!r} is already held by approved proposal {holders[key]}"
    return None


def _decide(session: Session, auth: OntoAuth, proposal_id: int, state: str, role: str | None = None) -> dict:
    """Approve or revoke a draft. Only the proposal row changes: published releases are never touched."""
    uid = _uid(auth)
    row = session.exec(scoped(OntoProposal, auth.tenant_id).where(OntoProposal.id == proposal_id)).first()
    if row is None:
        raise HTTPException(404, "not found")
    if state == "approved":
        if role is not None:
            _choose_role(row, role)
        _approvable(row)
        held = _held_by_other(session, auth.tenant_id, row)
        if held is not None:
            raise HTTPException(409, held)
        _approve(row, uid)
    elif row.state != "revoked":
        # approver_uid and approved_at hold the last decision; the table has no separate revoke columns.
        row.state, row.approver_uid, row.approved_at = "revoked", uid, None
    session.add(row)
    session.commit()
    session.refresh(row)
    return _proposal_view(row)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = None  # an org_location: the location role the steward picked in Odoo (2.9e)


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: int, session: db.SessionType, auth: OntoAuth = Depends(browser_auth),
            body: ApproveBody | None = None) -> dict:
    return _decide(session, auth, proposal_id, "approved", body.role if body else None)


@router.post("/proposals/{proposal_id}/revoke")
def revoke(proposal_id: int, session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    return _decide(session, auth, proposal_id, "revoked")


def _batch_problem(row: OntoProposal) -> str | None:
    """Why batch approval leaves a pending proposal for one-by-one review, or None when the row is eligible. Drift is never batch.
    An eligible org_location is listed for the browser, not approved (D54)."""
    payload = row.payload or {}
    if payload.get("source") == "drift" or payload.get("batch_approvable") is not True:
        return "not batch-approvable"
    if (row.validation or {}).get("ok") is not True:
        return "validation failed"
    if row.confidence is None or row.confidence < builder.BATCH_CONFIDENCE:
        return f"confidence below {builder.BATCH_CONFIDENCE}"
    if payload.get("kind") != ORG_KIND:
        try:
            _parse(row)
        except HTTPException as exc:
            return str(exc.detail)
    return None


class BatchApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = None  # only the proposals of this Builder run


@router.post("/proposals/batch-approve")
def batch_approve(session: db.SessionType, auth: OntoAuth = Depends(browser_auth),
                  body: BatchApprove | None = None) -> dict:
    """Approve every pending proposal that batch approval takes (plan 2.5). Every other pending proposal stays, with its reason.
    Template fits are left out entirely (D54). An org_location is never approved here: the eligible ones come back in
    org_locations as candidates, still pending. The browser applies each in Odoo, then approves it one by one."""
    uid = _uid(auth)
    run_id = body.run_id if body else None
    rows = session.exec(scoped(OntoProposal, auth.tenant_id).where(OntoProposal.state == "proposed")
                        .order_by(OntoProposal.id)).all()
    approved, skipped, org_locations = [], [], []
    for row in rows:
        if run_id is not None and (row.payload or {}).get("run_id") != run_id:
            continue
        payload = row.payload or {}
        if payload.get("kind") == TEMPLATE_KIND:
            continue  # informational: not approved, and not listed as skipped
        problem = _batch_problem(row)
        if problem is not None:
            skipped.append({"id": row.id, "reason": problem})
        elif payload.get("kind") == ORG_KIND:
            org_locations.append({"proposal_id": row.id, "location_id": payload["location_id"],
                                  "role": payload["proposed_role"]})
        elif (held := _held_by_other(session, auth.tenant_id, row)) is not None:
            skipped.append({"id": row.id, "reason": held})  # a batch lists the refusal, it does not fail the other rows
        else:
            _approve(row, uid)
            approved.append(row.id)
    session.commit()
    return {"approved": approved, "skipped": skipped, "org_locations": org_locations}


# Build-and-go: after a Builder run, the Builder approves what passed validation and the release publishes; review is
# optional. Objects first, then what extends them, so a property, rule or metric lands on an approved object.
AUTO_APPROVER = "builder:auto"  # payload.approved_by of a proposal the Builder approved itself
AUTO_ORDER = {"object": 0, CUSTOM_FIELD_KIND: 1, PROPERTY_KIND: 1, "rule": 2, "metric": 3, TERM_KIND: 4}


def _note(row: OntoProposal, reason: str) -> None:
    row.payload = {**(row.payload or {}), "auto_note": reason}


def auto_approve(session: Session, tenant_id, org_applied=()) -> dict:
    """Approve, as the Builder (AUTO_APPROVER), every pending Builder proposal that passed validation. Commits.

    A proposal that failed validation stays pending with its problems. When two proposals take one release key, the
    higher confidence wins and the other stays pending with a note (auto_note), as does one that repeats a template or
    approved key, or a key a steward revoked before. An org_location is approved only when Odoo applied its role
    (its id is in org_applied) and it is not ambiguous. Template fits are informational and stay as they are."""
    rows = session.exec(scoped(OntoProposal, tenant_id).order_by(OntoProposal.id)).all()
    rejected = {_release_key(r) for r in rows if r.state == "revoked"}
    approved = [r for r in rows if r.state == "approved" and (r.payload or {}).get("kind") not in NOT_RELEASE_KINDS]
    held = _template_keys() | {_release_key(r) for r in approved} | {k for r in approved for k in _link_keys(r)}
    counts: dict[str, int] = {}
    skipped, failed, candidates = [], [], []
    for row in rows:
        payload = row.payload or {}
        if row.state != "proposed" or payload.get("source") not in store.BUILDER_SOURCES or \
                payload.get("kind") == TEMPLATE_KIND:
            continue
        if payload.get("kind") == ORG_KIND:
            if row.id in org_applied and not payload.get("ambiguous") and (row.validation or {}).get("ok"):
                _approve(row, None, auto=True)
                counts[ORG_KIND] = counts.get(ORG_KIND, 0) + 1
            continue
        try:
            _approvable(row)
        except HTTPException as exc:
            failed.append(row.id)
            if (row.validation or {}).get("ok") is not False:  # failed validation already says why
                _note(row, str(exc.detail))
            continue
        candidates.append(row)
    winner: dict[str, OntoProposal] = {}
    for row in sorted(candidates, key=lambda r: (-(r.confidence or 0), r.id)):
        winner.setdefault(_release_key(row), row)
    for row in sorted(candidates, key=lambda r: (AUTO_ORDER.get((r.payload or {}).get("kind"), 5), r.id)):
        key, links = _release_key(row), _link_keys(row)
        if winner[key] is not row:
            reason = f"key {key!r}: kept proposal {winner[key].id}, which has higher confidence"
        elif key in rejected:
            reason = f"key {key!r} was revoked by a steward"
        elif key in held or any(link in held for link in links):
            reason = f"key {key!r} or one of its links is already in the release"
        else:
            _approve(row, None, auto=True)
            held.add(key)
            held.update(links)
            kind = (row.payload or {}).get("kind")
            counts[kind] = counts.get(kind, 0) + 1
            continue
        _note(row, reason)
        skipped.append({"id": row.id, "reason": reason})
    session.commit()
    return {"approved": counts, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------- releases


def _default_groups(domain: str) -> list[str]:
    best = max((d for d in DOMAIN_GROUPS if domain == d or domain.startswith(d + ".")), key=len, default=None)
    return list(DOMAIN_GROUPS[best]) if best else []


def _extra(kind: str, node, source: str) -> dict:
    """Provenance stored with a node: where it came from and, for objects, its access groups."""
    extra: dict = {"source": source}
    if kind == "object":
        groups = _default_groups(node.domain)
        extra |= {"group_xmlids": groups, "access_source": "domain_default" if groups else "unset",
                  "engine_only": False}
    return extra


def _release_with_approved(session: Session, tenant_id) -> tuple[Release, dict[str, dict], int]:
    """The committed templates plus every approved proposal, checked as one release. 422 lists every problem."""
    release = loader.load_release().model_copy(deep=True)
    provenance: dict[str, dict] = {}
    taken: set[str] = set()
    for kind, (_, field) in KINDS.items():
        for node in getattr(release, field):
            provenance[node.key] = _extra(kind, node, "template")
            taken.add(node.key)
    problems: list[str] = []
    applied = 0
    rows = session.exec(scoped(OntoProposal, tenant_id).where(OntoProposal.state == "approved")
                        .order_by(OntoProposal.id)).all()
    copies: dict[str, list[tuple[OntoProposal, str, object]]] = {}  # node key -> its approved copies, oldest first
    terms: list[tuple[OntoProposal, tuple[str, str]]] = []  # applied after every node is in place
    custom: list[tuple[OntoProposal, tuple]] = []  # custom fields, likewise
    props: list[tuple[OntoProposal, tuple]] = []  # Builder properties, likewise
    for row in rows:
        if (row.payload or {}).get("kind") in NOT_RELEASE_KINDS:
            continue  # D54: an OrgMap draft is applied in Odoo and a template fit is informational; the release never changes
        try:
            kind, node = _parse(row)
        except HTTPException as exc:
            problems.append(exc.detail)
            continue
        if kind == TERM_KIND:
            terms.append((row, node))
            continue
        if kind == CUSTOM_FIELD_KIND:
            custom.append((row, node))
            continue
        if kind == PROPERTY_KIND:
            props.append((row, node))
            continue
        copies.setdefault(node.key, []).append((row, kind, node))
    for key, group in copies.items():
        if key in taken:
            problems += [f"proposal {row.id}: key {key!r} is already in the release" for row, _, _ in group]
            continue
        # 2.12: copies an earlier run approved again carry the same kind and node. The newest one is used; a key whose
        # copies differ is refused, since the release cannot tell which one the steward meant.
        held = [((row.payload or {}).get("kind"), (row.payload or {}).get("node")) for row, _, _ in group]
        if any(h != held[-1] for h in held):
            ids = ", ".join(str(row.id) for row, _, _ in group)
            problems.append(f"key {key!r} is held by approved proposals {ids} with different payloads: revoke one of them")
            continue
        row, kind, node = group[-1]
        taken.add(key)
        getattr(release, KINDS[kind][1]).append(node)
        provenance[key] = _extra(kind, node, "proposal") | {
            "auto": (row.payload or {}).get("approved_by") == AUTO_APPROVER,
            "proposal_id": row.id,
            "approver_uid": row.approver_uid,
            "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        }
        applied += 1
        if kind == "object":  # the links the Builder made for the object's link properties come with it
            for data in (row.payload or {}).get("links") or []:
                try:
                    link = Link.model_validate(data)
                except ValidationError as exc:
                    problems.append(f"proposal {row.id}: link: {exc.errors()[0]['msg']}")
                    continue
                if link.key in taken:
                    problems.append(f"proposal {row.id}: link key {link.key!r} is already in the release")
                    continue
                taken.add(link.key)
                release.links.append(link)
                provenance[link.key] = {"source": "proposal", "proposal_id": row.id}
    for row, (object_key, prop, own) in custom:
        obj = next((item for item in release.objects if item.key == object_key), None)
        if obj is None:
            problems.append(f"proposal {row.id}: object {object_key!r} is not in the release")
        elif any(item.key == prop.key for item in obj.properties):
            problems.append(f"proposal {row.id}: {object_key} already has property {prop.key!r}")
        else:
            obj.properties.append(prop)
            merged = {model: dict(entries) for model, entries in (obj.odoo_fields or {}).items()}
            for model, entries in own.items():
                merged.setdefault(model, {}).update(entries)
            obj.odoo_fields = merged or None
            _auto_property(provenance, row, object_key, prop.key)
            applied += 1
    for row, (object_key, prop) in props:
        obj = next((item for item in release.objects if item.key == object_key), None)
        if obj is None:
            problems.append(f"proposal {row.id}: object {object_key!r} is not in the release")
        elif any(item.key == prop.key for item in obj.properties):
            # The approval check refuses a key the object has, so the object gained it since (a template update):
            # the object's own property wins and this overlay entry is superseded.
            provenance.setdefault(object_key, {}).setdefault("superseded", []).append(
                {"proposal_id": row.id, "property": prop.key})
        else:
            obj.properties.append(prop)
            _auto_property(provenance, row, object_key, prop.key)
            applied += 1
    for row, (target, synonym) in terms:
        node = next((n for _, field in KINDS.values() for n in getattr(release, field) if n.key == target), None)
        if node is None or not hasattr(node, "synonyms"):
            problems.append(f"proposal {row.id}: term target {target!r} is not a node of the release")
        elif synonym.lower() not in {s.lower() for s in node.synonyms}:
            node.synonyms.append(synonym)
            applied += 1
    problems += loader.problems_of(release)
    if problems:
        raise HTTPException(422, {"error": "invalid_release", "problems": problems})
    return release, provenance, applied


def _auto_property(provenance: dict[str, dict], row: OntoProposal, object_key: str, prop: str) -> None:
    if (row.payload or {}).get("approved_by") == AUTO_APPROVER:
        provenance.setdefault(object_key, {}).setdefault("auto_properties", []).append(prop)


def _counts(nodes: dict[str, _Item], links: dict[str, _Item]) -> dict:
    counts = {kind: 0 for kind in KINDS}
    for item in nodes.values():
        counts[item.kind] += 1
    counts["link"] = len(links)
    return counts


def _drift(session: Session, tenant_id, new_row: OntoRelease, old_row: OntoRelease) -> dict:
    """Keys added, removed or changed between two releases. Links are named link:<key>."""
    def snapshot(row: OntoRelease) -> dict[str, tuple]:
        nodes, links = _items(session, tenant_id, row)
        return {**{k: (v.kind, v.payload) for k, v in nodes.items()},
                **{f"link:{k}": ("link", v.payload) for k, v in links.items()}}

    new, old = snapshot(new_row), snapshot(old_row)
    return {
        "added": sorted(k for k in new if k not in old),
        "removed": sorted(k for k in old if k not in new),
        "changed": sorted(k for k in new if k in old and new[k] != old[k]),
    }


def _moved(session: Session, tenant_id, row: OntoRelease, before: OntoRelease | None) -> dict:
    """The response of publish and activate. `previous` is the release that was current before the call;
    drift compares to it. Both are null when the call did not change the current release."""
    previous = before if before is not None and before.id != row.id else None
    nodes, links = _items(session, tenant_id, row)
    current = store.current_release(session, tenant_id)
    return {
        "release_id": row.id,
        "hash": row.hash,
        "published_at": row.published_at,
        "current": current is not None and current.id == row.id,
        "counts": _counts(nodes, links),
        "previous": {"release_id": previous.id, "hash": previous.hash} if previous else None,
        "drift": _drift(session, tenant_id, row, previous) if previous else None,
    }


@router.get("/releases")
def releases(session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    # ponytail: counts are tallied in Python per release; one GROUP BY once a tenant has many releases.
    current = store.current_release(session, auth.tenant_id)
    rows = session.exec(scoped(OntoRelease, auth.tenant_id).order_by(OntoRelease.id.desc())).all()
    out = []
    for row in rows:
        nodes, links = _items(session, auth.tenant_id, row)
        out.append({"release_id": row.id, "hash": row.hash, "published_at": row.published_at,
                    "created_by_uid": row.created_by_uid,
                    "current": current is not None and row.id == current.id,
                    "counts": _counts(nodes, links)})
    return {"releases": out}


@router.post("/releases")
def publish_release(session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    """Publish and make current. Idempotent: the same content returns the stored release (and makes it current).

    The reply carries `provision` (contract 8): the payload the steward's browser posts to Odoo /onto/v1/provision.
    """
    uid = _uid(auth)
    tenant_id = auth.tenant_id
    before = store.current_release(session, tenant_id)
    release, provenance, applied = _release_with_approved(session, tenant_id)
    try:
        provision = provisioning.payload(release, provenance, release.digest())
    except SpecError as exc:  # an object the compiler refuses: nothing is published
        raise HTTPException(422, {"error": "invalid_release", "problems": [str(exc)]}) from exc
    try:
        row = store.publish(session, tenant_id, release, created_by_uid=uid, provenance=provenance)
    except ValueError as exc:  # release_rows: a key repeats across kinds in the templates
        raise HTTPException(422, {"error": "invalid_release", "problems": [str(exc)]}) from exc
    return {**_moved(session, tenant_id, row, before), "proposals_applied": applied, "provision": provision}


@router.post("/releases/{digest}/activate")
def activate(digest: str, session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    """Point the current release at a stored one: a rollback, or a roll-forward. Releases are untouched."""
    uid = _uid(auth)
    tenant_id = auth.tenant_id
    row = session.exec(scoped(OntoRelease, tenant_id).where(OntoRelease.hash == digest)).first()
    if row is None:
        raise HTTPException(404, "not found")
    before = store.current_release(session, tenant_id)
    store.set_current(session, tenant_id, row.id, uid)
    session.commit()
    return _moved(session, tenant_id, row, before)


@router.get("/releases/{digest}/provision")
def release_provision(digest: str, session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    """The provision payload of a stored release (contract 8), so a failed provision can be retried. Same as publish's."""
    row = session.exec(scoped(OntoRelease, auth.tenant_id).where(OntoRelease.hash == digest)).first()
    if row is None:
        raise HTTPException(404, "not found")
    nodes, _ = _items(session, auth.tenant_id, row)
    provenance = {key: item.provenance for key, item in nodes.items() if item.kind == "object"}
    return provisioning.payload(store.load_release(session, auth.tenant_id, digest), provenance, digest)


# ---------------------------------------------------------------- readiness gates (plan 2.6)


class HealthCheck(BaseModel):
    """One Data Health row (contract section 3). The gates read these three fields; Odoo's other fields are ignored."""
    check_key: str = Field(max_length=100)
    severity: Literal["info", "warning", "blocking"]
    count: int = Field(ge=0)


class ReadinessBody(BaseModel):
    facts: dict[str, Any] = Field(default_factory=dict, max_length=200)  # the profile's facts (contract section 2)
    checks: list[HealthCheck] = Field(default_factory=list, max_length=200)


@router.post("/readiness")
def readiness_gates(body: ReadinessBody) -> dict:
    """Which features the profile and Data Health allow. Pure: nothing is read from Odoo or stored."""
    return {"features": health.readiness(body.facts, [check.model_dump() for check in body.checks])}

# ---------------------------------------------------------------- Builder (plan 2.3, D52)


class BuilderRun(BaseModel):
    """The steward's own introspect (2.1) and profile (2.2) replies. Querygen never holds Odoo credentials (D52)."""

    model_config = ConfigDict(extra="forbid")
    introspect: dict
    profile: dict
    domains: list[str] | None = None


@router.post("/builder/run")
def builder_run(body: BuilderRun, session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    """Match, draft and propose. Writes unapproved builder proposals only; nothing is applied."""
    try:
        out, rows = builder.run(body.introspect, body.profile, body.domains,
                                store.approved_org_roles(session, auth.tenant_id),
                                store.approved_release_keys(session, auth.tenant_id))
    except builder.InputError as exc:
        raise HTTPException(422, str(exc)) from exc
    ids = store.replace_builder_proposals(session, auth.tenant_id, rows)
    builder.attach_proposal_ids(out, rows, ids)
    return {**out, "proposal_ids": ids}


# ---------------------------------------------------------------- drift (plan 2.5, contract 9)


class DriftBody(BaseModel):
    """The steward's own introspect (2.1), read in the browser and posted here (D52)."""
    model_config = ConfigDict(extra="forbid")
    introspect: dict


@router.post("/drift")
def drift_now(body: DriftBody, session: db.SessionType, auth: OntoAuth = Depends(browser_auth)) -> dict:
    """Compare the current release with the steward's introspect and store the result, as the Odoo job does."""
    try:
        out = drift_check.check(session, auth.tenant_id, body.introspect)
    except LookupError as exc:
        raise HTTPException(404, "no release published") from exc
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.add(JobRun(tenant_id=auth.tenant_id, job="drift", state="done", result=drift_check.counts(out),
                       heartbeat_at=datetime.now(timezone.utc)))
    session.commit()
    return out
