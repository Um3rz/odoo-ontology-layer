# -*- coding: utf-8 -*-
"""Onto result models: what querygen computes and what users act on.

Querygen writes these through /onto/v1/store/* as the bot (0.5). Field lists
here are the minimum each sub-phase needs; the sub-phase that freezes a
contract (4.2 status, 3.8 recommendations, 1.x forecasts) extends them.
"""

from odoo import fields, models

from .onto_scope import STOCK_RESULT_DIMS, WAREHOUSE_RESULT_DIMS

SEVERITIES = [
    ("info", "Info"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("critical", "Critical"),
]


class OntoStatus(models.Model):
    _name = "onto.status"
    _inherit = ["onto.mixin", "onto.scoped.mixin"]
    _onto_scope = {"fact": "stock.quant", "dims": STOCK_RESULT_DIMS}
    _description = "Onto stock status per product and location"
    _sql_constraints = [
        ("onto_status_product_location_uniq", "unique(product_id, location_id)",
         "One status per product and location."),
    ]

    product_id = fields.Many2one("product.product", required=True, index=True, ondelete="cascade")
    location_id = fields.Many2one("stock.location", required=True, index=True, ondelete="cascade")
    status = fields.Selection(
        [
            ("healthy", "Healthy"),
            ("low", "Low"),
            ("critical", "Critical"),
            ("out", "Out"),
            ("overstock", "Overstock"),
            ("slow", "Slow"),
            ("dead", "Dead"),
            ("near_expiry", "Near expiry"),
            ("expired", "Expired"),
        ],
        required=True,
    )
    refreshed_at = fields.Datetime()


class OntoForecast(models.Model):
    _name = "onto.forecast"
    _inherit = ["onto.mixin", "onto.scoped.mixin"]
    _onto_scope = {"fact": "stock.quant", "dims": STOCK_RESULT_DIMS}
    _description = "Onto forecast series (one row per series per run)"
    _sql_constraints = [
        ("onto_forecast_run_series_uniq", "unique(run_key, series_key)",
         "One forecast row per series per run."),
    ]

    run_key = fields.Char(required=True, index=True)
    series_key = fields.Char(required=True, index=True)
    product_id = fields.Many2one("product.product", index=True, ondelete="cascade")
    location_id = fields.Many2one("stock.location", index=True, ondelete="cascade")
    model_name = fields.Char()
    wape = fields.Float(digits=(16, 4))
    points = fields.Text(help="JSON list of forecast points.")
    quantiles = fields.Text(help="JSON map of quantile bands.")


class OntoKpiDaily(models.Model):
    _name = "onto.kpi.daily"
    _inherit = ["onto.mixin", "onto.scoped.mixin"]
    _onto_scope = {"fact": "stock.quant", "dims": WAREHOUSE_RESULT_DIMS}
    _description = "Onto daily KPI snapshot"

    day = fields.Date(required=True, index=True)
    kpi = fields.Char(required=True, index=True)
    warehouse_id = fields.Many2one("stock.warehouse", index=True, ondelete="cascade")
    value = fields.Float(digits=(16, 4))


class OntoAlert(models.Model):
    _name = "onto.alert"
    _inherit = ["onto.mixin", "onto.scoped.mixin"]
    _onto_scope = {"fact": "stock.quant", "dims": STOCK_RESULT_DIMS}
    _description = "Onto inventory alert"

    dedupe_key = fields.Char(required=True, index=True)
    severity = fields.Selection(SEVERITIES, required=True, default="medium")
    state = fields.Selection(
        [
            ("open", "Open"),
            ("acknowledged", "Acknowledged"),
            ("in_progress", "In progress"),
            ("resolved", "Resolved"),
            ("dismissed", "Dismissed"),
        ],
        required=True,
        default="open",
    )
    product_id = fields.Many2one("product.product", index=True, ondelete="cascade")
    location_id = fields.Many2one("stock.location", index=True, ondelete="cascade")
    description = fields.Text()


class OntoStockDaily(models.Model):
    _name = "onto.stock.daily"
    _inherit = ["onto.mixin", "onto.scoped.mixin"]
    _onto_scope = {"fact": "stock.quant", "dims": STOCK_RESULT_DIMS}
    _description = "Onto daily closing stock position (history)"
    _sql_constraints = [
        ("onto_stock_daily_key_uniq", "unique(product_id, location_id, date)",
         "One closing position per product, location and day."),
    ]

    product_id = fields.Many2one("product.product", required=True, index=True, ondelete="cascade")
    location_id = fields.Many2one("stock.location", required=True, index=True, ondelete="cascade")
    date = fields.Date(required=True, index=True)
    quantity = fields.Float(digits=(16, 4))
    value = fields.Float(digits=(16, 2))
