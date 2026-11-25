# -*- coding: utf-8 -*-
"""Parity check (1.8, plan A3.5 step 4): Odoo's own answer against ours, per user signature.

For one object and one sample user, three counts are compared over the object's base model:
  orm     search_count the user may read (ACL, record rules, companies; guard models as domains);
  live    the same rows through the governed query path (onto_query._visibility builds onto_visible and
          the guard tables), counted in SQL;
  stored  each stored-result model built on the base model, through its scoped _search, against the ORM
          count of the rows its dimensions point at. A non-projectable rule must show nothing, never more.

A mismatch suspends the object for that signature (onto.parity.suspension). onto_query refuses a suspended
pair with the same 404 as an unknown object. Stewards get a notification and an onto.audit row. A later clean
run lifts the suspension, and that is audited too.

Reads run as the sample user. Only the control records (suspensions, audit, notifications) are written as
superuser. Only counts are stored or returned, and logs carry the object key and error names only.
"""

import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.osv import expression

from . import onto_query
from .onto_rules import foreign_keys, guard_model_names
from .onto_scope import projection
from .onto_store import onto_now, user_signature

_logger = logging.getLogger(__name__)

STEWARD = "odoo_dashboards_bi.group_onto_steward"
MAX_SIGNATURES = 20  # sample users per object per run: one per (groups + companies) signature
PARITY_TOLERANCE = 0  # the counts must agree exactly


class OntoParitySuspension(models.Model):
    _name = "onto.parity.suspension"
    _inherit = ["onto.mixin"]
    _description = "Onto parity suspension: an object refused for one user signature"
    _sql_constraints = [
        ("onto_parity_suspension_uniq", "unique(company_id, object_key, signature)",
         "One suspension per object and signature."),
    ]

    object_key = fields.Char(required=True, index=True)
    signature = fields.Char(required=True, index=True, help="Hash of the user's groups and companies.")
    reason = fields.Char(required=True)
    created = fields.Datetime(default=lambda self: onto_now(), required=True, readonly=True)

    @api.model
    def _onto_parity_cron(self):
        """Weekly (data/onto_cron.xml): the check for every published managed object, sample users only."""
        run(self.env)


def published_objects(env, key=None):
    """The published managed datasets (one object each), or the one named by key. Sudo for metadata only."""
    domain = [("onto_managed", "=", True), ("state", "=", "active"), ("onto_engine_only", "=", False)]
    if key is not None:
        domain.append(("onto_object_key", "=", key))
    return env["dataset.saas"].sudo().with_context(active_test=False).search(domain, order="onto_object_key")


def sample_users(env, dataset):
    """One active internal user per signature among the users the object's groups admit."""
    users = env["res.users"].sudo().search(
        [("active", "=", True), ("share", "=", False), ("groups_id", "in", dataset.group_ids.ids)], order="id"
    )
    picked = {}
    for user in users:
        picked.setdefault(user_signature(user), user)
        if len(picked) >= MAX_SIGNATURES:
            break
    return list(picked.values())


def _setting(env, key):
    return env["onto.object.setting"].sudo().search(
        [("name", "=", key), ("company_id", "in", env.companies.ids)], limit=1
    )


def _orm_guard_domain(user_env, dataset, key):
    """The ORM form of the guard fragments onto_query applies: a row passes each guard when its foreign key is
    empty or one of the joined records the user may read (Odoo's own search). Same fk columns as the query path."""
    model = dataset.model_id.model
    setting = _setting(user_env, key)
    if not setting or not guard_model_names(setting):
        return []
    columns = {name for name, _ in dataset._view_column_types()}
    domain = []
    for joined in guard_model_names(setting):
        ids = user_env[joined].search([]).ids
        for fk in foreign_keys(user_env, model, joined):
            if fk in columns:
                domain = expression.AND([domain, ["|", (fk, "=", False), (fk, "in", ids)]])
    return domain


def _result_models(env, fact):
    """The stored-result models built on `fact`: their _onto_scope names it as the fact model."""
    names = []
    for name in sorted(env.registry):
        config = getattr(env[name], "_onto_scope", None)
        if config and config["fact"] == fact:
            names.append(name)
    return names


def _stored_check(user_env, fact, name):
    """One stored-result model. Its scope filters by the projected sets of the fact model's dimensions, so those sets
    must equal the values Odoo's own search gives for the user's visible fact rows (counts are stored, not values).
    A non-projectable rule shows nothing by design, so there is nothing to compare."""
    config = user_env[name]._onto_scope
    mode, sets = projection(user_env, fact, config["dims"])
    if mode == "none":
        return {"model": name, "mode": mode, "projectable": False, "match": True}
    if not sets:  # no restriction on this fact model: nothing is projected, so nothing to compare
        return {"model": name, "mode": mode, "projectable": True, "match": True, "projected": 0, "visible": 0}
    path_of = {field: path for path, field in config["dims"].items()}
    # ponytail: loads every visible fact row to read its dimension values; a grouped read per dimension is the upgrade.
    visible = user_env[fact].search([])
    truth = {field: sorted(visible.mapped(path_of[field]).ids) for field in sets}
    projected = {field: sorted(sets[field]) for field in sets}
    return {"model": name, "mode": mode, "projectable": True, "match": projected == truth,
            "projected": sum(len(ids) for ids in projected.values()),
            "visible": sum(len(ids) for ids in truth.values())}


