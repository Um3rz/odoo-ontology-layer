# -*- coding: utf-8 -*-
"""Data Health (2.6, D51): ten checks over products, stock and locations, computed in Odoo. Three reconciliation
checks (2.4, D53) compare stock value, on-hand quantities and revenue with the books.

run_checks() runs every check for each company under one run_id. For each check and company it replaces the
stored row, and it stores a row only when the check found something (a check that raises is stored as info,
"check failed"). Checks run as superuser with explicit company filters, so the counts describe the company,
not the reader. Stewards and stock managers read the rows through the company rule.

Only counts and record ids are stored. Messages are fixed text, except the reconciliation messages (D53), which
carry aggregate totals only. No record value reaches a row, a message or a fix link. Querygen reads these rows
through /onto/v1/health.
"""

import json
import logging
import uuid
from datetime import timedelta

from odoo import fields, models

from .onto_store import onto_now

_logger = logging.getLogger(__name__)

SAMPLE_LIMIT = 20
REORDER_WINDOW_DAYS = 90
STALE_DAYS = 180
FAILED = "check failed"
LAST_RUN_KEY = "onto.health.last_run.%d"  # ir.config_parameter: the latest run of one company, as JSON
SEVERITY_RANK = {"blocking": 0, "warning": 1, "info": 2}
RECONCILE_TOLERANCE_KEY = "onto.reconcile.tolerance"  # ir.config_parameter: a fraction of the larger total (D53)
RECONCILE_TOLERANCE_DEFAULT = 0.01
RECONCILE_FLOOR = 0.01  # absolute tolerance floor, in currency or units

# key -> (severity, model, default message). A reconciliation check returns its own message, with totals only.
CHECKS = {
    "product_no_supplier": ("warning", "product.template",
                            "Purchasable storable products have no vendor, so they cannot be ordered with a price."),
    "product_no_cost": ("warning", "product.product", "Storable products have a cost of zero."),
    "product_no_lead_time": ("info", "product.supplierinfo", "Vendor prices have a lead time of zero days."),
    "product_no_category": ("info", "product.template", "Products sit in the default All category."),
    "product_no_reorder_rule": ("info", "product.product",
                                "Storable products moved in the last 90 days have no minimum stock rule."),
    "product_duplicate": ("warning", "product.product", "Active products share an internal reference or barcode."),
    "uom_mismatch": ("warning", "product.template",
                     "The purchase unit of measure is in a different category from the stock unit."),
    "stale_count": ("warning", "stock.quant",
                    "Stock in internal locations has not been counted for more than 180 days."),
    "fefo_off": ("info", "product.template",
                 "Lot-tracked products with expiry dates do not use first-expired, first-out removal."),
    "unmapped_stock_location": ("warning", "stock.location",
                                "Internal locations holding stock have no role in the org map."),
    "reconcile_stock_value": ("warning", "stock.valuation.layer",
                              "Quant value in valued locations differs from the valuation layers."),
    "reconcile_on_hand": ("warning", "stock.quant",
                          "Quantities in internal locations differ from the net of done stock moves."),
    "reconcile_revenue_gl": ("warning", "account.move",
                             "Sales of the last full month differ from the income accounts."),
}


def _scan(model, domain):
    """(count, sample ids) of one domain. The sample is the newest records first, so it shows the latest findings."""
    return model.search_count(domain), model.search(domain, order="id desc", limit=SAMPLE_LIMIT).ids


def _sql_scan(cr, select, params):
    """(count, sample ids) of one SELECT of record ids, the same way as _scan."""
    cr.execute("SELECT count(*) FROM (%s) q" % select, params)
    count = cr.fetchone()[0]
    cr.execute("SELECT q.id FROM (%s) q ORDER BY q.id DESC LIMIT %d" % (select, SAMPLE_LIMIT), params)
    return count, [row[0] for row in cr.fetchall()]


def last_full_month(today):
    """(first day, last day) of the calendar month before `today`."""
    last = today.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


