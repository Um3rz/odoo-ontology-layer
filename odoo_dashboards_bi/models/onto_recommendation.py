# -*- coding: utf-8 -*-
"""Recommendations (3.7, contract 3.8): what querygen proposes and a person decides.

The bot proposes through store/upsert, with the option lines nested. A person decides through approve, modify
or reject, as themselves, under Odoo's own ACL and record rules (the approver check). Every decision bumps
`version`; a stale version is refused, and nothing changes after a choice or a terminal state.
"""

import json
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from . import onto_actions
from .onto_recommendation_line import MAX_ACTION_OPTIONS, MAX_DO_NOTHING_OPTIONS
from .onto_scope import WAREHOUSE_RESULT_DIMS
from .onto_store import StoreError, _access, _clean, onto_now

UPSERT_FIELDS = ("warehouse_id", "product_id", "location_id", "shortage", "reason_code", "release_hash", "confidence")
ROW_KEYS = frozenset(UPSERT_FIELDS) | {"external_ref", "company_id", "state", "lines", "preferred_option_key"}
# Frozen to what the Inventory Agent skeleton writes (querygen app/agents/inventory). figures and score are not
# bot-writable: Odoo computes them. The activity note is free text and the skeleton never writes one.
LINE_KEYS = frozenset(("option_key", "line_type", "action_type", "params", "reason"))
REASON_MAX = 300  # the skeleton's reason cap


