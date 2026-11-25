# -*- coding: utf-8 -*-
"""Managed Onto datasets (1.5, contract 3.2a): the dataset.saas rows /onto/v1/provision creates.

A managed dataset is a custom-query dataset named onto_<key> whose view is wad_onto_<key>_view. Its
dataset.saas row and its dataset.line.saas rows are locked: a write, create or unlink is refused unless
the provisioner runs inside managed_dataset_unlocked() (models/onto_provision.py). That flag is an
in-process contextvar, so no request and no context dict a client sends can set it. A name that slugs to
onto_* is reserved for Onto, so no other dataset can drop a managed view (D47).
odoo_dashboards_saas is reused unchanged (D26).
"""

import re
from contextvars import ContextVar

from odoo import _, api, fields, models
from odoo.exceptions import UserError

KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,49}")  # use fullmatch: a trailing newline is not a key
UNLOCKED = ContextVar("onto_managed_unlocked", default=False)  # set only by managed_dataset_unlocked()
MANAGED_MSG = "Onto manages this dataset. Change it through Onto's provisioning, not by hand."
RESERVED_MSG = "Names that start with onto_ are reserved for Onto's managed datasets."


def slug(name):
    """The slug saas builds a view name from (dataset.saas _view_table_name)."""
    return re.sub(r"[^0-9A-Za-z]", "_", name or "").lower()


def is_onto_name(name):
    return slug(name).startswith("onto_")


def dataset_name(key):
    return "onto_%s" % key


def view_name(key):
    return "wad_onto_%s_view" % key


class OntoDataset(models.Model):
    _inherit = "dataset.saas"

    onto_managed = fields.Boolean(default=False, copy=False, readonly=True)
    onto_release = fields.Char(copy=False, readonly=True)
    onto_object_key = fields.Char(copy=False, readonly=True, index=True)
    onto_spec_hash = fields.Char(copy=False, readonly=True)
    onto_engine_only = fields.Boolean(default=False, copy=False, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not UNLOCKED.get():
            for vals in vals_list:
                if vals.get("onto_managed") or is_onto_name(vals.get("name")):
                    raise UserError(_(RESERVED_MSG))
        return super().create(vals_list)

    def write(self, vals):
        if not UNLOCKED.get():
            if self.filtered("onto_managed"):
                raise UserError(_(MANAGED_MSG))
            if vals.get("onto_managed") or is_onto_name(vals.get("name")):
                raise UserError(_(RESERVED_MSG))
        return super().write(vals)

    def unlink(self):
        if not UNLOCKED.get() and self.filtered("onto_managed"):
            raise UserError(_(MANAGED_MSG))
        return super().unlink()


class OntoDatasetLine(models.Model):
    _inherit = "dataset.line.saas"

    @api.model_create_multi
    def create(self, vals_list):
        if not UNLOCKED.get():
            if any(self.env["dataset.saas"].browse(vals.get("dataset_id")).onto_managed for vals in vals_list):
                raise UserError(_(MANAGED_MSG))
        return super().create(vals_list)

    def write(self, vals):
        if not UNLOCKED.get() and self.filtered(lambda line: line.dataset_id.onto_managed):
            raise UserError(_(MANAGED_MSG))
        return super().write(vals)

    def unlink(self):
        if not UNLOCKED.get() and self.filtered(lambda line: line.dataset_id.onto_managed):
            raise UserError(_(MANAGED_MSG))
        return super().unlink()
