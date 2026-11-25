# -*- coding: utf-8 -*-
"""Onto tokens (contract 3.1): short-lived, HMAC-signed, minted and verified by Odoo only.

A query token (15 min) is minted by the browser through a sealed call and carries
the id of the key that minted it, so revoking or rotating that key kills it at once.
A bot token (30 min) is minted by an Odoo cron for the AI bot user and carries no key.
Querygen treats tokens as opaque.
"""

import calendar
import hashlib
import hmac
import json
import re
import secrets
import uuid

from odoo.addons.odoo_dashboards_saas.models.e2e import b64d, b64e

from .onto_store import onto_now

SECRET_PARAM = "onto.token_secret"
DB_UUID_PARAM = "database.uuid"
BOT_XMLID = "odoo_dashboards_bi.user_onto_bot"
TOKEN_VERSION = 1
TTL = {"query": 15 * 60, "bot": 30 * 60}
# (requests, window seconds) per token user. Bot traffic gets the higher bucket (D31).
RATE = {"query": (300, 60), "bot": (1200, 60)}
# Route kinds each token type may call. People (sealed) call "read" and "write" routes.
# "record" (3.10) is the one write a query token may make: its own user's chat turns.
ALLOWED_ROUTE_KINDS = {"query": ("read", "record"), "bot": ("read", "write", "bot", "record")}
SHAPE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class TokenError(Exception):
    """A refused token. status and code are what the route answers (plain JSON)."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def looks_like_token(value):
    """Two base64url parts and a dot. A raw API key (hex, no dot) never matches."""
    return bool(value) and SHAPE.fullmatch(value) is not None


def ensure_token_secret(env):
    """Generate onto.token_secret once. Rotating it (deleting the parameter) kills every token."""
    params = env["ir.config_parameter"].sudo()
    if not params.get_param(SECRET_PARAM):
        params.set_param(SECRET_PARAM, secrets.token_urlsafe(48))


def _secret(env):
    ensure_token_secret(env)
    return env["ir.config_parameter"].sudo().get_param(SECRET_PARAM).encode("utf-8")


def _db_uuid(env):
    return env["ir.config_parameter"].sudo().get_param(DB_UUID_PARAM)


def _now_unix():
    return calendar.timegm(onto_now().utctimetuple())


def _canonical(payload):
    """The signed bytes: JSON with sorted keys and no spaces. Querygen never builds these."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(secret, body):
    return hmac.new(secret, body, hashlib.sha256).digest()


def mint_token(env, user, typ, cids, kid):
    """A signed token for `user`. A query token needs the minting key's id; a bot token takes none."""
    if typ not in TTL:
        raise ValueError("unknown token type")
    if (typ == "query") != bool(kid):
        raise ValueError("query tokens carry a key id, bot tokens do not")
    now = _now_unix()
    payload = {
        "v": TOKEN_VERSION,
        "typ": typ,
        "db": _db_uuid(env),
        "uid": user.id,
        "cids": sorted(cids),
        "kid": kid or None,
        "iat": now,
        "exp": now + TTL[typ],
        "jti": str(uuid.uuid4()),
    }
    body = _canonical(payload)
    return "%s.%s" % (b64e(body), b64e(_sign(_secret(env), body)))


def verify_token(env, token, route_kind):
    """The payload of a token that may call a route of this kind. Raises TokenError otherwise.

    Checks, in order: shape and version, signature (constant time), database, expiry,
    the user is active and may use the companies in the token, the minting key still
    exists (query tokens), the bot is the bot (bot tokens), and the route kind."""
    try:
        part, sig_part = token.split(".")
        body = b64d(part)
        payload = json.loads(body.decode("utf-8"))
        given = b64d(sig_part)
    except ValueError:
        raise TokenError(401, "bad_token")
    if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION or payload.get("typ") not in TTL:
        raise TokenError(401, "bad_token")
    if not hmac.compare_digest(_sign(_secret(env), body), given):
        raise TokenError(401, "bad_token")
    if payload.get("db") != _db_uuid(env):
        raise TokenError(401, "bad_token")
    exp = payload.get("exp")
    if type(exp) is not int or _now_unix() >= exp:
        raise TokenError(401, "expired")

    uid, cids = payload.get("uid"), payload.get("cids")
    if type(uid) is not int or not isinstance(cids, list) or not cids:
        raise TokenError(401, "bad_token")
    user = env["res.users"].sudo().browse(uid).exists()
    if not user or not user.active or not set(cids) <= set(user.company_ids.ids):
        raise TokenError(401, "bad_token")

    typ, kid = payload["typ"], payload.get("kid")
    if typ == "query":
        key = env["dashboard.external.key.saas"].sudo().search([("e2e_key_id", "=", kid)], limit=1).apikey_id if kid else None
        if not key or key.user_id.id != uid or key.scope != "mobile":
            # Revoked or rotated: the minting key's row is gone.
            raise TokenError(401, "bad_token")
    elif kid is not None or uid != env.ref(BOT_XMLID).id:
        raise TokenError(401, "bad_token")

    if route_kind not in ALLOWED_ROUTE_KINDS[typ]:
        raise TokenError(403, "token_kind")
    return payload


def rate_limit_for(payload):
    """(bucket, limit, window) for a verified token. Each token user gets its own bucket per type."""
    limit, window = RATE[payload["typ"]]
    return "onto-%s:%s" % (payload["typ"], payload["uid"]), limit, window


def mint_bot_token(env):
    """A bot token for the AI bot user, minted inside Odoo with no HTTP call (the cron path, 3.5)."""
    bot = env.ref(BOT_XMLID)
    return mint_token(env, bot, "bot", bot.company_ids.ids, None)
