# -*- coding: utf-8 -*-
"""Onto introspection (2.1, 2.2): what the ontology builder reads from this database.

introspect() lists configuration: installed modules, companies, warehouses, locations (with their org
role), groups, users, record rules and the models with their fields. profile() answers row counts, filled
counts and min/max dates over stored columns, plus a few facts, with SQL aggregates (D50: no temporary
datasets).

Metadata and aggregates only: no business record value is read or returned. Both are for stewards; the
routes in controllers/onto.py check that and refuse query tokens.
"""

import psycopg2
from psycopg2 import sql
from psycopg2.errors import QueryCanceled

from odoo import _, models, release
from odoo.exceptions import AccessError
from odoo.addons.odoo_dashboards_saas.models.sql_guard import read_only

from .onto_rules import rule_kind

# The models a profile reads when the caller names none (the web app sends {}): the template base models (D50),
# plus the models those templates join to. Without the joined ones, querygen reads their fill as "no data".
PROFILE_MODELS = (
    "stock.quant", "stock.move", "stock.move.line", "stock.picking", "stock.lot", "stock.valuation.layer",
    "product.product", "product.template", "product.supplierinfo", "stock.warehouse.orderpoint",
    "purchase.order", "purchase.order.line", "pos.order", "pos.order.line", "sale.order", "sale.order.line",
    "account.move", "account.move.line",
    "res.partner", "res.company", "product.category", "stock.warehouse", "stock.location", "stock.scrap",
    "pos.session",
)
PROFILE_LIMIT = 60
PROFILE_TIMEOUT_MS = 30000  # statement_timeout of one model's aggregate
EXCLUDED_PREFIXES = ("ir.", "base.", "bus.", "mail.")  # internals: never listed or profiled
NOT_STORED_TYPES = ("one2many", "many2many")  # no column on the model's table
DATE_TYPES = ("date", "datetime")
HISTORY_DATES = {"stock.move": "date", "pos.order": "date_order", "account.move": "date"}
QC_ROLE = "qc_hold"


class IntrospectError(Exception):
    """A refused profile request. status and code are what the route answers."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def _xmlids(env, model_name):
    """{record id: "module.name"} for the records of model_name that have an external id."""
    rows = env["ir.model.data"].search_read([("model", "=", model_name)], ["module", "name", "res_id"])
    return {row["res_id"]: "%s.%s" % (row["module"], row["name"]) for row in rows}


def _selection(env, model_name, name, ttype):
    """[[value, label], ...] for a selection field the registry knows, else None."""
    field = env[model_name]._fields.get(name) if ttype == "selection" else None
    if field is None or field.type != "selection":
        return None
    return [[value, str(label)] for value, label in field._description_selection(env)]


def _iso(value):
    return value.isoformat() if value else None


def _aggregate_sql(table, fields):
    """One SELECT: COUNT(*), then per field its filled count (booleans: the TRUE count) and, for dates,
    min and max. Identifiers are quoted by psycopg2 and were checked against the table's columns."""
    parts = [sql.SQL("COUNT(*)")]
    for field in fields:
        column = sql.Identifier(field["name"])
        if field["ttype"] == "boolean":
            parts.append(sql.SQL("COUNT(*) FILTER (WHERE {} IS TRUE)").format(column))
        else:
            parts.append(sql.SQL("COUNT({})").format(column))
        if field["ttype"] in DATE_TYPES:
            parts.append(sql.SQL("MIN({})::date").format(column))
            parts.append(sql.SQL("MAX({})::date").format(column))
    return sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(parts), sql.Identifier(table))


def _field_results(row, fields):
    """The per-field results of _aggregate_sql, read back in the same order."""
    values = iter(row[1:])
    row_count = row[0]
    out = []
    for field in fields:
        filled = next(values)
        entry = {
            "name": field["name"],
            "filled": filled,
            "fill_rate": round(filled / row_count, 3) if row_count else 0.0,
        }
        if field["ttype"] in DATE_TYPES:
            entry["min"] = _iso(next(values))
            entry["max"] = _iso(next(values))
        out.append(entry)
    return out


