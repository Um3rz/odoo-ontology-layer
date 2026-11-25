"""Tenant-scoped storage of published Onto releases (plan 1.2; contract 3.4).

publish() writes one onto_release row with its nodes and links, and points the tenant's current
release (onto_release_current) at it, all in one transaction. The hash is the release's canonical
digest (schema.Release.digest), so publishing the same content again returns the release already
stored. A published release, and every node or link of one, is frozen: an update or delete raises
ReleaseImmutable. Only the pointer moves. Every query goes through app.db.tenant.scoped.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import event, inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.db.onto import OntoDrift, OntoEdge, OntoNode, OntoProposal, OntoRelease, OntoReleaseCurrent
from app.db.tenant import scoped
from app.ontology import graph
from app.ontology.builder import approval_covers
from app.ontology.builder.match import RELEASE_KINDS
from app.ontology.graph import Graph, release_rows
from app.ontology.schema import ODOO_16, Domain, Link, Metric, Object, OrgRole, Release, Rule


class ReleaseImmutable(RuntimeError):
    """A published release, or a node or link of one, cannot be changed or deleted."""


def _refuse_change(mapper, connection, target) -> None:
    released = isinstance(target, OntoRelease) or any(
        value is not None
        for value in (target.release_id, inspect(target).committed_state.get("release_id"))
    )
    if released:
        raise ReleaseImmutable(f"{type(target).__name__} {target.id} belongs to a published release")


for _model in (OntoRelease, OntoNode, OntoEdge):
    event.listen(_model, "before_update", _refuse_change)
    event.listen(_model, "before_delete", _refuse_change)


def _by_hash(session: Session, tenant_id: uuid.UUID, digest: str) -> OntoRelease | None:
    return session.exec(scoped(OntoRelease, tenant_id).where(OntoRelease.hash == digest)).first()


def current_release(session: Session, tenant_id: uuid.UUID) -> OntoRelease | None:
    """The release every read treats as current: the pointer row's release. With no pointer row yet, the latest one."""
    pointer = session.exec(scoped(OntoReleaseCurrent, tenant_id)).first()
    if pointer is None:
        return session.exec(scoped(OntoRelease, tenant_id).order_by(OntoRelease.id.desc())).first()
    return session.exec(scoped(OntoRelease, tenant_id).where(OntoRelease.id == pointer.release_id)).first()


def set_current(session: Session, tenant_id: uuid.UUID, release_id: int, set_by_uid: int | None) -> None:
    """Point this tenant's current release at release_id (an upsert on the one-row-per-tenant pointer). The caller commits."""
    now = datetime.now(timezone.utc)
    session.execute(
        pg_insert(OntoReleaseCurrent)
        .values(tenant_id=tenant_id, release_id=release_id, set_at=now, set_by_uid=set_by_uid,
                created_at=now, updated_at=now)
        .on_conflict_do_update(
            index_elements=["tenant_id"],
            set_={"release_id": release_id, "set_at": now, "set_by_uid": set_by_uid, "updated_at": now},
        )
    )


def _write(session: Session, tenant_id: uuid.UUID, release: Release, digest: str,
           created_by_uid: int | None, provenance: dict[str, dict] | None) -> OntoRelease:
    """Add one release with its nodes and links, flushed but not committed.

    A concurrent publish of the same content may win the (tenant_id, hash) race; its row is returned.
    """
    nodes, edges = release_rows(release)
    row = OntoRelease(tenant_id=tenant_id, hash=digest, created_by_uid=created_by_uid,
                      published_at=datetime.now(timezone.utc))
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        found = _by_hash(session, tenant_id, digest)
        if found is None:
            raise
        return found
    extra = provenance or {}
    session.add_all(
        OntoNode(tenant_id=tenant_id, release_id=row.id, status="published",
                 provenance={"odoo_version": release.odoo_version, **extra.get(node["key"], {})}, **node)
        for node in nodes
    )
    session.add_all(OntoEdge(tenant_id=tenant_id, release_id=row.id, **edge) for edge in edges)
    return row


def publish(session: Session, tenant_id: uuid.UUID, release: Release,
            created_by_uid: int | None = None, provenance: dict[str, dict] | None = None) -> OntoRelease:
    """Store the release for this tenant, or find the one already stored with the same hash, and make it current.

    Every call points the tenant's current release at the returned row (set_current), so republishing
    an older content makes that release current again: releases stay immutable, the pointer moves.
    `provenance` maps a node key to extra facts stored on that node (its source, access groups). It is
    not part of the hash, so the same content with other provenance returns the stored row unchanged.
    """
    digest = release.digest()
    row = _by_hash(session, tenant_id, digest)
    wrote = row is None
    if row is None:
        row = _write(session, tenant_id, release, digest, created_by_uid, provenance)
    set_current(session, tenant_id, row.id, created_by_uid)
    session.commit()
    if wrote:
        graph.invalidate(tenant_id)  # a new release replaces this tenant's cached graphs
    return row


