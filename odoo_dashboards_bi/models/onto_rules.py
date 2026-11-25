# -*- coding: utf-8 -*-
"""Record rules (1.7): read and classify ir.rule, describe them for Ontology Studio, and the one
helper that turns a user's record rules into SQL ids. Guard models (per object, off by default) live
here too.

Nothing copies a customer's rule. Domains are evaluated with Odoo's own rule context, and the visible
ids come from Odoo's own _search, so every rule Odoo applies is applied here too.
"""

import json
import os

import odoo
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.modules.module import get_module_path
from odoo.osv import expression
from odoo.tools.safe_eval import safe_eval
from psycopg2 import sql

from .onto_dataset import KEY_RE

# Dimensions a stock.quant rule may depend on (dotted many2one paths -> plain name). Only these
# can be projected onto stored results (onto_scope.py); anything else is "not projectable".
DIMENSIONS = {
    "stock.quant": {
        "company_id": "company",
        "location_id": "location",
        "location_id.warehouse_id": "warehouse",
        "product_id": "product",
        "product_id.categ_id": "category",
    },
}

_ODOO_DIR = os.path.normcase(os.path.abspath(os.path.dirname(odoo.__file__)))
# Odoo ships its core addons in two places: odoo/addons (base) and the top-level addons folder.
CORE_ROOTS = (
    _ODOO_DIR,
    os.path.normcase(os.path.abspath(os.path.join(os.path.dirname(_ODOO_DIR), "addons"))),
)
OPERATOR_WORDS = {
    "=": "is", "!=": "is not", "in": "is one of", "not in": "is none of", "child_of": "is within",
    "<": "is below", ">": "is above", "<=": "is at most", ">=": "is at least",
    "like": "matches", "ilike": "matches", "not like": "does not match", "not ilike": "does not match",
}


def visible_ids_sql(user_env, model_name):
    """(sql, params): a subquery of the ids of `model_name` that the user may read.

    Odoo's own _search applies the ACL, every record rule (global and group) and the active companies
    (allowed_company_ids), so the ids are what the user's search would return. user_env must be the
    non-sudo user environment: superuser code skips record rules. Flushes first, so unsaved ORM writes
    are included. This is the only version-specific code: Odoo 16 returns a Query whose subselect() is
    (text, params); later versions return an SQL object."""
    user_env.flush_all()
    query = user_env[model_name]._search([])
    if isinstance(query, list):  # Odoo 16 short-circuit: an empty result
        return "(SELECT NULL::integer WHERE FALSE)", []
    sub = query.subselect()
    if isinstance(sub, tuple):  # 16.0: text without parentheses
        return "(%s)" % sub[0], list(sub[1])
    return sub.code, list(sub.params)  # 17+: odoo.tools.SQL


def applicable_rules(env, model_name):
    """The active read rules of `model_name` that apply to env.user: global rules, and group rules for
    a group the user is in (the same selection Odoo makes). Sudo records, for metadata only."""
    rules = env["ir.rule"].sudo().search(
        [("model_id.model", "=", model_name), ("active", "=", True), ("perm_read", "=", True)], order="id"
    )
    groups = set(env.user.groups_id.ids)
    return rules.filtered(lambda r: not r.groups or bool(groups & set(r.groups.ids)))


def parsed_domain(rule, eval_context):
    """The rule's domain as Odoo evaluates it (safe_eval with the rule context), or None if it cannot be
    evaluated. None is never projectable: a rule Odoo cannot read must not widen anything."""
    try:
        raw = safe_eval(rule.domain_force, eval_context) if rule.domain_force else []
        return expression.normalize_domain(raw)
    except Exception:  # noqa: BLE001 - any evaluation failure means "not projectable"
        return None


def rule_kind(rule, domain):
    """'multi_company' (Odoo's company rule), 'functional' (another Odoo rule) or 'custom' (Studio,
    __export__, or any rule from an addon that is not Odoo's own)."""
    xmlid = rule.get_external_id().get(rule.id) or ""
    module = xmlid.split(".", 1)[0] if "." in xmlid else ""
    if not module or module in ("__export__", "studio_customization") or not _is_core(module):
        return "custom"
    paths = {token[0] for token in (domain or []) if isinstance(token, (list, tuple)) and len(token) == 3}
    if paths and paths == {"company_id"}:
        return "multi_company"
    return "functional"