class OntoIntrospect(models.AbstractModel):
    _name = "onto.introspect"
    _description = "Onto introspection: configuration and aggregates for the ontology builder (no record values)"

    def _allowed_model(self, name):
        """A model the builder may list or profile: in the registry, neither abstract nor transient, and not
        an internal (ir, base, bus, mail). x_ models are included."""
        if not isinstance(name, str) or name.startswith(EXCLUDED_PREFIXES) or name not in self.env.registry:
            return False
        model = self.env.registry[name]
        return not (model._abstract or model._transient)

    def _installed_modules(self, env):
        """{module name: installed version} for the modules in state installed."""
        rows = env["ir.module.module"].search_read([("state", "=", "installed")], ["name", "latest_version"])
        return {row["name"]: row["latest_version"] for row in rows}

    # --- 2.1 introspect ----------------------------------------------------------------------

    def introspect(self):
        if not self.env.su and not (
            self.env.user.has_group("odoo_dashboards_bi.group_onto_steward")
            or self.env.user.has_group("odoo_dashboards_bi.group_onto_bot")
        ):
            raise AccessError(_("Only ontology stewards and the Onto bot can inspect the full schema."))
        env = self.sudo().env
        group_xmlids = _xmlids(env, "res.groups")
        return {
            "odoo_version": release.series,
            "database_uuid": env["ir.config_parameter"].get_param("database.uuid"),
            "modules": [
                {"name": name, "version": version} for name, version in sorted(self._installed_modules(env).items())
            ],
            "companies": [{"id": c.id, "name": c.name} for c in env["res.company"].search([], order="id")],
            "warehouses": [
                {"id": w.id, "code": w.code, "name": w.name, "company_id": w.company_id.id}
                for w in env["stock.warehouse"].search([], order="id")
            ],
            "locations": self._location_rows(env),
            "groups": self._group_rows(env, group_xmlids),
            "users": self._user_rows(env, group_xmlids),
            "record_rules": self._rule_rows(env, group_xmlids),
            "models": self._model_rows(env, group_xmlids),
        }

    def _location_rows(self, env):
        # onto_role: the org map's role for the location (its own node, or the nearest mapped ancestor's).
        roles = {
            row["location_id"][0]: row["role"] or None
            for row in env["onto.org.location"].search_read([], ["location_id", "role"])
        }
        locations = env["stock.location"].with_context(active_test=False).search([], order="complete_name, id")
        return [
            {
                "id": loc.id,
                "complete_name": loc.complete_name,
                "usage": loc.usage,
                "warehouse_id": loc.warehouse_id.id or None,
                "company_id": loc.company_id.id or None,
                "active": loc.active,
                "onto_role": roles.get(loc.id),
            }
            for loc in locations
        ]

    def _group_rows(self, env, group_xmlids):
        rows = env["res.groups"].search_read([("id", "in", list(group_xmlids))], ["name", "category_id"])
        groups = [
            {
                "xmlid": group_xmlids[row["id"]],
                "name": row["name"],
                "category": row["category_id"][1] if row["category_id"] else None,
            }
            for row in rows
        ]
        return sorted(groups, key=lambda group: group["xmlid"])

    def _user_rows(self, env, group_xmlids):
        rows = env["res.users"].with_context(active_test=False).search_read(
            [], ["login", "active", "share", "groups_id", "company_ids"], order="id"
        )
        return [
            {
                "id": row["id"],
                "login": row["login"],
                "active": row["active"],
                "share": row["share"],
                "group_xmlids": sorted(group_xmlids[g] for g in row["groups_id"] if g in group_xmlids),
                "company_ids": row["company_ids"],
            }
            for row in rows
        ]

    def _rule_rows(self, env, group_xmlids):
        rules = env["ir.rule"].with_context(active_test=False).search([], order="id")
        return [
            {
                "id": rule.id,
                "name": rule.name,
                "model": rule.model_id.model,
                "global": not rule.groups,
                "group_xmlids": sorted(group_xmlids[g] for g in rule.groups.ids if g in group_xmlids),
                "domain": rule.domain_force or "",
                "active": rule.active,
                # The xmlid decides custom; the domain only labels multi_company/functional, so None is enough.
                "custom": rule_kind(rule, None) == "custom",
            }
            for rule in rules
        ]

    def _model_rows(self, env, group_xmlids):
        ir_models = [m for m in env["ir.model"].search([("transient", "=", False)], order="model")
                     if self._allowed_model(m.model)]
        rows = env["ir.model.fields"].search_read(
            [("model_id", "in", [m.id for m in ir_models])],
            ["model_id", "name", "field_description", "ttype", "relation", "store", "required", "readonly",
             "state", "groups"],
            order="name",
        )
        by_model = {}
        for row in rows:
            by_model.setdefault(row["model_id"][0], []).append(row)
        return [
            {
                "model": m.model,
                "name": m.name,
                "custom": m.state == "manual",
                "fields": [self._field_row(env, m.model, row, group_xmlids) for row in by_model.get(m.id, [])],
            }
            for m in ir_models
        ]

    def _field_row(self, env, model_name, row, group_xmlids):
        return {
            "name": row["name"],
            "string": row["field_description"],
            "type": row["ttype"],
            "relation": row["relation"] or None,
            "store": bool(row["store"]),
            "required": bool(row["required"]),
            "readonly": bool(row["readonly"]),
            "custom": row["state"] == "manual",
            "groups": ",".join(sorted(group_xmlids[g] for g in row["groups"] if g in group_xmlids)),
            "selection": _selection(env, model_name, row["name"], row["ttype"]),
        }

    # --- 2.2 profile -------------------------------------------------------------------------

    def profile(self, models=None):
        """Row counts, filled counts and facts. models: a list of at most 60 model names, or None for
        PROFILE_MODELS. Names that are not allowed or not installed are skipped. A malformed list
        raises IntrospectError."""
        if not self.env.su and not self.env.user.has_group("odoo_dashboards_bi.group_onto_steward"):
            raise AccessError(_("Only ontology stewards can profile the full database."))
        names = self._profile_names(models)
        self.env.flush_all()  # the aggregates must see the ORM's pending writes
        return {"models": [self._profile_model(name) for name in names], "facts": self._facts()}

    def _profile_names(self, models):
        if models is None:
            return [name for name in PROFILE_MODELS if self._allowed_model(name)]
        if not isinstance(models, list) or len(models) > PROFILE_LIMIT or not all(isinstance(m, str) for m in models):
            raise IntrospectError(422, "bad_models")
        return [name for name in dict.fromkeys(models) if self._allowed_model(name)]

    def _table_columns(self, table):
        """The column names the table really has: the only identifiers a profile may name."""
        self.env.cr.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        return {row[0] for row in self.env.cr.fetchall()}

    def _run_sql(self, query):
        """One row of an aggregate, in a read-only savepoint with the statement timeout (onto_query's pattern).
        Raw SQL: no ACL, record rule, company or active_test filter applies, so the counts cover every company
        and archived records (what Data Health gets from sudo with explicit company filters)."""
        cr = self.env.cr
        with read_only(cr):
            cr.execute("SET LOCAL statement_timeout = %s", (PROFILE_TIMEOUT_MS,))
            cr.execute(query)
            return cr.fetchone()

    def _profile_model(self, name):
        env = self.sudo().env
        table = env[name]._table
        stored = env["ir.model.fields"].search_read(
            [("model", "=", name), ("store", "=", True), ("ttype", "not in", NOT_STORED_TYPES)],
            ["name", "ttype"],
            order="name",
        )
        columns = self._table_columns(table)
        fields = [row for row in stored if row["name"] in columns]
        try:
            row = self._run_sql(_aggregate_sql(table, fields))
        except QueryCanceled:
            return {"model": name, "error": "timeout"}
        except psycopg2.Error:
            return {"model": name, "error": "failed"}
        return {"model": name, "row_count": row[0], "fields": _field_results(row, fields)}

    def _facts(self):
        env = self.sudo().env
        installed = self._installed_modules(env)
        history = {}
        for name, column in HISTORY_DATES.items():
            if not self._allowed_model(name):
                continue
            try:
                low, high = self._run_sql(sql.SQL("SELECT MIN({c})::date, MAX({c})::date FROM {t}").format(
                    c=sql.Identifier(column), t=sql.Identifier(env[name]._table)))
            except psycopg2.Error:
                continue  # no fact rather than a wrong one
            history[name] = (high - low).days if low and high else 0
        return {
            "lots_in_use": bool(env["stock.lot"].search([], limit=1)),
            "expiry_in_use": "use_expiration_date" in env["product.template"]._fields and bool(
                env["product.template"].search([("use_expiration_date", "=", True)], limit=1)),
            "pos_in_use": "point_of_sale" in installed and bool(env["pos.order"].search([], limit=1)),
            "invoicing_in_use": "account" in installed and bool(env["account.move"].search(
                [("move_type", "in", ("out_invoice", "in_invoice")), ("state", "=", "posted")], limit=1)),
            "qc_location_present": bool(env["onto.org.location"].search(
                [("role", "=", QC_ROLE), ("location_id.active", "=", True)], limit=1)),
            "mrp_installed": "mrp" in installed,
            "history_days": history,
            "currencies": sorted(set(env["res.company"].search([]).mapped("currency_id.name"))),
            "storable_products": env["product.product"].search_count([("active", "=", True), ("type", "=", "product")]),
            "warehouse_count": env["stock.warehouse"].search_count([("active", "=", True)]),
        }
