# -*- coding: utf-8 -*-
"""Onto routes (0.4): the token endpoint and whoami. Store routes follow in 0.5.

Every route is sealed (Saas mobile key) or takes an Onto token; there are no guest grants.
"""

import werkzeug.exceptions
from odoo import release
from odoo.exceptions import AccessError
from odoo.http import Controller, request
from odoo.modules.module import get_manifest

from ..models import (
    onto_introspect,
    onto_health,
    onto_org_map,
    onto_parity,
    onto_provision,
    onto_query,
    onto_rules,
    onto_scope,
    onto_store,
    onto_token,
)
from odoo.addons.odoo_dashboards_saas.controllers.external import external_route

from .onto_auth import onto_external_route
# --- 3.5 explore imports (begin) ---
from ..models import onto_explore
# --- 3.5 explore imports (end) ---
# --- p3-records (3.1): agent records begin ---
from ..models.onto_agent_run import RecordError
# --- p3-records (3.1): agent records end ---

STEWARD_GROUP = "odoo_dashboards_bi.group_onto_steward"
STOCK_MANAGER_GROUP = "stock.group_stock_manager"
MAX_ORGMAP_ENTRIES = 500
ONTO_GROUPS = (
    "odoo_dashboards_bi.group_onto_branch_manager",
    "odoo_dashboards_bi.group_onto_regional_manager",
    "odoo_dashboards_bi.group_onto_steward",
    "odoo_dashboards_bi.group_onto_inventory_admin",
    "odoo_dashboards_bi.group_onto_bot",
    # Data Health (2.6): stock managers see the Health page, so the web app needs to know they hold this group.
    STOCK_MANAGER_GROUP,
)


def onto_route(route, kind="read", **kw):
    """A sealed external_route that also takes an Onto token as Bearer (contract 3.9).

    kind: "read" (people, query tokens, bot tokens), "write" (people and bot tokens; query
    tokens are refused), "bot" (bot tokens only; a person is refused with 403), "record" (query
    tokens for their own chat turns, bot tokens; a person is refused with 403)."""

    def decorator(fn):
        @onto_external_route(route, token_kind=kind, **kw)
        def wrapper(self, **params):
            caller = getattr(request, "onto_caller", "sealed")
            if caller == "sealed" and kind in ("bot", "record"):
                raise werkzeug.exceptions.Forbidden()
            return fn(self, **params)

        return wrapper

    return decorator


def _is_org_role(value):
    """An OrgMap role, or nothing: an entry's role is optional (2.9e)."""
    return value is None or (isinstance(value, str) and value in onto_org_map.ROLE_VALUES)


def _store_json(call):
    """The store result as JSON, or the refusal with its status (sealed when the call was sealed)."""
    try:
        return request.make_json_response(call())
    except onto_store.StoreError as exc:
        body = {"error": exc.code}
        if exc.message:
            body["message"] = exc.message
        return request.make_json_response(body, status=exc.status)


