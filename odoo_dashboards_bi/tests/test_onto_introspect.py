import json

from odoo import Command
from odoo.tests.common import HttpCase, tagged

from ..models import onto_token
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

STEWARD = "odoo_dashboards_bi.group_onto_steward"
BRANCH = "odoo_dashboards_bi.group_onto_branch_manager"
PROBE = "Onto introspect probe"
INTROSPECT_KEYS = {
    "odoo_version", "database_uuid", "modules", "companies", "warehouses", "locations", "groups", "users",
    "record_rules", "models",
}


@tagged("post_install", "-at_install")
class TestOntoIntrospect(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        group_user = cls.env.ref("base.group_user")
        cls.steward = cls.env["res.users"].create({
            "name": "Onto Introspect Steward", "login": "onto_introspect_steward",
            "company_id": cls.company.id, "company_ids": [Command.set([cls.company.id])],
            "groups_id": [Command.set([group_user.id, cls.env.ref(STEWARD).id])],
        })
        cls.person = cls.env["res.users"].create({
            "name": "Onto Introspect Person", "login": "onto_introspect_person",
            "company_id": cls.company.id, "company_ids": [Command.set([cls.company.id])],
            "groups_id": [Command.set([group_user.id, cls.env.ref(BRANCH).id])],
        })
        cls.steward_key = cls.env["res.users.apikeys"].with_user(cls.steward)._generate("mobile", "onto-introspect")
        cls.person_key = cls.env["res.users.apikeys"].with_user(cls.person)._generate("mobile", "onto-introspect")

    def _introspect(self):
        resp = sealed_request(self, self.steward_key, "POST", "/onto/v1/introspect", body={})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _profile(self, models):
        resp = sealed_request(self, self.steward_key, "POST", "/onto/v1/profile", body={"models": models})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_the_steward_reads_both_routes_and_others_get_404(self):
        self.assertTrue(INTROSPECT_KEYS <= set(self._introspect()))
        self.assertEqual([m["model"] for m in self._profile(["res.partner"])["models"]], ["res.partner"])
        for path in ("/onto/v1/introspect", "/onto/v1/profile"):
            resp = sealed_request(self, self.person_key, "POST", path, body={})
            self.assertEqual(resp.status_code, 404, path)

    def test_a_query_token_is_refused_on_both_routes(self):
        resp = sealed_request(self, self.steward_key, "POST", "/onto/v1/token", body={})
        self.assertEqual(resp.status_code, 200, resp.text)
        headers = {"Authorization": "Bearer %s" % resp.json()["token"], "Content-Type": "application/json"}
        for path in ("/onto/v1/introspect", "/onto/v1/profile"):
            raw = self.opener.request("POST", self.base_url() + path, data="{}", headers=headers, timeout=12)
            self.assertEqual((raw.status_code, json.loads(raw.text)), (403, {"error": "token_kind"}), path)

    def test_the_onto_bot_reads_introspect_and_profile_stays_steward_only(self):
        bot_token = onto_token.mint_bot_token(self.env)
        headers = {"Authorization": "Bearer %s" % bot_token, "Content-Type": "application/json"}
        resp = self.url_open("/onto/v1/introspect", data="{}", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(INTROSPECT_KEYS <= set(resp.json()))
        resp = self.url_open("/onto/v1/profile", data="{}", headers=headers)
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_a_qc_location_shows_its_usage_and_onto_role(self):
        wh = self.env["stock.warehouse"].create(
            {"name": PROBE + " warehouse", "code": "OIQ", "company_id": self.company.id}
        )
        qc = self.env["stock.location"].create({
            "name": PROBE + " QC", "usage": "internal", "location_id": wh.view_location_id.id,
            "company_id": self.company.id,
        })
        Map = self.env["onto.org.map"]
        group = Map.create({"name": PROBE + " group", "kind": "group"})
        node = Map.create({
            "name": PROBE + " warehouse", "kind": "warehouse", "parent_id": group.id, "warehouse_id": wh.id,
        })
        Map.create({
            "name": PROBE + " QC", "kind": "location", "parent_id": node.id, "location_id": qc.id, "role": "qc_hold",
        })
        row = next(loc for loc in self._introspect()["locations"] if loc["id"] == qc.id)
        self.assertEqual((row["usage"], row["onto_role"], row["warehouse_id"]), ("internal", "qc_hold", wh.id))

    def test_an_applied_role_shows_as_onto_role_so_a_rerun_does_not_propose_it_again(self):
        # 2.9a: querygen's OrgMap draft skips every location whose onto_role is set. An applied role shows there,
        # and a location under a mapped one inherits it.
        wh = self.env["stock.warehouse"].create(
            {"name": PROBE + " apply warehouse", "code": "OIE", "company_id": self.company.id}
        )
        expired = self.env["stock.location"].create({
            "name": PROBE + " Expired", "usage": "internal", "location_id": wh.view_location_id.id,
            "company_id": self.company.id,
        })
        shelf = self.env["stock.location"].create({
            "name": PROBE + " shelf", "usage": "internal", "location_id": expired.id, "company_id": self.company.id,
        })
        self.env["onto.org.map"].apply_location_roles([{"location_id": expired.id, "role": "expired"}])
        rows = {loc["id"]: loc for loc in self._introspect()["locations"]}
        self.assertEqual(rows[expired.id]["onto_role"], "expired")
        self.assertEqual(rows[shelf.id]["onto_role"], "expired")

    def test_profile_counts_the_real_rows_of_partner_company_location_and_supplierinfo(self):
        # 2.9f: the raw COUNT(*) covers every company and archived record, so it equals the unfiltered ORM count.
        partner = self.env["res.partner"].create({"name": PROBE + " supplier"})
        template = self.env["product.template"].create({"name": PROBE + " supplied item"})
        self.env["product.supplierinfo"].create(
            {"partner_id": partner.id, "product_tmpl_id": template.id, "price": 3.0}
        )
        profiled = {m["model"]: m for m in self._profile(None)["models"]}
        for model in ("res.partner", "res.company", "stock.location", "product.supplierinfo"):
            self.assertIn(model, profiled, model)
            self.assertNotIn("error", profiled[model], model)
            expected = self.env[model].with_context(active_test=False).sudo().search_count([])
            self.assertEqual(profiled[model]["row_count"], expected, model)

    def test_the_sales_profile_reads_stored_columns_and_purchase_price_needs_sale_margin(self):
        # 2.9f: the Builder's sales_order_line "missing field" is purchase_price, which Odoo stores only when
        # sale_margin is installed. The profile must list it exactly then.
        if "sale.order.line" not in self.env.registry:
            self.skipTest("sale is not installed")
        names = {f["name"] for f in self._profile(["sale.order.line"])["models"][0]["fields"]}
        self.assertTrue({"product_uom_qty", "price_unit", "price_subtotal", "order_partner_id"} <= names)
        margin = self.env["ir.module.module"].search_count(
            [("name", "=", "sale_margin"), ("state", "=", "installed")]
        )
        self.assertEqual("purchase_price" in names, bool(margin))

    def test_a_custom_record_rule_shows_as_custom(self):
        group = self.env["res.groups"].create({"name": PROBE + " group"})
        self.env["ir.rule"].create({
            "name": PROBE + " rule", "model_id": self.env["ir.model"]._get("stock.quant").id,
            "groups": [Command.set([group.id])], "domain_force": "[('id', '!=', 0)]",
        })
        rules = {rule["name"]: rule for rule in self._introspect()["record_rules"]}
        mine = rules[PROBE + " rule"]
        self.assertEqual((mine["custom"], mine["model"], mine["global"]), (True, "stock.quant", False))
        self.assertTrue(any(not rule["custom"] for rule in rules.values()))  # Odoo's own rules are not custom

    def test_an_x_field_you_create_shows_as_custom(self):
        self.env["ir.model.fields"].create({
            "name": "x_onto_probe", "model_id": self.env["ir.model"]._get("res.partner").id,
            "ttype": "char", "state": "manual", "field_description": PROBE,
        })
        models = {m["model"]: m for m in self._introspect()["models"]}
        self.assertNotIn("ir.rule", models)  # internals are not listed
        fields = {f["name"]: f for f in models["res.partner"]["fields"]}
        self.assertEqual((fields["x_onto_probe"]["custom"], fields["x_onto_probe"]["type"]), (True, "char"))
        self.assertFalse(fields["name"]["custom"])

    def test_profile_counts_the_rows_and_fills_you_create(self):
        before = self._profile(["res.partner"])["models"][0]
        self.env["res.partner"].create([
            {"name": PROBE + " A", "email": "a@probe.example"},
            {"name": PROBE + " B", "email": "b@probe.example"},
            {"name": PROBE + " C"},
        ])
        after = self._profile(["res.partner"])["models"][0]
        email_before = {f["name"]: f for f in before["fields"]}["email"]
        email_after = {f["name"]: f for f in after["fields"]}["email"]
        self.assertEqual(after["row_count"] - before["row_count"], 3)
        self.assertEqual(email_after["filled"] - email_before["filled"], 2)
        self.assertEqual(email_after["fill_rate"], round(email_after["filled"] / after["row_count"], 3))

    def test_a_profile_of_partners_and_locations_has_rows_named_or_by_default(self):
        for body_models in (["res.partner", "stock.location"], None):  # named, then the default the web app sends
            profiled = {m["model"]: m for m in self._profile(body_models)["models"]}
            for model in ("res.partner", "stock.location"):
                self.assertGreater(profiled[model]["row_count"], 0, (model, body_models))

    def test_the_readiness_facts_count_storable_products_and_warehouses(self):
        before = self._profile(["res.partner"])["facts"]
        self.env["product.product"].create({"name": PROBE + " storable", "type": "product"})
        self.env["stock.warehouse"].create({
            "name": PROBE + " second warehouse", "code": "OIR", "company_id": self.company.id,
        })
        facts = self._profile(["res.partner"])["facts"]
        self.assertIsInstance(facts["storable_products"], int)
        self.assertGreaterEqual(facts["storable_products"], before["storable_products"] + 1)
        self.assertIsInstance(facts["warehouse_count"], int)
        self.assertGreaterEqual(facts["warehouse_count"], before["warehouse_count"] + 1)

    def test_a_field_outside_the_stored_set_is_not_profiled(self):
        names = {f["name"] for f in self._profile(["product.product"])["models"][0]["fields"]}
        self.assertIn("default_code", names)
        self.assertNotIn("qty_available", names)  # computed, not stored

    def test_profile_skips_names_it_may_not_read_and_refuses_a_bad_list(self):
        self.assertEqual(self._profile(["no.such.model", "ir.rule"])["models"], [])
        resp = sealed_request(self, self.steward_key, "POST", "/onto/v1/profile", body={"models": "stock.quant"})
        self.assertEqual((resp.status_code, resp.json()), (422, {"error": "bad_models"}))

    def test_no_record_value_appears_in_either_reply(self):
        self.env["res.partner"].create({"name": "Probe value kx93", "comment": "Probe note kx93"})
        for path, body in (("/onto/v1/introspect", {}), ("/onto/v1/profile", {"models": None})):
            resp = sealed_request(self, self.steward_key, "POST", path, body=body)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertNotIn("kx93", resp.text, path)
