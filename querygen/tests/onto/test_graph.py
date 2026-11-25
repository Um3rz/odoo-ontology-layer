"""The in-memory graph over the canonical templates (plan 1.2). No database: runs without one."""
import uuid

import pytest

from app.ontology import graph as onto_graph
from app.ontology.graph import Graph, cached, invalidate, release_rows
from app.ontology.loader import load_release
from app.ontology.schema import Domain

STOCK_TO_SUPPLIER = ["position_product", "product_supplier_terms", "supplier_info_partner"]


@pytest.fixture(scope="module")
def graph():
    nodes, edges = release_rows(load_release())
    return Graph([n["key"] for n in nodes], edges)


def test_stock_position_reaches_the_supplier_in_three_links(graph):
    hops = graph.path("stock_position", "partner")  # partner is the supplier through supplier_info
    assert [h["payload"]["key"] for h in hops] == STOCK_TO_SUPPLIER
    assert [h["dst_key"] for h in hops] == ["product", "supplier_info", "partner"]


def test_search_stops_at_three_hops(graph):
    assert "supplier_info" in graph.within("stock_position", 2)
    assert "partner" not in graph.within("stock_position", 2)
    assert "partner" in graph.within("stock_position")
    assert graph.path("stock_position", "partner", max_hops=2) is None
    assert graph.path("stock_position", "partner", max_hops=9) is not None  # clamped to 3, still reachable


def test_links_are_followed_in_their_declared_direction(graph):
    assert graph.path("partner", "stock_position") is None


def test_trivial_and_unknown_nodes(graph):
    assert graph.path("product", "product") == []
    assert graph.path("product", "no_such_object") is None
    assert graph.within("no_such_object") == set()


def test_release_rows_cover_every_node_and_link():
    release = load_release()
    nodes, edges = release_rows(release)
    expected = len(release.domains) + len(release.objects) + len(release.rules) + len(release.metrics) + len(release.org_roles)
    assert len(nodes) == expected and len(edges) == len(release.links)


def test_release_rows_refuse_a_key_repeated_across_kinds():
    release = load_release()
    release.domains.append(Domain(key="product", label="Product"))  # an object already has this key
    with pytest.raises(ValueError, match="product"):
        release_rows(release)


def test_cache_builds_once_per_tenant_and_hash():
    tenant, other = uuid.uuid4(), uuid.uuid4()
    calls = []

    def build():
        calls.append(1)
        return Graph([], [])

    first = cached(tenant, "hash-1", build)
    assert cached(tenant, "hash-1", build) is first and len(calls) == 1
    assert cached(other, "hash-1", build) is not first  # the tenant is part of the key
    cached(tenant, "hash-2", build)  # another hash is another entry
    assert len(calls) == 3


def test_invalidate_drops_only_one_tenants_entries():
    tenant, other = uuid.uuid4(), uuid.uuid4()
    kept = cached(other, "hash", lambda: Graph([], []))
    cached(tenant, "hash", lambda: Graph([], []))
    invalidate(tenant)
    assert (tenant, "hash") not in onto_graph._CACHE
    assert cached(other, "hash", lambda: pytest.fail("rebuilt")) is kept


def test_a_failed_build_is_not_cached():
    tenant = uuid.uuid4()

    def missing():
        raise LookupError("no release")

    with pytest.raises(LookupError):
        cached(tenant, "hash", missing)
    assert cached(tenant, "hash", lambda: Graph([], [])) is not None
