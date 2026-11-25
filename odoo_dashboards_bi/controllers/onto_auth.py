# -*- coding: utf-8 -*-
"""The Onto route decorator (contract 3.9): Saas's sealed external_route, plus Onto token Bearer auth.

It reuses Saas's sealed transport, key lookup, rate limits and error helpers unchanged. The imports below are
Saas's private helpers on purpose: one auth and encryption system, not a second copy of it.
"""

import functools
import json

import werkzeug.exceptions
from odoo import http
from odoo.http import request

from odoo.addons.odoo_dashboards_saas.controllers.external import (
    PATH_ARG_RE,
    _plain_error,
    _rate_limited,
    _seal,
    _sealed_call,
    _too_many_requests,
)
from odoo.addons.odoo_dashboards_saas.controllers.mobile_helpers import error_response

from ..models import onto_token


def _body_params():
    """Flat JSON object from a POST/PUT/PATCH/DELETE body; {} for other methods or an
    empty body. Malformed/non-object JSON is a 400 -- never silently ignored."""
    if request.httprequest.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return {}
    raw = request.httprequest.get_data(as_text=True)
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise werkzeug.exceptions.BadRequest()
    if not isinstance(data, dict):
        raise werkzeug.exceptions.BadRequest()
    return data


def _token_call(self, fn, kwargs, route_kind):
    """Onto routes with token_kind: Authorization: Bearer <Onto token> (models/onto_token.py).
    Only a token has this shape, so a raw API key is refused as sealed_required. Plain JSON
    both ways, since no sealed key is involved. request.onto_caller tells the route whether
    a query or a bot token called it."""
    auth_header = request.httprequest.headers.get("Authorization") or ""
    value = auth_header[len("Bearer "):].strip() if auth_header.startswith("Bearer ") else ""
    if not onto_token.looks_like_token(value):
        return _plain_error(401, "sealed_required")
    try:
        payload = onto_token.verify_token(request.env, value, route_kind)
    except onto_token.TokenError as exc:
        return _plain_error(exc.status, exc.code)
    bucket, limit, window = onto_token.rate_limit_for(payload)
    if _rate_limited(bucket, limit, window):
        return _too_many_requests(window)
    request.update_env(
        user=payload["uid"],
        context=dict(request.env.context, allowed_company_ids=payload["cids"]),
    )
    request.onto_caller = payload["typ"]
    params = _body_params()
    params.update(kwargs)
    return fn(self, **params)


def onto_external_route(route=None, token_kind=None, **kw):
    """Same shape as Saas's external_route. A request with X-OD-Key takes Saas's sealed path
    unchanged. Any other request must carry an Onto token of a kind that may call this route
    (token_kind); without one it gets a plain 401 sealed_required."""
    path_args = frozenset(PATH_ARG_RE.findall(route or ""))

    def decorator(fn):
        @http.route(route, auth="none", type="http", csrf=False, readonly=False, **kw)
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                response = _run(self, kwargs)
            except werkzeug.exceptions.HTTPException as exc:
                # Every refusal and bad request on these routes answers JSON, not werkzeug's HTML page.
                response = error_response(
                    exc.code, exc.name.lower().replace(" ", "_"), exc.description or exc.name
                )
            return _seal(response)

        def _run(self, kwargs):
            if request.httprequest.headers.get("X-OD-Key"):
                return _sealed_call(self, fn, kwargs, path_args)
            if token_kind:
                return _token_call(self, fn, kwargs, token_kind)
            return _plain_error(401, "sealed_required")

        return wrapper

    return decorator
