from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..models import onto_scope
from ..models.onto_rules import classify_rules, guard_fragments, visible_ids_sql
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

BRANCH = "odoo_dashboards_bi.group_onto_branch_manager"
STEWARD = "odoo_dashboards_bi.group_onto_steward"


def _restricted_group(env):
    return env["res.groups"].create({"name": "Onto rules test: restricted"})


def _user(env, company, name, groups, branches):
    return env["res.users"].create({
        "name": name,
        "login": name.lower().replace(" ", "_"),
        "company_id": company.id,
        "company_ids": [Command.set([company.id])],
        "groups_id": [Command.set([env.ref("base.group_user").id] + [g.id for g in groups])],
        "onto_branch_ids": [Command.set([w.id for w in branches])],
    })


@tagged("post_install", "-at_install")
class TestOntoRules(TransactionCase):
    def setUp(self):
        super().setUp()
        onto_scope.clear_cache()
        self.company = self.env.company
        self.wh1 = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        self.wh2 = self.env["stock.warehouse"].create(
            {"name": "Rules test branch 2", "code": "RT2", "company_id": self.company.id}
        )
        self.loc1, self.loc2 = self.wh1.lot_stock_id, self.wh2.lot_stock_id
        self.product = self.env["product.product"].create({"name": "Rules test product", "type": "product"})
        self.restricted = _restricted_group(self.env)
        branch = self.env.ref(BRANCH)
        self.u1 = _user(self.env, self.company, "Restricted Rules", [branch, self.restricted], [self.wh1, self.wh2])
        self.u2 = _user(self.env, self.company, "Open Rules", [branch], [self.wh1, self.wh2])

    def _rule(self, model_name, domain, groups=None):
        return self.env["ir.rule"].create({
            "name": "Rules test %s" % domain,
            "model_id": self.env["ir.model"]._get(model_name).id,
            "groups": [Command.set([g.id for g in (groups or [self.restricted])])],
            "domain_force": str(domain),
        })

    def _quant(self, location):
        self.env["stock.quant"]._update_available_quantity(self.product, location, 5)

    def _status(self, location, status="healthy"):
        return self.env["onto.status"].create(
            {"product_id": self.product.id, "location_id": location.id, "status": status}
        )

    def _statuses(self, user):
        return self.env["onto.status"].with_user(user).search([("product_id", "=", self.product.id)])

    def test_warehouse_rule_restricts_status_reads_for_a_restricted_user(self):
        self._rule("stock.quant", [("location_id.warehouse_id", "in", [self.wh1.id])])
        self._quant(self.loc1)
        self._quant(self.loc2)
        self._status(self.loc1)
        self._status(self.loc2, "low")
        self.assertEqual(self._statuses(self.u1).location_id, self.loc1)
        self.assertEqual(self._statuses(self.u2).location_id, self.loc1 | self.loc2)

    def test_a_non_projectable_rule_shows_restricted_users_nothing(self):
        self._rule("stock.quant", ["&", ("location_id.warehouse_id", "in", [self.wh1.id]),
                                   ("product_id", "=", self.product.id)])
        self._quant(self.loc1)
        self._status(self.loc1)
        self.assertFalse(self._statuses(self.u1))
        self.assertEqual(self._statuses(self.u2).location_id, self.loc1)
        scope = onto_scope.scope_payload(self.env(user=self.u1), "onto.status")
        self.assertEqual(scope["mode"], "none")
        rule = classify_rules(self.env(user=self.u1), "stock.quant", for_user=True)
        mixed = [r for r in rule if r["name"].startswith("Rules test ['&'")]
        self.assertEqual((len(mixed), mixed[0]["projectable"]), (1, False))
        self.assertIn("mixes", mixed[0]["reason"])

    def test_rules_are_classified_and_described(self):
        rule = self._rule("stock.quant", [("location_id.warehouse_id", "in", [self.wh1.id])])
        rows = {r["name"]: r for r in classify_rules(self.env(user=self.u1), "stock.quant", for_user=True)}
        mine = rows[rule.name]
        self.assertEqual((mine["kind"], mine["global"], mine["projectable"]), ("custom", False, True))
        self.assertEqual(mine["dimension"], "warehouse")
        self.assertEqual(mine["groups"], [self.restricted.name])
        self.assertIn(self.wh1.name, mine["description"])
        self.assertIn("Projectable on warehouse.", mine["description"])
        self.assertTrue(any(r["kind"] == "multi_company" for r in rows.values()))

    def test_visible_ids_sql_matches_the_orm_search_for_two_users(self):
        self._rule("stock.quant", [("location_id.warehouse_id", "in", [self.wh1.id])])
        self._quant(self.loc1)
        self._quant(self.loc2)
        seen = {}
        for user in (self.u1, self.u2):
            sql, params = visible_ids_sql(self.env(user=user), "stock.quant")
            self.env.cr.execute("SELECT id FROM %s WHERE id IN %s" % (self.env["stock.quant"]._table, sql), params)
            ids = {row[0] for row in self.env.cr.fetchall()}
            self.assertEqual(ids, set(self.env["stock.quant"].with_user(user).search([]).ids))
            seen[user] = ids
        self.assertLess(len(seen[self.u1]), len(seen[self.u2]))

        sql, params = visible_ids_sql(self.env(user=self.u1), "stock.warehouse")
        self.env.cr.execute("SELECT id FROM %s WHERE id IN %s" % (self.env["stock.warehouse"]._table, sql), params)
        self.assertEqual(
            {row[0] for row in self.env.cr.fetchall()},
            set(self.env["stock.warehouse"].with_user(self.u1).search([]).ids),
        )

    def test_guard_fragment_keeps_visible_customers_and_null_customers(self):
        partner_a = self.env["res.partner"].create({"name": "Rules customer A"})
        partner_b = self.env["res.partner"].create({"name": "Rules customer B"})
        # A group rule would not hide partner_b: Odoo ORs group rules, and base.group_user already reads
        # every non-private partner. A global rule is ANDed with everything, so it does exclude partner_b.
        self.env["ir.rule"].create({
            "name": "Rules test: guard customer",
            "model_id": self.env["ir.model"]._get("res.partner").id,
            "domain_force": str([("id", "!=", partner_b.id)]),
        })
        self.assertEqual(
            self.env["res.partner"].with_user(self.u1).search([("id", "in", [partner_a.id, partner_b.id])]),
            partner_a,
        )
        customer_location = self.env.ref("stock.stock_location_customers")

        def delivery(partner_id):
            return self.env["stock.picking"].create({
                "picking_type_id": self.wh1.out_type_id.id,
                "location_id": self.loc1.id,
                "location_dest_id": customer_location.id,
                "partner_id": partner_id,
            })

        visible, hidden, no_customer = delivery(partner_a.id), delivery(partner_b.id), delivery(False)
        setting = self.env["onto.object.setting"].create(
            {"name": "Deliveries", "base_model": "stock.picking", "guard_models": '["res.partner"]'}
        )
        items = guard_fragments(self.env(user=self.u1), setting)
        self.assertEqual([item["model"] for item in items], ["res.partner"])
        self.env.cr.execute(items[0]["create"][0], items[0]["create"][1])
        self.env.cr.execute(
            "SELECT id FROM stock_picking WHERE id IN %s AND " + " AND ".join(items[0]["fragments"]),
            [tuple((visible + hidden + no_customer).ids)],
        )
        self.assertEqual({row[0] for row in self.env.cr.fetchall()}, {visible.id, no_customer.id})
        self.assertTrue(hidden.exists())  # still there: the guard hides it, it does not delete it

        off = self.env["onto.object.setting"].create({"name": "Off", "base_model": "stock.picking"})
        self.assertEqual(guard_fragments(self.env(user=self.u1), off), [])

    def test_scope_payload_projects_each_result_model(self):
        self._rule("stock.quant", [("location_id.warehouse_id", "in", [self.wh1.id])])
        self._quant(self.loc1)
        self._quant(self.loc2)
        user_env = self.env(user=self.u1)
        status = onto_scope.scope_payload(user_env, "onto.status")
        self.assertEqual(status["mode"], "dims")
        self.assertIn(self.loc1.id, status["dims"]["location_id"])
        self.assertNotIn(self.loc2.id, status["dims"]["location_id"])
        kpi = onto_scope.scope_payload(user_env, "onto.kpi.daily")
        self.assertEqual(kpi["dims"], {"warehouse_id": [self.wh1.id]})
        self.assertIsNone(onto_scope.scope_payload(user_env, "onto.audit"))
        self.assertIsNone(onto_scope.scope_payload(user_env, "stock.quant"))

    def test_guard_models_must_be_joined_by_a_many2one_and_be_a_json_list(self):
        with self.assertRaises(ValidationError):
            self.env["onto.object.setting"].create(
                {"name": "No link", "base_model": "stock.picking", "guard_models": '["res.country"]'}
            )
        with self.assertRaises(ValidationError):
            self.env["onto.object.setting"].create(
                {"name": "Not json", "base_model": "stock.picking", "guard_models": "res.partner"}
            )