def _is_core(module):
    path = get_module_path(module, display_warning=False)
    if not path:
        return False
    path = os.path.normcase(os.path.abspath(path))
    return any(path == root or path.startswith(root + os.sep) for root in CORE_ROOTS)


def _refuse(reason):
    return {"ok": False, "key": None, "reason": reason}


def _dimension_key(path, dims):
    """The longest key of dims that the dotted path starts with (or equals), or None."""
    best = None
    for key in dims:
        if (path == key or path.startswith(key + ".")) and (best is None or len(key) > len(best)):
            best = key
    return best


def _follows_fields(model, path):
    """True when every step of a dotted path is a field, and every step but the last is a many2one."""
    current, parts = model, path.split(".")
    for index, name in enumerate(parts):
        field = current._fields.get(name)
        if field is None:
            return False
        if index < len(parts) - 1:
            if field.type != "many2one":
                return False
            current = current.env[field.comodel_name]
    return True


def analyse_domain(env, model_name, domain, dims):
    """Whether a normalized domain depends on one dimension only.

    dims maps dotted many2one paths to labels. Returns {"ok", "key", "reason"}: key is the dims key the
    domain follows, or None when it is always true. A domain is projectable when every condition follows
    the same key and none is always false. Projection then shows a value only when a readable row carries
    it (onto_scope.py)."""
    if domain is None:
        return _refuse("its condition cannot be evaluated")
    keys = set()
    for token in domain:
        if isinstance(token, str):  # '&', '|', '!'
            continue
        if not isinstance(token, (list, tuple)) or len(token) != 3:
            return _refuse("it has a condition Odoo does not expect")
        if tuple(token) == expression.TRUE_LEAF:
            continue
        if tuple(token) == expression.FALSE_LEAF:
            return _refuse("it can never match")
        path = token[0]
        if not isinstance(path, str):
            return _refuse("it has a condition Odoo does not expect")
        key = _dimension_key(path, dims)
        if key is None:
            return _refuse("it depends on %s, which is not a dimension that can be projected" % path)
        if not _follows_fields(env[model_name], path):
            return _refuse("it cannot follow the path %s" % path)
        keys.add(key)
    if len(keys) > 1:
        return _refuse("it mixes %s" % " and ".join(sorted(keys)))
    return {"ok": True, "key": next(iter(keys)) if keys else None, "reason": None}


def _path_text(env, model_name, path):
    names, current = [], env[model_name]
    for name in path.split("."):
        field = current._fields.get(name)
        names.append(str(field.string) if field is not None else name)
        if field is None or field.type != "many2one":
            break
        current = env[field.comodel_name]
    return " > ".join(names)


def _last_field(env, model_name, path):
    current, field = env[model_name], None
    for name in path.split("."):
        field = current._fields.get(name)
        if field is None or field.type != "many2one":
            return field
        current = env[field.comodel_name]
    return field


def _value_text(env, model_name, path, value):
    if value is False or value is None:
        return "not set"
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    last = _last_field(env, model_name, path)
    if last is not None and last.type == "many2one":
        ids = [v for v in values if type(v) is int]  # False (not set) and True are not record ids
        names = env[last.comodel_name].sudo().browse(ids).exists().mapped("display_name")
        if names:
            return ", ".join(names)
    return ", ".join(str(v) for v in values)


def _leaf_text(env, model_name, token):
    if tuple(token) == expression.TRUE_LEAF:
        return "always"
    if tuple(token) == expression.FALSE_LEAF:
        return "never"
    path, operator, value = token
    return "%s %s %s" % (
        _path_text(env, model_name, path), OPERATOR_WORDS.get(operator, operator),
        _value_text(env, model_name, path, value),
    )


def _condition_text(env, model_name, domain):
    """A normalized domain (prefix notation) as one sentence, built on a stack from the last token."""
    stack = []
    for token in reversed(domain):
        if isinstance(token, str):
            if token == "!":
                stack.append("not (%s)" % stack.pop())
            else:
                left, right = stack.pop(), stack.pop()
                stack.append("(%s %s %s)" % (left, "and" if token == "&" else "or", right))
        else:
            stack.append(_leaf_text(env, model_name, token))
    return stack.pop()


