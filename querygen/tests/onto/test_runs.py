"""Plan 3.1 (querygen half): Redis checkpointer, record outbox, no text in Postgres.

Redis tests need a live Redis 8 at REDIS_URL (the compose redis service). The
Postgres tests use the scratch database from conftest. Nothing here calls Odoo:
a stub `send` stands in for it. Odoo-side checks (record rules, idempotent
write, token scope) belong to od-demo-addons.
"""
import json
import os
import uuid
from functools import partial
from typing import TypedDict

import httpx
import pytest
from langgraph.graph import END, START, StateGraph
from redis.asyncio import Redis
from sqlalchemy import text

from app.agents import checkpointer, record
from app.db.onto import ONTO_TABLES, DecisionLog, JobRun, LlmUsage
from app.db.tenant import onto_key
from app.odoo.client import OdooError

ODOO = "https://odoo.example.test"
TOKEN = "aaa.bbb"
DAY = 24 * 3600


class _State(TypedDict):
    n: int


def _graph(saver):
    """a -> b, interrupted before b: the run is half-done after the first invoke."""
    def bump(state: _State) -> dict:
        return {"n": state["n"] + 1}

    g = StateGraph(_State)
    g.add_node("a", bump)
    g.add_node("b", bump)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g.compile(checkpointer=saver, interrupt_before=["b"])


def _record(ikey: str, question: str = "How much stock is left?") -> dict:
    return {
        "run": {"id": "run-1", "kind": "chat", "state": "done", "trace": []},
        "chat": {"thread_id": None, "title": "t", "messages": [{"role": "user", "text": question}]},
        "usage": {"model": "m", "input_tokens": 1, "output_tokens": 1, "credits": 0.0},
        "idempotency_key": ikey,
    }


def _redis() -> Redis:
    return Redis.from_url(os.environ["REDIS_URL"])


async def _down(url, bearer, rec):
    raise OdooError(0, "ConnectError")


# ---------------------------------------------------------------- record outbox

async def test_record_waits_while_odoo_is_down_and_lands_when_it_is_back():
    tenant, r = uuid.uuid4(), _redis()
    try:
        await record.enqueue(r, tenant, _record("run-1:1"), owner_uid=7)
        assert await record.flush(r, tenant, odoo_url=ODOO, bearers={7: TOKEN}, send=_down) == 0
        assert await r.zcard(onto_key(tenant, "outbox")) == 1

        sent = []

        async def up(url, bearer, rec):
            sent.append((url, bearer, rec["idempotency_key"]))

        assert await record.flush(r, tenant, odoo_url=ODOO, bearers={7: TOKEN}, send=up) == 1
        assert sent == [(ODOO, TOKEN, "run-1:1")]
        # Accepted, so nothing of the record is left in Redis.
        assert [k async for k in r.scan_iter(match=f"onto:{tenant}:*")] == []
    finally:
        await r.aclose()


async def test_replayed_record_is_held_once_and_sent_once():
    tenant, r = uuid.uuid4(), _redis()
    sent = []

    async def up(url, bearer, rec):
        sent.append(rec["idempotency_key"])

    try:
        await record.enqueue(r, tenant, _record("run-1:1"), owner_uid=7)
        await record.enqueue(r, tenant, _record("run-1:1"), owner_uid=7)
        assert await r.zcard(onto_key(tenant, "outbox")) == 1
        assert await record.flush(r, tenant, odoo_url=ODOO, bearers={7: TOKEN}, send=up) == 1
        assert await record.flush(r, tenant, odoo_url=ODOO, bearers={7: TOKEN}, send=up) == 0
        assert sent == ["run-1:1"]
    finally:
        await r.aclose()


async def test_a_bearer_only_sends_its_own_users_records():
    tenant, r = uuid.uuid4(), _redis()
    sent = []

    async def up(url, bearer, rec):
        sent.append(rec)

    try:
        await record.enqueue(r, tenant, _record("run-2:1"), owner_uid=2)
        assert await record.flush(r, tenant, odoo_url=ODOO, bearers={1: TOKEN}, send=up) == 0
        assert sent == []
        assert await r.zcard(onto_key(tenant, "outbox")) == 1
    finally:
        await r.aclose()


async def test_held_records_expire_after_seven_days():
    tenant, r = uuid.uuid4(), _redis()
    try:
        await record.enqueue(r, tenant, _record("run-3:1"), owner_uid=7)
        item_ttl = await r.ttl(onto_key(tenant, "outbox", "item", "run-3:1"))
        index_ttl = await r.ttl(onto_key(tenant, "outbox"))
        assert 0 < item_ttl <= record.OUTBOX_TTL_S
        assert 0 < index_ttl <= record.OUTBOX_TTL_S
    finally:
        await r.delete(onto_key(tenant, "outbox", "item", "run-3:1"), onto_key(tenant, "outbox"))
        await r.aclose()


