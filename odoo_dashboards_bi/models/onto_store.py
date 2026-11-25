# -*- coding: utf-8 -*-
"""Onto storage: shared base, the time helper, the temporary branch mapping,
the append-only audit trail, and the settings models.

Result models live in onto_results.py; chat and agent-run records in onto_chat.py and onto_agent_run.py;
recommendations in onto_recommendation.py.
"""

import hashlib

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from psycopg2 import sql


def onto_now():
    """The one place Onto reads the clock (the simulation clock replaces it later)."""
    return fields.Datetime.now()


def user_signature(user):
    """Hash of a user's groups and companies: the unit parity is checked and suspended by (1.8). No names."""
    raw = "groups:%s|companies:%s" % (
        ",".join(str(i) for i in sorted(user.groups_id.ids)),
        ",".join(str(i) for i in sorted(user.company_ids.ids)),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def branches_of(user):
    """Warehouses (branches) a user's Onto results are projected onto. Temporary until OrgMap (1.3).

    The record rules in security/onto_security.xml use the same mapping."""
    return user.onto_branch_ids


def branch_of(location):
    """The branch (warehouse) a stock location belongs to, or an empty record."""
    return location.warehouse_id


class OntoMixin(models.AbstractModel):
    _name = "onto.mixin"
    _description = "Onto tenant-scoped record"

    company_id = fields.Many2one(
        "res.company", required=True, index=True, default=lambda self: self.env.company
    )


class ResUsers(models.Model):
    _inherit = "res.users"

    onto_branch_ids = fields.Many2many(
        "stock.warehouse",
        "onto_user_warehouse_rel",
        "user_id",
        "warehouse_id",
        string="Onto branches",
        help="Branches (warehouses) whose Onto results this user may read.",
    )


class OntoAudit(models.Model):
    _name = "onto.audit"
    _inherit = ["onto.mixin"]
    _description = "Onto audit trail (append-only)"
    _order = "id desc"

    occurred_at = fields.Datetime(default=lambda self: onto_now(), required=True, readonly=True)
    action = fields.Char(required=True, readonly=True)
    actor_id = fields.Many2one(
        "res.users", default=lambda self: self.env.user, required=True, readonly=True
    )
    res_model = fields.Char(readonly=True)
    res_id = fields.Integer(readonly=True)
    detail = fields.Text(
        readonly=True, help="Ids, counts and decisions only. Never business values."
    )

    def write(self, vals):
        if not self.env.context.get("onto_gc"):
            raise UserError(_("Onto audit rows are append-only."))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get("onto_gc"):
            raise UserError(_("Onto audit rows are append-only."))
        return super().unlink()


class OntoPolicy(models.Model):
    _name = "onto.policy"
    _inherit = ["onto.mixin"]
    _description = "Onto policy (scoped thresholds and option weights)"

    name = fields.Char(required=True)
    kind = fields.Selection(
        [("threshold", "Threshold"), ("option_weights", "Option weights")],
        required=True,
        default="threshold",
    )
    value = fields.Text(help="JSON payload. Querygen interprets it; Odoo stores it.")
    active = fields.Boolean(default=True)


class OntoRule(models.Model):
    _name = "onto.rule"
    _inherit = ["onto.mixin"]
    _description = "Onto inventory rule (compiled by querygen, evaluated by Odoo)"

    name = fields.Char(required=True)
    condition = fields.Text(help="JSON condition tree from the rules builder.")
    severity = fields.Selection(
        [
            ("info", "Info"),
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("critical", "Critical"),
        ],
        required=True,
        default="medium",
    )
    version = fields.Integer(default=1, required=True)
    active = fields.Boolean(default=True)


class OntoAuthority(models.Model):
    _name = "onto.authority"
    _inherit = ["onto.mixin"]
    _description = "Onto approval matrix row"

    action_type = fields.Selection(
        [
            ("transfer", "Internal transfer"),
            ("rfq", "Purchase request"),
            ("count", "Count"),
            ("adjustment", "Adjustment"),
            ("scrap", "Scrap"),
            ("orderpoint", "Reorder rule"),
            ("activity", "Activity"),
        ],
        required=True,
    )
    level = fields.Selection(
        [("1", "Level 1"), ("2", "Level 2"), ("3", "Level 3"), ("4", "Level 4")],
        required=True,
    )
    value_min = fields.Float(digits=(16, 2))
    value_max = fields.Float(digits=(16, 2))
    approver_group_id = fields.Many2one("res.groups")


class OntoRoute(models.Model):
    _name = "onto.route"
    _inherit = ["onto.mixin"]
    _description = "Onto alert routing rule"

    name = fields.Char(required=True)
    alert_type = fields.Char()
    warehouse_id = fields.Many2one("stock.warehouse")
    role_group_id = fields.Many2one("res.groups")


class OntoChannel(models.Model):
    _name = "onto.channel"
    _inherit = ["onto.mixin"]
    _description = "Onto notification channel setting"

    name = fields.Char(required=True)
    code = fields.Selection(
        [
            ("inbox", "Inbox"),
            ("activity", "Activity"),
            ("email", "Email"),
            ("push", "Push"),
        ],
        required=True,
    )
    enabled = fields.Boolean(default=True)


class OntoEvent(models.Model):
    _name = "onto.event"
    _inherit = ["onto.mixin"]
    _description = "Tenant calendar event (promotion, closure) used by forecasts"

    name = fields.Char(required=True)
    date_from = fields.Date(required=True)
    date_to = fields.Date()


# The company switches: each change is audited, and the agent setting route reads and sets them.
SWITCHES = ("ai_enabled", "agents_paused")


class OntoAgentSetting(models.Model):
    _name = "onto.agent.setting"
    _inherit = ["onto.mixin"]
    _description = "Onto agent settings (one per company)"
    _sql_constraints = [
        ("onto_agent_setting_company_uniq", "unique(company_id)", "One agent setting per company."),
    ]

    # Off until a steward switches AI on after reading what leaves Odoo (D29).
    ai_enabled = fields.Boolean(default=False, string="AI features enabled")
    # Kill switch (A4.6, 3.6): on refuses every agent run of the company. Querygen reads it with store/read.
    agents_paused = fields.Boolean(default=False, string="Agents paused (kill switch)")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            for flag in SWITCHES:
                if rec[flag]:
                    rec._onto_audit_switch(flag, False, True)
        return records

    def write(self, vals):
        flags = [flag for flag in SWITCHES if flag in vals]
        before = {rec.id: {flag: rec[flag] for flag in flags} for rec in self}
        res = super().write(vals)
        for rec in self:
            for flag in flags:
                if before[rec.id][flag] != rec[flag]:
                    rec._onto_audit_switch(flag, before[rec.id][flag], rec[flag])
        return res

    @api.model
    def onto_switches(self, changes):
        """The calling company's switches, read (empty changes, creates nothing) or set (creates the row on first
        use). write() audits each switch that actually changes."""
        company = self.env.company
        setting = self.search([("company_id", "=", company.id)], limit=1)
        if changes:
            setting = setting or self.create({"company_id": company.id})
            setting.write(changes)
        return {"company_id": company.id, **{flag: bool(setting[flag]) for flag in SWITCHES}}

    def _onto_audit_switch(self, flag, old, new):
        self.env["onto.audit"].create(
            {
                "company_id": self.company_id.id,
                "action": "onto.agent.%s" % flag,
                "res_model": self._name,
                "res_id": self.id,
                "detail": "%s %s -> %s" % (flag, old, new),
            }
        )


# ---------------------------------------------------------------------------
# Store routes (contract 3.3). Only the models and fields below cross these routes.
# ---------------------------------------------------------------------------

MAX_UPSERT_ROWS = 1000
MAX_READ_LIMIT = 200
READ_OPERATORS = ("=", "!=", "in", "not in", "<", "<=", ">", ">=")

# Readable fields per model. Audit is never served here. The agent setting (the kill switch, read by querygen),
# agent runs (read by the Agents screens; record rules limit each user's runs) and chat threads and messages
# (read by the Chat screen; record rules show each user their own) are read-only here.
STORE_FIELDS = {
    "onto.status": ("product_id", "location_id", "status", "refreshed_at"),
    "onto.forecast": (
        "run_key", "series_key", "product_id", "location_id", "model_name", "wape", "points", "quantiles",
    ),
    "onto.recommendation": (
        "external_ref", "state", "version", "product_id", "location_id", "warehouse_id", "shortage",
        "reason_code", "release_hash", "confidence", "escalation_level", "requester_id", "requested_at",
        "preferred_line_id", "chosen_line_id", "chosen_by", "chosen_at", "decision_reason",
    ),
    "onto.recommendation.line": (
        "recommendation_id", "option_key", "line_type", "action_type", "params", "figures", "cost", "score",
        "reason", "modified_from_id",
    ),
    "onto.stock.daily": ("product_id", "location_id", "date", "quantity", "value"),
    "onto.kpi.daily": ("day", "kpi", "warehouse_id", "value"),
    "onto.alert": ("dedupe_key", "severity", "state", "product_id", "location_id", "description"),
    "onto.agent.setting": ("company_id", "ai_enabled", "agents_paused"),
    "onto.agent.run": (
        "run_key", "kind", "trigger", "playbook", "state", "model_name", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens", "credits", "trace", "recommendation_ids",
    ),
    "onto.chat.thread": ("user_id", "title", "last_message_at", "create_date"),
    "onto.chat.message": ("thread_id", "role", "text", "card", "create_date"),
}
# Upsert key = the columns of the model's unique constraint (the ON CONFLICT target).
# Models without one (kpi.daily, alert) are read-only here until a phase adds a key.
# onto.recommendation is upserted with its option lines nested (onto_recommendation.py, 3.7); its lines have no key
# of their own here, so they can only be written inside a proposal.
# onto.stock.daily has no bot upsert: Odoo owns that table and only the stock-history writer (3.10,
# onto_stock_daily.py) writes it. It stays readable through store/read (STORE_FIELDS).
UPSERT_KEYS = {
    "onto.status": ("product_id", "location_id"),
    "onto.forecast": ("run_key", "series_key"),
    "onto.recommendation": ("external_ref",),
}
# model -> callable method names. Empty: no Onto action exists before the approval phases (6.3).
ACTION_METHODS = {}


class StoreError(Exception):
    """A refused store call. status and code are what the route answers; message is an optional safe text."""

    def __init__(self, status, code, message=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


def _access(model, operation):
    try:
        model.check_access_rights(operation)
    except AccessError:
        raise StoreError(403, "forbidden")


def _clean(env, model_name, name, value):
    """A JSON value as the column needs it. Raises StoreError for any value the field cannot hold."""
    if value is None:
        return None
    field = env[model_name]._fields[name]
    if field.type == "many2one":
        if type(value) is not int:
            raise StoreError(422, "bad_value")
        if not env[field.comodel_name].browse(value).exists():
            raise StoreError(422, "bad_reference")
        return value
    if field.type == "selection":
        if not isinstance(value, str) or value not in dict(field.selection):
            raise StoreError(422, "bad_value")
        return value
    if field.type == "integer":
        if type(value) is not int:
            raise StoreError(422, "bad_value")
        return value
    if field.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StoreError(422, "bad_value")
        return float(value)
    if field.type in ("char", "text"):
        if not isinstance(value, str):
            raise StoreError(422, "bad_value")
        return value
    if field.type in ("date", "datetime") and isinstance(value, str):
        try:
            parsed = fields.Date.to_date(value) if field.type == "date" else fields.Datetime.to_datetime(value)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None:
            return parsed
    raise StoreError(422, "bad_value")


def onto_upsert(env, model_name, key, rows):
    """Insert or update up to MAX_UPSERT_ROWS rows in one INSERT ... ON CONFLICT statement.

    Writes only rows whose allowlisted fields changed and that belong to the caller's
    companies. Returns how many rows were written, so a repeat of the same rows writes 0.
    Raw SQL, so write_uid and write_date are set here and the access check is ours."""
    if model_name not in UPSERT_KEYS:
        raise StoreError(422, "bad_model")
    if not isinstance(key, list) or not all(isinstance(name, str) for name in key) or sorted(key) != sorted(
        UPSERT_KEYS[model_name]
    ):
        raise StoreError(422, "bad_key")
    if not isinstance(rows, list) or len(rows) > MAX_UPSERT_ROWS:
        raise StoreError(422, "bad_rows")
    if model_name == "onto.recommendation":
        # Proposals carry their option lines nested and are locked after a decision (3.7).
        return env[model_name].onto_upsert_nested(rows)
    model = env[model_name]
    _access(model, "create")
    _access(model, "write")
    if not rows:
        return {"written": 0}

    allowed = set(STORE_FIELDS[model_name])
    columns = None
    seen = set()
    row_values = []
    for row in rows:
        if not isinstance(row, dict):
            raise StoreError(422, "bad_row")
        names = set(row) - {"company_id"}
        if not names <= allowed or not set(key) <= names:
            raise StoreError(422, "bad_row")
        if columns is None:
            columns = sorted(names)
        elif sorted(names) != columns:
            raise StoreError(422, "ragged_rows")
        company_id = row.get("company_id", env.company.id)
        if type(company_id) is not int or company_id not in env.companies.ids:
            raise StoreError(422, "bad_company")
        cleaned = {name: _clean(env, model_name, name, row[name]) for name in columns}
        key_value = tuple(cleaned[name] for name in key)
        if key_value in seen:
            raise StoreError(422, "duplicate_key")
        seen.add(key_value)
        row_values.append((cleaned, company_id))

    update_cols = [name for name in columns if name not in key]
    if not update_cols:
        raise StoreError(422, "bad_row")
    now, uid = onto_now(), env.uid
    insert_cols = columns + ["company_id", "create_uid", "create_date", "write_uid", "write_date"]
    params = []
    for cleaned, company_id in row_values:
        params.extend(cleaned[name] for name in columns)
        params.extend([company_id, uid, now, uid, now])
    table = sql.Identifier(model._table)
    placeholder_row = sql.SQL("(") + sql.SQL(", ").join([sql.Placeholder()] * len(insert_cols)) + sql.SQL(")")
    sets = [sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(name)) for name in update_cols]
    sets += [sql.SQL("write_uid = EXCLUDED.write_uid"), sql.SQL("write_date = EXCLUDED.write_date")]
    changed = sql.SQL(" OR ").join(
        sql.SQL("{t}.{c} IS DISTINCT FROM EXCLUDED.{c}").format(t=table, c=sql.Identifier(name))
        for name in update_cols
    )
    statement = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES {rows} ON CONFLICT ({key}) DO UPDATE SET {sets} "
        "WHERE {table}.company_id = EXCLUDED.company_id AND ({changed}) RETURNING id"
    ).format(
        table=table,
        cols=sql.SQL(", ").join(sql.Identifier(name) for name in insert_cols),
        rows=sql.SQL(", ").join([placeholder_row] * len(row_values)),
        key=sql.SQL(", ").join(sql.Identifier(name) for name in key),
        sets=sql.SQL(", ").join(sets),
        changed=changed,
    )
    env.cr.execute(statement, params)
    written = len(env.cr.fetchall())
    model.invalidate_model()
    return {"written": written}


