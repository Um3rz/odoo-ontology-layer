"""LangGraph checkpointer for agent runs (plan 3.1, contract 3.4).

ONTO_CHECKPOINTER=redis (default): AsyncRedisSaver, one saver per tenant with the
prefixes onto:{tenant_id}:checkpoint and onto:{tenant_id}:checkpoint_write. Every
checkpoint write sets a 24 h TTL; reads do not extend it.

ONTO_CHECKPOINTER=postgres (fallback, plan L8): AsyncPostgresSaver on querygen's
Postgres. LangGraph owns that table and it has no tenant column, so the thread id
carries the tenant. The table holds graph state, question text included, which
CLAUDE.md forbids in Postgres. Use it only if the Redis spike fails and the owner
accepts that. purge_finished_threads() drops threads idle for 24 h; the agent
service must call it on a schedule.

UNVERIFIED: the spike (plan 3.1 step 1: Redis 8 search/JSON modules, per-tenant
prefixes, TTL) has not been run. It runs at the 3.1 test pass.
"""
import asyncio
import os
import re

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from app.db.tenant import onto_key

RUN_TTL_MINUTES = 24 * 60
PURGE_SQL = """
    SELECT thread_id FROM checkpoints
    GROUP BY thread_id
    HAVING max((checkpoint->>'ts')::timestamptz) < now() - interval '24 hours'
"""


class TenantRedisSaver(AsyncRedisSaver):
    async def asetup(self) -> None:
        await super().asetup()
        # ponytail: the library's write-key registry names its keys write_keys_zset:... with no tenant
        # prefix, so it is off and reads use the search index. Ceiling: slower reads on long threads.
        # Upgrade path: a registry keyed under onto:{tenant_id}:.
        self._key_registry = None


_setup_lock = asyncio.Lock()  # only taken on setup, so it never binds to a loop unless contended
_redis_client: Redis | None = None
_redis_savers: dict[str, AsyncRedisSaver] = {}
_pg_saver: AsyncPostgresSaver | None = None


def backend() -> str:
    value = os.getenv("ONTO_CHECKPOINTER", "redis").strip().lower()
    if value not in ("redis", "postgres"):
        raise RuntimeError("ONTO_CHECKPOINTER must be 'redis' or 'postgres'")
    return value


def thread_id(tenant_id, run_id) -> str:
    """The LangGraph thread id of a run. Tenant-prefixed so Postgres rows carry the tenant too."""
    return onto_key(tenant_id, str(run_id))


def _client() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(os.environ["REDIS_URL"])
    return _redis_client


async def _postgres_saver() -> AsyncPostgresSaver:
    global _pg_saver
    if _pg_saver is None:
        conninfo = re.sub(r"^postgresql\+\w+://", "postgresql://", os.environ["DATABASE_URL"])
        pool = AsyncConnectionPool(
            conninfo, max_size=5, open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        _pg_saver = saver
    return _pg_saver


async def checkpointer(tenant_id) -> BaseCheckpointSaver:
    """The saver for one tenant, set up once and cached until aclose()."""
    async with _setup_lock:
        if backend() == "postgres":
            return await _postgres_saver()
        key = str(tenant_id)
        if key not in _redis_savers:
            saver = TenantRedisSaver(
                redis_client=_client(),
                ttl={"default_ttl": RUN_TTL_MINUTES, "refresh_on_read": False},
                checkpoint_prefix=onto_key(tenant_id, "checkpoint"),
                checkpoint_write_prefix=onto_key(tenant_id, "checkpoint_write"),
            )
            await saver.asetup()
            _redis_savers[key] = saver
        return _redis_savers[key]


async def purge_finished_threads() -> int:
    """Postgres fallback only: delete threads whose last checkpoint is over 24 h old. Returns the count."""
    if backend() != "postgres":
        return 0
    async with _setup_lock:
        saver = await _postgres_saver()
    async with saver.conn.connection() as conn:
        rows = await (await conn.execute(PURGE_SQL)).fetchall()
    for row in rows:
        await saver.adelete_thread(row["thread_id"])
    return len(rows)


async def aclose() -> None:
    """Drop cached savers and connections. The service calls this on shutdown; tests use it between loops."""
    global _redis_client, _pg_saver
    _redis_savers.clear()
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
    if _pg_saver is not None:
        await _pg_saver.conn.close()
        _pg_saver = None