class OntoHealth(models.Model):
    _name = "onto.health"
    _inherit = ["onto.mixin"]
    _description = "Onto Data Health result: one row per check and company that has findings"

    check_key = fields.Char(required=True, index=True)
    severity = fields.Selection([("info", "Info"), ("warning", "Warning"), ("blocking", "Blocking")], required=True)
    model = fields.Char(required=True)
    count = fields.Integer(required=True, default=0)
    sample_ids = fields.Text(help="JSON list of up to 20 record ids, newest first.")
    message = fields.Char(required=True)
    computed_at = fields.Datetime(required=True)
    run_id = fields.Char(required=True, index=True)

    # ---- the checks: each returns (count, sample ids) for one company ----

    def _check_product_no_supplier(self, company):
        return _scan(self.env["product.template"], [
            ("active", "=", True), ("type", "=", "product"), ("purchase_ok", "=", True),
            ("seller_ids", "=", False), ("company_id", "in", [company.id, False]),
        ])

    def _check_product_no_cost(self, company):
        # standard_price is company-dependent: read it in the company's context.
        return _scan(self.env["product.product"].with_company(company), [
            ("active", "=", True), ("type", "=", "product"), ("standard_price", "=", 0),
            ("product_tmpl_id.company_id", "in", [company.id, False]),
        ])

    def _check_product_no_lead_time(self, company):
        return _scan(self.env["product.supplierinfo"], [
            ("delay", "=", 0), ("company_id", "in", [company.id, False]),
        ])

    def _check_product_no_category(self, company):
        category = self.env.ref("product.product_category_all", raise_if_not_found=False)
        if not category:
            return 0, []
        return _scan(self.env["product.template"], [
            ("active", "=", True), ("categ_id", "=", category.id), ("company_id", "in", [company.id, False]),
        ])

    def _check_product_no_reorder_rule(self, company):
        since = onto_now() - timedelta(days=REORDER_WINDOW_DAYS)
        return _sql_scan(self.env.cr, """
            SELECT pp.id
            FROM product_product pp
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE pp.active AND pt.type = 'product'
              AND (pt.company_id = %(company)s OR pt.company_id IS NULL)
              AND EXISTS (SELECT 1 FROM stock_move sm
                          WHERE sm.product_id = pp.id AND sm.company_id = %(company)s AND sm.date >= %(since)s)
              AND NOT EXISTS (SELECT 1 FROM stock_warehouse_orderpoint op
                              WHERE op.product_id = pp.id AND op.company_id = %(company)s)
        """, {"company": company.id, "since": since})

    def _check_product_duplicate(self, company):
        return _sql_scan(self.env.cr, """
            WITH scoped AS (
                SELECT pp.id, pp.default_code, pp.barcode
                FROM product_product pp
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                WHERE pp.active AND (pt.company_id = %(company)s OR pt.company_id IS NULL)
            ),
            shared_codes AS (
                SELECT default_code FROM scoped WHERE default_code <> '' GROUP BY default_code HAVING count(*) > 1
            ),
            shared_barcodes AS (
                SELECT barcode FROM scoped WHERE barcode <> '' GROUP BY barcode HAVING count(*) > 1
            )
            SELECT s.id FROM scoped s
            WHERE s.default_code IN (SELECT default_code FROM shared_codes)
               OR s.barcode IN (SELECT barcode FROM shared_barcodes)
        """, {"company": company.id})

    def _check_uom_mismatch(self, company):
        return _sql_scan(self.env.cr, """
            SELECT pt.id
            FROM product_template pt
            JOIN uom_uom stock_uom ON stock_uom.id = pt.uom_id
            JOIN uom_uom purchase_uom ON purchase_uom.id = pt.uom_po_id
            WHERE pt.active AND (pt.company_id = %(company)s OR pt.company_id IS NULL)
              AND stock_uom.category_id IS DISTINCT FROM purchase_uom.category_id
        """, {"company": company.id})

    def _check_stale_count(self, company):
        # inventory_date on a quant is the next scheduled count in Odoo 16, so the last count comes from the
        # location (last_inventory_date). A location never counted is measured from when its stock appeared.
        cutoff = onto_now().date() - timedelta(days=STALE_DAYS)
        return _sql_scan(self.env.cr, """
            SELECT q.id
            FROM stock_quant q
            JOIN stock_location l ON l.id = q.location_id
            WHERE q.company_id = %(company)s AND l.usage = 'internal' AND q.quantity > 0
              AND COALESCE(l.last_inventory_date, q.create_date::date) < %(cutoff)s
        """, {"company": company.id, "cutoff": cutoff})

    def _check_fefo_off(self, company):
        if "use_expiration_date" not in self.env["product.template"]._fields:
            return 0, []  # product_expiry is not installed, so no product has expiry dates
        # ponytail: reads the category's own removal strategy only; a location's strategy (used when the category
        # has none) is not read. Upgrade: resolve the location chain as stock_quant._get_removal_strategy does.
        return _sql_scan(self.env.cr, """
            SELECT pt.id
            FROM product_template pt
            LEFT JOIN product_category pc ON pc.id = pt.categ_id
            LEFT JOIN product_removal pr ON pr.id = pc.removal_strategy_id
            WHERE pt.active AND pt.tracking = 'lot' AND pt.use_expiration_date
              AND (pt.company_id = %(company)s OR pt.company_id IS NULL)
              AND COALESCE(pr.method, 'fifo') <> 'fefo'
        """, {"company": company.id})

    def _check_unmapped_stock_location(self, company):
        stocked = self.env["stock.quant"].read_group(
            [("company_id", "=", company.id), ("quantity", ">", 0), ("location_id.usage", "=", "internal")],
            ["quantity:sum"], ["location_id"], lazy=False,
        )
        location_ids = [group["location_id"][0] for group in stocked if group["location_id"]]
        if not location_ids:
            return 0, []
        # A NULL role is unmapped (D43). onto.org.location holds the role of every stock location.
        return _scan(self.env["onto.org.location"], [
            ("location_id", "in", location_ids), ("role", "=", False),
        ])

    # ---- reconciliation (2.4, D53): totals per company; a gap beyond the tolerance is one warning row ----

    def _check_reconcile_stock_value(self, company):
        if "stock.valuation.layer" not in self.env:  # stock_account is not installed, so there are no layers
            return 0, []
        quant_value, layer_value = self._totals_stock_value(company)
        return self._reconcile_gap(quant_value, layer_value, "Quant value", "valuation layers")

    def _check_reconcile_on_hand(self, company):
        quantity, net_moves = self._totals_on_hand(company)
        return self._reconcile_gap(quantity, net_moves, "Quantity in internal locations", "done moves net")

    def _check_reconcile_revenue_gl(self, company):
        if "pos.order" not in self.env or "account.move" not in self.env:  # D53: both modules are needed
            return 0, []
        first, last = last_full_month(onto_now().date())
        sales, income = self._totals_revenue_gl(company, first, last)
        return self._reconcile_gap(sales, income, "Sales for %s" % first.strftime("%Y-%m"), "income accounts")

    def _reconcile_gap(self, left, right, left_label, right_label):
        """(0, []) when the totals agree within the tolerance, else (1, [], message) with the gap in percent.

        The tolerance is onto.reconcile.tolerance, a fraction of the larger total (default 1%), and never less than
        the absolute floor."""
        rate = float(self.env["ir.config_parameter"].sudo().get_param(RECONCILE_TOLERANCE_KEY)
                     or RECONCILE_TOLERANCE_DEFAULT)
        scale, gap = max(abs(left), abs(right)), abs(left - right)
        if gap <= max(rate * scale, RECONCILE_FLOOR):
            return 0, []
        return 1, [], "%s %.2f against %s %.2f: a gap of %.2f%% of the larger total." % (
            left_label, left, right_label, right, gap / scale * 100)

    def _totals_stock_value(self, company):
        """(quant value, valuation layer value) of one company. Quants count in the locations Odoo values (internal,
        or transit of a company, as stock_account's _should_be_valued) at the company's standard cost. standard_price
        is company-dependent, so it is read in the company's context, as product_no_cost does."""
        cr = self.env.cr
        cr.execute("""
            SELECT q.product_id, SUM(q.quantity)
            FROM stock_quant q
            JOIN stock_location l ON l.id = q.location_id
            WHERE q.company_id = %(company)s
              AND (l.usage = 'internal' OR (l.usage = 'transit' AND l.company_id IS NOT NULL))
            GROUP BY q.product_id
        """, {"company": company.id})
        quantities = dict(cr.fetchall())
        products = self.env["product.product"].with_company(company).browse(list(quantities))
        value = sum(quantities[product.id] * product.standard_price for product in products)
        cr.execute("SELECT COALESCE(SUM(value), 0) FROM stock_valuation_layer WHERE company_id = %s", (company.id,))
        return float(value), float(cr.fetchone()[0])

    def _totals_on_hand(self, company):
        """(quantity in internal locations, net of done moves across the internal boundary) of one company, both in
        the product UoM. A line counts +qty when it lands in an internal location and -qty when it leaves one, so a
        move between two internal locations nets to zero. Consumables have no quants and are left out."""
        cr = self.env.cr
        cr.execute("""
            SELECT COALESCE(SUM(q.quantity), 0)
            FROM stock_quant q
            JOIN stock_location l ON l.id = q.location_id
            WHERE q.company_id = %(company)s AND l.usage = 'internal'
        """, {"company": company.id})
        quantity = cr.fetchone()[0]
        cr.execute("""
            SELECT COALESCE(SUM(ml.qty_done / lu.factor * pu.factor
                                * ((dst.usage = 'internal')::int - (src.usage = 'internal')::int)), 0)
            FROM stock_move_line ml
            JOIN stock_location src ON src.id = ml.location_id
            JOIN stock_location dst ON dst.id = ml.location_dest_id
            JOIN product_product pp ON pp.id = ml.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id AND pt.type = 'product'
            JOIN uom_uom pu ON pu.id = pt.uom_id
            JOIN uom_uom lu ON lu.id = ml.product_uom_id
            WHERE ml.company_id = %(company)s AND ml.state = 'done'
        """, {"company": company.id})
        return float(quantity), float(cr.fetchone()[0])

    def _totals_revenue_gl(self, company, first, last):
        """(sales, income) of one company for the dates first..last (one calendar month, D53).

        Sales are the untaxed revenue Odoo books in the period, without double counting:
        - POS: the lines of the orders that a posted session journal entry (pos.session.move_id) books and that are
          dated in the period. The entry is dated when the session closes, so POS sales follow that date, not the
          order date. Odoo books no session sales for an invoiced order (pos_session._accumulate_amounts): its
          revenue sits in the customer invoice. The POS side therefore leaves invoiced orders out, and the invoice
          side counts them. A session with no posted entry yet is in neither side.
        - Invoices: product lines of posted out_invoice moves, less those of posted out_refund moves, dated in the period.

        Income is the balance of the 'income' accounts (not income_other) on posted lines dated in the period, sign
        flipped. It includes every journal entry, so income booked by hand with no sale behind it shows as a gap.
        Amounts are in document currency, so the check is exact for invoices in the company currency."""
        cr = self.env.cr
        cr.execute("""
            SELECT
                (SELECT COALESCE(SUM(pol.price_subtotal), 0)
                 FROM pos_order_line pol
                 JOIN pos_order po ON po.id = pol.order_id
                 JOIN pos_session ps ON ps.id = po.session_id
                 JOIN account_move sm ON sm.id = ps.move_id
                 WHERE po.company_id = %(company)s AND po.account_move IS NULL
                   AND sm.state = 'posted' AND sm.date BETWEEN %(first)s AND %(last)s)
                +
                (SELECT COALESCE(SUM(CASE WHEN am.move_type = 'out_refund' THEN -aml.price_subtotal
                                          ELSE aml.price_subtotal END), 0)
                 FROM account_move_line aml
                 JOIN account_move am ON am.id = aml.move_id
                 WHERE am.company_id = %(company)s AND am.move_type IN ('out_invoice', 'out_refund')
                   AND am.state = 'posted' AND aml.display_type = 'product'
                   AND am.date BETWEEN %(first)s AND %(last)s),
                (SELECT COALESCE(-SUM(aml.balance), 0)
                 FROM account_move_line aml
                 JOIN account_move am ON am.id = aml.move_id
                 JOIN account_account aa ON aa.id = aml.account_id
                 WHERE am.company_id = %(company)s AND am.state = 'posted' AND aa.account_type = 'income'
                   AND am.date BETWEEN %(first)s AND %(last)s)
        """, {"company": company.id, "first": first, "last": last})
        sales, income = cr.fetchone()
        return float(sales), float(income)

    # ---- the run and the reply ----

    def run_checks(self, companies=None):
        """Every check for each company (every company when None: the daily cron) under one run_id. A check that
        raises is stored as info "check failed"; the run goes on."""
        self.env.flush_all()  # the SQL checks read the database, so pending ORM writes go first
        Health = self.sudo()
        if companies is None:
            companies = self.env["res.company"].sudo().search([])
        run_id, computed_at = uuid.uuid4().hex, onto_now()
        for company in companies:
            for key, (severity, model, default) in CHECKS.items():
                try:
                    with self.env.cr.savepoint():
                        found = getattr(Health, "_check_" + key)(company)
                    count, sample = found[0], found[1]
                    message = found[2] if len(found) > 2 else default  # a reconciliation check words its own gap
                    row = dict(severity=severity, message=message, count=count, sample=sample) if count else None
                except Exception as exc:  # noqa: BLE001 - one failing check must not stop the run
                    _logger.warning("Onto health check %s failed for company %s: %s",
                                    key, company.id, type(exc).__name__)
                    row = dict(severity="info", message=FAILED, count=0, sample=[])
                Health.search([("check_key", "=", key), ("company_id", "=", company.id)]).unlink()
                if row:
                    Health.create({
                        "check_key": key, "severity": row["severity"], "model": model,
                        "company_id": company.id, "count": row["count"], "sample_ids": json.dumps(row["sample"]),
                        "message": row["message"], "computed_at": computed_at, "run_id": run_id,
                    })

        stamp = json.dumps({"run_id": run_id, "computed_at": fields.Datetime.to_string(computed_at)})
        params = self.env["ir.config_parameter"].sudo()
        for company in companies:
            params.set_param(LAST_RUN_KEY % company.id, stamp)

    def payload(self, check_key=None):
        """The reply of /onto/v1/health and /run for the caller's companies (env.companies): their rows, most severe
        first. run_id and computed_at come from the latest run stamp of those companies, so a clean run reports when
        it ran even though it stored no rows."""
        companies = self.env.companies
        domain = [("company_id", "in", companies.ids)]
        if check_key:
            domain.append(("check_key", "=", check_key))
        rows = self.search(domain)
        params = self.env["ir.config_parameter"].sudo()
        stamps = []
        for company in companies:
            raw = params.get_param(LAST_RUN_KEY % company.id)
            if raw:
                stamps.append(json.loads(raw))
        latest = max(stamps, key=lambda stamp: stamp["computed_at"], default=None)
        checks = []
        for row in sorted(rows, key=lambda r: (SEVERITY_RANK[r.severity], r.check_key)):
            sample = json.loads(row.sample_ids or "[]")
            checks.append({
                "check_key": row.check_key,
                "severity": row.severity,
                "model": row.model,
                "company_id": row.company_id.id,
                "count": row.count,
                "sample_ids": sample,
                "message": row.message,
                "fix_url": "/web#model=%s&view_type=form&id=%d" % (row.model, sample[0]) if sample else None,
            })
        return {
            "run_id": latest["run_id"] if latest else None,
            "computed_at": latest["computed_at"] if latest else None,
            "checks": checks,
        }
