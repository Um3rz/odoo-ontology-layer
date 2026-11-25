"""The business map as a graph in querygen memory (plan 1.2; D7).

Pure code, no database import: store.py writes and reads the rows, this module decides
what they mean. An edge follows the direction its link declares (source to target). Search
is breadth first and never goes past MAX_HOPS (3).
"""
import threading
import uuid
from typing import Callable, Iterable

from app.ontology.schema import MAX_HOPS, Release

_CACHE: dict[tuple[uuid.UUID, str], "Graph"] = {}
_LOCK = threading.Lock()


def release_rows(release: Release) -> tuple[list[dict], list[dict]]:
    """Every node and link of one release, as the dicts store.py writes."""
    groups = (("domain", release.domains), ("object", release.objects), ("rule", release.rules),
              ("metric", release.metrics), ("org_role", release.org_roles))
    nodes = [{"key": n.key, "kind": kind, "payload": n.model_dump(mode="json")}
             for kind, group in groups for n in group]
    keys = [n["key"] for n in nodes]
    repeated = sorted({k for k in keys if keys.count(k) > 1})
    if repeated:
        raise ValueError(f"node keys repeat across kinds: {repeated}")
    edges = [{"src_key": link.source, "dst_key": link.target, "kind": "link",
              "payload": link.model_dump(mode="json")} for link in release.links]
    return nodes, edges


class Graph:
    """One release as adjacency. Edges are dicts with src_key, dst_key and payload (the link)."""

    def __init__(self, keys: Iterable[str], edges: Iterable[dict]):
        self.keys = set(keys)
        self._out: dict[str, list[dict]] = {}
        for edge in edges:
            self._out.setdefault(edge["src_key"], []).append(edge)

    def _search(self, start: str, max_hops: int) -> dict[str, dict | None]:
        """Breadth first. Maps each reached key to the edge that first reached it (None for start)."""
        reached: dict[str, dict | None] = {start: None}
        frontier = [start]
        for _ in range(min(max_hops, MAX_HOPS)):
            next_frontier = []
            for key in frontier:
                for edge in self._out.get(key, []):
                    if edge["dst_key"] not in reached:
                        reached[edge["dst_key"]] = edge
                        next_frontier.append(edge["dst_key"])
            if not next_frontier:
                break
            frontier = next_frontier
        return reached

    def within(self, start: str, hops: int = MAX_HOPS) -> set[str]:
        """Every key reachable from start in at most `hops` links (start included)."""
        return set(self._search(start, hops)) if start in self.keys else set()

    def path(self, src: str, dst: str, max_hops: int = MAX_HOPS) -> list[dict] | None:
        """The shortest directed path from src to dst as its edges. [] when src is dst, None when none fits in max_hops."""
        if src not in self.keys or dst not in self.keys:
            return None
        reached = self._search(src, max_hops)
        if dst not in reached:
            return None
        hops: list[dict] = []
        key = dst
        while reached[key] is not None:
            hops.append(reached[key])
            key = reached[key]["src_key"]
        return hops[::-1]


def cached(tenant_id: uuid.UUID, digest: str, build: Callable[[], Graph]) -> Graph:
    """The graph of one tenant's release hash, built once. A build that raises is not cached."""
    # ponytail: one lock held across a build; per-key locks if builds get slow
    with _LOCK:
        key = (tenant_id, digest)
        if key not in _CACHE:
            _CACHE[key] = build()
        return _CACHE[key]


def invalidate(tenant_id: uuid.UUID) -> None:
    """Forget every graph of one tenant. store.publish() calls it when a new release is written."""
    with _LOCK:
        for key in [k for k in _CACHE if k[0] == tenant_id]:
            del _CACHE[key]