def _close(left, right):
    return abs(left - right) <= PARITY_TOLERANCE


def check_user(env, user, dataset):
    """Parity for one user and one object: {"match", "values", "tolerance"}. None when the user cannot read the
    base model at all (the query path refuses that user anyway, so there is nothing to compare)."""
    key, model = dataset.onto_object_key, dataset.model_id.model
    user_env = env(user=user, context=dict(env.context, allowed_company_ids=user.company_ids.ids))
    try:
        user_env[model].check_access_rights("read")
    except AccessError:
        return None
    try:
        orm = user_env[model].search_count(_orm_guard_domain(user_env, dataset, key))
        fragments = onto_query._visibility(user_env, model, dataset, key)
    except (onto_query.QueryError, ValueError) as exc:
        code = getattr(exc, "code", "guard_configuration")
        return {"match": False, "values": {"error": code}, "tolerance": PARITY_TOLERANCE}
    where = " AND ".join(["id IN (SELECT id FROM onto_visible)"] + fragments)
    user_env.cr.execute("SELECT count(*) FROM %s WHERE %s" % (user_env[model]._table, where))
    live = user_env.cr.fetchone()[0]
    stored = [_stored_check(user_env, model, name) for name in _result_models(user_env, model)]
    values = {"orm": orm, "live": live, "stored": stored}
    match = _close(orm, live) and all(entry["match"] for entry in stored)
    return {"match": match, "values": values, "tolerance": PARITY_TOLERANCE}


def _checked(env, dataset, user):
    """check_user inside a savepoint. A check that errors counts as a mismatch: it never passes."""
    try:
        with env.cr.savepoint():
            return check_user(env, user, dataset)
    except Exception as exc:  # noqa: BLE001 - fail closed; the error text may carry values, so only its type is logged
        _logger.warning("Onto parity check failed for object %s: %s", dataset.onto_object_key, type(exc).__name__)
        return {"match": False, "values": {"error": "check_failed"}, "tolerance": PARITY_TOLERANCE}


def _reason(outcome):
    values = outcome["values"]
    if "error" in values:
        return "check could not run (%s)" % values["error"]
    if not _close(values["orm"], values["live"]):
        return "query count %d differs from ORM count %d" % (values["live"], values["orm"])
    return "stored results differ on %s" % ", ".join(e["model"] for e in values["stored"] if not e["match"])


def _notify_stewards(env, company, key):
    """A notification to each active steward of the company, in the existing inbox (dashboard.notification.saas)."""
    stewards = env.ref(STEWARD).sudo().users.filtered(
        lambda u: u.active and not u.share and company in u.company_ids.ids
    )
    for steward in stewards:
        env["dashboard.notification.saas"].sudo().create({
            "user_id": steward.id,
            "kind": "odoo",
            "title": "Onto parity check failed",
            "body": "Object %s is suspended for one set of users until a later check matches. "
                    "Its queries return not found meanwhile." % key,
        })


def _apply(env, key, user, signature, outcome):
    """Sets or lifts the suspension of one object and signature. Returns "suspended", "lifted" or None."""
    Suspension = env["onto.parity.suspension"].sudo()
    audit = env["onto.audit"].sudo()
    found = Suspension.search([("object_key", "=", key), ("signature", "=", signature)])
    if outcome["match"]:
        if not found:
            return None
        audit.create({"company_id": found[0].company_id.id, "action": "onto.parity.lifted",
                      "res_model": Suspension._name, "res_id": found[0].id,
                      "detail": "object=%s signature=%s" % (key, signature[:12])})
        found.unlink()
        return "lifted"
    if found:
        found.reason = _reason(outcome)
        return None
    company = user.company_id.id
    created = Suspension.create({"company_id": company, "object_key": key, "signature": signature,
                                 "reason": _reason(outcome)})
    audit.create({"company_id": company, "action": "onto.parity.suspended",
                  "res_model": Suspension._name, "res_id": created.id,
                  "detail": "object=%s signature=%s" % (key, signature[:12])})
    _notify_stewards(env, company, key)
    return "suspended"


def run(env, key=None):
    """The check for every published object (or the one named), for its sample users; sets or lifts the
    suspensions. Returns {"results": [...], "suspended": n, "lifted": n}. Each result is one user's
    {"object", "signature", "match", "values", "tolerance"}."""
    results, suspended, lifted = [], 0, 0
    for dataset in published_objects(env, key):
        for user in sample_users(env, dataset):
            outcome = _checked(env, dataset, user)
            if outcome is None:
                continue
            signature = user_signature(user)
            change = _apply(env, dataset.onto_object_key, user, signature, outcome)
            suspended += change == "suspended"
            lifted += change == "lifted"
            results.append(dict(outcome, object=dataset.onto_object_key, signature=signature))
    return {"results": results, "suspended": suspended, "lifted": lifted}


def suspensions(env):
    """This company's suspensions, for Ontology Studio (the caller's own rules apply)."""
    return [
        {"object": row.object_key, "signature": row.signature, "reason": row.reason,
         "created": fields.Datetime.to_string(row.created)}
        for row in env["onto.parity.suspension"].search([], order="created desc", limit=200)
    ]
