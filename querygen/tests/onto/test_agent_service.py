"""Plan 3.2 (querygen half): the agent service over a live Redis and the scratch Postgres.

Needs REDIS_URL (the compose redis service) and the scratch database from conftest. Odoo is a stub:
whoami is answered from WHO, and the outbox flush is replaced by a fake. Runs execute in this
test's event loop; _drain waits for them.
"""
import asyncio
import json
import uuid

import httpx
import pytest

from app import db
from app.agents import checkpointer, events, runs, service
from app.db.onto import Tenant
from app.db.tenant import onto_key
from app.odoo.client import OdooClient
from app.utils import browser_auth as ba

ODOO = "https://odoo.example.test"
WEB = "https://app.example.test"
DB_UUID = "33a1b60e-0000-0000-0000-000000000002"
WHO = {
    "alice.sig": {"database": {"uuid": DB_UUID}, "ai_enabled": True, "user": {"id": 7}, "groups": []},
    "bob.sig": {"database": {"uuid": DB_UUID}, "ai_enabled": True, "user": {"id": 8}, "groups": []},
    "off.sig": {"database": {"uuid": DB_UUID}, "ai_enabled": False, "user": {"id": 9}, "groups": []},
}
ALICE = {"Authorization": "Bearer alice.sig", "X-Odoo-Url": ODOO}
BOB = {"Authorization": "Bearer bob.sig", "X-Odoo-Url": ODOO}


def _odoo(req: httpx.Request) -> httpx.Response:
    who = WHO.get(req.headers["authorization"].partition(" ")[2])
    return httpx.Response(401, json={}) if who is None else httpx.Response(200, json=who)


@pytest.fixture()
def tenant(engine, session, clean_tenants, monkeypatch):
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(ba, "odoo_client_factory",
                        lambda base, tok: OdooClient(base, tok, transport=httpx.MockTransport(_odoo), backoff=0))
    monkeypatch.setattr(ba, "token_limiter", ba.RateLimiter(1000, 60))
    monkeypatch.setattr(ba, "tenant_limiter", ba.RateLimiter(1000, 60))
    monkeypatch.setattr(ba, "_origins_cache", (-1e9, set()))
    monkeypatch.delenv("ONTO_WEB_ORIGINS", raising=False)
    ba._whoami_cache.clear()
    runs.BOOK._held.clear()
    row = Tenant(odoo_base_url=ODOO, db_uuid=DB_UUID, web_origins=[WEB])
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


async def test_two_chats_run_and_stream_independently(tenant):
    try:
        async with _client() as c:
            first = (await c.post("/agents/runs", json={"text": "alpha", "agent": "echo"}, headers=ALICE)).json()["run_id"]
            second = (await c.post("/agents/runs", json={"text": "beta", "agent": "echo"}, headers=ALICE)).json()["run_id"]
            await _drain()
            one = (await c.get(f"/agents/runs/{first}/events", headers=ALICE)).text
            two = (await c.get(f"/agents/runs/{second}/events", headers=ALICE)).text
        assert "Echo: alpha" in one and "beta" not in one
        assert "Echo: beta" in two and "alpha" not in two
        assert "event: done" in one and "event: done" in two
    finally:
        await _wipe(tenant.id)


async def test_another_user_cannot_read_a_run(tenant):
    try:
        async with _client() as c:
            rid = (await c.post("/agents/runs", json={"text": "private", "agent": "echo"}, headers=ALICE)).json()["run_id"]
            await _drain()
            other = await c.get(f"/agents/runs/{rid}/events", headers=BOB)
            missing = await c.get(f"/agents/runs/{uuid.uuid4().hex}/events", headers=ALICE)
        assert other.status_code == 404 and other.json() == {"error": "not_found"}
        assert missing.status_code == 404
    finally:
        await _wipe(tenant.id)


async def test_a_queued_run_waits_for_its_owners_token(tenant):
    try:
        rid = await runs.start_chat(tenant.id, 7, "later")
        assert await runs.claim_ready([tenant.id]) == 0
        assert (await runs.view(tenant.id, rid))["state"] == "queued"
        runs.BOOK.put(tenant.id, 7, ODOO, "alice.sig")
        assert await runs.claim_ready([tenant.id]) == 1
        await _drain()
        assert (await runs.view(tenant.id, rid))["state"] == "done"
    finally:
        await _wipe(tenant.id)


async def test_the_per_tenant_cap_holds_back_extra_runs(tenant, monkeypatch):
    monkeypatch.setenv("ONTO_RUNS_PER_TENANT", "1")
    try:
        runs.BOOK.put(tenant.id, 7, ODOO, "alice.sig")
        first = await runs.start_chat(tenant.id, 7, "a")
        second = await runs.start_chat(tenant.id, 7, "b")
        assert await runs.claim_ready([tenant.id]) == 1
        assert await runs.claim_ready([tenant.id]) == 0  # the first is still listed as running
        await _drain()
        assert (await runs.view(tenant.id, first))["state"] == "done"
        assert await runs.claim_ready([tenant.id]) == 1
        await _drain()
        assert (await runs.view(tenant.id, second))["state"] == "done"
    finally:
        await _wipe(tenant.id)