def _describe(env, model_name, rule, domain, analysis, groups, dimension):
    """A plain sentence for the steward: who the rule applies to, what it keeps, and whether it projects."""
    label = env[model_name]._description
    if domain is None:
        keeps = "Odoo cannot evaluate this rule's condition."
    elif all(tuple(t) == expression.TRUE_LEAF for t in domain if not isinstance(t, str)):
        keeps = "Keeps every %s record." % label
    else:
        try:
            keeps = "Keeps only the %s records where %s." % (label, _condition_text(env, model_name, domain))
        except (IndexError, TypeError, ValueError):
            keeps = "Keeps only the %s records that match its condition." % label
    applies = "Applies to every user." if not rule.groups else "Applies to users in: %s." % ", ".join(groups)
    if not analysis["ok"]:
        projection = "Not projectable (%s): restricted users see no stored results from this model." % (
            analysis["reason"]
        )
    elif dimension:
        projection = "Projectable on %s." % dimension
    else:
        projection = "Projectable (no dimension)."
    company = " Depends on the companies the user has selected." if "company" in (rule.domain_force or "") else ""
    return " ".join([keeps, applies + company, projection])


def classify_rules(env, model_name, for_user=False):
    """Every read rule of a model, classified and described for Ontology Studio.

    for_user=True lists only the rules that apply to env.user (env must be that user's environment).
    Otherwise every rule of the model is listed; conditions on `user` are evaluated for env.user."""
    if for_user:
        rules = applicable_rules(env, model_name)
    else:
        rules = env["ir.rule"].sudo().search(
            [("model_id.model", "=", model_name), ("active", "=", True), ("perm_read", "=", True)], order="id"
        )
    context = env["ir.rule"]._eval_context()
    dims = DIMENSIONS.get(model_name, {})
    rows = []
    for rule in rules:
        domain = parsed_domain(rule, context)
        analysis = analyse_domain(env, model_name, domain, dims)
        groups = rule.groups.mapped("name")
        dimension = dims.get(analysis["key"]) if analysis["key"] else None
        rows.append({
            "id": rule.id,
            "name": rule.name,
            "xmlid": rule.get_external_id().get(rule.id) or None,
            "kind": rule_kind(rule, domain),
            "global": not rule.groups,
            "groups": groups,
            "company_dependent": "company" in (rule.domain_force or ""),
            "projectable": analysis["ok"],
            "dimension": dimension,
            "reason": analysis["reason"],
            "description": _describe(env, model_name, rule, domain, analysis, groups, dimension),
        })
    return rows


def studio_payload(env, object_name=None):
    """Per object (onto.object.setting): its base model and guard models, each with its rules (Studio)."""
    domain = [("name", "=", object_name)] if object_name else []
    objects = []
    for setting in env["onto.object.setting"].search(domain, order="name"):
        guards = guard_model_names(setting)
        objects.append({
            "object": setting.name,
            "base_model": setting.base_model,
            "guard_models": guards,
            "models": [
                {"model": name, "role": role, "rules": classify_rules(env, name)}
                for name, role in [(setting.base_model, "base")] + [(name, "guard") for name in guards]
            ],
        })
    return {"objects": objects}


# ---------------------------------------------------------------------------
# Guard models (1.7): per object, off by default. A guard applies the joined model's record rules to
# the object's many2one foreign keys, so an order whose customer the user cannot read is hidden.
# ---------------------------------------------------------------------------


class OntoObjectSetting(models.Model):
    _name = "onto.object.setting"
    _inherit = ["onto.mixin"]
    _description = "Onto object setting: guard models whose record rules also apply"
    _sql_constraints = [
        ("onto_object_setting_name_uniq", "unique(company_id, name)", "One setting per object and company."),
    ]

    name = fields.Char(required=True)
    base_model = fields.Char(required=True, help="Technical name of the model the object reads, e.g. stock.picking.")
    guard_models = fields.Char(
        default="[]",
        help='JSON list of joined models whose record rules also apply, e.g. ["res.partner"]. Off when empty.',
    )

    @api.constrains("base_model", "guard_models")
    def _check_guard_models(self):
        for rec in self:
            if rec.base_model not in self.env.registry:
                raise ValidationError(_("Unknown model %s.") % rec.base_model)
            try:
                names = guard_model_names(rec)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            for name in names:
                if name not in self.env.registry or not foreign_keys(self.env, rec.base_model, name):
                    raise ValidationError(_("%s has no many2one field to %s.") % (rec.base_model, name))


