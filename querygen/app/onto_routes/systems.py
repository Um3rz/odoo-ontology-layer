"""POST /systems/run/{job} and GET /systems/run/{job_run_id}: Odoo's crons start querygen jobs (plan 3.3; contract 3.5).

Odoo's onto_tick sends its bot token as `Authorization: Bearer <bot token>` with `X-Odoo-Url`, the same
way the parity and drift jobs do. parity._bot_client makes the checks: registered database, the Onto bot
group, AI switch on (D29). The route answers 202 with the job_run id and queues the run on the agent
service's queue (runs.enqueue_job). A live run of the same job answers 409. The cron only logs it.
Progress and the result are read with GET; the job_run row holds counts only.
"""
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import db
from app.agents import runs
from app.db.onto import JobRun
from app.db.tenant import scoped
from app.onto_routes import parity
from app.systems.jobs import JOBS
from app.utils.browser_auth import OntoAuthError

MAX_PARAMS_BYTES = 4096

router = APIRouter(prefix="/systems", tags=["systems"])


class JobIn(BaseModel):
    params: dict = Field(default_factory=dict)


@router.post("/run/{job}", status_code=202)
async def start_job(job: str, request: Request, session: db.SessionType, body: JobIn | None = None) -> JSONResponse:
    tenant, client = await parity._bot_client(request, session)
    await client.aclose()  # the job makes its own Odoo calls, with the same bearer
    if job not in JOBS:
        return JSONResponse({"error": "unknown_job"}, status_code=404)
    params = body.params if body is not None else {}
    if len(json.dumps(params)) > MAX_PARAMS_BYTES:
        return JSONResponse({"error": "params_too_large"}, status_code=422)
    runs.BOOK.put(tenant.id, None, tenant.odoo_base_url, request.headers.get("authorization", "").partition(" ")[2])
    try:
        job_run_id = await runs.enqueue_job(tenant.id, job, params)
    except runs.JobBusy:
        return JSONResponse({"error": "already_running"}, status_code=409)
    await runs.claim_ready([tenant.id])
    return JSONResponse({"accepted": True, "job_run_id": job_run_id}, status_code=202)


@router.get("/run/{job_run_id}")
async def job_status(job_run_id: int, request: Request, session: db.SessionType) -> dict:
    tenant, client = await parity._bot_client(request, session)
    await client.aclose()
    row = session.exec(scoped(JobRun, tenant.id).where(JobRun.id == job_run_id)).first()
    if row is None:
        raise OntoAuthError(404, "not_found")
    return {"job_run_id": row.id, "job": row.job, "state": row.state,
            "heartbeat_at": row.heartbeat_at, "result": row.result}