async def test_a_run_whose_process_died_resumes_from_its_checkpoint(tenant):
    try:
        r = runs.redis()
        rid = await runs.start_chat(tenant.id, 7, "first")
        saver = await checkpointer.checkpointer(tenant.id)
        config = {"configurable": {"thread_id": checkpointer.thread_id(tenant.id, rid)}}
        # The dead process got as far as the checkpoint before the answer node...
        await runs._echo_graph(tenant.id, rid, saver, interrupt_before=("answer",)).ainvoke(
            {"question": "first"}, config)
        # ...and left the run listed as running, with no heartbeat.
        await r.lrem(onto_key(tenant.id, "queue"), 0, rid)
        await r.sadd(onto_key(tenant.id, "running"), rid)
        await r.hset(onto_key(tenant.id, "run", rid, "meta"), "state", "running")
        assert await runs.requeue_stale([tenant.id]) == 1
        runs.BOOK.put(tenant.id, 7, ODOO, "alice.sig")
        assert await runs.claim_ready([tenant.id]) == 1
        await _drain()
        assert (await runs.view(tenant.id, rid))["state"] == "done"
        seen = await events.read(r, tenant.id, rid, events.START_ID, block_ms=100)
        # The step ran once, before the crash; the resume adds the answer and done.
        assert [body["type"] for _, body in seen] == ["step", "answer", "done"]
        assert seen[1][1]["data"]["text"] == "Echo: first"
    finally:
        await _wipe(tenant.id)


async def test_last_event_id_skips_what_the_client_already_has(tenant):
    try:
        r = runs.redis()
        rid = await runs.start_chat(tenant.id, 7, "x")
        ids = [await events.emit(r, tenant.id, rid, "step", {"n": n}) for n in (1, 2, 3)]
        await events.emit(r, tenant.id, rid, "done", {"state": "done"})
        async with _client() as c:
            resp = await c.get(f"/agents/runs/{rid}/events", headers={**ALICE, "Last-Event-ID": ids[1]})
        assert resp.status_code == 200
        assert f"id: {ids[2]}\n" in resp.text
        assert f"id: {ids[1]}\n" not in resp.text and f"id: {ids[0]}\n" not in resp.text
        assert "event: done" in resp.text
    finally:
        await _wipe(tenant.id)


async def test_a_finished_run_drops_its_input_and_queues_the_record(tenant):
    try:
        runs.BOOK.put(tenant.id, 7, ODOO, "alice.sig")
        rid = await runs.start_chat(tenant.id, 7, "the question")
        await runs.claim_ready([tenant.id])
        await _drain()
        r = runs.redis()
        assert await r.hexists(onto_key(tenant.id, "run", rid, "meta"), "input") == 0
        held = json.loads(await r.get(onto_key(tenant.id, "outbox", "item", f"{rid}:0")))
        assert held["owner_uid"] == 7
        assert held["record"]["run"]["state"] == "done"
        assert held["record"]["chat"]["messages"][1]["text"] == "Echo: the question"
        assert held["record"]["usage"]["credits"] == 0.0  # D33: recorded, not charged
    finally:
        await _wipe(tenant.id)


async def test_the_service_loop_flushes_records_only_for_owners_it_holds(tenant, monkeypatch):
    sent = []

    async def fake_flush(redis, tenant_id, *, odoo_url, bearers, send=None):
        sent.append((odoo_url, bearers))
        return 1

    monkeypatch.setattr(service.record, "flush", fake_flush)
    try:
        assert await service.flush_outbox([tenant.id]) == 0  # no bearer held: nothing sent
        runs.BOOK.put(tenant.id, 7, ODOO, "alice.sig")
        assert await service.flush_outbox([tenant.id]) == 1
        assert sent == [(ODOO, {7: "alice.sig"})]
    finally:
        await _wipe(tenant.id)


async def test_ai_off_gets_no_run_and_finished_runs_do_not_resume(tenant):
    try:
        async with _client() as c:
            off = await c.post("/agents/runs", json={"text": "hi", "agent": "echo"},
                               headers={**ALICE, "Authorization": "Bearer off.sig"})
            assert off.status_code == 403 and off.json() == {"error": "ai_features_off"}
            rid = (await c.post("/agents/runs", json={"text": "hi", "agent": "echo"}, headers=ALICE)).json()["run_id"]
            await _drain()
            resumed = await c.post(f"/agents/runs/{rid}/resume", headers=ALICE)
        assert resumed.status_code == 409 and resumed.json() == {"error": "not_resumable"}
    finally:
        await _wipe(tenant.id)
