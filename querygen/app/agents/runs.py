"""Run queue, per-tenant cap, heartbeats and execution (plan 3.2; contracts 3.4 and 3.6).

Redis keys, all under onto:{tenant_id}: (contract 3.4):
  queue           run ids waiting to start (list; new runs at the tail, requeued runs at the head)
  running         run ids executing now (set); its size is the per-tenant cap
  run:{id}:meta   hash: kind, job, owner, state, input, created. 24 h; input is dropped when the run ends
  run:{id}:hb     heartbeat, set every 15 s with a 60 s TTL. Missing while listed as running = stale

Tokens never reach Redis. BOOK keeps the bearers this process was last handed, in memory only.
A run starts only in a process that holds a bearer for its owner, so a run that outlived its
process waits for the owner's next request (chat) or the next cron tick (system runs). The
owner is the Odoo user id; system runs have owner None, as in record.py.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from fastapi.concurrency import run_in_threadpool
from langgraph.graph import END, START, StateGraph
from redis.asyncio import Redis
from sqlmodel import Session, select

from app import db
from app.agents import bi, checkpointer, events, record
from app.agents.inventory import supervisor as inventory
from app.db.onto import JobRun, Tenant
from app.db.tenant import onto_key, scoped
from app.odoo.client import OdooClient
from app.systems.jobs import JOBS, JobContext

log = logging.getLogger(__name__)

HEARTBEAT_S = 15
HEARTBEAT_TTL_S = 60
RUN_TTL_S = 24 * 3600
BOOK_TTL_S = 1800  # ponytail: fixed 30 min, the bot token's life. Upgrade: read exp from whoami.


class JobBusy(Exception):
    """A run of this job holds a fresh heartbeat (plan 3.3: the route answers 409)."""


# KEYS: queue, running, meta, heartbeat. ARGV: run id, cap, heartbeat TTL.
# One script checks state, cap and queue entry, so two processes cannot claim the same run.
CLAIM = """
if redis.call('HGET', KEYS[3], 'state') ~= 'queued' then return 0 end
if redis.call('SCARD', KEYS[2]) >= tonumber(ARGV[2]) then return 0 end
if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 0 then return 0 end
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[3], 'state', 'running')
redis.call('SET', KEYS[4], '1', 'EX', ARGV[3])
return 1
"""

TASKS: set[asyncio.Task] = set()
_client: Redis | None = None
_tenant_cache: tuple[float, list[uuid.UUID]] = (-1e9, [])


def redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(os.environ["REDIS_URL"])
    return _client


def cap() -> int:
    try:
        return max(1, int(os.getenv("ONTO_RUNS_PER_TENANT") or 4))
    except ValueError:
        return 4


async def close() -> None:
    """Cancel local runs (their heartbeats lapse and another process resumes them) and drop clients."""
    global _client
    for task in list(TASKS):
        task.cancel()
    TASKS.clear()
    await checkpointer.aclose()
    if _client is not None:
        await _client.aclose()
        _client = None


class TokenBook:
    """Bearers this process was last handed, per (tenant, owner). Memory only, never persisted."""

    def __init__(self) -> None:
        self._held: dict[tuple[str, int | None], tuple[str, str, float]] = {}

    def put(self, tenant_id, owner: int | None, odoo_url: str, bearer: str) -> None:
        self._held[(str(tenant_id), owner)] = (odoo_url, bearer, time.monotonic() + BOOK_TTL_S)

    def get(self, tenant_id, owner: int | None) -> tuple[str, str] | None:
        key = (str(tenant_id), owner)
        hit = self._held.get(key)
        if hit is None or hit[2] < time.monotonic():
            self._held.pop(key, None)
            return None
        return hit[0], hit[1]

    def bearers(self, tenant_id) -> dict[int | None, str]:
        t, now = str(tenant_id), time.monotonic()
        return {owner: v[1] for (tid, owner), v in self._held.items() if tid == t and v[2] >= now}

    def url(self, tenant_id) -> str | None:
        t, now = str(tenant_id), time.monotonic()
        for (tid, _owner), v in self._held.items():
            if tid == t and v[2] >= now:
                return v[0]
        return None


BOOK = TokenBook()


def _meta(tenant_id, run_id) -> str:
    return onto_key(tenant_id, "run", str(run_id), "meta")


def _hb(tenant_id, run_id) -> str:
    return onto_key(tenant_id, "run", str(run_id), "hb")


def _queue(tenant_id) -> str:
    return onto_key(tenant_id, "queue")


def _running(tenant_id) -> str:
    return onto_key(tenant_id, "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_owner(owner: int | None) -> str:
    return "" if owner is None else str(owner)


def decode_owner(raw: bytes | str | None) -> int | None:
    return None if raw in (None, b"", "") else int(raw)


async def known_tenants() -> list[uuid.UUID]:
    """Tenant ids from the registry, refreshed every 30 s. Used instead of SCAN over the keyspace."""
    global _tenant_cache
    if time.monotonic() - _tenant_cache[0] > 30:
        ids = await run_in_threadpool(_load_tenant_ids)
        _tenant_cache = (time.monotonic(), ids)
    return _tenant_cache[1]


def _load_tenant_ids() -> list[uuid.UUID]:
    with Session(db.engine) as s:
        return list(s.exec(select(Tenant.id)))


async def _enqueue(tenant_id, run_id: str, *, kind: str, owner: int | None, job: str, payload: dict) -> None:
    r = redis()
    meta = _meta(tenant_id, run_id)
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(meta, mapping={"kind": kind, "job": job, "owner": encode_owner(owner), "state": "queued",
                                 "input": json.dumps(payload), "created": _now()})
        pipe.expire(meta, RUN_TTL_S)
        pipe.rpush(_queue(tenant_id), run_id)
        await pipe.execute()


async def start_chat(tenant_id, owner: int | None, text: str, agent: str = "echo") -> str:
    """A chat run of one agent kind: "bi" (plan 3.5) or "echo", the stand-in the 3.2 tests use."""
    run_id = uuid.uuid4().hex
    await _enqueue(tenant_id, run_id, kind="chat", owner=owner, job="", payload={"text": text, "agent": agent})
    return run_id


async def start_inventory(tenant_id, owner: int | None, text: str) -> str:
    run_id = uuid.uuid4().hex
    await _enqueue(tenant_id, run_id, kind="inventory", owner=owner, job="", payload={"text": text})
    return run_id


async def view(tenant_id, run_id: str) -> dict | None:
    meta = await redis().hgetall(_meta(tenant_id, run_id))
    if not meta:
        return None
    return {"kind": meta[b"kind"].decode(), "state": meta[b"state"].decode(), "owner": decode_owner(meta[b"owner"])}


async def list_runs(tenant_id, owner: int | None, limit: int = 50) -> list[dict]:
    r = redis()
    out: list[dict] = []
    async for key in r.scan_iter(match=onto_key(tenant_id, "run", "*", "meta"), count=200):
        meta = await r.hgetall(key)
        if meta and decode_owner(meta[b"owner"]) == owner:
            out.append({"run_id": key.decode().split(":")[-2], "kind": meta[b"kind"].decode(),
                        "state": meta[b"state"].decode(), "created": meta[b"created"].decode()})
            if len(out) >= limit:
                break
    return out


async def claim_ready(tenants) -> int:
    """Start each queued run of these tenants whose owner's bearer this process holds. Returns how many started."""
    r = redis()
    started = 0
    for tenant_id in tenants:
        queue = _queue(tenant_id)
        for raw in await r.lrange(queue, 0, -1):
            run_id = raw.decode()
            meta = await r.hgetall(_meta(tenant_id, run_id))
            if meta.get(b"state") != b"queued":
                await r.lrem(queue, 1, run_id)  # ended, expired, or claimed elsewhere
                continue
            held = BOOK.get(tenant_id, decode_owner(meta[b"owner"]))
            if held is None:
                continue  # waits for the owner's next request or cron tick
            claimed = await r.register_script(CLAIM)(
                keys=[queue, _running(tenant_id), _meta(tenant_id, run_id), _hb(tenant_id, run_id)],
                args=[run_id, cap(), HEARTBEAT_TTL_S],
            )
            if not claimed:
                continue
            _spawn(_execute(tenant_id, run_id, meta, held))
            started += 1
    return started