def load_graph(session: Session, tenant_id: uuid.UUID, digest: str) -> Graph:
    """The graph of one release, read from Postgres once per hash, then served from memory.

    LookupError when this tenant has no release with that hash (another tenant's map is invisible).
    """
    return graph.cached(tenant_id, digest, lambda: _build(session, tenant_id, digest))


def load_release(session: Session, tenant_id: uuid.UUID, digest: str) -> Release:
    """The stored release with this hash, rebuilt from its rows (the inverse of publish). LookupError when this tenant has none."""
    row = _by_hash(session, tenant_id, digest)
    if row is None:
        raise LookupError(f"no release {digest[:12]} for this tenant")
    nodes = session.exec(scoped(OntoNode, tenant_id).where(OntoNode.release_id == row.id)).all()
    edges = session.exec(scoped(OntoEdge, tenant_id).where(OntoEdge.release_id == row.id)).all()
    by_kind: dict[str, list[dict]] = {}
    for node in nodes:
        by_kind.setdefault(node.kind, []).append(node.payload)
    version = next((n.provenance["odoo_version"] for n in nodes if n.provenance and "odoo_version" in n.provenance), ODOO_16)
    return Release(
        odoo_version=version,
        domains=[Domain.model_validate(p) for p in by_kind.get("domain", [])],
        objects=[Object.model_validate(p) for p in by_kind.get("object", [])],
        rules=[Rule.model_validate(p) for p in by_kind.get("rule", [])],
        metrics=[Metric.model_validate(p) for p in by_kind.get("metric", [])],
        org_roles=[OrgRole.model_validate(p) for p in by_kind.get("org_role", [])],
        links=[Link.model_validate(e.payload) for e in edges],
    )


def _build(session: Session, tenant_id: uuid.UUID, digest: str) -> Graph:
    release = _by_hash(session, tenant_id, digest)
    if release is None:
        raise LookupError(f"no release {digest[:12]} for this tenant")
    nodes = session.exec(scoped(OntoNode, tenant_id).where(OntoNode.release_id == release.id)).all()
    edges = session.exec(scoped(OntoEdge, tenant_id).where(OntoEdge.release_id == release.id)).all()
    return Graph(
        keys=[node.key for node in nodes],
        edges=[{"src_key": e.src_key, "dst_key": e.dst_key, "payload": e.payload} for e in edges],
    )


BUILDER_SOURCES = ("builder", "llm", "infer")


DRIFT_SOURCE = "drift"
DRIFT_NODE_PREFIX = "drift:"  # a drift proposal's node_key: the prefix and the node ("<object>.<property>")


def open_drift(session: Session, tenant_id: uuid.UUID) -> dict[str, OntoDrift]:
    """This tenant's open drift rows (plan 2.5), by node key. Suspension reads them; a node has at most one open row."""
    rows = session.exec(scoped(OntoDrift, tenant_id).where(OntoDrift.resolved_at.is_(None))).all()
    return {row.node_key: row for row in rows}


def drift_sync(session: Session, tenant_id: uuid.UUID, digest: str, broken: list[dict], unchecked: set[str]) -> int:
    """Make this tenant's open drift rows match one comparison (plan 2.5, contract 9). Commits; returns rows resolved.

    A broken binding with no open row gets one row and one pending proposal (source drift, never batch-approvable).
    An open row that is no longer broken is resolved, and its pending proposal is removed, unless its node sits in
    `unchecked`: an object whose model this Odoo does not have is not in use, so its rows stay as they are.
    """
    now = datetime.now(timezone.utc)
    known = {(row.node_key, row.binding): row
             for row in session.exec(scoped(OntoDrift, tenant_id).where(OntoDrift.resolved_at.is_(None))).all()}
    wanted = {(item["node"], item["binding"]) for item in broken}
    for item in broken:
        if (item["node"], item["binding"]) in known:
            continue
        session.add(OntoDrift(tenant_id=tenant_id, release_hash=digest, node_key=item["node"],
                              binding=item["binding"], problem=item["problem"], detected_at=now))
        session.add(OntoProposal(
            tenant_id=tenant_id, node_key=DRIFT_NODE_PREFIX + item["node"], confidence=None,
            payload={"kind": "drift", "source": DRIFT_SOURCE, "batch_approvable": False, "release": digest,
                     "node": {"key": item["node"]}, "binding": item["binding"], "problem": item["problem"]},
            validation={"ok": False, "problems": [item["problem"]]}, state="proposed"))
    resolved = 0
    for (node, binding), row in known.items():
        if (node, binding) in wanted or node in unchecked:
            continue
        row.resolved_at = now
        session.add(row)
        resolved += 1
        pending = session.exec(scoped(OntoProposal, tenant_id).where(
            OntoProposal.node_key == DRIFT_NODE_PREFIX + node, OntoProposal.state == "proposed")).all()
        for proposal in pending:
            payload = proposal.payload or {}
            if payload.get("source") == DRIFT_SOURCE and payload.get("binding") == binding:
                session.delete(proposal)
    session.commit()
    return resolved


