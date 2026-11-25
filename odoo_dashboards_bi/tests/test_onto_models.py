from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged

from ..hooks import grant_onto_bot_groups
from ..models.onto_store import branch_of, branches_of

BRANCH = "odoo_dashboards_bi.group_onto_branch_manager"
REGIONAL = "odoo_dashboards_bi.group_onto_regional_manager"
STEWARD = "odoo_dashboards_bi.group_onto_steward"
ADMIN = "odoo_dashboards_bi.group_onto_inventory_admin"


@tagged("post_install", "-at_install")
class TestOntoModels(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.wh1 = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.wh2 = cls.env["stock.warehouse"].create(
            {"name": "Onto test branch 2", "code": "OT2", "company_id": cls.company.id}
        )
        cls.loc1 = cls.wh1.lot_stock_id
        cls.loc2 = cls.wh2.lot_stock_id
        cls.product = cls.env["product.product"].create({"name": "Onto test product"})
        cls.bot = cls.env.ref("odoo_dashboards_bi.user_onto_bot")

    def _user(self, name, groups, branches=()):
        return self.env["res.users"].create(
            {
                "name": name,
                "login": name.lower().replace(" ", "_"),
                "company_id": self.company.id,
                "company_ids": [Command.set([self.company.id])],
                "groups_id": [
                    Command.set([self.env.ref("base.group_user").id] + [self.env.ref(g).id for g in groups])
                ],
                "onto_branch_ids": [Command.set([w.id for w in branches])],
            }
        )

    def _status(self, location, status="healthy"):
        return self.env["onto.status"].create(
            {"product_id": self.product.id, "location_id": location.id, "status": status}
        )

    def test_unique_keys_are_enforced(self):
        self._status(self.loc1)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._status(self.loc1)

        rec = self.env["onto.recommendation"].create({"external_ref": "rec-1"})
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env["onto.recommendation"].create({"external_ref": "rec-1"})
        self.env["onto.recommendation.line"].create({"recommendation_id": rec.id, "option_key": "transfer"})
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env["onto.recommendation.line"].create({"recommendation_id": rec.id, "option_key": "transfer"})

        stock_row = {"product_id": self.product.id, "location_id": self.loc1.id, "date": "2026-10-27"}
        self.env["onto.stock.daily"].create(stock_row)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env["onto.stock.daily"].create(stock_row)

        forecast = {"run_key": "run-1", "series_key": "series-1"}
        self.env["onto.forecast"].create(forecast)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env["onto.forecast"].create(forecast)

    def test_audit_is_append_only_except_onto_gc(self):
        audit = self.env["onto.audit"].create({"action": "test.action"})
        with self.assertRaises(UserError):
            audit.write({"detail": "changed"})
        with self.assertRaises(UserError):
            audit.unlink()
        audit.with_context(onto_gc=True).unlink()
        self.assertFalse(audit.exists())

    def test_ai_is_off_by_default_and_changes_are_audited(self):
        setting = self.env["onto.agent.setting"].create({})
        self.assertFalse(setting.ai_enabled)
        domain = [("action", "=", "onto.agent.ai_enabled")]
        before = self.env["onto.audit"].search_count(domain)
        setting.ai_enabled = True
        self.assertEqual(self.env["onto.audit"].search_count(domain), before + 1)

    def test_bot_is_internal_with_no_key_or_password(self):
        self.assertTrue(self.bot.has_group("base.group_user"))
        self.assertTrue(self.bot.has_group("odoo_dashboards_bi.group_onto_bot"))
        self.assertFalse(self.bot.password)
        self.assertFalse(self.env["res.users.apikeys"].sudo().search_count([("user_id", "=", self.bot.id)]))

    def test_bot_read_groups_are_granted_and_the_grant_is_idempotent(self):
        self.assertTrue(self.bot.has_group("stock.group_stock_user"))
        grant_onto_bot_groups(self.env)
        self.assertTrue(self.bot.has_group("stock.group_stock_user"))

    def test_branch_manager_reads_only_own_branch_and_cannot_write(self):
        self._status(self.loc1)
        self._status(self.loc2, "low")
        manager = self._user("Branch One", [BRANCH], [self.wh1])
        visible = self.env["onto.status"].with_user(manager).search([("product_id", "=", self.product.id)])
        self.assertEqual(visible.location_id, self.loc1)
        with self.assertRaises(AccessError):
            self.env["onto.status"].with_user(manager).create(
                {"product_id": self.product.id, "location_id": self.loc1.id, "status": "low"}
            )

    def test_user_without_onto_group_is_refused(self):
        plain = self._user("Plain User", [])
        with self.assertRaises(AccessError):
            self.env["onto.status"].with_user(plain).search([])

    def test_bot_writes_results_but_not_settings(self):
        self.env["onto.status"].with_user(self.bot).create(
            {"product_id": self.product.id, "location_id": self.loc1.id, "status": "low"}
        )
        with self.assertRaises(AccessError):
            self.env["onto.policy"].with_user(self.bot).create({"name": "Refused", "kind": "threshold"})

    def test_regional_manager_is_scoped_by_company_too(self):
        other = self.env["res.company"].create({"name": "Onto other company"})
        other_wh = self.env["stock.warehouse"].create(
            {"name": "Other warehouse", "code": "OTH", "company_id": other.id}
        )
        self._status(self.loc1)
        self.env["onto.status"].create(
            {
                "product_id": self.product.id,
                "location_id": other_wh.lot_stock_id.id,
                "status": "critical",
                "company_id": other.id,
            }
        )
        regional = self._user("Regional Two", [REGIONAL], [self.wh1, other_wh])
        visible = (
            self.env["onto.status"]
            .with_user(regional)
            .with_context(allowed_company_ids=[self.company.id])
            .search([("product_id", "=", self.product.id)])
        )
        self.assertEqual(visible.location_id, self.loc1)

    def test_chat_threads_are_private(self):
        alice = self._user("Alice Chat", [BRANCH], [self.wh1])
        bob = self._user("Bob Chat", [BRANCH], [self.wh1])
        thread = self.env["onto.chat.thread"].with_user(alice).create({"title": "Stock?"})
        self.assertTrue(self.env["onto.chat.thread"].with_user(alice).search([("id", "=", thread.id)]))
        self.assertFalse(self.env["onto.chat.thread"].with_user(bob).search([("id", "=", thread.id)]))

    def test_stewards_see_system_runs_not_other_users_chats(self):
        alice = self._user("Alice Runs", [BRANCH], [self.wh1])
        self.env["onto.agent.run"].create(
            {"run_key": "chat-1", "user_id": alice.id, "kind": "chat", "state": "done"}
        )
        self.env["onto.agent.run"].create(
            {"run_key": "job-1", "user_id": self.bot.id, "kind": "job", "state": "done"}
        )
        steward = self._user("Steward Runs", [STEWARD], [self.wh1])
        seen = (
            self.env["onto.agent.run"]
            .with_user(steward)
            .search([("run_key", "in", ["chat-1", "job-1"])])
        )
        self.assertEqual(seen.run_key, "job-1")

    def test_branch_helpers_follow_the_user_and_location(self):
        user = self._user("Helper User", [BRANCH], [self.wh1])
        self.assertEqual(branches_of(user), self.wh1)
        self.assertEqual(branch_of(self.loc1), self.wh1)

    def test_admin_can_change_settings_and_the_group_is_implied_for_system_admins(self):
        admin = self._user("Inventory Admin", [ADMIN], [self.wh1])
        policy = self.env["onto.policy"].with_user(admin).create({"name": "Min stock", "kind": "threshold"})
        self.assertTrue(policy.exists())
        self.assertTrue(self.env.ref("base.group_system").implied_ids.filtered(
            lambda g: g.id == self.env.ref("odoo_dashboards_bi.group_onto_inventory_admin").id
        ))
