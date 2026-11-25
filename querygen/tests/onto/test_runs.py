"""Plan 3.1 (querygen half): Redis checkpointer, record outbox, no text in Postgres.

Redis tests need a live Redis 8 at REDIS_URL (the compose redis service). The
Postgres tests use the scratch database from conftest. Nothing here calls Odoo:
a stub `send` stands in for it. Odoo-side checks (record rules, idempotent
write, token scope) belong to od-demo-addons.
"""
import json
import uuid
from functools import partial
from typing import TypedDict

import httpx
import pytest
from langgraph.graph import END, START, StateGraph
from redis.asyncio import Redis
from sqlalchemy import text

from app.agents import checkpointer, record
from app.db.onto import DecisionLog, JobRun, LlmUsage
from app.odoo.client import OdooError

ODOO = "https://odoo.example.test"
TOKEN = "aaa.bbb"
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


# ---------------------------------------------------------------- record outbox

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


