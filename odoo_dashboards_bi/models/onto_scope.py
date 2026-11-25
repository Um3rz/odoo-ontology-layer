# -*- coding: utf-8 -*-
"""Scope projection and stored-result filtering (1.7).

Stored results (onto.status, forecasts, KPIs, recommendations, alerts, stock history) are computed by
the bot, so the customer's record rules on the fact model behind them (stock.quant) do not apply to
them by themselves. This module projects the user's rules onto each result's dimensions:

  1. Read the rules that apply to the user on the fact model (onto_rules.applicable_rules).
  2. Odoo's standard company rule is left out: the result model's own company rule enforces it.
  3. Every other rule must depend on one dimension the result carries (location, product, warehouse,
     company). Group rules are OR-ed, so they must share one dimension.
  4. The projected set of a dimension is its distinct values over the fact rows the user may read
     (visible_ids_sql). A result row is shown only when its value is in the set.
  5. Anything else Odoo would restrict (a mixed rule, an unknown path, a rule that cannot be evaluated,
     or no read access to the fact model while a restriction applies) hides every stored result from the
     user. It never widens. A value with no readable fact row is hidden too.

Projected sets are cached for 15 minutes per database, user, company set and rule set.
"""

import calendar

from odoo import models
from odoo.exceptions import AccessError
from odoo.osv import expression
from psycopg2 import sql

from .onto_rules import analyse_domain, applicable_rules, parsed_domain, rule_kind, visible_ids_sql
from .onto_store import onto_now

CACHE_TTL = 15 * 60
CACHE_MAX = 1000

# Stored results on stock.quant (fact model) -> the result field each dimension filters.
STOCK_RESULT_DIMS = {"location_id": "location_id", "product_id": "product_id", "company_id": "company_id"}
# Stored results keyed by warehouse (KPIs, recommendations); the warehouse follows the location.
WAREHOUSE_RESULT_DIMS = {"location_id.warehouse_id": "warehouse_id", "company_id": "company_id"}
# Recommendation lines carry their recommendation's warehouse.
LINE_RESULT_DIMS = {"location_id.warehouse_id": "recommendation_id.warehouse_id", "company_id": "company_id"}

_cache = {}


def _now():
    return calendar.timegm(onto_now().utctimetuple())


def clear_cache():
    """Drop every projected set (tests, and any code that changes rules and needs them applied now)."""
    _cache.clear()


def _cached(key, compute):
    now = _now()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = compute()
    if len(_cache) >= CACHE_MAX:
        for stale in [k for k, (until, _) in _cache.items() if until <= now]:
            del _cache[stale]
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
    _cache[key] = (now + CACHE_TTL, value)
    return value


def projection(env, fact_model, dims):
    """(mode, sets) for env.user on fact_model, projected onto dims (fact path -> result field).

    mode 'all': no restriction. 'none': nothing may be shown. 'dims': sets maps each result field to the
    values the user's stored results may carry. env must be the user's (non-sudo) environment."""
    rules = applicable_rules(env, fact_model)
    signature = tuple((rule.id, str(rule.write_date)) for rule in rules)
    key = (
        env.cr.dbname, env.uid, tuple(sorted(env.companies.ids)), fact_model,
        tuple(sorted(dims.items())), signature,
    )
    return _cached(key, lambda: _compute(env, fact_model, dims, rules))


def _compute(env, fact_model, dims, rules):
    context = env["ir.rule"]._eval_context()
    global_keys, group_keys, group_seen, group_open = set(), set(), False, False
    for rule in rules:
        domain = parsed_domain(rule, context)
        if rule_kind(rule, domain) == "multi_company":
            continue  # the result model's own company rule covers it, with the same company set
        analysis = analyse_domain(env, fact_model, domain, dims)
        if not analysis["ok"]:
            return ("none", {})
        key = analysis["key"]
        if rule.groups:
            group_seen = True
            if key is None:
                group_open = True  # an always-true group rule: the group rules together admit everything
            else:
                group_keys.add(key)
        elif key is not None:
            global_keys.add(key)
    restricting_groups = group_seen and not group_open
    if restricting_groups and len(group_keys) > 1:
        return ("none", {})  # OR-ed group rules on two dimensions cannot be projected without widening
    keys = global_keys | (group_keys if restricting_groups else set())
    if not keys:
        return ("all", {})
    try:
        visible_sql, params = visible_ids_sql(env, fact_model)
    except AccessError:
        return ("none", {})  # restricted, and no read access to the fact model
    sets = {dims[key]: _projected_ids(env, fact_model, key, visible_sql, params) for key in sorted(keys)}
    return ("dims", sets)


