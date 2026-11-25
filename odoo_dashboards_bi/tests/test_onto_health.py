# -*- coding: utf-8 -*-
"""Data Health (2.6, D51). Each check finds its offender and not its clean twin. Assertions name only the records
the test created, because the database may hold seed data. A failing check never stops the run. The routes admit
stewards and stock managers only. No record value reaches a message or a fix link."""

import json
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

from odoo import Command
from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..models.onto_health import CHECKS, last_full_month
from ..models.onto_store import onto_now
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

STEWARD = "odoo_dashboards_bi.group_onto_steward"
STOCK_MANAGER = "stock.group_stock_manager"
HEALTH = "/onto/v1/health"
RUN = "/onto/v1/health/run"


@tagged("post_install", "-at_install")
class TestOntoHealthChecks(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.Health = cls.env["onto.health"]
        cls.vendor = cls.env["res.partner"].create({"name": "Health test vendor"})
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.root = cls.env.ref("stock.stock_location_locations")
        cls.supplier_location = cls.env.ref("stock.stock_location_suppliers")

    def _product(self, name, **vals):
        return self.env["product.product"].create(dict({"name": name, "type": "product"}, **vals))

    def _template(self, name, **vals):
        return self.env["product.template"].create(dict({"name": name, "type": "product"}, **vals))

    def _location(self, name):
        return self.env["stock.location"].create({
            "name": name, "usage": "internal", "location_id": self.root.id, "company_id": self.company.id,
        })

    def _stored(self, key):
        return self.Health.search([("check_key", "=", key), ("company_id", "=", self.company.id)])

    def _sample(self, key):
        """Run every check for the test company, then the sample ids the check stored (empty if it found nothing)."""
        self.Health.run_checks(companies=self.company)
        row = self._stored(key)
        return json.loads(row.sample_ids) if row else []

    def test_every_check_has_a_method(self):
        self.assertEqual(len(CHECKS), 13)
        for key in CHECKS:
            self.assertTrue(hasattr(type(self.Health), "_check_" + key), key)

    def test_product_no_supplier(self):
        offender = self._template("Health no vendor")
        clean = self._template("Health with vendor", seller_ids=[Command.create({"partner_id": self.vendor.id})])
        sample = self._sample("product_no_supplier")
        self.assertIn(offender.id, sample)
        self.assertNotIn(clean.id, sample)

    def test_product_no_cost(self):
        offender = self._product("Health free")
        clean = self._product("Health costed", standard_price=5.0)
        sample = self._sample("product_no_cost")
        self.assertIn(offender.id, sample)
        self.assertNotIn(clean.id, sample)

    def test_product_no_lead_time(self):
        template = self._template("Health lead time")
        offender = self.env["product.supplierinfo"].create(
            {"partner_id": self.vendor.id, "product_tmpl_id": template.id, "delay": 0})
        clean = self.env["product.supplierinfo"].create(
            {"partner_id": self.vendor.id, "product_tmpl_id": template.id, "delay": 3})
        sample = self._sample("product_no_lead_time")
        self.assertIn(offender.id, sample)
        self.assertNotIn(clean.id, sample)

    def test_product_no_category(self):
        offender = self._template("Health default category")  # the All category by default
        own = self.env["product.category"].create({"name": "Health category"})
        clean = self._template("Health own category", categ_id=own.id)
        sample = self._sample("product_no_category")
        self.assertIn(offender.id, sample)
        self.assertNotIn(clean.id, sample)

    def test_product_no_reorder_rule(self):
        offender = self._product("Health moving")
        clean = self._product("Health with rule")
        for product in (offender, clean):
            self.env["stock.move"].create({
                "name": "Health move", "product_id": product.id, "product_uom_qty": 1,
                "product_uom": product.uom_id.id, "location_id": self.supplier_location.id,
                "location_dest_id": self.warehouse.lot_stock_id.id,
            })
        self.env["stock.warehouse.orderpoint"].create({
            "product_id": clean.id, "warehouse_id": self.warehouse.id,
            "location_id": self.warehouse.lot_stock_id.id, "product_min_qty": 1, "product_max_qty": 5,
        })
        sample = self._sample("product_no_reorder_rule")
        self.assertIn(offender.id, sample)
        self.assertNotIn(clean.id, sample)

    def test_product_duplicate(self):
        first = self._product("Health duplicate one", default_code="HZ-DUP-1")
        second = self._product("Health duplicate two", default_code="HZ-DUP-1")
        clean = self._product("Health unique", default_code="HZ-UNIQUE-1")
        sample = self._sample("product_duplicate")
        self.assertIn(first.id, sample)
        self.assertIn(second.id, sample)
        self.assertNotIn(clean.id, sample)

    def test_uom_mismatch(self):
        offender = self._template("Health purchase uom")
        clean = self._template("Health same uom")
        self.env.flush_all()
        # product.template keeps the two categories equal in ORM writes, so the mismatch is written in SQL.
        self.env.cr.execute("UPDATE product_template SET uom_po_id = %s WHERE id = %s",
                            (self.env.ref("uom.product_uom_kgm").id, offender.id))
        self.env.invalidate_all()
        sample = self._sample("uom_mismatch")
        self.assertIn(offender.id, sample)
        self.assertNotIn(clean.id, sample)

    def test_stale_count(self):
        today = onto_now().date()
        old, recent = self._location("Health old count"), self._location("Health recent count")
        old.last_inventory_date = today - timedelta(days=200)
        recent.last_inventory_date = today
        product = self._product("Health counted")
        for location in (old, recent):
            self.env["stock.quant"]._update_available_quantity(product, location, 5)
        offender = self.env["stock.quant"].search([("location_id", "=", old.id)])
        clean = self.env["stock.quant"].search([("location_id", "=", recent.id)])
        sample = self._sample("stale_count")
        self.assertIn(offender.id, sample)
        self.assertNotIn(clean.id, sample)

    def test_fefo_off(self):
        if "use_expiration_date" not in self.env["product.template"]._fields:
            self.skipTest("product_expiry is not installed, so no product has expiry dates")
        fifo = self.env["product.category"].create({"name": "Health fifo"})  # no removal strategy: not FEFO
        fefo = self.env["product.category"].create({
            "name": "Health fefo", "removal_strategy_id": self.env.ref("product_expiry.removal_fefo").id,
        })
        offender = self._template("Health expiry fifo", tracking="lot", use_expiration_date=True, categ_id=fifo.id)
        clean = self._template("Health expiry fefo", tracking="lot", use_expiration_date=True, categ_id=fefo.id)
        sample = self._sample("fefo_off")
        self.assertIn(offender.id, sample)
        self.assertNotIn(clean.id, sample)

    def test_unmapped_stock_location(self):
        product = self._product("Health stocked")
        loose = self._location("Health loose shelf")
        mapped = self._location("Health mapped shelf")
        group = self.env["onto.org.map"].create({"name": "Health group", "kind": "group"})
        self.env["onto.org.map"].create({
            "name": "Health mapped shelf", "kind": "location", "parent_id": group.id,
            "location_id": mapped.id, "role": "central",
        })
        for location in (loose, mapped):
            self.env["stock.quant"]._update_available_quantity(product, location, 5)
        sample = self._sample("unmapped_stock_location")
        self.assertIn(loose.id, sample)
        self.assertNotIn(mapped.id, sample)

    def test_a_failing_check_is_stored_as_info_and_the_others_still_run(self):
        offender = self._product("Health free cost")  # product_no_cost finds it
        with patch.object(type(self.Health), "_check_product_no_supplier", side_effect=RuntimeError("boom")):
            self.Health.run_checks(companies=self.company)
        failed = self._stored("product_no_supplier")
        self.assertEqual((failed.severity, failed.message, failed["count"]), ("info", "check failed", 0))
        self.assertEqual(json.loads(failed.sample_ids), [])
        self.assertIn(offender.id, json.loads(self._stored("product_no_cost").sample_ids))

    def test_a_second_run_replaces_each_checks_rows(self):
        self.Health.run_checks(companies=self.company)
        first = {row.check_key: row.run_id for row in self.Health.search([("company_id", "=", self.company.id)])}
        self.Health.run_checks(companies=self.company)
        rows = self.Health.search([("company_id", "=", self.company.id)])
        self.assertEqual(len(rows), len(set(rows.mapped("check_key"))))  # one row per check and company
        self.assertTrue(all(row.run_id != first.get(row.check_key) for row in rows))

    def test_a_clean_run_still_reports_when_it_ran(self):
        with ExitStack() as stack:
            for key in CHECKS:  # every check finds nothing, so the run stores no rows
                stack.enter_context(patch.object(type(self.Health), "_check_" + key, return_value=(0, [])))
            self.Health.run_checks(companies=self.company)
        reply = self.Health.with_context(allowed_company_ids=self.company.ids).payload()
        self.assertEqual(reply["checks"], [])
        self.assertIsNotNone(reply["computed_at"])
        self.assertIsNotNone(reply["run_id"])

    def test_fix_links_and_messages_carry_no_record_values(self):
        self._product("QZNAME product", default_code="QZCODE")  # no cost: product_no_cost finds it
        self.Health.run_checks(companies=self.company)
        checks = self.Health.with_context(allowed_company_ids=self.company.ids).payload()["checks"]
        self.assertTrue(checks)
        for check in checks:
            self.assertNotIn("QZ", check["message"])
            if check["sample_ids"]:
                self.assertEqual(
                    check["fix_url"],
                    "/web#model=%s&view_type=form&id=%d" % (check["model"], check["sample_ids"][0]),
                )
                self.assertNotIn("QZ", check["fix_url"])

    def test_the_daily_cron_is_registered(self):
        cron = self.env.ref("odoo_dashboards_bi.ir_cron_onto_health_run")
        self.assertEqual((cron.interval_type, cron.interval_number), ("days", 1))
        self.assertTrue(cron.active)


@tagged("post_install", "-at_install")
class TestOntoHealthRoutes(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.steward = cls._user("onto_health_steward", [STEWARD])
        cls.manager = cls._user("onto_health_manager", [STOCK_MANAGER])
        cls.nobody = cls._user("onto_health_nobody", [])

    @classmethod
    def _user(cls, login, xmlids):
        user = cls.env["res.users"].create({
            "name": login, "login": login, "company_id": cls.company.id,
            "company_ids": [Command.set([cls.company.id])],
            "groups_id": [Command.set([cls.env.ref("base.group_user").id] + [cls.env.ref(x).id for x in xmlids])],
        })
        return user, cls.env["res.users.apikeys"].with_user(user)._generate("mobile", "onto-health-test")

    def test_the_read_route_admits_stewards_and_stock_managers_only(self):
        for user in (self.steward, self.manager):
            resp = sealed_request(self, user[1], "POST", HEALTH, body={})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(set(resp.json()), {"run_id", "computed_at", "checks"})
        self.assertEqual(sealed_request(self, self.nobody[1], "POST", HEALTH, body={}).status_code, 404)

    def test_run_is_steward_only_and_replies_in_the_read_shape(self):
        resp = sealed_request(self, self.steward[1], "POST", RUN, body={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(set(resp.json()), {"run_id", "computed_at", "checks"})
        self.assertEqual(sealed_request(self, self.manager[1], "POST", RUN, body={}).status_code, 404)
        self.assertEqual(sealed_request(self, self.nobody[1], "POST", RUN, body={}).status_code, 404)

    def test_an_unknown_check_key_is_refused(self):
        resp = sealed_request(self, self.steward[1], "POST", HEALTH, body={"check_key": "no_such_check"})
        self.assertEqual((resp.status_code, resp.json()), (422, {"error": "unknown_check"}))


@tagged("post_install", "-at_install")
class TestOntoReconcileChecks(TransactionCase):
    """Reconciliation (2.4, D53). Each test works in its own company, so seed data in the database cannot move the
    totals. A within-tolerance case patches the totals (the inputs) where building the books for it would take a whole
    month or a whole POS session."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Health = cls.env["onto.health"]
        cls.company = cls.env["res.company"].create({"name": "Onto reconcile test company"})
        cls.root = cls.env.ref("stock.stock_location_locations")
        cls.supplier_location = cls.env.ref("stock.stock_location_suppliers")

    def _need(self, *models):
        for model in models:
            if model not in self.env:
                self.skipTest("%s is not installed" % model)

    def _product(self, name, **vals):
        return self.env["product.product"].with_company(self.company).create(
            dict({"name": name, "type": "product"}, **vals))

    def _location(self, name):
        return self.env["stock.location"].create({
            "name": name, "usage": "internal", "location_id": self.root.id, "company_id": self.company.id,
        })

    def _quant(self, product, location, quantity):
        self.env["stock.quant"].with_company(self.company)._update_available_quantity(product, location, quantity)

    def _run(self):
        self.Health.run_checks(companies=self.company)

    def _stored(self, key):
        return self.Health.search([("check_key", "=", key), ("company_id", "=", self.company.id)])

    def test_an_empty_company_has_no_reconciliation_rows(self):
        self._run()
        for key in ("reconcile_stock_value", "reconcile_on_hand", "reconcile_revenue_gl"):
            self.assertFalse(self._stored(key), key)

    def test_stock_value_flags_quants_with_no_valuation_layer(self):
        self._need("stock.valuation.layer")
        product = self._product("Recon costed", standard_price=5.0)
        self._quant(product, self._location("Recon shelf"), 10)  # 10 x 5 = 50 of value, and no layer behind it
        self._run()
        row = self._stored("reconcile_stock_value")
        self.assertEqual((row.severity, row["count"], json.loads(row.sample_ids)), ("warning", 1, []))
        self.assertIn("50.00", row.message)

    def test_stock_value_within_tolerance_has_no_row(self):
        self._need("stock.valuation.layer")
        with patch.object(type(self.Health), "_totals_stock_value", return_value=(100.0, 100.5)):
            self._run()
        self.assertFalse(self._stored("reconcile_stock_value"))

    def test_the_tolerance_is_a_fraction_of_the_larger_total_with_a_floor(self):
        self._need("stock.valuation.layer")
        params = self.env["ir.config_parameter"].sudo()

        def outside(left, right):
            with patch.object(type(self.Health), "_totals_stock_value", return_value=(left, right)):
                self._run()
            return bool(self._stored("reconcile_stock_value"))

        self.assertTrue(outside(100.0, 102.0))  # 1% of 102 is 1.02 and the gap is 2.00
        self.assertFalse(outside(100.0, 101.0))  # the gap is 1.00, inside 1.01
        self.assertFalse(outside(0.0, 0.005))  # inside the 0.01 floor
        params.set_param("onto.reconcile.tolerance", "0.05")
        self.assertFalse(outside(100.0, 102.0))  # 5% of 102 is 5.10

    def test_on_hand_flags_a_quant_with_no_move_behind_it(self):
        self._quant(self._product("Recon counted"), self._location("Recon bin"), 5)
        self._run()
        row = self._stored("reconcile_on_hand")
        self.assertEqual((row.severity, row["count"]), ("warning", 1))
        self.assertIn("5.00", row.message)  # 5.00 in quants against 0.00 net of moves

    def test_on_hand_matches_a_done_receipt(self):
        product = self._product("Recon received")
        move = self.env["stock.move"].with_company(self.company).create({
            "name": "Recon receipt", "product_id": product.id, "product_uom": product.uom_id.id,
            "product_uom_qty": 5, "location_id": self.supplier_location.id,
            "location_dest_id": self._location("Recon receiving bin").id, "company_id": self.company.id,
        })
        move._action_confirm()
        move.quantity_done = 5
        move._action_done()
        self._run()
        self.assertFalse(self._stored("reconcile_on_hand"))

    def _manual_income(self, amount, day):
        """A posted journal entry crediting an income account: revenue in the books with no sale behind it."""
        income = self.env["account.account"].create({
            "code": "RC400", "name": "Recon income", "account_type": "income", "company_id": self.company.id,
        })
        bank = self.env["account.account"].create({
            "code": "RC100", "name": "Recon bank", "account_type": "asset_cash", "company_id": self.company.id,
        })
        journal = self.env["account.journal"].create({
            "name": "Recon journal", "code": "RCJ", "type": "general", "company_id": self.company.id,
        })
        move = self.env["account.move"].with_company(self.company).create({
            "journal_id": journal.id, "date": day, "company_id": self.company.id,
            "line_ids": [
                Command.create({"account_id": income.id, "name": "Recon income", "debit": 0.0, "credit": amount}),
                Command.create({"account_id": bank.id, "name": "Recon bank", "debit": amount, "credit": 0.0}),
            ],
        })
        move.action_post()

    def test_revenue_flags_income_booked_with_no_sale(self):
        self._need("pos.order", "account.move")
        _, last = last_full_month(onto_now().date())
        self._manual_income(100.0, last)
        self._run()
        row = self._stored("reconcile_revenue_gl")
        self.assertEqual((row.severity, row["count"]), ("warning", 1))
        self.assertIn("100.00", row.message)

    def test_revenue_within_tolerance_has_no_row(self):
        self._need("pos.order", "account.move")
        with patch.object(type(self.Health), "_totals_revenue_gl", return_value=(100.0, 100.4)):
            self._run()
        self.assertFalse(self._stored("reconcile_revenue_gl"))
