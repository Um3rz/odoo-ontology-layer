"""Outbox for agent records to Odoo (plan 3.1, contract 3.10).

A finished chat turn or run is held in Redis, then posted to Odoo's
POST /onto/v1/agent/record. An item leaves the outbox only when Odoo answers 2xx,
so a record waits out an Odoo outage (7 days at most, contract 3.4). Odoo dedupes
on idempotency_key, so a retry after a lost reply changes nothing.

The held copy contains chat text, so it lives in Redis only, under
onto:{tenant_id}:outbox. No bearer token is stored: flush() is given the bearers it
may use, keyed by the Odoo user who owns the record (chat turns) or None (system
runs, which use the bot token).
"""
import json
import logging
import time

from app.db.tenant import onto_key
from app.odoo.client import OdooClient, OdooError

log = logging.getLogger(__name__)

OUTBOX_TTL_S = 7 * 24 * 3600
RECORD_PATH = "/onto/v1/agent/record"


async def enqueue(redis, tenant_id, record: dict, owner_uid: int | None) -> None:
    """Hold a record until Odoo accepts it. The same idempotency_key replaces the held copy."""
    ikey = record["idempotency_key"]
    index = onto_key(tenant_id, "outbox")
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(onto_key(tenant_id, "outbox", "item", ikey),
                 json.dumps({"owner_uid": owner_uid, "record": record}), ex=OUTBOX_TTL_S)
        pipe.zadd(index, {ikey: time.time()})
        pipe.expire(index, OUTBOX_TTL_S)
        await pipe.execute()


async def odoo_send(odoo_url: str, bearer: str, record: dict, *, transport=None) -> None:
    """POST one record. Raises OdooError unless Odoo answers 2xx with a JSON object."""
    async with OdooClient(odoo_url, bearer, transport=transport) as client:
        await client.request("POST", RECORD_PATH, json=record)


async def flush(redis, tenant_id, *, odoo_url: str, bearers: dict, send=odoo_send) -> int:
    """Post the held records whose owner has a bearer now. Returns how many Odoo accepted.

    Stops at the first failure that means Odoo is unreachable or failing, so the rest
    wait for the next flush. Any other refusal skips that one record; it is retried later.
    """
    index = onto_key(tenant_id, "outbox")
    delivered = 0
    for member in await redis.zrange(index, 0, -1):
        ikey = member.decode()
        item_key = onto_key(tenant_id, "outbox", "item", ikey)
        raw = await redis.get(item_key)
        if raw is None:  # expired: forget it
            await redis.zrem(index, ikey)
            continue
        held = json.loads(raw)
        if held["owner_uid"] not in bearers:
            continue
        try:
            await send(odoo_url, bearers[held["owner_uid"]], held["record"])
        except OdooError as e:
            log.warning("agent record not accepted ikey=%s status=%s", ikey, e.status)
            if e.status == 0 or e.status >= 500:
                break
            continue
        await redis.delete(item_key)
        await redis.zrem(index, ikey)
        delivered += 1
    return delivered


async def submit(redis, tenant_id, record: dict, owner_uid: int | None, *,
                 odoo_url: str, bearer: str, send=odoo_send) -> int:
    """A turn or run just ended: hold the record, then try to deliver it and older held records for this owner."""
    await enqueue(redis, tenant_id, record, owner_uid)
    return await flush(redis, tenant_id, odoo_url=odoo_url, bearers={owner_uid: bearer}, send=send)
