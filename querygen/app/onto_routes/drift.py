"""POST /onto/jobs/drift: the drift job (plan 2.5, contract 9). It copies the parity job's pattern (onto_routes/parity.py).

Odoo starts it with the tenant's Onto bot token, and _bot_client checks the token exactly as for parity. The job asks
Odoo for one introspect (2.1, the same bot token), compares it with the tenant's current release (ontology/drift.py)
and stores the result: drift rows, pending proposals, resolutions. job_run keeps counts only. The reply to the caller
is the drift reply (contract 9), which names the broken bindings by node and field, never a record value.

A run with a fresh heartbeat blocks another one for the tenant (409). Odoo's call is made once.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import db
from app.db.onto import JobRun
from app.odoo.client import OdooClient, OdooError
from app.onto_routes import parity
from app.ontology import drift, store
from app.ontology.builder.profile import InputError

JOB = "drift"
INTROSPECT_PATH = "/onto/v1/introspect"

router = APIRouter()


def _finish(session: db.SessionType, row: JobRun, state: str, result: dict) -> None:
    row.state, row.result, row.heartbeat_at = state, result, datetime.now(timezone.utc)
    session.add(row)
    session.commit()


async def run_job(session: db.SessionType, tenant_id, odoo: OdooClient) -> tuple[int, dict]:
    """(status, body) of one drift job: refuses a live run (409) or a tenant with no release, asks Odoo once."""
    if parity._running(session, tenant_id, JOB) is not None:
        return 409, {"error": "already_running"}
    if store.current_release(session, tenant_id) is None:
        return 404, {"error": "no_release"}
    row = JobRun(tenant_id=tenant_id, job=JOB, state="running", heartbeat_at=datetime.now(timezone.utc))
    session.add(row)
    session.commit()
    try:
        reply = await odoo.request("POST", INTROSPECT_PATH, json={})
    except OdooError as exc:
        _finish(session, row, "failed", {"error": "odoo_unavailable", "status": exc.status})
        return 502, {"error": "odoo_unavailable"}
    try:
        out = drift.check(session, tenant_id, reply)
    except InputError:
        _finish(session, row, "failed", {"error": "odoo_unexpected"})
        return 502, {"error": "odoo_unexpected"}
    _finish(session, row, "done", drift.counts(out))
    return 200, {"job_run_id": row.id, "state": "done", **out}


@router.post("/jobs/drift")
async def drift_job(request: Request, session: db.SessionType) -> JSONResponse:
    tenant, client = await parity._bot_client(request, session)
    try:
        status, body = await run_job(session, tenant.id, client)
    finally:
        await client.aclose()
    return JSONResponse(body, status_code=status)