async def requeue(tenant_id, run_id: str) -> bool:
    """Move a running run back to the head of the queue. False if another process got there first."""
    r = redis()
    if not await r.srem(_running(tenant_id), run_id):
        return False
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(_meta(tenant_id, run_id), "state", "queued")
        pipe.delete(_hb(tenant_id, run_id))
        pipe.lpush(_queue(tenant_id), run_id)
        await pipe.execute()
    if await r.hget(_meta(tenant_id, run_id), "kind") == b"job":
        await _set_job_row(tenant_id, run_id, state="queued")
    return True


async def resume(tenant_id, run_id: str) -> None:
    """Put back a run whose heartbeat lapsed (a client's resume). A live run is left alone."""
    r = redis()
    if await r.sismember(_running(tenant_id), run_id) and not await r.exists(_hb(tenant_id, run_id)):
        await requeue(tenant_id, run_id)


async def alive(tenant_id, run_id: str) -> bool:
    return bool(await redis().exists(_meta(tenant_id, run_id)))


async def requeue_stale(tenants) -> int:
    """Requeue every run listed as running whose heartbeat has lapsed (its process died). Returns the count."""
    r = redis()
    n = 0
    for tenant_id in tenants:
        for raw in await r.smembers(_running(tenant_id)):
            run_id = raw.decode()
            if not await r.exists(_hb(tenant_id, run_id)) and await requeue(tenant_id, run_id):
                n += 1
    return n


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


