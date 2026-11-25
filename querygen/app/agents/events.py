"""Per-run event stream in Redis (plan 3.2; contract 3.6).

A run appends its events with XADD to onto:{tenant_id}:run:{id}:events. The Redis
stream id is the SSE id, so Last-Event-ID is the last id a client saw, and any
process can serve any run. The stream is capped at about 2000 entries and expires
24 h after the last write, like the run's other keys.
"""
import json
import re
from datetime import datetime, timezone

from app.db.tenant import onto_key

TTL_S = 24 * 3600
MAXLEN = 2000
TYPES = frozenset({"step", "tool", "decision", "interrupt", "answer", "error", "done"})
START_ID = "0-0"
_ID = re.compile(r"^\d+-\d+$")


def stream_key(tenant_id, run_id) -> str:
    return onto_key(tenant_id, "run", str(run_id), "events")


def valid_id(value: str) -> bool:
    return _ID.fullmatch(value) is not None


async def emit(redis, tenant_id, run_id, kind: str, data: dict | None = None) -> str:
    """Append one event and return its stream id (the SSE id)."""
    if kind not in TYPES:
        raise ValueError(f"unknown event type {kind}")
    key = stream_key(tenant_id, run_id)
    body = {"type": kind, "ts": datetime.now(timezone.utc).isoformat(), "data": data or {}}
    async with redis.pipeline(transaction=True) as pipe:
        pipe.xadd(key, {"e": json.dumps(body)}, maxlen=MAXLEN, approximate=True)
        pipe.expire(key, TTL_S)
        entry_id, _ = await pipe.execute()
    return entry_id.decode()


async def read(redis, tenant_id, run_id, after: str, block_ms: int) -> list[tuple[str, dict]]:
    """Entries after `after`, waiting up to block_ms for the first one. Empty list on timeout."""
    reply = await redis.xread({stream_key(tenant_id, run_id): after}, count=100, block=block_ms)
    if not reply:
        return []
    return [(entry_id.decode(), json.loads(fields[b"e"])) for entry_id, fields in reply[0][1]]


def frame(entry_id: str, body: dict) -> str:
    """One SSE frame. json.dumps escapes newlines, so the data line is always one line."""
    return f"id: {entry_id}\nevent: {body['type']}\ndata: {json.dumps(body)}\n\n"
