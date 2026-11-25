# -*- coding: utf-8 -*-
"""Stock history (3.10): the closing quantity and value of each product x internal location, one row per day.

onto.stock.daily is declared in onto_results.py (table onto_stock_daily, the table the "Stock history" template
binds to). This module adds the writer: only pairs with a done move on a day get a row that day. Stock at any
date is the latest row on or before it. Quantities come from done move lines, the same source as stock at any
date. Values come from stock valuation layers, through the binding below.

Days are tenant-local (the company partner's timezone, as in onto_query). One day is one committed transaction
under a statement timeout, and the cursor (ir.config_parameter) is the last day written. The same code runs the
backfill, the nightly catch-up and a resume after a timeout.

ponytail: a done move dated on a day the cursor has already passed is not picked up; a rebuild would be needed.
ponytail: a pair without moves keeps the unit cost of its last row until it moves again (its value is not re-priced daily).
"""

import logging
import time
from datetime import date, datetime, timedelta, time as dtime

import pytz

from odoo import _, api, models
from odoo.release import version_info

from .onto_query import DEFAULT_TZ
from .onto_store import onto_now

_logger = logging.getLogger(__name__)

# Ontology bindings for the valuation, keyed by Odoo major version (plan 1.1). Only these names reach the queries.
# _valuation_binding() checks them against the registry, so a missing stock_account turns values off instead of failing.
BINDINGS = {
    16: {
        "valuation": {
            "model": "stock.valuation.layer", "product": "product_id", "company": "company_id",
            "quantity": "quantity", "value": "value", "date": "stock_move_id.date",
        },
    },
}
LAST_DAY_KEY = "onto.stock_daily.last_day"  # ir.config_parameter: last day whose rows are written (ISO date)
LAST_SUCCESS_KEY = "onto.stock_daily.last_success"  # ir.config_parameter: when that day was written (UTC, ISO)
STEP_BUDGET_SECONDS = 20  # one run's time budget: the 30 s tick budget (plan 6.1) less margin
STATEMENT_TIMEOUT_MS = 10000  # one day's transaction
LOCK_ID = 3103010  # pg_advisory_xact_lock: two writers never write days at the same time
UPSERT_CHUNK = 500


def _zone(env):
    name = env.company.sudo().partner_id.tz or DEFAULT_TZ
    try:
        return pytz.timezone(name)
    except pytz.UnknownTimeZoneError:
        return pytz.timezone(DEFAULT_TZ)


def _local_day(zone, utc_naive):
    return pytz.utc.localize(utc_naive).astimezone(zone).date()


def _day_bounds(zone, day):
    """[start, end) of a tenant-local day as naive UTC datetimes (the form the database stores)."""
    def at(local_day):
        return zone.localize(datetime.combine(local_day, dtime())).astimezone(pytz.utc).replace(tzinfo=None)
    return at(day), at(day + timedelta(days=1))


def _valuation_binding(env):
    """The valuation binding of this Odoo version, or None when its model or one of its fields is missing."""
    binding = BINDINGS.get(version_info[0], {}).get("valuation")
    if not binding or binding["model"] not in env:
        return None
    fields = env[binding["model"]]._fields
    names = [binding["product"], binding["company"], binding["quantity"], binding["value"],
             binding["date"].split(".")[0]]
    return binding if all(name in fields for name in names) else None


