# -*- coding: utf-8 -*-
from odoo import api, models

from ..hooks import grant_onto_bot_groups


class ResUsersOntoBot(models.Model):
    _inherit = "res.users"

    @api.model
    def _onto_grant_bot_read_groups(self):
        """Called from data/onto_bot_groups.xml (not noupdate) on every install, update and -i."""
        grant_onto_bot_groups(self.env)