class _Echo(TypedDict, total=False):
    question: str
    answer: str


def _echo_graph(tenant_id, run_id: str, saver, *, interrupt_before: tuple[str, ...] = ()):
    """Stand-in graph until the BI Agent (3.5) registers real ones. Checkpointed after every node.

    interrupt_before exists for tests: it stops a run mid-way, as a crash would.
    """
    async def think(_state: _Echo) -> dict:
        await events.emit(redis(), tenant_id, run_id, "step", {"text": "Reading the question"})
        return {}

    async def answer(state: _Echo) -> dict:
        # An event emitted just before a crash can repeat once after resume (at least once).
        text = f"Echo: {state.get('question', '')}"
        await events.emit(redis(), tenant_id, run_id, "answer", {"text": text})
        return {"answer": text}

    g = StateGraph(_Echo)
    g.add_node("think", think)
    g.add_node("answer", answer)
    g.add_edge(START, "think")
    g.add_edge("think", "answer")
    g.add_edge("answer", END)
    return g.compile(checkpointer=saver, interrupt_before=list(interrupt_before))


async def _run_chat(tenant_id, run_id: str, payload: dict, held: tuple | None = None) -> dict:
    saver = await checkpointer.checkpointer(tenant_id)
    config = {"configurable": {"thread_id": checkpointer.thread_id(tenant_id, run_id)}}
    resumed = await saver.aget_tuple(config) is not None  # a checkpoint means a crash: continue it
    if payload.get("agent") == "bi":
        async def emit(kind: str, data: dict) -> None:
            await events.emit(redis(), tenant_id, run_id, kind, data)

        return await bi.run(tenant_id=tenant_id, text=payload.get("text", ""), saver=saver, config=config,
                            resumed=resumed, held=held, emit=emit)
    final = await _echo_graph(tenant_id, run_id, saver).ainvoke(
        None if resumed else {"question": payload.get("text", "")}, config)
    return {"answer": final.get("answer", "")}


def _write_job_row(tenant_id, job_run_id: str, fields: dict) -> None:
    with Session(db.engine) as s:
        row = s.exec(scoped(JobRun, tenant_id).where(JobRun.id == int(job_run_id))).first()
        if row is not None:
            for key, value in fields.items():
                setattr(row, key, value)
            s.add(row)
            s.commit()


async def _set_job_row(tenant_id, job_run_id: str, **fields) -> None:
    await run_in_threadpool(_write_job_row, tenant_id, job_run_id, fields)