async def test_odoo_wire_format_and_refusal_is_not_retried():
    seen = []
    replies = iter([
        httpx.Response(200, json={"thread_id": 1, "run_id": 2, "generation_log_id": 3}),
        httpx.Response(403, json={"error": "forbidden"}),
    ])

    def handler(req: httpx.Request):
        seen.append(req)
        return next(replies)

    send = partial(record.odoo_send, transport=httpx.MockTransport(handler))
    await send(ODOO, TOKEN, _record("run-1:1"))
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/onto/v1/agent/record"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(req.content)["idempotency_key"] == "run-1:1"

    with pytest.raises(OdooError) as refused:
        await send(ODOO, TOKEN, _record("run-1:2"))
    assert refused.value.status == 403
    assert len(seen) == 2  # a 403 is not retried


# ---------------------------------------------------------------- checkpointer

async def test_checkpoints_are_tenant_prefixed_and_expire_in_24_hours():
    tenant, r = uuid.uuid4(), _redis()
    try:
        saver = await checkpointer.checkpointer(tenant)
        config = {"configurable": {"thread_id": checkpointer.thread_id(tenant, uuid.uuid4())}}
        await _graph(saver).ainvoke({"n": 0}, config)
        keys = [k.decode() async for k in r.scan_iter(match=f"*{tenant}*")]
        assert keys
        for key in keys:
            assert key.startswith(f"onto:{tenant}:"), key
            assert 0 < await r.ttl(key) <= DAY, key
    finally:
        await checkpointer.aclose()
        await r.aclose()


async def test_graph_resumes_from_redis_after_a_restart():
    tenant = uuid.uuid4()
    config = {"configurable": {"thread_id": checkpointer.thread_id(tenant, uuid.uuid4())}}
    try:
        await _graph(await checkpointer.checkpointer(tenant)).ainvoke({"n": 0}, config)  # stops before b
        await checkpointer.aclose()  # the process restarts: no cached client or saver
        out = await _graph(await checkpointer.checkpointer(tenant)).ainvoke(None, config)
        assert out == {"n": 2}
    finally:
        await checkpointer.aclose()


async def test_one_tenant_cannot_read_another_tenants_checkpoint():
    owner, other = uuid.uuid4(), uuid.uuid4()
    config = {"configurable": {"thread_id": checkpointer.thread_id(owner, uuid.uuid4())}}
    try:
        await _graph(await checkpointer.checkpointer(owner)).ainvoke({"n": 0}, config)
        peer = await checkpointer.checkpointer(other)
        assert await peer.aget_tuple(config) is None
    finally:
        await checkpointer.aclose()


def test_checkpointer_backend_defaults_to_redis_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("ONTO_CHECKPOINTER", raising=False)
    assert checkpointer.backend() == "redis"
    monkeypatch.setenv("ONTO_CHECKPOINTER", "postgres")
    assert checkpointer.backend() == "postgres"
    monkeypatch.setenv("ONTO_CHECKPOINTER", "memory")
    with pytest.raises(RuntimeError):
        checkpointer.backend()


async def test_postgres_fallback_purges_threads_idle_for_24_hours(scratch_url, monkeypatch):
    monkeypatch.setenv("ONTO_CHECKPOINTER", "postgres")
    monkeypatch.setenv("DATABASE_URL", scratch_url)
    tenant = uuid.uuid4()
    old = checkpointer.thread_id(tenant, uuid.uuid4())
    new = checkpointer.thread_id(tenant, uuid.uuid4())
    try:
        saver = await checkpointer.checkpointer(tenant)
        for tid in (old, new):
            await _graph(saver).ainvoke({"n": 0}, {"configurable": {"thread_id": tid}})
        async with saver.conn.connection() as conn:
            await conn.execute(
                "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', %s::jsonb) WHERE thread_id = %s",
                (json.dumps("2020-01-01T00:00:00+00:00"), old),
            )
        await checkpointer.purge_finished_threads()
        assert await saver.aget_tuple({"configurable": {"thread_id": old}}) is None
        assert await saver.aget_tuple({"configurable": {"thread_id": new}}) is not None
    finally:
        await checkpointer.aclose()


# ---------------------------------------------------------------- storage holds no text

def test_operations_tables_have_no_text_columns():
    banned = {"question", "answer", "text", "prompt", "message", "messages", "content", "body", "history", "card"}
    for model in (DecisionLog, JobRun, LlmUsage):
        assert not banned & set(model.__table__.columns.keys()), model.__tablename__


async def test_no_chat_text_reaches_querygen_postgres(engine, monkeypatch):
    # The Redis path is the default. The Postgres fallback keeps graph state by design, so this guard is for Redis.
    monkeypatch.delenv("ONTO_CHECKPOINTER", raising=False)
    marker = f"zz-question-{uuid.uuid4().hex}"
    tenant, r = uuid.uuid4(), _redis()
    try:
        saver = await checkpointer.checkpointer(tenant)
        await _graph(saver).ainvoke({"n": 0}, {"configurable": {"thread_id": checkpointer.thread_id(tenant, uuid.uuid4())}})
        await record.submit(r, tenant, _record("run-4:1", marker), 7, odoo_url=ODOO, bearer=TOKEN, send=_down)
    finally:
        await checkpointer.aclose()
        await r.delete(onto_key(tenant, "outbox", "item", "run-4:1"), onto_key(tenant, "outbox"))
        await r.aclose()
    with engine.connect() as conn:
        for table in ONTO_TABLES:
            rows = conn.execute(text(f'SELECT * FROM "{table}"')).fetchall()
            assert marker not in str(rows), table
