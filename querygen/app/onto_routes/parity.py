"""POST /onto/jobs/parity: the parity job (plan 1.8; contract 3.9, jobs).

Odoo starts it with the tenant's Onto bot token: `Authorization: Bearer <bot token>` and
`X-Odoo-Url: <registered origin>`. Querygen treats the token as opaque. Odoo's `whoami` verifies it, and
this route then requires the registered database, the Onto bot's group and the AI switch on (D29). The job
asks Odoo to run the check (`POST /onto/v1/parity/run`, the same bot token) and keeps counts only in
`job_run`. No object key, signature, value, token or Odoo row is stored, logged or returned.

A run with a fresh heartbeat blocks another one for the tenant (409). Odoo's call is made once: a parity
run writes suspensions, so a retry could double-count.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import db
from app.db.onto import JobRun, Tenant
from app.db.tenant import scoped, tenant_by_url
from app.odoo.client import OdooClient, OdooError
from app.odoo.tokens import has_token_shape, token_hash
from app.utils import browser_auth as ba
from app.utils.browser_auth import OntoAuthError

JOB = "parity"
BOT_GROUP = "odoo_dashboards_agents.group_onto_bot"
RUNNING_WINDOW = timedelta(minutes=10)  # a "running" row with a fresher heartbeat blocks a new run
ODOO_TIMEOUT_S = 300.0  # the check walks every object for every user signature
RUN_PATH = "/onto/v1/parity/run"

router = APIRouter()


def odoo_client_factory(base_url: str, token: str) -> OdooClient:
    """Seam for tests: swap in a stubbed transport."""
    return OdooClient(base_url, token, timeout=ODOO_TIMEOUT_S, retries=0)


def _check_bot(tenant: Tenant, who: dict) -> None:
    """Odoo has verified the token. It must be the registered database's Onto bot, with AI features on."""
    if (who.get("database") or {}).get("uuid") != tenant.db_uuid:
        raise OntoAuthError(403, "tenant_mismatch")
    if BOT_GROUP not in (who.get("groups") or []):
        raise OntoAuthError(403, "not_the_bot")
    if who.get("ai_enabled") is not True:
        raise OntoAuthError(403, "ai_features_off")


async def _bot_client(request: Request, session: db.SessionType) -> tuple[Tenant, OdooClient]:
    """The tenant and a client for the bot's token, after the same checks browser_auth makes."""
    tenant = await run_in_threadpool(tenant_by_url, session, request.headers.get("x-odoo-url"))
    if tenant is None:
        raise OntoAuthError(403, "unknown_tenant")
    if not ba.tenant_limiter.allow(str(tenant.id)):
        raise OntoAuthError(429, "rate_limited", {"Retry-After": "60"})
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not has_token_shape(token):  # a raw API key has no dot: refused
        raise OntoAuthError(401, "invalid_token")
    if not ba.token_limiter.allow(token_hash(token)):
        raise OntoAuthError(429, "rate_limited", {"Retry-After": "60"})

    client = odoo_client_factory(tenant.odoo_base_url, token)
    try:
        who = await client.whoami()
    except OdooError as exc:
        await client.aclose()
        if exc.status in (401, 403):
            raise OntoAuthError(401, "invalid_token")
        raise OntoAuthError(502, "odoo_unavailable")
    try:
        _check_bot(tenant, who)
    except OntoAuthError:
        await client.aclose()
        raise
    return tenant, client


def summarize(reply: dict) -> dict:
    """Counts only, from Odoo's parity reply. Raises ValueError for any shape the job does not expect."""
    results = reply.get("results")
    if not isinstance(results, list) or not all(
        isinstance(row, dict) and isinstance(row.get("match"), bool) and isinstance(row.get("object"), str)
        for row in results
    ):
        raise ValueError("unexpected parity reply")
    counts = {name: reply.get(name, 0) for name in ("suspended", "lifted")}
    if not all(type(value) is int for value in counts.values()):
        raise ValueError("unexpected parity counts")
    return {
        "objects": len({row["object"] for row in results}),
        "checks": len(results),
        "matched": sum(row["match"] for row in results),
        "mismatched": sum(not row["match"] for row in results),
        **counts,
    }


def _running(session: db.SessionType, tenant_id, job: str = JOB) -> JobRun | None:
    fresh = datetime.now(timezone.utc) - RUNNING_WINDOW
    return session.exec(
        scoped(JobRun, tenant_id).where(JobRun.job == job, JobRun.state == "running", JobRun.heartbeat_at >= fresh)
    ).first()


async def run_job(session: db.SessionType, tenant_id, odoo: OdooClient) -> tuple[int, dict]:
    """(status, body) of one parity job: refuses a live run (409), asks Odoo once, keeps counts in job_run."""
    if _running(session, tenant_id) is not None:
        return 409, {"error": "already_running"}
    row = JobRun(tenant_id=tenant_id, job=JOB, state="running", heartbeat_at=datetime.now(timezone.utc))
    session.add(row)
    session.commit()
    try:
        counts = summarize(await odoo.request("POST", RUN_PATH, json={}))
    except OdooError as exc:
        row.state, row.result = "failed", {"error": "odoo_unavailable", "status": exc.status}
        session.add(row)
        session.commit()
        return 502, {"error": "odoo_unavailable"}
    except ValueError:
        row.state, row.result = "failed", {"error": "odoo_unexpected"}
        session.add(row)
        session.commit()
        return 502, {"error": "odoo_unexpected"}
    row.state, row.result = "done", counts
    row.heartbeat_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    return 200, {"job_run_id": row.id, "state": "done", "result": counts}


@router.post("/jobs/parity")
async def parity_job(request: Request, session: db.SessionType) -> JSONResponse:
    tenant, client = await _bot_client(request, session)
    try:
        status, body = await run_job(session, tenant.id, client)
    finally:
        await client.aclose()
    return JSONResponse(body, status_code=status)