async def _beat(tenant_id, run_id: str, kind: str, task: asyncio.Task) -> None:
    r = redis()
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        if not await r.sismember(_running(tenant_id), run_id):
            task.cancel()  # requeued while we stalled: this copy must stop
            return
        await r.set(_hb(tenant_id, run_id), _now(), ex=HEARTBEAT_TTL_S)
        if kind == "job":
            try:
                await _set_job_row(tenant_id, run_id, heartbeat_at=datetime.now(timezone.utc))
            except Exception as exc:  # noqa: BLE001 - a missed row write must not stop the run
                log.warning("job heartbeat not written id=%s error=%s", run_id, type(exc).__name__)


async def _run_job(tenant_id, run_id: str, job: str, params: dict, held: tuple[str, str]) -> dict:
    url, bearer = held
    client = OdooClient(url, bearer)
    try:
        return await JOBS[job](JobContext(tenant_id=tenant_id, job_run_id=int(run_id), params=params, odoo=client))
    finally:
        await client.aclose()


async def _run_inventory(tenant_id, run_id: str, payload: dict, held: tuple[str, str], owner: int | None) -> dict:
    """An Inventory Agent run (plan 3.6). Reads use the owner's token. The one write uses the bot's token, if this process holds one."""
    url, bearer = held
    odoo = OdooClient(url, bearer)
    bot_held = BOOK.get(tenant_id, None)
    bot = OdooClient(*bot_held) if bot_held else None

    async def emit(kind: str, data: dict) -> None:
        await events.emit(redis(), tenant_id, run_id, kind, data)

    ctx = inventory.Ctx(run_id=run_id, odoo=odoo, bot=bot, model=inventory.MODEL, emit=emit, owner_uid=owner)
    try:
        saver = await checkpointer.checkpointer(tenant_id)
        return await inventory.run(ctx, payload.get("text", ""), saver=saver,
                                   thread_id=checkpointer.thread_id(tenant_id, run_id))
    finally:
        await odoo.aclose()
        if bot is not None:
            await bot.aclose()


async def _execute(tenant_id, run_id: str, meta: dict, held: tuple[str, str]) -> None:
    kind = meta[b"kind"].decode()
    payload = json.loads(meta[b"input"])
    owner = decode_owner(meta[b"owner"])
    beat = asyncio.create_task(_beat(tenant_id, run_id, kind, asyncio.current_task()))
    try:
        if kind == "job":
            await _set_job_row(tenant_id, run_id, state="running", heartbeat_at=datetime.now(timezone.utc))
            outcome = await _run_job(tenant_id, run_id, meta[b"job"].decode(), payload, held)
        elif kind == "inventory":
            outcome = await _run_inventory(tenant_id, run_id, payload, held, owner)
        else:
            outcome = await _run_chat(tenant_id, run_id, payload, held)
        state = "done"
    except asyncio.CancelledError:
        raise  # heartbeat lapses; the next claim in any process resumes from the checkpoint
    except Exception as exc:  # noqa: BLE001 - the run fails, the service keeps going
        log.error("run failed id=%s kind=%s error=%s", run_id, kind, type(exc).__name__)
        outcome, state = {}, "failed"
    finally:
        beat.cancel()
    await _finish(tenant_id, run_id, kind, state, outcome, payload, owner)


async def _finish(tenant_id, run_id: str, kind: str, state: str, outcome: dict, payload: dict,
                  owner: int | None) -> None:
    r = redis()
    await events.emit(r, tenant_id, run_id, "done", {"state": state})
    meta = _meta(tenant_id, run_id)
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(meta, "state", state)
        pipe.hdel(meta, "input")  # the question text leaves Redis with the run (the outbox copy is separate)
        pipe.expire(meta, RUN_TTL_S)
        pipe.srem(_running(tenant_id), run_id)
        pipe.delete(_hb(tenant_id, run_id))
        await pipe.execute()
    if kind == "job":
        await _set_job_row(tenant_id, run_id, state=state, heartbeat_at=datetime.now(timezone.utc),
                           result=outcome if state == "done" else {"error": "run_failed"})
    await record.enqueue(r, tenant_id, _record(run_id, kind, state, owner, outcome, payload), owner)
    log.info("run ended id=%s kind=%s state=%s", run_id, kind, state)


