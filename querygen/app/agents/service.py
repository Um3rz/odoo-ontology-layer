"""Agent service: /agents/* routes, the background loop, and the agent app (plan 3.2; contracts 3.5, 3.6).

main.py includes `router`, so the api container serves /agents/* and runs the loop. The `agent`
compose service runs `app` (uvicorn app.agents.service:app) with the same router and loop. A run
starts in whichever process holds its owner's bearer, so the two can share one Redis safely.

Browser routes use browser_auth (the query token, checked through Odoo's whoami) and the
registered-origin CORS of app/utils/browser_auth.py. Usage goes on the run record (3.10). Runs are
not blocked on credits (D33).
"""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents import checkpointer, events, record, runs
from app.onto_routes.parity import BOT_GROUP
from app.utils.browser_auth import (
    OntoAuth,
    OntoAuthError,
    OntoCORSMiddleware,
    browser_auth,
    onto_auth_error_handler,
)

log = logging.getLogger(__name__)

LOOP_S = 1.0
PURGE_EVERY_S = 60.0
TERMINAL = frozenset({"done", "failed", "cancelled"})
KEEPALIVE_MS = 15000  # an SSE comment every 15 s keeps proxies from closing an idle stream
RUN_ID = r"^[0-9a-f]{32}$"  # uuid4().hex, see runs.start_chat

router = APIRouter(prefix="/agents", tags=["agents"])


class RunIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["bi", "echo", "inventory", "chat"] = "bi"  # chat = legacy name for the 3.2 stand-in
    agent: Literal["bi", "echo"] | None = None  # explicit stand-in switch used by the 3.2 tests


def _owner(auth: OntoAuth) -> int | None:
    """The Odoo user id, or None for the bot (system runs). Only called after browser_auth."""
    if BOT_GROUP in (auth.whoami.get("groups") or []):
        return None
    uid = (auth.whoami.get("user") or {}).get("id")
    if type(uid) is not int:
        raise OntoAuthError(403, "no_user")
    return uid


def _bearer(request: Request) -> str:
    return request.headers.get("authorization", "").partition(" ")[2]


async def _owned_run(auth: OntoAuth, run_id: str) -> dict:
    """The run, or 404. A run of another owner gets the same 404 as a missing one."""
    meta = await runs.view(auth.tenant_id, run_id)
    if meta is None or meta["owner"] != _owner(auth):
        raise OntoAuthError(404, "not_found")
    return meta


@router.post("/runs", status_code=202)
async def create_run(body: RunIn, request: Request, auth: OntoAuth = Depends(browser_auth)) -> dict:
    owner = _owner(auth)
    runs.BOOK.put(auth.tenant_id, owner, auth.odoo_base_url, _bearer(request))
    agent = body.agent or ("echo" if body.kind in ("echo", "chat") else "bi")
    if body.kind == "inventory":
        run_id = await runs.start_inventory(auth.tenant_id, owner, body.text)
    else:
        run_id = await runs.start_chat(auth.tenant_id, owner, body.text, agent)
    await runs.claim_ready([auth.tenant_id])
    return {"run_id": run_id, "state": "queued"}


@router.get("/runs")
async def list_runs(auth: OntoAuth = Depends(browser_auth)) -> dict:
    return {"runs": await runs.list_runs(auth.tenant_id, _owner(auth))}


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_run(request: Request, run_id: str = Path(pattern=RUN_ID), auth: OntoAuth = Depends(browser_auth)) -> dict:
    meta = await _owned_run(auth, run_id)
    if meta["state"] in TERMINAL:
        raise OntoAuthError(409, "not_resumable")
    runs.BOOK.put(auth.tenant_id, meta["owner"], auth.odoo_base_url, _bearer(request))
    await runs.resume(auth.tenant_id, run_id)
    await runs.claim_ready([auth.tenant_id])
    return {"run_id": run_id, "state": "queued"}


@router.get("/runs/{run_id}/events")
async def run_events(request: Request, run_id: str = Path(pattern=RUN_ID), auth: OntoAuth = Depends(browser_auth)):
    await _owned_run(auth, run_id)
    last = request.headers.get("last-event-id", events.START_ID)
    if not events.valid_id(last):
        raise OntoAuthError(400, "bad_last_event_id")
    return StreamingResponse(
        _stream(request, auth.tenant_id, run_id, last),
        media_type="text/event-stream",
        # X-Accel-Buffering: no turns off nginx buffering; the proxy must do the same (see docker-compose.yml).
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _stream(request: Request, tenant_id, run_id: str, last: str):
    r = runs.redis()
    while True:
        if await request.is_disconnected():
            return
        batch = await events.read(r, tenant_id, run_id, last, block_ms=KEEPALIVE_MS)
        if not batch:
            if not await runs.alive(tenant_id, run_id):
                return  # the run expired
            yield ": keepalive\n\n"
            continue
        for entry_id, body in batch:
            last = entry_id
            yield events.frame(entry_id, body)
            if body["type"] == "done":
                return


async def flush_outbox(tenants) -> int:
    """Post held agent records for tenants whose owners hold a bearer here. The rest wait (contract 3.4)."""
    sent = 0
    for tenant_id in tenants:
        bearers, url = runs.BOOK.bearers(tenant_id), runs.BOOK.url(tenant_id)
        if bearers and url:
            sent += await record.flush(runs.redis(), tenant_id, odoo_url=url, bearers=bearers)
    return sent


async def service_loop() -> None:
    """Every second: requeue stale runs, start runnable ones, flush the outbox; purge threads each minute."""
    next_purge = 0.0
    while True:
        try:
            tenants = await runs.known_tenants()
            await runs.requeue_stale(tenants)
            await runs.claim_ready(tenants)
            await flush_outbox(tenants)
            if time.monotonic() >= next_purge:
                await checkpointer.purge_finished_threads()
                next_purge = time.monotonic() + PURGE_EVERY_S
        except Exception as exc:  # noqa: BLE001 - keep looping; log the type only, never content
            log.error("agent loop step failed: %s", type(exc).__name__)
        await asyncio.sleep(LOOP_S)


def start_loop() -> asyncio.Task | None:
    """Start this process's loop. Needs REDIS_URL, so tests and REDIS-less runs skip it."""
    if not os.getenv("REDIS_URL"):
        return None
    return asyncio.create_task(service_loop())


async def stop_loop(task: asyncio.Task | None) -> None:
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await runs.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = start_loop()
    yield
    await stop_loop(task)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_exception_handler(OntoAuthError, onto_auth_error_handler)
app.add_middleware(OntoCORSMiddleware)
app.include_router(router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
