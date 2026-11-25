"""Registry of the querygen jobs Odoo's crons start (plan 3.3; contract 3.5).

A handler gets a JobContext and returns counts only. Rows read from Odoo stay in memory and are
dropped when the handler returns. Later phases register their jobs here (daily_review, forecast, ...)
and nothing else changes. Only names in JOBS are accepted; anything else is a 404.
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.odoo.client import OdooClient


@dataclass
class JobContext:
    tenant_id: object
    job_run_id: int
    params: dict
    odoo: OdooClient


JobHandler = Callable[[JobContext], Awaitable[dict]]


async def echo(ctx: JobContext) -> dict:
    """No-op job: proves the path from Odoo's cron to a finished job_run row."""
    return {"ok": True}


JOBS: dict[str, JobHandler] = {"echo": echo}
