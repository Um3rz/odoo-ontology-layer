"""Fixtures for the ground-truth suite (plan 1.10).

The suite runs only against the seeded Odoo 16 database named in GROUND_TRUTH_DATABASE_URL. Without that
variable every test here is skipped with a message. scripts/onto_ground_truth.py builds the URL from
odoo.conf in-process and never prints it. The connection is read-only at the session level, and the
database name must be odoo_onto, so the suite cannot reach any other database by accident.

Results are written as JSON to GROUND_TRUTH_RESULTS when that variable names a path; the runner turns
them into the markdown report.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

ENV_URL = "GROUND_TRUTH_DATABASE_URL"
ENV_RESULTS = "GROUND_TRUTH_RESULTS"
ENV_BUDGET = "GROUND_TRUTH_BUDGET_S"
EXPECTED_DATABASE = "odoo_onto"
STATEMENT_TIMEOUT_MS = 120_000  # a query over 2 minutes is a gap, not a hang (plan run rule)
DEFAULT_BUDGET_S = 600.0  # the whole run stays under 10 minutes; later cases are recorded as gaps


@dataclass
class GroundTruth:
    conn: object
    _templates: object
    _degraded: object
    results: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    budget_s: float = DEFAULT_BUDGET_S
    database: str = EXPECTED_DATABASE

    def release(self, name: str):
        return self._templates if name == "templates" else self._degraded

    @property
    def templates(self):
        return self._templates

    def over_budget(self) -> bool:
        return time.monotonic() - self.started > self.budget_s


def degraded_release(release):
    """The templates as they provision on a database without sale_margin (plan F5 in cases.FINDINGS).

    Drops the unit_cost property and every metric that reads it, directly or through another metric. The
    rest of the release is unchanged, so every other metric runs against the real templates' bindings.
    """
    from app.ontology.schema import formula_names
    from .cases import DROPPED_PROPERTIES

    dropped_props = {prop for (_obj, prop) in DROPPED_PROPERTIES}
    objects = []
    for obj in release.objects:
        keep = [p for p in obj.properties if (obj.key, p.key) not in DROPPED_PROPERTIES]
        objects.append(obj.model_copy(update={"properties": keep}))
    dropped_metrics: set[str] = set()
    changed = True
    while changed:
        changed = False
        for metric in release.metrics:
            if metric.key in dropped_metrics:
                continue
            names = formula_names(metric.derived_from) if metric.derived_from else set()
            reads = names | ({metric.measure} if metric.measure else set()) | set(metric.filters)
            if reads & (dropped_props | dropped_metrics):
                dropped_metrics.add(metric.key)
                changed = True
    metrics = [m for m in release.metrics if m.key not in dropped_metrics]
    return release.model_copy(update={"objects": objects, "metrics": metrics})


@pytest.fixture(scope="session")
def ground_truth():
    url = os.environ.get(ENV_URL)
    if not url:
        pytest.skip(
            f"{ENV_URL} is not set. The ground truth runs only against the seeded Odoo 16 database; "
            "run scripts/onto_ground_truth.py, which reads odoo.conf and sets it."
        )
    # app.db asserts DATABASE_URL at import. The ground truth never opens that engine, so a placeholder is enough.
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://ground_truth@127.0.0.1:1/unused")

    import psycopg2

    try:
        conn = psycopg2.connect(
            url,
            connect_timeout=10,
            options=f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}",
        )
    except psycopg2.Error as exc:  # never show the URL: only the exception class
        pytest.fail(f"cannot connect to the ground-truth database ({type(exc).__name__})", pytrace=False)
    conn.set_session(readonly=True, autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT current_database(), current_setting('transaction_read_only')")
    database, read_only = cur.fetchone()
    conn.rollback()
    if database != EXPECTED_DATABASE:
        conn.close()
        pytest.fail(f"refusing to run: connected to {database!r}, the ground truth is only for {EXPECTED_DATABASE!r}",
                    pytrace=False)
    if read_only != "on":
        conn.close()
        pytest.fail("refusing to run: the session is not read-only", pytrace=False)

    from app.ontology.loader import load_release

    templates = load_release()
    budget = float(os.environ.get(ENV_BUDGET, DEFAULT_BUDGET_S))
    gt = GroundTruth(conn=conn, _templates=templates, _degraded=degraded_release(templates), budget_s=budget)
    yield gt
    conn.close()
    path = os.environ.get(ENV_RESULTS)
    if path:
        from .cases import FINDINGS, GROUND_TRUTH_GAPS, TOLERANCE

        payload = {
            "database": EXPECTED_DATABASE,
            "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - gt.started, 1),
            "budget_s": budget,
            "tolerance": TOLERANCE,
            "gaps": GROUND_TRUTH_GAPS,
            "findings": FINDINGS,
            "cases": gt.results,
        }
        Path(path).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
