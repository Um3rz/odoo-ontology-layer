"""Plan 3.3 (querygen half): POST /systems/run/{job} answers 202, 409 or 404, and a job runs on the queue.

Odoo's cron sends the bot token; the same live Redis, scratch Postgres and stubbed Odoo as
test_agent_service.py. Only the bot may start a job. parity._bot_client makes the checks.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import db
from app.agents import runs, service
from app.db.onto import JobRun, Tenant
from app.db.tenant import onto_key
from app.odoo.client import OdooClient
from app.onto_routes import parity
from app.onto_routes.parity import BOT_GROUP
from app.utils import browser_auth as ba

ODOO = "https://odoo.example.test"
DB_UUID = "33a1b60e-0000-0000-0000-000000000003"
WHO = {
    "bot.sig": {"database": {"uuid": DB_UUID}, "ai_enabled": True, "user": {"id": 3}, "groups": [BOT_GROUP]},
    "botoff.sig": {"database": {"uuid": DB_UUID}, "ai_enabled": False, "user": {"id": 3}, "groups": [BOT_GROUP]},
    "alice.sig": {"database": {"uuid": DB_UUID}, "ai_enabled": True, "user": {"id": 7}, "groups": []},
}
BOT = {"Authorization": "Bearer bot.sig", "X-Odoo-Url": ODOO}


def _odoo(req: httpx.Request) -> httpx.Response:
    who = WHO.get(req.headers["authorization"].partition(" ")[2])
    return httpx.Response(401, json={}) if who is None else httpx.Response(200, json=who)


@pytest.fixture()
def tenant(engine, session, clean_tenants, monkeypatch):
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(parity, "odoo_client_factory",
                        lambda base, tok: OdooClient(base, tok, transport=httpx.MockTransport(_odoo), retries=0))
    monkeypatch.setattr(ba, "token_limiter", ba.RateLimiter(1000, 60))
    monkeypatch.setattr(ba, "tenant_limiter", ba.RateLimiter(1000, 60))
    runs.BOOK._held.clear()
    row = Tenant(odoo_base_url=ODOO, db_uuid=DB_UUID, web_origins=[])
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url="http://querygen.test")


async def _drain() -> None:
    while runs.TASKS:
        await asyncio.gather(*list(runs.TASKS), return_exceptions=True)


async def _wipe(tenant_id) -> None:
    r = runs.redis()
    keys = [k async for k in r.scan_iter(match=f"onto:{tenant_id}:*")]
    if keys:
        await r.delete(*keys)
    await runs.close()


async def test_the_bot_starts_the_echo_job_and_it_finishes_with_counts_only(tenant):
    try:
        async with _client() as c:
            resp = await c.post("/systems/run/echo", json={"params": {"as_of": "2026-11-09"}}, headers=BOT)
            assert resp.status_code == 202 and resp.json()["accepted"] is True
            job_run_id = resp.json()["job_run_id"]
            await _drain()
            status = await c.get(f"/systems/run/{job_run_id}", headers=BOT)
        assert status.json()["state"] == "done"
        assert status.json()["result"] == {"ok": True}
        # The run record goes to Odoo through the outbox (3.10), keyed by the job_run id.
        held = await runs.redis().get(onto_key(tenant.id, "outbox", "item", f"{job_run_id}:0"))
        assert held is not None
    finally:
        await _wipe(tenant.id)


async def test_a_user_token_cannot_start_a_job(tenant):
    try:
        async with _client() as c:
            resp = await c.post("/systems/run/echo", json={}, headers={**BOT, "Authorization": "Bearer alice.sig"})
        assert resp.status_code == 403 and resp.json() == {"error": "not_the_bot"}
    finally:
        await _wipe(tenant.id)


async def test_an_unregistered_job_is_not_found(tenant):
    try:
        async with _client() as c:
            resp = await c.post("/systems/run/nope", json={}, headers=BOT)
        assert resp.status_code == 404 and resp.json() == {"error": "unknown_job"}
    finally:
        await _wipe(tenant.id)


async def test_ai_off_refuses_the_cron(tenant):
    try:
        async with _client() as c:
            resp = await c.post("/systems/run/echo", json={}, headers={**BOT, "Authorization": "Bearer botoff.sig"})
        assert resp.status_code == 403 and resp.json() == {"error": "ai_features_off"}
    finally:
        await _wipe(tenant.id)


async def test_a_live_run_of_the_same_job_answers_409(tenant, session):
    try:
        session.add(JobRun(tenant_id=tenant.id, job="echo", state="running",
                           heartbeat_at=datetime.now(timezone.utc)))
        session.commit()
        async with _client() as c:
            resp = await c.post("/systems/run/echo", json={}, headers=BOT)
        assert resp.status_code == 409 and resp.json() == {"error": "already_running"}
        session.expire_all()
        assert len(session.exec(select(JobRun).where(JobRun.tenant_id == tenant.id)).all()) == 1
    finally:
        await _wipe(tenant.id)


async def test_a_lapsed_run_does_not_block_and_is_superseded(tenant, session):
    try:
        old = JobRun(tenant_id=tenant.id, job="echo", state="running",
                     heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        session.add(old)
        session.commit()
        session.refresh(old)
        async with _client() as c:
            resp = await c.post("/systems/run/echo", json={}, headers=BOT)
            await _drain()
        assert resp.status_code == 202
        session.expire_all()
        assert session.get(JobRun, old.id).state == "cancelled"
    finally:
        await _wipe(tenant.id)


async def test_the_next_tick_supersedes_a_run_still_waiting_for_a_token(tenant):
    try:
        # A restarted process holds no bot bearer, so the queued run waits (contract 3.2: fresh token).
        waiting = await runs.enqueue_job(tenant.id, "echo", {})
        assert await runs.claim_ready([tenant.id]) == 0
        async with _client() as c:
            resp = await c.post("/systems/run/echo", json={}, headers=BOT)
            await _drain()
        assert resp.status_code == 202
        assert (await runs.view(tenant.id, str(waiting)))["state"] == "cancelled"
    finally:
        await _wipe(tenant.id)