def _json_text(value):
    """A JSON object as the Text column keeps it (canonical text), or None. Anything else is a bad line."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StoreError(422, "bad_line")
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class OntoRecommendation(models.Model):
    _name = "onto.recommendation"
    _inherit = ["onto.mixin", "onto.scoped.mixin"]
    _onto_scope = {"fact": "stock.quant", "dims": WAREHOUSE_RESULT_DIMS}
    _description = "Onto recommendation"
    _sql_constraints = [
        ("onto_recommendation_external_ref_uniq", "unique(external_ref)",
         "A recommendation has one external reference."),
    ]

    external_ref = fields.Char(required=True, index=True)
    state = fields.Selection(
        [
            ("proposed", "Proposed"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("expired", "Expired"),
        ],
        required=True,
        default="proposed",
    )
    version = fields.Integer(default=1, required=True)
    warehouse_id = fields.Many2one("stock.warehouse", index=True, ondelete="cascade")
    location_id = fields.Many2one("stock.location", index=True, ondelete="cascade")
    product_id = fields.Many2one("product.product", index=True, ondelete="cascade")
    shortage = fields.Float(digits=(16, 4))
    reason_code = fields.Char()
    release_hash = fields.Char()
    confidence = fields.Float(digits=(16, 4))
    escalation_level = fields.Integer(default=0)
    requester_id = fields.Many2one("res.users", readonly=True)
    requested_at = fields.Datetime(readonly=True)
    option_ids = fields.One2many("onto.recommendation.line", "recommendation_id")
    preferred_line_id = fields.Many2one("onto.recommendation.line", ondelete="set null")
    chosen_line_id = fields.Many2one("onto.recommendation.line", ondelete="set null", readonly=True)
    chosen_by = fields.Many2one("res.users", readonly=True)
    chosen_at = fields.Datetime(readonly=True)
    decision_reason = fields.Text(readonly=True)

    def _onto_guard(self):
        """Only Onto's own paths write a recommendation: the bot's upsert and the decisions below."""
        if not (self.env.su or self.env.context.get("onto_write")):
            raise AccessError(_("Recommendations change only through their Onto actions."))

    def write(self, vals):
        self._onto_guard()
        return super().write(vals)

    # ----- proposals: the bot's store/upsert, with the option lines nested (3.8) -----

    @api.model
    def onto_upsert_nested(self, rows):
        """Each row is one proposal keyed by external_ref, with its option lines. Every row is checked before
        anything is written, then the writes run in one savepoint. A proposal already decided is refused (409
        locked); a re-upsert of a proposed one replaces its lines and bumps version."""
        _access(self, "create")
        _access(self, "write")
        plans, refs = [], set()
        for row in rows:
            plan = self._plan(row)
            if plan["ref"] in refs:
                raise StoreError(422, "duplicate_key")
            refs.add(plan["ref"])
            plans.append(plan)
        if not plans:
            return {"written": 0}
        try:
            with self.env.cr.savepoint():
                for plan in plans:
                    self._apply(plan)
        except ValidationError as exc:
            raise StoreError(422, "bad_line") from exc
        return {"written": len(plans)}

    def _plan(self, row):
        if not isinstance(row, dict) or not set(row) <= ROW_KEYS:
            raise StoreError(422, "bad_row")
        ref = row.get("external_ref")
        if not isinstance(ref, str) or not ref or len(ref) > 128:
            raise StoreError(422, "bad_row")
        if row.get("state", "proposed") != "proposed":
            raise StoreError(422, "bad_value")
        company_id = row.get("company_id", self.env.company.id)
        if type(company_id) is not int or company_id not in self.env.companies.ids:
            raise StoreError(422, "bad_company")
        values = {name: _clean(self.env, self._name, name, row.get(name)) for name in UPSERT_FIELDS}
        preferred = row.get("preferred_option_key")
        lines = self._plan_lines(row.get("lines"), preferred)
        self.env.cr.execute("SELECT id, company_id, state FROM onto_recommendation WHERE external_ref = %s", [ref])
        found = self.env.cr.fetchone()
        if found and (found[1] != company_id or found[2] != "proposed"):
            raise StoreError(409, "locked")
        return {
            "ref": ref,
            "id": found[0] if found else None,
            "company_id": company_id,
            "values": values,
            "preferred": preferred,
            "lines": lines,
        }

    def _plan_lines(self, raw_lines, preferred):
        if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= MAX_ACTION_OPTIONS + MAX_DO_NOTHING_OPTIONS:
            raise StoreError(422, "bad_line")
        plans, keys, actions = [], set(), 0
        for raw in raw_lines:
            plan = self._plan_line(raw)
            if plan["option_key"] in keys:
                raise StoreError(422, "bad_line")
            keys.add(plan["option_key"])
            if plan["line_type"] == "action":
                actions += 1
            plans.append(plan)
        if actions > MAX_ACTION_OPTIONS or len(plans) - actions > MAX_DO_NOTHING_OPTIONS:
            raise StoreError(422, "bad_line")
        if preferred is not None and (not isinstance(preferred, str) or preferred not in keys):
            raise StoreError(422, "bad_line")
        return plans

    def _plan_line(self, raw):
        if not isinstance(raw, dict) or not set(raw) <= LINE_KEYS:
            raise StoreError(422, "bad_line")
        key = raw.get("option_key")
        if not isinstance(key, str) or not key or len(key) > 64 or key.startswith("modified:"):
            raise StoreError(422, "bad_line")
        line_type = raw.get("line_type", "action")
        if line_type == "action":
            onto_actions.validate(raw.get("action_type"), raw.get("params"))
            if "note" in raw["params"]:  # free text: a proposal carries no note; an approver may add one by modifying
                raise StoreError(422, "bad_line")
            action_type, params = raw["action_type"], _json_text(raw["params"])
        elif line_type == "do_nothing":
            # The skeleton sends an empty params object on its do-nothing line; any real params are refused.
            if raw.get("action_type") is not None or raw.get("params") not in (None, {}):
                raise StoreError(422, "bad_line")
            action_type, params = None, None
        else:
            raise StoreError(422, "bad_line")
        reason = raw.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > REASON_MAX):
            raise StoreError(422, "bad_line")
        return {
            "option_key": key,
            "line_type": line_type,
            "action_type": action_type,
            "params": params,
            "reason": reason,
        }

    def _apply(self, plan):
        """Writes one checked proposal. Its lines are replaced; requester and requested_at stay as first written."""
        env = self.with_context(onto_write=True).env
        if plan["id"]:
            rec = env[self._name].browse(plan["id"])
            rec.option_ids.unlink()
            rec.write(dict(plan["values"], version=rec.version + 1))
        else:
            rec = env[self._name].create(
                dict(
                    plan["values"],
                    external_ref=plan["ref"],
                    company_id=plan["company_id"],
                    requester_id=self.env.uid,
                    requested_at=onto_now(),
                )
            )
        made = {}
        for line in plan["lines"]:
            made[line["option_key"]] = env["onto.recommendation.line"].create(
                dict(line, recommendation_id=rec.id, company_id=rec.company_id.id)
            )
        rec.write({"preferred_line_id": made[plan["preferred"]].id if plan["preferred"] else False})

    # ----- decisions: approve, modify, reject (3.8) -----

    def _decide_guard(self, version):
        """The approver check (3.8): the caller must be able to write this record under Odoo's own ACL and record
        rules (403), it must still be proposed (409 locked), and version must be the current one (409 stale)."""
        self.ensure_one()
        try:
            self.check_access_rights("write")
            self.check_access_rule("write")
        except AccessError as exc:
            raise StoreError(403, "forbidden") from exc
        if self.state != "proposed":
            raise StoreError(409, "locked")
        if type(version) is not int:
            raise StoreError(422, "bad_value")
        if version != self.version:
            raise StoreError(409, "stale")

    def _line_for(self, line_id):
        if line_id is None:
            line = self.preferred_line_id
        elif type(line_id) is int:
            line = self.option_ids.filtered(lambda option: option.id == line_id)
        else:
            raise StoreError(422, "bad_line")
        if not line:
            raise StoreError(422, "bad_line")
        return line

    def _audit(self, action, detail):
        self.env["onto.audit"].create(
            {
                "company_id": self.company_id.id,
                "action": action,
                "res_model": self._name,
                "res_id": self.id,
                "detail": detail,
            }
        )

    def _decision_payload(self):
        self.invalidate_recordset()
        return {
            "id": self.id,
            "external_ref": self.external_ref,
            "state": self.state,
            "version": self.version,
            "chosen_line_id": self.chosen_line_id.id or None,
        }

    def action_approve(self, version=None, line_id=None):
        """Approves one option (the preferred one when line_id is null) and carries it out as the caller. The audit
        row, the state change and the action are one savepoint: a refused action leaves the proposal untouched."""
        self._decide_guard(version)
        line = self._line_for(line_id)
        with self.env.cr.savepoint():
            self._audit("onto.recommendation.approve", "line=%d type=%s" % (line.id, line.line_type))
            rec = self.with_context(onto_write=True)
            rec.write(
                {
                    "state": "approved",
                    "chosen_line_id": line.id,
                    "chosen_by": self.env.uid,
                    "chosen_at": onto_now(),
                    "version": version + 1,
                }
            )
            if line.line_type == "action":
                onto_actions.execute(self.env, self, line)
            rec.write({"state": "done"})
        return self._decision_payload()

    def action_modify(self, version=None, line_id=None, changes=None):
        """Adds a modified copy of one action option, with allowlisted params changed. The original stays."""
        self._decide_guard(version)
        source = self._line_for(line_id)
        if source.line_type != "action":
            raise StoreError(422, "bad_line")
        patch = onto_actions.modification(source.action_type, changes)
        params = json.loads(source.params or "{}")
        params.update(patch)
        onto_actions.validate(source.action_type, params)
        with self.env.cr.savepoint():
            self._audit("onto.recommendation.modify", "line=%d fields=%s" % (source.id, ",".join(sorted(patch))))
            new = self.env["onto.recommendation.line"].with_context(onto_write=True).create(
                {
                    "recommendation_id": self.id,
                    "company_id": self.company_id.id,
                    "option_key": "modified:%s" % uuid.uuid4().hex,
                    "line_type": "action",
                    "action_type": source.action_type,
                    "params": _json_text(params),
                    "figures": source.figures,
                    "reason": source.reason,
                    "modified_from_id": source.id,
                }
            )
            new.option_key = "modified:%d" % new.id
            self.with_context(onto_write=True).write({"version": version + 1})
        payload = self._decision_payload()
        payload["line_id"] = new.id
        return payload

    def action_reject(self, version=None, reason=None):
        """Rejects the proposal with the approver's reason. Nothing is carried out."""
        self._decide_guard(version)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise StoreError(422, "bad_value")
        self._audit("onto.recommendation.reject", "decision=rejected")
        self.with_context(onto_write=True).write(
            {
                "state": "rejected",
                "decision_reason": reason.strip(),
                "chosen_by": self.env.uid,
                "chosen_at": onto_now(),
                "version": version + 1,
            }
        )
        return self._decision_payload()

    def onto_mark_escalated(self, level):
        """Records a higher escalation level. Idempotent and not a business change, so version stays (3.7)."""
        self.ensure_one()
        if type(level) is int and level > self.escalation_level:
            self.with_context(onto_write=True).write({"escalation_level": level})
        return self.escalation_level

    # ----- form button (the backend view; the sealed routes are the same methods) -----

    def onto_button_approve(self):
        """Approves the preferred option at the version the user was looking at (the view passes it in context)."""
        self._form_call(self.action_approve, version=self.env.context.get("onto_version"))

    def _form_call(self, method, **kwargs):
        try:
            method(**kwargs)
        except StoreError as exc:
            raise UserError(_("Not approved: %s") % (exc.message or exc.code)) from exc