def _projected_ids(env, fact_model, fact_path, visible_sql, params):
    """Distinct non-empty values of a dotted many2one path over the fact rows the user may read."""
    alias, current, joins = "q", env[fact_model], []
    parts = fact_path.split(".")
    for index, name in enumerate(parts[:-1]):
        field = current._fields[name]
        comodel = env[field.comodel_name]
        joined = "j%d" % index
        joins.append(sql.SQL("JOIN {} {} ON {}.id = {}.{}").format(
            sql.Identifier(comodel._table), sql.Identifier(joined), sql.Identifier(joined),
            sql.Identifier(alias), sql.Identifier(name),
        ))
        alias, current = joined, comodel
    column = sql.Identifier(alias, parts[-1])
    statement = sql.SQL(
        "SELECT DISTINCT {col} FROM {table} q {joins} WHERE q.id IN {visible} AND {col} IS NOT NULL"
    ).format(
        col=column,
        table=sql.Identifier(env[fact_model]._table),
        joins=sql.SQL(" ").join(joins),
        visible=sql.SQL(visible_sql),
    )
    env.cr.execute(statement, params)
    return sorted(row[0] for row in env.cr.fetchall())


def scope_domain(records):
    """Leaves that limit a stored-result recordset to the user's projected scope.

    None: no limit. [FALSE_LEAF]: nothing. Otherwise one 'in' leaf per projected result field."""
    config = getattr(records, "_onto_scope", None)
    if not config or records.env.su:
        return None  # superuser code (crons, tests with sudo) is not scoped
    mode, sets = projection(records.env, config["fact"], config["dims"])
    if mode == "all":
        return None
    if mode == "none":
        return [expression.FALSE_LEAF]
    return [(path, "in", ids) for path, ids in sorted(sets.items())]


class OntoScopedMixin(models.AbstractModel):
    """Filters a stored result model by the user's projected scope. Used by every onto.* result model
    that declares _onto_scope = {"fact": <model>, "dims": <fact path -> result field>}."""

    _name = "onto.scoped.mixin"
    _description = "Onto stored results filtered by the user's projected record-rule scope"

    def _search(self, domain, offset=0, limit=None, order=None, count=False, access_rights_uid=None):
        leaves = scope_domain(self)
        if leaves is not None:
            domain = expression.AND([list(domain or []), leaves])
        return super()._search(
            domain, offset=offset, limit=limit, order=order, count=count, access_rights_uid=access_rights_uid
        )

    def read_group(self, domain, fields, groupby, offset=0, limit=None, orderby=False, lazy=True):
        leaves = scope_domain(self)
        if leaves is not None:
            domain = expression.AND([list(domain or []), leaves])
        return super().read_group(
            domain, fields, groupby, offset=offset, limit=limit, orderby=orderby, lazy=lazy
        )

    def _filter_access_rules_python(self, operation):
        # read() on specific ids checks record rules here, not in _search.
        allowed = super()._filter_access_rules_python(operation)
        leaves = scope_domain(self) if operation == "read" else None
        if leaves is None:
            return allowed
        if leaves == [expression.FALSE_LEAF]:
            return allowed.browse()
        return allowed.filtered_domain(leaves)


def scope_payload(env, model_name):
    """The caller's projected scope for one stored-result model, or None when it is not one (404)."""
    if not isinstance(model_name, str) or model_name not in env.registry:
        return None
    config = getattr(env[model_name], "_onto_scope", None)
    if not config:
        return None
    mode, sets = projection(env, config["fact"], config["dims"])
    return {"model": model_name, "mode": mode, "dims": sets, "ttl": CACHE_TTL}
