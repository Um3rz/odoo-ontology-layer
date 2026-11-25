"""Plan 3.11 (querygen half): qg_gc deletes exactly what has expired and rolls up completed months of usage.

Postgres through the scratch database from conftest. Nothing here calls Odoo, Redis or a model.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.db.onto import DecisionLog, JobRun, LlmUsage, OdooTick, Tenant
from app.systems import qg_gc

NOW = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=31)
EDGE = NOW - timedelta(days=30)  # exactly 30 days old: kept, only strictly older rows expire
FRESH = NOW - timedelta(days=1)


def _tenant(session) -> uuid.UUID:
    row = Tenant(odoo_base_url=f"https://{uuid.uuid4().hex}.example.test", db_uuid=str(uuid.uuid4()))
    session.add(row)
    session.commit()
    return row.id


def test_gc_deletes_exactly_what_has_expired(session, monkeypatch):
    monkeypatch.setattr(qg_gc, "BATCH", 2)  # three expired rows take two batches
    a, b = _tenant(session), _tenant(session)
    session.add_all([
        DecisionLog(tenant_id=a, run_id="a-old1", decision_type="t", created_at=OLD),
        DecisionLog(tenant_id=a, run_id="a-old2", decision_type="t", created_at=OLD),
        DecisionLog(tenant_id=a, run_id="a-old3", decision_type="t", created_at=OLD),
        DecisionLog(tenant_id=a, run_id="a-edge", decision_type="t", created_at=EDGE),
        DecisionLog(tenant_id=a, run_id="a-fresh", decision_type="t", created_at=FRESH),
        DecisionLog(tenant_id=b, run_id="b-old", decision_type="t", created_at=OLD),
        DecisionLog(tenant_id=b, run_id="b-fresh", decision_type="t", created_at=FRESH),
        JobRun(tenant_id=a, job="daily_review", state="done", created_at=OLD),
        JobRun(tenant_id=a, job="daily_review", state="done", created_at=EDGE),
        JobRun(tenant_id=a, job="daily_review", state="done", created_at=FRESH),
        OdooTick(tenant_id=a, at=OLD, lag_s=0.0),
        OdooTick(tenant_id=a, at=FRESH, lag_s=0.0),
        # September is a closed month at NOW: its daily rows roll up. October and November stay daily.
        LlmUsage(tenant_id=a, day="2026-09-15", provider="openai", model="m1", input_tokens=10, output_tokens=5, cost=0.5),
        LlmUsage(tenant_id=a, day="2026-09-15", provider="openai", model="m1", input_tokens=2, output_tokens=1, cost=0.25),
        LlmUsage(tenant_id=a, day="2026-09-30", provider="openai", model="m1", input_tokens=3, output_tokens=0, cost=0.1),
        LlmUsage(tenant_id=a, day="2026-09-15", provider="openai", model="m2", input_tokens=1, output_tokens=1, cost=0.0),
        LlmUsage(tenant_id=a, day="2026-11-02", provider="openai", model="m1", input_tokens=4, output_tokens=4, cost=0.2),
        LlmUsage(tenant_id=a, day="2026-08", provider="openai", model="m1", input_tokens=7, output_tokens=7, cost=1.0),
        LlmUsage(tenant_id=b, day="2026-09-15", provider="openai", model="m1", input_tokens=5, output_tokens=5, cost=0.5),
    ])
    session.commit()

    counts = qg_gc.run(session, NOW)

    assert counts == {"decision_log": 4, "job_run": 1, "odoo_tick": 1,
                      "llm_usage_days_rolled": 5, "llm_usage_months_written": 3}
    assert set(session.exec(select(DecisionLog.run_id)).all()) == {"a-edge", "a-fresh", "b-fresh"}
    assert sorted(session.exec(select(JobRun.created_at)).all()) == [EDGE, FRESH]
    assert session.exec(select(OdooTick.at)).all() == [FRESH]
    usage = sorted(
        (r.tenant_id == a, r.day, r.model, r.input_tokens, r.output_tokens, round(r.cost, 6))
        for r in session.exec(select(LlmUsage)).all()
    )
    assert usage == [
        (False, "2026-09", "m1", 5, 5, 0.5),  # b: its one September row, rolled up
        (True, "2026-08", "m1", 7, 7, 1.0),   # a: an existing monthly row, untouched
        (True, "2026-09", "m1", 15, 6, 0.85),
        (True, "2026-09", "m2", 1, 1, 0.0),
        (True, "2026-11-02", "m1", 4, 4, 0.2),
    ]