def guard_model_names(setting):
    """The joined models a setting guards (empty when off). Raises ValueError for a malformed list."""
    try:
        names = json.loads(setting.guard_models or "[]")
    except ValueError as exc:
        raise ValueError("guard_models must be a JSON list of model names.") from exc
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("guard_models must be a JSON list of model names.")
    return names


def foreign_keys(env, base_model, joined_model):
    """The stored many2one fields of base_model that point at joined_model."""
    return [
        name for name, field in env[base_model]._fields.items()
        if field.type == "many2one" and field.comodel_name == joined_model and field.store
    ]


def guard_fragments(user_env, object_setting, columns=None):
    """Guard SQL for one object: one item per guard model, [] when the object has none (the default).

    Each item: "model"; "table" (onto_visible_<n>); "create" = (sql, params), which drops and rebuilds that
    temp table with the ids of the joined model the user may read (visible_ids_sql, so the joined model's
    record rules apply); and "fragments", one per many2one from the base model to the joined model:
    (<fk> IS NULL OR <fk> IN (SELECT id FROM onto_visible_<n>)). The fk names are unqualified columns, so
    the caller applies them to the base table. columns (optional, the view's column names) keeps only the
    fks the view exposes, because a fragment on a missing column would not run. The caller runs "create"
    in its transaction first."""
    items = []
    for index, joined in enumerate(guard_model_names(object_setting), start=1):
        fks = foreign_keys(user_env, object_setting.base_model, joined)
        if columns is not None:
            fks = [fk for fk in fks if fk in columns]
        if not fks:
            raise ValueError("%s has no many2one field to %s in this object." % (object_setting.base_model, joined))
        table = "onto_visible_%d" % index
        visible_sql, params = visible_ids_sql(user_env, joined)
        create = (
            "DROP TABLE IF EXISTS pg_temp.%s; CREATE TEMP TABLE %s ON COMMIT DROP AS SELECT id FROM %s t(id)"
            % (table, table, visible_sql),
            params,
        )
        items.append({
            "model": joined,
            "table": table,
            "create": create,
            "fragments": [
                "(%s IS NULL OR %s IN (SELECT id FROM %s))" % (fk, fk, table) for fk in fks
            ],
        })
    return items


# ---------------------------------------------------------------------------
# Steward toggle (1.7 / 1.9): the only write to a guard list. Steward-only route, validated here.
# ---------------------------------------------------------------------------

GUARD_LIMIT = 5


class GuardError(Exception):
    """A refused guard change. status and code are what the route answers."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def set_guard_models(env, key, names):
    """Sets the guard models of one published object for the company (stewards only; the route checks that).

    Each name must be a model in the registry with a stored many2one to the object's base model. The base
    model comes from the published managed dataset, so an unpublished object is 404. Audited (ids and names only)."""
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise GuardError(404, "not_found")
    dataset = env["dataset.saas"].sudo().with_context(active_test=False).search(
        [("onto_managed", "=", True), ("onto_object_key", "=", key), ("state", "=", "active"),
         ("onto_engine_only", "=", False)], limit=1,
    )
    if not dataset:
        raise GuardError(404, "not_found")
    base = dataset.model_id.model
    if not isinstance(names, list) or len(names) > GUARD_LIMIT or not all(isinstance(n, str) for n in names):
        raise GuardError(422, "bad_guard_models")
    names = sorted(set(names))
    for name in names:
        if name not in env.registry or not foreign_keys(env, base, name):
            raise GuardError(422, "bad_guard_model")
    setting = env["onto.object.setting"].search([("name", "=", key), ("company_id", "=", env.company.id)], limit=1)
    values = {"base_model": base, "guard_models": json.dumps(names)}
    if setting:
        setting.write(values)
    else:
        setting = env["onto.object.setting"].create(dict(values, name=key, company_id=env.company.id))
    env["onto.audit"].sudo().create({
        "company_id": env.company.id,
        "action": "onto.object.guard_models",
        "res_model": setting._name,
        "res_id": setting.id,
        "detail": "object=%s guards=%s" % (key, ",".join(names) or "none"),
    })
    return {"object": key, "base_model": base, "guard_models": names}