def _inventory_record(run_id: str, state: str, owner: int | None, outcome: dict) -> dict:
    """The contract 3.10 body for an Inventory Agent run: kind playbook, the trace, and the model usage."""
    body = dict(outcome)
    trace = body.pop("trace", [])
    usage = body.pop("usage", {})
    return {
        "run": {"id": run_id, "kind": "playbook", "playbook": body.get("playbook"),
                "trigger": "user" if owner is not None else "system", "state": state,
                "outcome": body, "trace": trace, "recommendation_ids": []},
        # credits stay 0.0 until pricing is decided (D33, D39); the USD cost is in outcome.usage.cost
        "usage": {"model": usage.get("model") or "unknown", "input_tokens": usage.get("input_tokens", 0),
                  "output_tokens": usage.get("output_tokens", 0), "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "credits": 0.0},
        "idempotency_key": f"{run_id}:0",
    }


FAILED_TEXT = "The answer could not be produced. Try again."


def _record(run_id: str, kind: str, state: str, owner: int | None, outcome: dict, payload: dict) -> dict:
    """The contract 3.10 body. A chat turn carries its answer card, model usage and a trace of tool names, timings and
    short summaries; tool results never leave querygen."""
    if kind == "inventory":
        return _inventory_record(run_id, state, owner, outcome)
    text = payload.get("text", "")
    usage = outcome.get("usage") or {}
    rec = {
        "run": {"id": run_id, "kind": kind, "playbook": None,
                "trigger": "user" if owner is not None else "system", "state": state,
                "outcome": {} if kind == "chat" else outcome, "trace": outcome.get("trace") or [],
                "recommendation_ids": []},
        "usage": {"model": outcome.get("model") or ("echo" if payload.get("agent") != "bi" else "none"),
                  "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "credits": 0.0},
        "idempotency_key": f"{run_id}:0",
    }
    if kind == "chat":
        answer = outcome.get("answer") or ("" if state == "done" else FAILED_TEXT)
        assistant = {"role": "assistant", "text": answer}
        if outcome.get("card") is not None:
            assistant["card"] = outcome["card"]
        rec["chat"] = {"thread_id": None, "title": text[:60],
                       "messages": [{"role": "user", "text": text}, assistant]}
    return rec


def _open_job_run(tenant_id, job: str) -> tuple[int, list[int]]:
    """Insert the queued job_run row and return (its id, the ids it superseded).

    Refuses (JobBusy) while a run of this job has a fresh heartbeat. Queued runs and runs whose
    heartbeat lapsed are superseded: the new request from the cron replaces them.
    ponytail: two requests racing here can both queue; cron ticks are minutes apart, so accepted.
    """
    with Session(db.engine) as s:
        fresh = datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_TTL_S)
        busy = s.exec(scoped(JobRun, tenant_id).where(
            JobRun.job == job, JobRun.state == "running", JobRun.heartbeat_at >= fresh)).first()
        if busy is not None:
            raise JobBusy(job)
        old = list(s.exec(scoped(JobRun, tenant_id).where(
            JobRun.job == job, JobRun.state.in_(["queued", "running"]))))
        for row in old:
            row.state = "cancelled"
            s.add(row)
        row = JobRun(tenant_id=tenant_id, job=job, state="queued")
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id, [o.id for o in old]


async def enqueue_job(tenant_id, job_name: str, params: dict) -> int:
    """Queue one run of a registered job and return its job_run id. Raises JobBusy while one is live."""
    if job_name not in JOBS:
        raise KeyError(job_name)
    job_run_id, superseded = await run_in_threadpool(_open_job_run, tenant_id, job_name)
    r = redis()
    for old_id in superseded:
        meta = _meta(tenant_id, old_id)
        if await r.exists(meta):
            await r.hset(meta, "state", "cancelled")
            await r.srem(_running(tenant_id), str(old_id))  # a live copy stops at its next heartbeat
            await r.delete(_hb(tenant_id, old_id))
    await _enqueue(tenant_id, str(job_run_id), kind="job", owner=None, job=job_name, payload=params)
    return job_run_id
