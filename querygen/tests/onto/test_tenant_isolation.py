import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.onto import (
    JobRun, OntoEdge, OntoNode, OntoProposal, OntoRelease, Tenant,
)
from app.db.tenant import normalize_origin, onto_key, scoped, tenant_by_url
from app.models.models import QueryRequests


def two_tenants(session):
    a = Tenant(odoo_base_url="https://a.example.test", db_uuid="uuid-a")
    b = Tenant(odoo_base_url="https://b.example.test", db_uuid="uuid-b")
    session.add_all([a, b])
    session.commit()
    return a, b


def test_tenant_a_never_reads_tenant_b(session, clean_tenants):
    a, b = two_tenants(session)
    for t, k in ((a, "a-node"), (b, "b-node")):
        session.add(OntoNode(tenant_id=t.id, key=k, kind="object"))
        session.add(OntoEdge(tenant_id=t.id, src_key=k, dst_key="x", kind="has"))
        session.add(OntoProposal(tenant_id=t.id, node_key=k))
        session.add(OntoRelease(tenant_id=t.id, hash="same-hash"))  # same hash, different tenants: allowed
        session.add(JobRun(tenant_id=t.id, job="nightly", state="done"))
    session.commit()

    for model in (OntoNode, OntoEdge, OntoProposal, OntoRelease, JobRun):
        rows = session.exec(scoped(model, a.id)).all()
        assert len(rows) == 1 and all(r.tenant_id == a.id for r in rows), model.__name__
        rows = session.exec(scoped(model, b.id)).all()
        assert len(rows) == 1 and all(r.tenant_id == b.id for r in rows), model.__name__

    got = session.exec(scoped(OntoNode, a.id).where(OntoNode.key == "b-node")).all()
    assert got == []  # extra filters can only narrow, never widen


def test_unknown_tenant_sees_nothing(session, clean_tenants):
    a, _ = two_tenants(session)
    session.add(OntoNode(tenant_id=a.id, key="n", kind="object"))
    session.commit()
    assert session.exec(scoped(OntoNode, uuid.uuid4())).all() == []


def test_helper_refuses_a_table_without_tenant_id():
    with pytest.raises(TypeError):
        scoped(QueryRequests, uuid.uuid4())
    with pytest.raises(TypeError):
        scoped(Tenant, uuid.uuid4())  # the registry itself is not tenant-owned


def test_rows_must_belong_to_a_registered_tenant(session, clean_tenants):
    session.add(OntoNode(tenant_id=uuid.uuid4(), key="orphan", kind="object"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_same_release_hash_twice_for_one_tenant_is_refused(session, clean_tenants):
    a, _ = two_tenants(session)
    session.add_all([OntoRelease(tenant_id=a.id, hash="h"), OntoRelease(tenant_id=a.id, hash="h")])
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_updated_at_moves_on_update(session, clean_tenants):
    a, _ = two_tenants(session)
    before = a.updated_at
    a.db_uuid = "uuid-a2"
    session.add(a)
    session.commit()
    session.refresh(a)
    assert a.updated_at >= before


def test_key_helper_always_prefixes_the_tenant():
    t = uuid.uuid4()
    assert onto_key(t, "whoami", "abc") == f"onto:{t}:whoami:abc"
    assert onto_key(t, "run", "1", "events").startswith(f"onto:{t}:")


def test_registry_lookup_and_normalisation(session, clean_tenants):
    a, _ = two_tenants(session)
    assert tenant_by_url(session, "https://A.example.test/web?x=1").id == a.id
    assert tenant_by_url(session, "https://evil.example.test") is None
    assert tenant_by_url(session, "https://a.example.test:8443") is None  # port is part of identity
    assert tenant_by_url(session, "ftp://a.example.test") is None
    assert tenant_by_url(session, None) is None
    assert normalize_origin("http://localhost:8070/") == "http://localhost:8070"