@tagged("post_install", "-at_install")
class TestOntoRulesRoutes(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.wh1 = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.loc1 = cls.wh1.lot_stock_id
        cls.wh2 = cls.env["stock.warehouse"].create(
            {"name": "Rules route branch 2", "code": "RR2", "company_id": cls.company.id}
        )
        cls.product = cls.env["product.product"].create({"name": "Rules route product", "type": "product"})
        cls.restricted = _restricted_group(cls.env)
        cls.env["ir.rule"].create({
            "name": "Rules route warehouse",
            "model_id": cls.env["ir.model"]._get("stock.quant").id,
            "groups": [Command.set([cls.restricted.id])],
            "domain_force": str([("location_id.warehouse_id", "in", [cls.wh1.id])]),
        })
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.loc1, 5)
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.wh2.lot_stock_id, 5)
        branch = cls.env.ref(BRANCH)
        cls.user = _user(cls.env, cls.company, "Route Rules", [branch, cls.restricted], [cls.wh1, cls.wh2])
        cls.steward = _user(cls.env, cls.company, "Route Steward", [cls.env.ref(STEWARD)], [cls.wh1])
        cls.key = cls.env["res.users.apikeys"].with_user(cls.user)._generate("mobile", "onto-rules-test")
        cls.steward_key = cls.env["res.users.apikeys"].with_user(cls.steward)._generate("mobile", "onto-rules-test")
        cls.env["onto.object.setting"].create(
            {"name": "Deliveries", "base_model": "stock.picking", "guard_models": '["res.partner"]'}
        )

    def setUp(self):
        super().setUp()
        onto_scope.clear_cache()

    def test_scope_route_returns_the_callers_projected_sets(self):
        resp = sealed_request(self, self.key, "POST", "/onto/v1/scope", body={"model": "onto.status"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["mode"], "dims")
        self.assertIn(self.loc1.id, body["dims"]["location_id"])
        self.assertNotIn(self.wh2.lot_stock_id.id, body["dims"]["location_id"])

    def test_scope_route_refuses_models_that_are_not_stored_results(self):
        resp = sealed_request(self, self.key, "POST", "/onto/v1/scope", body={"model": "onto.audit"})
        self.assertEqual(resp.status_code, 404)

    def test_studio_rules_are_for_stewards_and_describe_each_object(self):
        resp = sealed_request(self, self.key, "POST", "/onto/v1/rules", body={})
        self.assertEqual(resp.status_code, 404)
        resp = sealed_request(self, self.steward_key, "POST", "/onto/v1/rules", body={"object": "Deliveries"})
        self.assertEqual(resp.status_code, 200, resp.text)
        (obj,) = resp.json()["objects"]
        self.assertEqual((obj["base_model"], obj["guard_models"]), ("stock.picking", ["res.partner"]))
        self.assertEqual([m["role"] for m in obj["models"]], ["base", "guard"])
        self.assertEqual(obj["models"][1]["model"], "res.partner")
        self.assertIsInstance(obj["models"][0]["rules"], list)
