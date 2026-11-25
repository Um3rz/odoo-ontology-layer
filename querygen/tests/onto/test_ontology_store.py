"""Release storage on the scratch database (plan 1.2): publish, immutability, the graph load and tenant isolation."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.db.onto import OntoEdge, OntoNode, OntoRelease, Tenant
from app.db.tenant import scoped
from app.ontology import graph as onto_graph
from app.ontology import store
from app.ontology.graph import release_rows
from app.ontology.loader import load_release
from app.ontology.store import ReleaseImmutable, load_graph, publish

STOCK_TO_SUPPLIER = ["position_product", "product_supplier_terms", "supplier_info_partner"]


def add_tenant(session, name):
    tenant = Tenant(odoo_base_url=f"https://{name}.example.test", db_uuid=f"uuid-{name}")
    session.add(tenant)
    session.commit()
    return tenant


def node_count(release):
    return len(release_rows(release)[0])


def test_publishing_twice_returns_the_same_release(session, clean_tenants):
    tenant = add_tenant(session, "a")
    release = load_release()
    first = publish(session, tenant.id, release)
    again = publish(session, tenant.id, load_release())
    assert first.hash == release.digest() and again.id == first.id
    assert len(session.exec(scoped(OntoRelease, tenant.id)).all()) == 1
    nodes = session.exec(scoped(OntoNode, tenant.id).where(OntoNode.release_id == first.id)).all()
    assert len(nodes) == node_count(release)


def test_content_order_does_not_change_the_hash(session, clean_tenants):
    tenant = add_tenant(session, "a")
    release = load_release()
    shuffled = release.model_copy(update={
        "objects": list(reversed(release.objects)), "links": list(reversed(release.links)),
    })
    assert shuffled.digest() == release.digest()
    assert publish(session, tenant.id, shuffled).id == publish(session, tenant.id, release).id


def test_a_changed_release_is_a_new_row_and_replaces_the_cached_graph(session, clean_tenants):
    tenant = add_tenant(session, "a")
    old = publish(session, tenant.id, load_release())
    assert load_graph(session, tenant.id, old.hash).path("stock_position", "partner") is not None
    changed = load_release()
    changed.links = [link for link in changed.links if link.key != "supplier_info_partner"]
    new = publish(session, tenant.id, changed)
    assert new.hash != old.hash and new.id != old.id
    assert (tenant.id, old.hash) not in onto_graph._CACHE
    assert load_graph(session, tenant.id, new.hash).path("stock_position", "partner") is None
    assert load_graph(session, tenant.id, old.hash).path("stock_position", "partner") is not None  # old release intact


def test_the_graph_is_read_from_postgres_once_per_hash(session, clean_tenants, monkeypatch):
    tenant = add_tenant(session, "a")
    row = publish(session, tenant.id, load_release())
    reads = []
    real_build = store._build

    def counting_build(*args):
        reads.append(args)
        return real_build(*args)

    monkeypatch.setattr(store, "_build", counting_build)
    first = load_graph(session, tenant.id, row.hash)
    assert load_graph(session, tenant.id, row.hash) is first and len(reads) == 1


def test_the_stored_graph_answers_the_stock_to_supplier_path(session, clean_tenants):
    tenant = add_tenant(session, "a")
    row = publish(session, tenant.id, load_release())
    hops = load_graph(session, tenant.id, row.hash).path("stock_position", "partner")
    assert [hop["payload"]["key"] for hop in hops] == STOCK_TO_SUPPLIER


def test_another_tenants_map_is_invisible(session, clean_tenants):
    a, b = add_tenant(session, "a"), add_tenant(session, "b")
    release = load_release()
    row_a = publish(session, a.id, release)
    with pytest.raises(LookupError):
        load_graph(session, b.id, row_a.hash)
    assert session.exec(scoped(OntoRelease, b.id)).all() == []
    assert session.exec(scoped(OntoNode, b.id)).all() == []
    row_b = publish(session, b.id, release)  # the same content is stored once per tenant
    assert row_b.id != row_a.id and row_b.hash == row_a.hash
    assert len(session.exec(scoped(OntoNode, a.id).where(OntoNode.release_id == row_a.id)).all()) == node_count(release)
    assert load_graph(session, b.id, row_b.hash).path("stock_position", "partner") is not None


def test_a_published_release_cannot_be_changed_or_deleted(session, clean_tenants):
    tenant = add_tenant(session, "a")
    row = publish(session, tenant.id, load_release())
    digest, release_id = row.hash, row.id
    row.published_at = None
    with pytest.raises(ReleaseImmutable):
        session.commit()
    session.rollback()

    row = session.exec(scoped(OntoRelease, tenant.id)).one()
    session.delete(row)
    with pytest.raises(ReleaseImmutable):
        session.commit()
    session.rollback()

    node = session.exec(scoped(OntoNode, tenant.id).where(OntoNode.release_id == release_id)).first()
    node.kind = "changed"
    with pytest.raises(ReleaseImmutable):
        session.commit()
    session.rollback()

    edge = session.exec(scoped(OntoEdge, tenant.id)).first()
    session.delete(edge)
    with pytest.raises(ReleaseImmutable):
        session.commit()
    session.rollback()
    assert session.exec(scoped(OntoRelease, tenant.id)).one().hash == digest


def test_draft_nodes_stay_editable(session, clean_tenants):
    tenant = add_tenant(session, "a")
    session.add(OntoNode(tenant_id=tenant.id, key="draft_object", kind="object"))
    session.commit()
    node = session.exec(scoped(OntoNode, tenant.id)).one()
    node.status = "approved"
    session.commit()
    assert node.status == "approved"


def test_one_node_per_key_per_release(session, clean_tenants):
    tenant = add_tenant(session, "a")
    row = publish(session, tenant.id, load_release())
    session.add(OntoNode(tenant_id=tenant.id, release_id=row.id, key="product", kind="object"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_concurrent_publish_of_the_same_content_returns_the_winner(session, clean_tenants, monkeypatch):
    tenant = add_tenant(session, "a")
    winner = publish(session, tenant.id, load_release())
    real_lookup = store._by_hash
    calls = []

    def stale_first_lookup(s, tenant_id, digest):
        calls.append(1)
        return None if len(calls) == 1 else real_lookup(s, tenant_id, digest)  # the loser looked before the winner committed

    monkeypatch.setattr(store, "_by_hash", stale_first_lookup)
    loser = publish(session, tenant.id, load_release())
    assert loser.id == winner.id and len(calls) == 2
