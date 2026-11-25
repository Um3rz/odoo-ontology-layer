"""Direct-browser authentication for Onto routes (plan 0.6, contract 3.6).

Bearer = an Odoo-minted query token (opaque to us); X-Odoo-Url = the tenant.
Flow: tenant from the registry -> rate limits -> shape check -> Odoo `whoami`
(cached for the token's life, keyed by a token hash) -> database.uuid must match
the registry -> `ai_enabled`. There is NO service token and no cookie auth.
Odoo, not querygen, decides who the user is and what they may see.
"""
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session, select
from starlette.middleware.cors import CORSMiddleware

from app import db
from app.db.onto import Tenant
from app.db.tenant import normalize_origin, onto_key, tenant_by_url
from app.odoo.client import OdooClient, OdooError
from app.odoo.tokens import has_token_shape, token_hash

log = logging.getLogger(__name__)

# Paths served by Onto routers. CORS for these is the registered-origin list;
# every other path keeps the existing CORS behaviour untouched.
ONTO_PREFIXES = ("/onto", "/agents", "/systems", "/ontology")
ALLOWED_HEADERS = "Authorization, X-Odoo-Url, Last-Event-ID, Content-Type"
ALLOWED_METHODS = "GET, POST, OPTIONS"

WHOAMI_TTL_S = 900  # a query token lives 15 min (A2.3); see the note on the cache
TOKEN_LIMIT = (120, 60.0)  # requests per window (seconds), per token hash
TENANT_LIMIT = (600, 60.0)  # per tenant
ORIGINS_TTL_S = 30


def is_onto_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in ONTO_PREFIXES)


class OntoAuthError(Exception):
    def __init__(self, status: int, error: str, headers: dict | None = None):
        self.status, self.error, self.headers = status, error, headers or {}


async def onto_auth_error_handler(_: Request, exc: OntoAuthError):
    return JSONResponse({"error": exc.error}, status_code=exc.status, headers=exc.headers)


# ---------------------------------------------------------------- CORS

_origins_cache: tuple[float, set[str]] = (-1e9, set())


def registered_origins() -> set[str]:
    """Registered web-app origins: tenant registry plus ONTO_WEB_ORIGINS (comma list)."""
    global _origins_cache
    now = time.monotonic()
    if now - _origins_cache[0] < ORIGINS_TTL_S:
        return _origins_cache[1]
    found = {normalize_origin(o) for o in os.getenv("ONTO_WEB_ORIGINS", "").split(",")}
    with Session(db.engine) as s:
        for origins in s.exec(select(Tenant.web_origins)):
            found.update(normalize_origin(o) for o in origins or [])
    found.discard(None)
    _origins_cache = (now, found)
    return found


class OntoCORSMiddleware:
    """Registered-origin CORS for Onto paths. Credentials off; preflight needs no auth."""

    def __init__(self, app, origins=registered_origins):
        self.app, self.origins = app, origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_onto_path(scope["path"]):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        origin = headers.get("origin")
        if origin is None:
            return await self.app(scope, receive, send)
        allowed = normalize_origin(origin) in await run_in_threadpool(self.origins)
        is_preflight = scope["method"] == "OPTIONS" and "access-control-request-method" in headers

        if is_preflight:
            if not allowed:
                resp = JSONResponse({"error": "origin_not_registered"}, status_code=403,
                                    headers={"Vary": "Origin"})
            else:
                resp = Response(status_code=204, headers={
                    "Vary": "Origin",
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": ALLOWED_METHODS,
                    "Access-Control-Allow-Headers": ALLOWED_HEADERS,
                    "Access-Control-Max-Age": "600",
                })
            return await resp(scope, receive, send)

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers", []))
                hdrs.append((b"vary", b"Origin"))
                if allowed:
                    hdrs.append((b"access-control-allow-origin", origin.encode()))
                message = {**message, "headers": hdrs}
            await send(message)

        await self.app(scope, receive, send_with_cors)


class NonOntoCORSMiddleware(CORSMiddleware):
    """The app's existing CORS, stepping aside on Onto paths so a wildcard
    ALLOWED_ORIGIN can never apply to them."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and is_onto_path(scope["path"]):
            return await self.app(scope, receive, send)
        await super().__call__(scope, receive, send)


# ---------------------------------------------------------------- rate limits

class RateLimiter:
    # ponytail: in-process sliding window; move to Redis (onto:{tenant}: keys) with the agent service in 3.1
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self.hits) > 10_000:  # drop idle keys
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]:
                del self.hits[k]
        return True


token_limiter = RateLimiter(*TOKEN_LIMIT)
tenant_limiter = RateLimiter(*TENANT_LIMIT)

# ---------------------------------------------------------------- whoami cache

# ponytail: in-process; Redis onto:{tenant}:whoami:{hash} once Redis is wired (3.1).
# TTL is the 15-min token maximum, not the token's exact exp: whoami (Odoo 0.4)
# should return the token expiry so this can shrink to the real remaining life.
_whoami_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str) -> dict | None:
    hit = _whoami_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    _whoami_cache.pop(key, None)
    return None


def _cache_put(key: str, value: dict) -> None:
    now = time.monotonic()
    if len(_whoami_cache) > 10_000:
        for k in [k for k, v in _whoami_cache.items() if v[0] <= now]:
            del _whoami_cache[k]
    _whoami_cache[key] = (now + WHOAMI_TTL_S, value)


# ---------------------------------------------------------------- the dependency

@dataclass
class OntoAuth:
    tenant_id: object
    odoo_base_url: str
    whoami: dict  # as returned by Odoo; held in memory only
    ai_enabled: bool


def odoo_client_factory(base_url: str, token: str) -> OdooClient:
    """Seam for tests: swap in a stubbed transport."""
    return OdooClient(base_url, token)


def _db_uuid(who: dict) -> str | None:
    return who.get("database_uuid") or (who.get("database") or {}).get("uuid")


async def browser_auth(request: Request, session: db.SessionType) -> OntoAuth:
    tenant = await run_in_threadpool(tenant_by_url, session, request.headers.get("x-odoo-url"))
    if tenant is None:
        raise OntoAuthError(403, "unknown_tenant")
    base_url, tenant_id, tenant_uuid = tenant.odoo_base_url, tenant.id, tenant.db_uuid

    if not tenant_limiter.allow(str(tenant_id)):
        raise OntoAuthError(429, "rate_limited", {"Retry-After": "60"})
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not has_token_shape(token):  # a raw API key has no dot: refused
        raise OntoAuthError(401, "invalid_token")
    h = token_hash(token)
    if not token_limiter.allow(h):
        raise OntoAuthError(429, "rate_limited", {"Retry-After": "60"})

    key = onto_key(tenant_id, "whoami", h)
    who = _cache_get(key)
    if who is None:
        client = odoo_client_factory(base_url, token)
        try:
            who = await client.whoami()
        except OdooError as e:
            if e.status in (401, 403):
                raise OntoAuthError(401, "invalid_token")
            raise OntoAuthError(502, "odoo_unavailable")
        finally:
            await client.aclose()
        if _db_uuid(who) != tenant_uuid:
            raise OntoAuthError(403, "tenant_mismatch")
        _cache_put(key, who)

    if who.get("ai_enabled") is not True:
        raise OntoAuthError(403, "ai_features_off")
    return OntoAuth(tenant_id, base_url, who, True)