def approved_org_roles(session: Session, tenant_id: uuid.UUID) -> dict[int, str]:
    """The role each approved org_location was approved with, by location id (2.9a). The Builder does not offer these again."""
    rows = session.exec(scoped(OntoProposal, tenant_id).where(OntoProposal.state == "approved")).all()
    return {(row.payload or {})["location_id"]: (row.payload or {}).get("proposed_role")
            for row in rows if (row.payload or {}).get("kind") == "org_location"}


def _release_held(rows: list[OntoProposal]) -> dict[str, OntoProposal]:
    """The approved rows that hold a release key (2.12), by node key."""
    return {row.node_key: row for row in rows if (row.payload or {}).get("kind") in RELEASE_KINDS}


def approved_release_keys(session: Session, tenant_id: uuid.UUID) -> set[str]:
    """The release keys an approved proposal already holds (2.12). The Builder does not offer these again."""
    return set(_release_held(session.exec(scoped(OntoProposal, tenant_id).where(OntoProposal.state == "approved")).all()))


def replace_builder_proposals(session: Session, tenant_id: uuid.UUID, rows: list[dict]) -> list[int]:
    """Replace this tenant's unapproved Builder proposals (plan 2.3) with one run's rows, and return their ids, in order.

    Only rows still in state proposed whose payload source is builder or llm are dropped. Approved and revoked rows,
    and rows from other sources, stay. An org_location row that an approved row already covers (2.9a) is not stored
    again: its id in the result is the approved row's. So is a metric, object or custom field whose key an approved
    row already holds (2.12). Commits.
    """
    for row in session.exec(scoped(OntoProposal, tenant_id).where(OntoProposal.state == "proposed")).all():
        if (row.payload or {}).get("source") in BUILDER_SOURCES:
            session.delete(row)
    approved_rows = session.exec(scoped(OntoProposal, tenant_id).where(OntoProposal.state == "approved")).all()
    approved = {row.node_key: row for row in approved_rows if (row.payload or {}).get("kind") == "org_location"}
    held_release = _release_held(approved_rows)
    added, slots = [], []
    for r in rows:
        if (r["payload"] or {}).get("kind") in RELEASE_KINDS and r["node_key"] in held_release:
            slots.append(held_release[r["node_key"]])
            continue
        held = approved.get(r["node_key"])
        if held is not None and approval_covers(r["payload"], (held.payload or {}).get("proposed_role")):
            slots.append(held)
            continue
        row = OntoProposal(tenant_id=tenant_id, node_key=r["node_key"], payload=r["payload"],
                           confidence=r["confidence"], validation=r["validation"], state="proposed")
        added.append(row)
        slots.append(row)
    session.add_all(added)
    session.commit()
    return [slot.id for slot in slots]


def dedupe_approved_org_locations(session: Session, tenant_id: uuid.UUID) -> list[int]:
    """Delete the older approved copy of each approved org_location that repeats its node_key and proposed_role (2.10e,
    rows from before 2.9a). The newest copy stays, and a repeat with another role stays too. Only this tenant's approved
    org_location rows are touched, and no release reads them (D54). Nothing calls this: an admin runs it by hand.
    Commits; returns the ids deleted."""
    rows = session.exec(scoped(OntoProposal, tenant_id).where(OntoProposal.state == "approved")
                        .order_by(OntoProposal.id)).all()
    newest: dict[tuple, OntoProposal] = {}
    older: list[OntoProposal] = []
    for row in rows:
        payload = row.payload or {}
        if row.state != "approved" or payload.get("kind") != "org_location":
            continue
        slot = (row.node_key, payload.get("proposed_role"))
        if slot in newest:
            older.append(newest[slot])
        newest[slot] = row
    for row in older:
        session.delete(row)
    session.commit()
    return [row.id for row in older]


def _same_node(a: OntoProposal, b: OntoProposal) -> bool:
    return (a.payload or {}).get("kind") == (b.payload or {}).get("kind") and \
        (a.payload or {}).get("node") == (b.payload or {}).get("node")


def dedupe_approved_duplicate_keys(session: Session, tenant_id: uuid.UUID) -> list[int]:
    """Revoke the older approved copy of each release key that two or more approved proposals hold with the same node
    (2.12, rows from before the Builder stopped offering them again). The newest copy stays; the release keeps it anyway.
    A key whose copies differ is left alone: the release refuses it until the steward revokes one. org_location and
    template rows are not release nodes and are skipped. Nothing calls this: an admin runs it by hand. Commits; returns
    the ids revoked."""
    rows = session.exec(scoped(OntoProposal, tenant_id).where(OntoProposal.state == "approved")
                        .order_by(OntoProposal.id)).all()
    groups: dict[str, list[OntoProposal]] = {}
    for row in rows:
        if row.state == "approved" and (row.payload or {}).get("kind") not in {"org_location", "template"}:
            groups.setdefault(row.node_key, []).append(row)
    revoked = []
    for group in groups.values():
        newest = group[-1]
        if len(group) > 1 and all(_same_node(row, newest) for row in group):
            for row in group[:-1]:
                row.state, row.approved_at = "revoked", None
                session.add(row)
                revoked.append(row.id)
    session.commit()
    return revoked
