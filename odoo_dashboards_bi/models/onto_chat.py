# -*- coding: utf-8 -*-
"""Onto chat threads and messages. Customer content stays in Odoo (D37).

Written through /onto/v1/agent/record (contract 3.10) as the token's own user. Record
rules (security/onto_security.xml) show each user their own threads and messages.
"""

from odoo import fields, models


class OntoChatThread(models.Model):
    _name = "onto.chat.thread"
    _inherit = ["onto.mixin"]
    _description = "Onto chat thread"
    _order = "last_message_at desc, id desc"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, default=lambda self: self.env.user,
        ondelete="cascade",
    )
    company_ids = fields.Many2many("res.company", string="Companies")
    title = fields.Char()
    last_message_at = fields.Datetime()
    message_ids = fields.One2many("onto.chat.message", "thread_id")


class OntoChatMessage(models.Model):
    _name = "onto.chat.message"
    _inherit = ["onto.mixin"]
    _description = "Onto chat message"
    _order = "id"

    thread_id = fields.Many2one("onto.chat.thread", required=True, index=True, ondelete="cascade")
    role = fields.Selection([("user", "User"), ("assistant", "Assistant")], required=True)
    text = fields.Text()
    card = fields.Text(help="JSON answer card: figures, chart spec, trust badge, metric refs.")
    run_id = fields.Many2one("onto.agent.run", ondelete="set null")
