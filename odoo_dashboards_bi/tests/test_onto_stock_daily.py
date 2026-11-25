# -*- coding: utf-8 -*-
"""Stock history (3.10). Written, not run: the canonical addon suite runs it at the phase end (plan 6.1).

The backfilled rows must equal the stock at each date worked out from the done move lines, for 20 sampled product x
location pairs. A day must write rows only for the pairs that had a done move on it. Assertions name only the
products and locations the test creates, because the database may hold other stock."""

import random
from datetime import timedelta

from odoo.tests.common import TransactionCase, tagged

from ..models.onto_stock_daily import LAST_DAY_KEY, _day_bounds, _local_day, _zone
from ..models.onto_store import onto_now


@tagged("post_install", "-at_install")
class TestOntoStockDaily(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.Daily = cls.env["onto.stock.daily"]
        cls.supplier = cls.env.ref("stock.stock_location_suppliers")
        cls.customer = cls.env.ref("stock.stock_location_customers")
        cls.root = cls.env.ref("stock.stock_location_locations")
        cls.products = cls.env["product.product"].create(
            [{"name": "Stock history product %d" % i, "type": "product"} for i in range(8)])
        cls.locations = cls.env["stock.location"].create(
            [{"name": "Stock history bin %d" % i, "usage": "internal", "location_id": cls.root.id,
              "company_id": cls.company.id} for i in range(4)])

    def _done_move(self, product, source, dest, qty, when):
        """A done move whose lines carry `when` as their date (the date the cron reads)."""
        move = self.env["stock.move"].create({
            "name": "Stock history", "product_id": product.id, "product_uom_qty": qty,
            "product_uom": product.uom_id.id, "location_id": source.id, "location_dest_id": dest.id,
            "company_id": self.company.id,
        })
        move._action_confirm()
        move._action_assign()
        move.quantity_done = qty
        move._action_done()
        move.move_line_ids.write({"date": when})
        move.write({"date": when})
        return move

    def _start_backfill_at(self, day):
        """The cursor says the day before `day` is written, so the next run starts at `day`."""
        self.env["ir.config_parameter"].sudo().set_param(LAST_DAY_KEY, (day - timedelta(days=1)).isoformat())

    def _stock_at(self, product, location, day):
        """Quantity of a bin at the end of `day`, worked out from the done move lines only."""
        _start, end = _day_bounds(_zone(self.env), day)
        lines = self.env["stock.move.line"].search(
            [("product_id", "=", product.id), ("state", "=", "done"), ("date", "<", end)])
        total = 0.0
        for line in lines:
            qty = line.product_uom_id._compute_quantity(line.qty_done, product.uom_id, round=False)
            if line.location_dest_id == location:
                total += qty
            if line.location_id == location:
                total -= qty
        return total

    def test_backfill_matches_stock_at_date_for_sampled_pairs(self):
        zone = _zone(self.env)
        base = _local_day(zone, onto_now()) - timedelta(days=10)
        rng = random.Random(3103)

        def when():
            return _day_bounds(zone, base + timedelta(days=rng.randint(0, 7)))[0] + timedelta(hours=rng.randint(0, 20))

        pairs = [(product, location) for product in self.products for location in self.locations]
        sampled = rng.sample(pairs, 20)
        for product, location in sampled:
            for _ in range(3):
                self._done_move(product, self.supplier, location, rng.randint(5, 20), when())
            self._done_move(product, location, self.customer, rng.randint(1, 4), when())

        self._start_backfill_at(base)
        self.Daily._onto_run_days(600)  # the window is about ten days, so one run finishes it

        for product, location in sampled:
            for offset in (0, 2, 4, 6, 8):
                day = base + timedelta(days=offset)
                row = self.Daily.sudo().search(
                    [("product_id", "=", product.id), ("location_id", "=", location.id), ("date", "<=", day)],
                    order="date desc", limit=1)
                self.assertAlmostEqual(
                    row.quantity if row else 0.0, self._stock_at(product, location, day), places=4,
                    msg="%s at %s on %s" % (product.name, location.name, day))

    def test_a_day_writes_only_the_pairs_that_moved(self):
        zone = _zone(self.env)
        day = _local_day(zone, onto_now()) - timedelta(days=3)
        moved = self.products[0]
        self._done_move(moved, self.supplier, self.locations[0], 4, _day_bounds(zone, day)[0] + timedelta(hours=1))

        self._start_backfill_at(day)
        self.Daily._onto_run_days(600)

        rows = self.Daily.sudo().search([("product_id", "in", self.products.ids), ("date", "=", day)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.product_id, moved)
        self.assertEqual(rows.location_id, self.locations[0])
        self.assertEqual(rows.mapped("quantity"), [4.0])