class OntoStockDaily(models.Model):
    _inherit = "onto.stock.daily"

    @api.model
    def _onto_stock_daily_cron(self):
        """Inactive cron (data/onto_cron.xml), superseded by onto_tick (3.3). Kept so its code still resolves."""
        self._onto_run_days(STEP_BUDGET_SECONDS)

    @api.model
    def action_backfill_stock_history(self):
        """Server action (steward). Writes from the first done move up to yesterday, as far as one budget allows."""
        result = self._onto_run_days(STEP_BUDGET_SECONDS)
        if result["left"]:
            self.env.ref("odoo_dashboards_bi.ir_cron_onto_tick")._trigger()  # onto_tick carries on (3.3)
        message = _("%(days)d day(s) written, %(rows)d row(s), %(left)d day(s) left.") % result
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Stock history"), "message": message, "sticky": False,
                       "type": "warning" if result["left"] else "success"},
        }

    @api.model
    def _onto_run_days(self, budget):
        """Write each day after the cursor, up to yesterday, one committed day at a time, until the budget is spent.
        Returns the days written, the rows written and the days left. The caller runs it again while days are left
        (onto_tick, 3.3)."""
        self = self.sudo()
        started = time.monotonic()
        params = self.env["ir.config_parameter"].sudo()
        zone = _zone(self.env)
        yesterday = _local_day(zone, onto_now()) - timedelta(days=1)
        cursor = params.get_param(LAST_DAY_KEY)
        if cursor:
            day = date.fromisoformat(cursor) + timedelta(days=1)
        else:  # backfill: the first day with a done move (nothing to do when there is none)
            first = self._first_done_day(zone)
            day = first or yesterday + timedelta(days=1)
        days = rows = 0
        while day <= yesterday and time.monotonic() - started < budget:
            rows += self._snapshot_day(day, zone)
            params.set_param(LAST_DAY_KEY, day.isoformat())
            params.set_param(LAST_SUCCESS_KEY, onto_now().isoformat(timespec="seconds"))
            self.env.cr.commit()  # one day is one unit: a later timeout keeps this day and loses nothing
            days += 1
            day += timedelta(days=1)
        left = max((yesterday - day).days + 1, 0)
        _logger.info("Onto stock history: %d day(s), %d row(s), %d day(s) left, %.1f s",
                     days, rows, left, time.monotonic() - started)
        return {"days": days, "rows": rows, "left": left}

    def _first_done_day(self, zone):
        line = self.env["stock.move.line"].search([("state", "=", "done")], order="date asc", limit=1)
        return _local_day(zone, line.date) if line else None

    def _snapshot_day(self, day, zone):
        """Write the closing rows of one day for the pairs that had a done move on it. Returns the rows written."""
        cr = self.env.cr
        cr.execute("SET LOCAL statement_timeout = %s", (STATEMENT_TIMEOUT_MS,))
        cr.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        start, end = _day_bounds(zone, day)
        deltas, companies = self._day_deltas(start, end)
        if not deltas:
            return 0
        pairs = list(deltas)
        before = self._closing_before(pairs, day)
        binding = _valuation_binding(self.env)
        costs = None
        if binding:
            costs = self._unit_costs(binding, {p for p, _loc in pairs}, set(companies.values()), end)
        uid, now = self.env.uid, onto_now()
        rows = []
        for pair in pairs:
            product_id, location_id = pair
            quantity = before.get(pair, 0.0) + deltas[pair]
            value = None if costs is None else quantity * costs.get((product_id, companies[pair]), 0.0)
            rows.append((uid, now, uid, now, companies[pair], product_id, location_id, day, quantity, value))
        self._upsert_rows(rows)
        return len(rows)

    def _day_deltas(self, start, end):
        """Net change per (product, internal location) from the done move lines of one day, in product UoM."""
        lines = self.env["stock.move.line"].search(
            [("state", "=", "done"), ("date", ">=", start), ("date", "<", end)])
        deltas, companies = {}, {}
        for line in lines:
            qty = line.product_uom_id._compute_quantity(line.qty_done, line.product_id.uom_id, round=False)
            for location, sign in ((line.location_dest_id, 1), (line.location_id, -1)):
                if location.usage != "internal" or not location.company_id:
                    continue  # onto_stock_daily.company_id is required: a location with no company has no row
                pair = (line.product_id.id, location.id)
                deltas[pair] = deltas.get(pair, 0.0) + sign * qty
                companies[pair] = location.company_id.id
        return deltas, companies

    def _closing_before(self, pairs, day):
        """Quantity of each pair as last written before `day` (its closing on the day before it moved)."""
        self.env.cr.execute("""
            SELECT DISTINCT ON (r.product_id, r.location_id) r.product_id, r.location_id, r.quantity
              FROM onto_stock_daily r
              JOIN unnest(%s::int[], %s::int[]) AS p(product_id, location_id)
                ON r.product_id = p.product_id AND r.location_id = p.location_id
             WHERE r."date" < %s
             ORDER BY r.product_id, r.location_id, r."date" DESC
        """, ([p for p, _loc in pairs], [loc for _p, loc in pairs], day))
        return {(p, loc): float(qty or 0.0) for p, loc, qty in self.env.cr.fetchall()}

    def _unit_costs(self, binding, product_ids, company_ids, end):
        """Average unit cost per (product, company): total value over total quantity of its valuation layers before
        `end`. Layers carry no location, so a location's value is its quantity times this cost."""
        if not product_ids:
            return {}
        p, c, q, v, d = (binding[key] for key in ("product", "company", "quantity", "value", "date"))
        groups = self.env[binding["model"]].read_group(
            [(p, "in", list(product_ids)), (c, "in", list(company_ids)), (d, "<", end)],
            [q, v], [p, c], lazy=False,
        )
        costs = {}
        for group in groups:
            quantity, value = group.get(q) or 0.0, group.get(v) or 0.0
            costs[(group[p][0], group[c][0])] = value / quantity if quantity > 0 else 0.0
        return costs

    def _upsert_rows(self, rows):
        cr = self.env.cr
        for offset in range(0, len(rows), UPSERT_CHUNK):
            chunk = rows[offset:offset + UPSERT_CHUNK]
            values = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(chunk))
            cr.execute("""
                INSERT INTO onto_stock_daily
                    (create_uid, create_date, write_uid, write_date, company_id, product_id, location_id,
                     "date", quantity, "value")
                VALUES {values}
                ON CONFLICT (product_id, location_id, "date") DO UPDATE SET
                    quantity = EXCLUDED.quantity, "value" = EXCLUDED."value", company_id = EXCLUDED.company_id,
                    write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date
            """.format(values=values), [item for row in chunk for item in row])