class OntoController(Controller):
    @external_route("/onto/v1/token", methods=["POST"])
    def token(self, cids=None, **kw):
        """A query token for the caller, minted from their sealed mobile key. Sealed-only:
        a Bearer token is refused, so no token is minted from a token (3.1)."""
        user = request.env.user
        allowed = user.company_ids.ids
        if cids is None:
            cids = [user.company_id.id]
        elif not isinstance(cids, list) or not cids or not all(type(c) is int and c in allowed for c in cids):
            raise werkzeug.exceptions.Forbidden()
        kid = request.httprequest.headers.get("X-OD-Key", "").strip()
        token = onto_token.mint_token(request.env, user, "query", cids, kid)
        return request.make_json_response({"token": token, "expires_in": onto_token.TTL["query"]})

    @onto_route("/onto/v1/whoami", kind="read")
    def whoami(self, **kw):
        user = request.env.user
        company = request.env.company
        # ai_enabled is one company flag every Onto caller may see, so it is read with an
        # explicit company filter and no record-rule dependence.
        setting = request.env["onto.agent.setting"].sudo().search(
            [("company_id", "=", company.id)], limit=1
        )
        groups = [xmlid for xmlid in ONTO_GROUPS if user.has_group(xmlid)]
        legacy_groups = [xmlid.replace("odoo_dashboards_bi.", "odoo_dashboards_agents.", 1)
                         for xmlid in groups if xmlid.startswith("odoo_dashboards_bi.")]
        return request.make_json_response(
            {
                "user": {"id": user.id, "name": user.name},
                "groups": groups + legacy_groups,
                "companies": [{"id": c.id, "name": c.name} for c in user.company_ids],
                "active_company_id": company.id,
                "odoo_version": release.version,
                "database": {"uuid": request.env["ir.config_parameter"].sudo().get_param("database.uuid")},
                "module_version": get_manifest("odoo_dashboards_bi")["version"],
                "ai_enabled": bool(setting.ai_enabled),
            }
        )

    @onto_route("/onto/v1/agent_setting", kind="write", methods=["POST"])
    def agent_setting(self, ai_enabled=None, agents_paused=None, **kw):
        """Stewards only (D29, 3.6): the company's AI switches. An empty body reads them; a body with true or false
        sets them, and each switch that changes writes one onto.audit row. Query tokens get 403, anyone else 404."""
        if not request.env.user.has_group(STEWARD_GROUP):
            raise werkzeug.exceptions.NotFound()
        if kw:
            return request.make_json_response({"error": "unknown_field"}, status=422)
        changes = {
            name: value
            for name, value in (("ai_enabled", ai_enabled), ("agents_paused", agents_paused))
            if value is not None
        }
        if not all(isinstance(value, bool) for value in changes.values()):
            return request.make_json_response({"error": "bad_value"}, status=422)
        return request.make_json_response(request.env["onto.agent.setting"].onto_switches(changes))

    @onto_route("/onto/v1/store/upsert", kind="bot", methods=["POST"])
    def store_upsert(self, model=None, key=None, rows=None, **kw):
        """Bot tokens only: one INSERT ... ON CONFLICT per call, up to 1000 rows (3.3)."""
        return _store_json(lambda: onto_store.onto_upsert(request.env, model, key, rows))

    @onto_route("/onto/v1/store/read", kind="read", methods=["POST"])
    def store_read(self, model=None, domain=None, fields=None, order=None, limit=200, offset=0, **kw):
        """search_read as the caller: ACL, record rules and companies apply (3.3)."""
        return _store_json(
            lambda: onto_store.onto_read(
                request.env, model, [] if domain is None else domain, fields, order, limit, offset,
                caller=getattr(request, "onto_caller", "sealed"),
            )
        )

    @onto_route("/onto/v1/store/action", kind="write", methods=["POST"])
    def store_action(self, model=None, id=None, method=None, args=None, **kw):
        """Allowlisted model methods only; the allowlist is empty until 6.3 (3.3)."""
        return _store_json(
            lambda: onto_store.onto_action(request.env, model, id, method, {} if args is None else args)
        )

    @onto_route("/onto/v1/provision", kind="write", methods=["POST"])
    def provision(self, release=None, objects=None, **kw):
        """Stewards only (3.2a): create or update the managed datasets of one release. No querygen call."""
        if not request.env.user.has_group("odoo_dashboards_bi.group_onto_steward"):
            raise werkzeug.exceptions.NotFound()
        try:
            return request.make_json_response(onto_provision.provision(request.env, release, objects))
        except onto_provision.ProvisionError as exc:
            return request.make_json_response({"error": exc.code, "message": exc.message}, status=exc.status)

    @onto_route("/onto/v1/query", kind="read", methods=["POST"])
    def query(self, cids=None, **spec):
        """A governed read of one published object (1.6, 3.2). A token's companies are its own; a sealed
        caller may name companies it belongs to (default: its main company)."""
        token_cids = None if getattr(request, "onto_caller", "sealed") == "sealed" else request.env.context.get(
            "allowed_company_ids"
        )
        try:
            result = onto_query.run_query(request.env, spec, token_cids or cids)
        except onto_query.QueryError as exc:
            return request.make_json_response({"error": exc.code, "message": exc.message}, status=exc.status)
        return request.make_json_response(result)

    @onto_route("/onto/v1/scope", kind="read", methods=["POST"])
    def scope(self, model=None, **kw):
        """The caller's projected scope for one stored-result model (1.7): cached 15 min per user and companies."""
        payload = onto_scope.scope_payload(request.env, model)
        if payload is None:
            raise werkzeug.exceptions.NotFound()
        return request.make_json_response(payload)

    @onto_route("/onto/v1/rules", kind="read", methods=["POST"])
    def rules(self, **params):
        """Ontology Studio: the record rules of each object's models, in plain language. Stewards only."""
        if not request.env.user.has_group("odoo_dashboards_bi.group_onto_steward"):
            raise werkzeug.exceptions.NotFound()
        return request.make_json_response(onto_rules.studio_payload(request.env, params.get("object")))

    @onto_route("/onto/v1/object-setting", kind="write", methods=["POST"])
    def object_setting(self, **params):
        """Stewards only (1.7, 1.9): the guard models of one published object. The only write this route makes."""
        if not request.env.user.has_group(STEWARD_GROUP):
            raise werkzeug.exceptions.NotFound()
        if set(params) - {"object", "guard_models"}:
            return request.make_json_response({"error": "unknown_field"}, status=422)
        try:
            payload = onto_rules.set_guard_models(request.env, params.get("object"), params.get("guard_models"))
        except onto_rules.GuardError as exc:
            return request.make_json_response({"error": exc.code}, status=exc.status)
        return request.make_json_response(payload)

    @onto_route("/onto/v1/parity/run", kind="write", methods=["POST"])
    def parity_run(self, **params):
        """The parity check (1.8) for one object, or for every published object. Stewards, and the Onto bot
        (its token runs the check for the querygen job at /onto/jobs/parity). Anyone else is 404."""
        bot = getattr(request, "onto_caller", "sealed") == "bot"
        if not (bot or request.env.user.has_group(STEWARD_GROUP)):
            raise werkzeug.exceptions.NotFound()
        key = params.get("object")
        if key is not None and (not isinstance(key, str) or not onto_parity.published_objects(request.env, key)):
            raise werkzeug.exceptions.NotFound()
        return request.make_json_response(onto_parity.run(request.env, key))

    @onto_route("/onto/v1/parity/list", kind="read", methods=["POST"])
    def parity_list(self, **params):
        """Ontology Studio (1.8): this company's parity suspensions. Stewards only."""
        if not request.env.user.has_group(STEWARD_GROUP):
            raise werkzeug.exceptions.NotFound()
        return request.make_json_response({"suspensions": onto_parity.suspensions(request.env)})

    # kind="write" is the token gate, as on provision: query tokens get 403 token_kind. Both routes only read.
    @onto_route("/onto/v1/introspect", kind="write", methods=["POST"])
    def introspect(self, **params):
        """What the ontology builder reads from this database (2.1): configuration only. Stewards, and the Onto
        bot (its token runs the querygen drift job, 2.5). Anyone else is 404."""
        bot = getattr(request, "onto_caller", "sealed") == "bot"
        if not (bot or request.env.user.has_group(STEWARD_GROUP)):
            raise werkzeug.exceptions.NotFound()
        return request.make_json_response(request.env["onto.introspect"].introspect())

    @onto_route("/onto/v1/profile", kind="write", methods=["POST"])
    def profile(self, models=None, **params):
        """Row counts, fill rates and facts over stored columns (2.2, D50): aggregates only. Stewards only."""
        if not request.env.user.has_group(STEWARD_GROUP):
            raise werkzeug.exceptions.NotFound()
        try:
            payload = request.env["onto.introspect"].profile(models)
        except onto_introspect.IntrospectError as exc:
            return request.make_json_response({"error": exc.code}, status=exc.status)
        return request.make_json_response(payload)
    @onto_route("/onto/v1/health", kind="read", methods=["POST"])
    def health(self, check_key=None, **params):
        """Data Health (2.6): the stored check results. Stewards and stock managers; anyone else is 404."""
        user = request.env.user
        if not (user.has_group(STEWARD_GROUP) or user.has_group(STOCK_MANAGER_GROUP)):
            raise werkzeug.exceptions.NotFound()
        if check_key is not None and (not isinstance(check_key, str) or check_key not in onto_health.CHECKS):
            return request.make_json_response({"error": "unknown_check"}, status=422)
        return request.make_json_response(request.env["onto.health"].payload(check_key))

    @onto_route("/onto/v1/health/run", kind="write", methods=["POST"])
    def health_run(self, **params):
        """Data Health (2.6): recompute every check for the caller's companies now. Stewards only."""
        user = request.env.user
        if not user.has_group(STEWARD_GROUP):
            raise werkzeug.exceptions.NotFound()
        health = request.env["onto.health"]
        health.run_checks(companies=user.company_ids)
        return request.make_json_response(health.payload())

    @onto_route("/onto/v1/orgmap/apply", kind="write", methods=["POST"])
    def orgmap_apply(self, locations=None, **params):
        """Builder OrgMap drafts (D54, contract 11): the role of each stock location in the org map, as the steward.
        A role is optional per entry (2.9e) but must be one of the org map roles when sent; an unknown one refuses the
        whole request with 422 before anything is written. The token gate refuses query tokens; a bot token is refused
        here; anyone else gets 404."""
        if getattr(request, "onto_caller", "sealed") != "sealed":
            raise werkzeug.exceptions.Forbidden()
        if not request.env.user.has_group(STEWARD_GROUP):
            raise werkzeug.exceptions.NotFound()
        if not isinstance(locations, list) or len(locations) > MAX_ORGMAP_ENTRIES or not all(
            isinstance(entry, dict) and type(entry.get("location_id")) is int for entry in locations
        ):
            return request.make_json_response({"error": "invalid_body"}, status=422)
        if not all(_is_org_role(entry.get("role")) for entry in locations):
            return request.make_json_response({"error": "unknown_role"}, status=422)
        return request.make_json_response({"results": request.env["onto.org.map"].apply_location_roles(locations)})

    # --- p3-records (3.1): agent records begin ---
    @onto_route("/onto/v1/agent/record", kind="record", methods=["POST"])
    def agent_record(self, run=None, chat=None, usage=None, idempotency_key=None, **kw):
        """One finished agent turn (contract 3.10). A query token writes its own user's chat turns;
        a bot token writes system runs. A replay of the same idempotency_key changes nothing. Unknown
        fields (a user id, say) are refused, so no caller can write a record for another user."""
        if kw:
            return request.make_json_response({"error": "unknown_field"}, status=422)
        if len(request.httprequest.get_data()) > 1024 * 1024:  # contract 3.9: body <= 1 MB
            raise werkzeug.exceptions.RequestEntityTooLarge()
        caller = getattr(request, "onto_caller", "sealed")
        try:
            result = request.env["onto.agent.run"].record_turn(caller, run, chat, usage, idempotency_key)
        except RecordError as exc:
            return request.make_json_response({"error": exc.code}, status=exc.status)
        return request.make_json_response(result)
    # --- p3-records (3.1): agent records end ---
    # ----- 3.7 recommendations: the person's decisions (contract 3.8). Sealed only; the bot never decides. -----

    @onto_route("/onto/v1/recommendations/<int:rec_id>/approve", kind="write", methods=["POST"])
    def recommendation_approve(self, rec_id=None, version=None, line_id=None, **kw):
        rec = _decidable_recommendation(rec_id)
        return _store_json(lambda: rec.action_approve(version=version, line_id=line_id))

    @onto_route("/onto/v1/recommendations/<int:rec_id>/modify", kind="write", methods=["POST"])
    def recommendation_modify(self, rec_id=None, version=None, line_id=None, changes=None, **kw):
        rec = _decidable_recommendation(rec_id)
        return _store_json(lambda: rec.action_modify(version=version, line_id=line_id, changes=changes))

    @onto_route("/onto/v1/recommendations/<int:rec_id>/reject", kind="write", methods=["POST"])
    def recommendation_reject(self, rec_id=None, version=None, reason=None, **kw):
        rec = _decidable_recommendation(rec_id)
        return _store_json(lambda: rec.action_reject(version=version, reason=reason))
    # ----- end 3.7 -----

    # --- 3.5 BI agent: exploration and value lookup (begin) ---
    @onto_route("/onto/v1/explore", kind="read", methods=["POST"])
    def explore(self, cids=None, **body):
        """Uncertified exploration (A3.4a): an explore spec, or {"describe": "<model>"} for the schema graph. A query
        token may call it. Every path is checked as the caller: ACL, field groups and record rules."""
        token_cids = None if getattr(request, "onto_caller", "sealed") == "sealed" else request.env.context.get(
            "allowed_company_ids")
        try:
            if "describe" in body:
                payload = onto_explore.describe(request.env, body, token_cids or cids)
            else:
                payload = onto_explore.run_explore(request.env, body, token_cids or cids)
        except onto_query.QueryError as exc:
            return request.make_json_response({"error": exc.code, "message": exc.message}, status=exc.status)
        return request.make_json_response(payload)

    @onto_route("/onto/v1/lookup_values", kind="read", methods=["POST"])
    def lookup_values(self, cids=None, **body):
        """Real values of one managed column that match a partial name, at most 20 (3.5). As the caller: groups,
        record rules, guard models and field groups apply."""
        token_cids = None if getattr(request, "onto_caller", "sealed") == "sealed" else request.env.context.get(
            "allowed_company_ids")
        try:
            payload = onto_explore.lookup_values(request.env, body, token_cids or cids)
        except onto_query.QueryError as exc:
            return request.make_json_response({"error": exc.code, "message": exc.message}, status=exc.status)
        return request.make_json_response(payload)
    # --- 3.5 BI agent: exploration and value lookup (end) ---


def _decidable_recommendation(rec_id):
    """The recommendation the sealed person can see, or 404 (hidden and absent look the same). Bot tokens get 403."""
    if getattr(request, "onto_caller", "sealed") != "sealed":
        raise werkzeug.exceptions.Forbidden()
    try:
        rec = request.env["onto.recommendation"].search([("id", "=", rec_id)])
    except AccessError:
        raise werkzeug.exceptions.NotFound()
    if not rec:
        raise werkzeug.exceptions.NotFound()
    return rec