def _scalar(value):
    return value is None or isinstance(value, (str, int, float))


def _domain_ok(domain, allowed):
    for leaf in domain:
        if isinstance(leaf, str):
            if leaf not in ("&", "|", "!"):
                return False
            continue
        if not isinstance(leaf, list) or len(leaf) != 3:
            return False
        name, operator, value = leaf
        if name not in allowed or operator not in READ_OPERATORS:
            return False
        if operator in ("in", "not in"):
            if not isinstance(value, list) or not all(_scalar(item) for item in value):
                return False
        elif not _scalar(value):
            return False
    return True


def onto_read(env, model_name, domain, read_fields, order, limit, offset, caller="sealed"):
    """search_read as the token's user: ACL, record rules and the company set all apply.

    Chat is the person's own: a bot token (caller "bot") is refused it, since bot runs never read user chats."""
    if model_name not in STORE_FIELDS:
        raise StoreError(422, "bad_model")
    if caller == "bot" and model_name in ("onto.chat.thread", "onto.chat.message"):
        raise StoreError(403, "forbidden")
    allowed = set(STORE_FIELDS[model_name]) | {"id", "company_id", "write_date"}
    if not isinstance(read_fields, list) or not read_fields or not set(read_fields) <= allowed:
        raise StoreError(422, "bad_fields")
    if not isinstance(domain, list) or not _domain_ok(domain, allowed):
        raise StoreError(422, "bad_domain")
    if order is not None:
        parts = order.split() if isinstance(order, str) else []
        if not parts or len(parts) > 2 or parts[0] not in allowed:
            raise StoreError(422, "bad_order")
        if len(parts) == 2 and parts[1].lower() not in ("asc", "desc"):
            raise StoreError(422, "bad_order")
        order = " ".join(parts)
    if type(limit) is not int or not 1 <= limit <= MAX_READ_LIMIT or type(offset) is not int or offset < 0:
        raise StoreError(422, "bad_page")
    try:
        return env[model_name].search_read(domain, read_fields, offset=offset, limit=limit, order=order)
    except AccessError:
        raise StoreError(403, "forbidden")
    except (ValueError, TypeError, UserError):
        raise StoreError(422, "bad_domain")


def onto_action(env, model_name, rec_id, method, args):
    """Calls an allowlisted method on one record as the caller. Nothing is allowlisted yet."""
    if method not in ACTION_METHODS.get(model_name, ()):
        raise StoreError(422, "method_not_allowed")
    if type(rec_id) is not int or not isinstance(args, dict):
        raise StoreError(422, "bad_args")
    record = env[model_name].browse(rec_id).exists()
    if not record:
        raise StoreError(422, "bad_record")
    return getattr(record, method)(**args)
