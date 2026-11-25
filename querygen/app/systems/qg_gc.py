"""Plan 3.11 (querygen half): nightly retention for querygen's operations tables (contract 3.4).

run(session, now) works tenant by tenant, through `scoped`, in batches of BATCH rows:
- decision_log (created_at), job_run (created_at) and odoo_tick (at) older than 30 days are deleted.
- llm_usage: the daily rows of every month before now's month are summed per (provider, model) into
  one monthly row whose `day` is "YYYY-MM", and the daily rows are deleted. This month keeps its daily
  rows. A monthly row is 7 characters and a daily row 10, so the two are never mixed up. Monthly rows
  live in llm_usage because contract 3.4 names no monthly table.

`now` is the job's as_of, never the wall clock. Redis needs nothing: its keys expire themselves.
Nothing here reads Odoo. There is no scheduler in querygen, so Odoo or an operator calls run().
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func
from sqlmodel import Session, select

from app.db.onto import DecisionLog, JobRun, LlmUsage, OdooTick, Tenant
from app.db.tenant import scoped

BATCH = 5000
KEEP = timedelta(days=30)
# ponytail: tenants are walked one at a time with one query per batch; parallel tenants only when there are hundreds.


def run(session: Session, now: datetime) -> dict[str, int]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware: it is the job's as_of")
    cutoff = now - KEEP
    this_month = f"{now.year:04d}-{now.month:02d}-01"
    counts = {"decision_log": 0, "job_run": 0, "odoo_tick": 0, "llm_usage_days_rolled": 0, "llm_usage_months_written": 0}
    for tenant_id in session.execute(select(Tenant.id)).scalars().all():
        counts["decision_log"] += _delete_expired(session, DecisionLog, tenant_id, DecisionLog.created_at < cutoff)
        counts["job_run"] += _delete_expired(session, JobRun, tenant_id, JobRun.created_at < cutoff)
        counts["odoo_tick"] += _delete_expired(session, OdooTick, tenant_id, OdooTick.at < cutoff)
        _roll_up_usage(session, tenant_id, this_month, counts)
    return counts


def _delete_expired(session: Session, model, tenant_id, expired) -> int:
    deleted = 0
    while True:
        ids = session.execute(
            scoped(model, tenant_id).where(expired).with_only_columns(model.id).order_by(model.id).limit(BATCH)
        ).scalars().all()
        if not ids:
            return deleted
        result = session.execute(delete(model).where(model.tenant_id == tenant_id, model.id.in_(ids)))
        deleted += result.rowcount
        session.commit()


def _roll_up_usage(session: Session, tenant_id, this_month: str, counts: dict[str, int]) -> None:
    daily_before_this_month = and_(func.length(LlmUsage.day) == 10, LlmUsage.day < this_month)
    while True:
        rows = session.execute(
            scoped(LlmUsage, tenant_id).where(daily_before_this_month).order_by(LlmUsage.id).limit(BATCH)
        ).scalars().all()
        if not rows:
            return
        sums: dict[tuple[str, str, str], list] = defaultdict(lambda: [0, 0, 0.0])
        for r in rows:
            total = sums[(r.day[:7], r.provider, r.model)]
            total[0] += r.input_tokens
            total[1] += r.output_tokens
            total[2] += r.cost
        for (month, provider, model), (tokens_in, tokens_out, cost) in sums.items():
            monthly = session.execute(
                scoped(LlmUsage, tenant_id).where(
                    LlmUsage.day == month, LlmUsage.provider == provider, LlmUsage.model == model
                )
            ).scalars().first()
            if monthly is None:
                session.add(LlmUsage(tenant_id=tenant_id, day=month, provider=provider, model=model,
                                     input_tokens=tokens_in, output_tokens=tokens_out, cost=cost))
                counts["llm_usage_months_written"] += 1
            else:
                monthly.input_tokens += tokens_in
                monthly.output_tokens += tokens_out
                monthly.cost += cost
        session.execute(delete(LlmUsage).where(
            LlmUsage.tenant_id == tenant_id, LlmUsage.id.in_([r.id for r in rows])
        ))
        session.commit()
        counts["llm_usage_days_rolled"] += len(rows)
