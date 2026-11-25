# -*- coding: utf-8 -*-
"""Option lines of a recommendation (3.7, contract 3.8).

Lines are written only through onto.recommendation's own paths (the bot's nested upsert, and the approver's
modify): every other write, by anyone but superuser, is refused by the guard below.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .onto_scope import LINE_RESULT_DIMS

MAX_ACTION_OPTIONS = 3  # original action options; a modified copy is additional and points to its source
MAX_DO_NOTHING_OPTIONS = 1


class OntoRecommendationLine(models.Model):
    _name = "onto.recommendation.line"
    _inherit = ["onto.mixin", "onto.scoped.mixin"]
    _onto_scope = {"fact": "stock.quant", "dims": LINE_RESULT_DIMS}
    _description = "Onto recommendation option line"
    _sql_constraints = [
        ("onto_recommendation_line_option_uniq", "unique(recommendation_id, option_key)",
         "One line per option on a recommendation."),
    ]

    recommendation_id = fields.Many2one(
        "onto.recommendation", required=True, index=True, ondelete="cascade"
    )
    option_key = fields.Char(required=True)
    line_type = fields.Selection(
        [("action", "Action"), ("do_nothing", "Do nothing")], required=True, default="action"
    )
    action_type = fields.Char()
    params = fields.Text(help="JSON parameters of the option.")
    figures = fields.Text(help="JSON figures behind the option (quantities, costs, dates).")
    cost = fields.Float(digits=(16, 4))
    score = fields.Float(digits=(16, 4))
    reason = fields.Char()
    modified_from_id = fields.Many2one(
        "onto.recommendation.line", index=True, ondelete="set null", readonly=True
    )

    @api.constrains("recommendation_id", "line_type", "modified_from_id")
    def _check_option_limits(self):
        for line in self:
            if line.modified_from_id and line.line_type != "action":
                raise ValidationError(_("Only an action option can be modified."))
        for rec in self.mapped("recommendation_id"):
            originals = rec.option_ids.filtered(lambda option: not option.modified_from_id)
            actions = len(originals.filtered(lambda option: option.line_type == "action"))
            do_nothing = len(originals) - actions
            if actions > MAX_ACTION_OPTIONS or do_nothing > MAX_DO_NOTHING_OPTIONS:
                raise ValidationError(
                    _("A recommendation has at most three action options and one do-nothing option.")
                )

    def _onto_guard(self):
        if not (self.env.su or self.env.context.get("onto_write")):
            raise AccessError(_("Recommendation options change only through their Onto actions."))

    @api.model_create_multi
    def create(self, vals_list):
        self._onto_guard()
        return super().create(vals_list)

    def write(self, vals):
        self._onto_guard()
        return super().write(vals)

    def unlink(self):
        self._onto_guard()
        return super().unlink()
