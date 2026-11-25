import json
from unittest.mock import patch

from odoo import Command
from odoo.tests.common import HttpCase, tagged

from ..models import onto_parity, onto_query, onto_scope, onto_store, onto_token
from ..models.onto_provision import provision
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request
from .test_onto_query import POS_SQL, RELEASE, STOCK_GROUP, _col, _matrix, _obj, _reply

PARITY = "/onto/v1/parity/run"
LIST = "/onto/v1/parity/list"
SETTING = "/onto/v1/object-setting"
QUERY = "/onto/v1/query"
STEWARD = "odoo_dashboards_bi.group_onto_steward"
OBJECT = "stock_position"
TITLE = "Onto parity check failed"
# The view also exposes owner_id, so the guard test (res.partner through owner_id) has a column to filter.
OWNER_SQL = POS_SQL.replace("q.location_id AS location_id", "q.location_id AS location_id, q.owner_id AS owner_id")


def _every_row(env, model_name):
    """A broken visibility: every row of the model, whatever the user may read (the projection's failure mode)."""
    return "(SELECT id FROM %s)" % env[model_name]._table, []


@tagged("post_install", "-at_install")
class TestOntoParity(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.wh1 = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.wh2 = cls.env["stock.warehouse"].create(
            {"name": "Parity test branch 2", "code": "PT2", "company_id": cls.company.id}
        )
        cls.loc1, cls.loc2 = cls.wh1.lot_stock_id, cls.wh2.lot_stock_id
        cls.product = cls.env["product.product"].create({"name": "Parity test product", "type": "product"})
        for location in (cls.loc1, cls.loc2):
            cls.env["stock.quant"]._update_available_quantity(cls.product, location, 5)
            cls.env["onto.status"].sudo().create(
                {"product_id": cls.product.id, "location_id": location.id, "status": "healthy"}
            )
        restricted = cls.env["res.groups"].create({"name": "Onto parity test: restricted"})
        cls.env["ir.rule"].create({
            "name": "Onto parity test: warehouse one",
            "model_id": cls.env["ir.model"]._get("stock.quant").id,
            "groups": [Command.set([restricted.id])],
            "domain_force": str([("location_id.warehouse_id", "in", [cls.wh1.id])]),
        })
        cls.restricted = cls._user("onto_parity_restricted", [cls.env.ref(STOCK_GROUP), restricted])
        cls.open = cls._user("onto_parity_open", [cls.env.ref(STOCK_GROUP)])
        cls.steward = cls._user("onto_parity_steward", [cls.env.ref(STOCK_GROUP), cls.env.ref(STEWARD)])
        cls.nobody = cls._user("onto_parity_nobody", [])
        settings = cls.env["onto.agent.setting"].search([("company_id", "=", cls.company.id)])
        cls.setting = settings or cls.env["onto.agent.setting"].create({"company_id": cls.company.id})
        cls.setting.ai_enabled = True

    @classmethod
    def _user(cls, login, groups):
        user = cls.env["res.users"].create({
            "name": login, "login": login, "company_id": cls.company.id,
            "company_ids": [Command.set([cls.company.id])],
            "groups_id": [Command.set([cls.env.ref("base.group_user").id] + [g.id for g in groups])],
        })
        return user, cls.env["res.users.apikeys"].with_user(user)._generate("mobile", "onto-parity-test")

    def setUp(self):
        super().setUp()
        onto_scope.clear_cache()
        # As in test_onto_query: provision commits, so it runs on a fresh cursor nested in the test.
        with self.registry.cursor() as cr:
            result = provision(self.env(cr=cr), {"hash": RELEASE}, [
                _obj(OBJECT, OWNER_SQL, "stock.quant", ("quantity", "inventory_quantity", "location_id", "owner_id")),
            ])
        self.assertEqual([r["status"] for r in result["results"]], ["created"])

    def _dataset(self):
        return onto_parity.published_objects(self.env, OBJECT)

    def _compile(self, payload):
        return _reply(payload, _matrix("wad_onto_stock_position_view", [("on_hand", "quantity", "SUM")]),
                      [_col("on_hand")], bindings={"on_hand": "quantity"})

    def _query_as(self, user):
        """The governed query for one user. Returns (response, the querygen mock): compile is answered locally."""
        spec = {"release": RELEASE, "object": OBJECT, "metric": "on_hand", "limit": 5000}
        with patch.object(onto_query, "querygen_call",
                          side_effect=lambda env, path, payload, timeout: self._compile(payload)) as call:
            resp = sealed_request(self, user[1], "POST", QUERY, body=spec)
        return resp, call

    def _suspensions(self):
        return self.env["onto.parity.suspension"].sudo().search([("object_key", "=", OBJECT)])

    def _suspension_of(self, user):
        """The suspension of one user's signature. The run also checks the seed's users, whose rows are not the test's."""
        signature = onto_store.user_signature(user)
        return self._suspensions().filtered(lambda row: row.signature == signature)

    def _own(self):
        """Signatures of the test's own users (the ones the object's groups admit)."""
        return {onto_store.user_signature(user) for user, _ in (self.restricted, self.open, self.steward)}

    def test_each_signature_agrees_when_the_projection_is_right(self):
        dataset = self._dataset()
        for user in (self.restricted[0], self.open[0], self.steward[0]):
            outcome = onto_parity.check_user(self.env, user, dataset)
            self.assertTrue(outcome["match"], outcome)
            self.assertEqual(outcome["tolerance"], 0)
            orm = self.env["stock.quant"].with_user(user).search_count([])
            self.assertEqual((outcome["values"]["orm"], outcome["values"]["live"]), (orm, orm))
        restricted = onto_parity.check_user(self.env, self.restricted[0], dataset)
        stored = {entry["model"]: entry for entry in restricted["values"]["stored"]}
        self.assertEqual((stored["onto.status"]["mode"], stored["onto.status"]["match"]), ("dims", True))

    def test_a_broken_query_projection_is_caught_suspended_and_refused(self):
        with patch.object(onto_query, "visible_ids_sql", side_effect=_every_row):
            result = onto_parity.run(self.env, OBJECT)
        # The scratch DB holds quants of a second company. "Every row" shows them to every single-company user, so
        # those users mismatch too. The admin sees both companies, so the broken projection changes nothing for them.
        admin = self.env.ref("base.user_admin")
        restricted_sig = onto_store.user_signature(self.restricted[0])
        admin_sig = onto_store.user_signature(admin)
        mismatched = [r["signature"] for r in result["results"] if not r["match"]]
        self.assertIn(restricted_sig, mismatched)
        self.assertIn(admin_sig, [r["signature"] for r in result["results"]])
        self.assertNotIn(admin_sig, mismatched)
        # The run's counts include every seed user, so the test checks its own users' suspensions instead.
        own_mismatched = [sig for sig in self._own() if sig in mismatched]
        self.assertEqual(len(self._suspensions().filtered(lambda row: row.signature in self._own())),
                         len(own_mismatched))
        suspension = self._suspension_of(self.restricted[0])
        self.assertEqual(len(suspension), 1)
        self.assertIn("differs", suspension.reason)
        audit = self.env["onto.audit"].sudo().search([
            ("action", "=", "onto.parity.suspended"), ("res_model", "=", suspension._name),
            ("res_id", "=", suspension.id),
        ])
        self.assertEqual(len(audit), 1)
        notes = self.env["dashboard.notification.saas"].sudo().search(
            [("title", "=", TITLE), ("user_id", "=", self.steward[0].id)])
        self.assertTrue(notes)

        refused, call = self._query_as(self.restricted)
        self.assertEqual((refused.status_code, refused.json()["error"]), (404, "not_found"), refused.text)
        self.assertEqual(call.call_count, 0)  # refused before querygen is asked anything
        admin_key = self.env["res.users.apikeys"].with_user(admin)._generate("mobile", "onto-parity-test")
        served, _ = self._query_as((admin, admin_key))
        self.assertEqual(served.status_code, 200, served.text)

    def test_a_clean_run_lifts_the_suspension(self):
        with patch.object(onto_query, "visible_ids_sql", side_effect=_every_row):
            onto_parity.run(self.env, OBJECT)
        self.assertEqual(len(self._suspension_of(self.restricted[0])), 1)
        onto_parity.run(self.env, OBJECT)
        self.assertFalse(self._suspensions().filtered(lambda row: row.signature in self._own()))
        resp, _ = self._query_as(self.restricted)
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_a_broken_stored_projection_is_caught(self):
        def every_location(env, fact_model, fact_path, visible_sql, params):
            return sorted(env[fact_model].sudo().search([]).mapped(fact_path).ids)

        with patch.object(onto_scope, "_projected_ids", side_effect=every_location):
            onto_scope.clear_cache()
            onto_parity.run(self.env, OBJECT)
        onto_scope.clear_cache()
        suspension = self._suspension_of(self.restricted[0])
        self.assertEqual(len(suspension), 1)
        self.assertIn("stored results differ on", suspension.reason)
        self.assertIn("onto.status", suspension.reason)

    def test_a_check_that_cannot_run_suspends_the_object(self):
        with patch.object(onto_query, "_visibility", side_effect=RuntimeError("boom")):
            result = onto_parity.run(self.env, OBJECT)
        self.assertTrue(result["results"])
        self.assertTrue(all(r["values"] == {"error": "check_failed"} for r in result["results"]))
        suspension = self._suspension_of(self.restricted[0])
        self.assertEqual(len(suspension), 1)
        self.assertIn("check could not run", suspension.reason)

    def test_parity_with_a_guard_model_agrees_with_the_query_path(self):
        partner_a = self.env["res.partner"].create({"name": "Parity customer A"})
        partner_b = self.env["res.partner"].create({"name": "Parity customer B"})
        self.env["ir.rule"].create({
            "name": "Onto parity test: hide customer B",
            "model_id": self.env["ir.model"]._get("res.partner").id,
            "domain_force": str([("id", "!=", partner_b.id)]),
        })
        self.env["stock.quant"]._update_available_quantity(self.product, self.loc2, 3, owner_id=partner_a)
        self.env["stock.quant"]._update_available_quantity(self.product, self.loc2, 4, owner_id=partner_b)
        self.env["onto.object.setting"].create(
            {"name": OBJECT, "base_model": "stock.quant", "guard_models": '["res.partner"]'}
        )
        outcome = onto_parity.check_user(self.env, self.open[0], self._dataset())
        self.assertTrue(outcome["match"], outcome)
        # Owners the user may read: partner B is hidden by the rule, so its quants drop out. Seed owners count too.
        readable = self.env["res.partner"].with_user(self.open[0]).search([]).ids
        expected = self.env["stock.quant"].with_user(self.open[0]).search_count(
            ["|", ("owner_id", "=", False), ("owner_id", "in", readable)]
        )
        self.assertEqual((outcome["values"]["orm"], outcome["values"]["live"]), (expected, expected))

    def test_the_steward_runs_the_check_and_sees_the_suspensions(self):
        resp = sealed_request(self, self.steward[1], "POST", PARITY, body={"object": OBJECT})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        mine = [r for r in body["results"] if r["signature"] in self._own()]
        self.assertTrue(mine)
        self.assertTrue(all(r["match"] for r in mine))
        listed = sealed_request(self, self.steward[1], "POST", LIST, body={})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([s for s in listed.json()["suspensions"] if s["signature"] in self._own()], [])
        for user in (self.restricted, self.nobody):
            self.assertEqual(sealed_request(self, user[1], "POST", PARITY, body={"object": OBJECT}).status_code, 404)
            self.assertEqual(sealed_request(self, user[1], "POST", LIST, body={}).status_code, 404)
        missing = sealed_request(self, self.steward[1], "POST", PARITY, body={"object": "no_such_object"})
        self.assertEqual(missing.status_code, 404)

    def test_the_onto_bot_runs_the_check_with_its_own_token_and_query_tokens_cannot(self):
        bot_token = onto_token.mint_bot_token(self.env)
        headers = {"Authorization": "Bearer %s" % bot_token, "Content-Type": "application/json"}
        resp = self.url_open(PARITY, data=json.dumps({"object": OBJECT}), headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["results"])
        query_token = sealed_request(self, self.steward[1], "POST", "/onto/v1/token", body={}).json()["token"]
        headers = {"Authorization": "Bearer %s" % query_token, "Content-Type": "application/json"}
        resp = self.url_open(PARITY, data=json.dumps({"object": OBJECT}), headers=headers)
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_the_guard_route_is_steward_only_and_validates_each_model(self):
        body = {"object": OBJECT, "guard_models": ["res.partner"]}
        resp = sealed_request(self, self.steward[1], "POST", SETTING, body=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"object": OBJECT, "base_model": "stock.quant", "guard_models": ["res.partner"]})
        setting = self.env["onto.object.setting"].search([("name", "=", OBJECT), ("company_id", "=", self.company.id)])
        self.assertEqual(setting.guard_models, '["res.partner"]')
        self.assertEqual(sealed_request(self, self.restricted[1], "POST", SETTING, body=body).status_code, 404)
        self.assertEqual(sealed_request(self, self.steward[1], "POST", SETTING,
                                        body={"object": "no_such_object", "guard_models": []}).status_code, 404)
        bad = sealed_request(self, self.steward[1], "POST", SETTING,
                             body={"object": OBJECT, "guard_models": ["res.country"]})
        self.assertEqual((bad.status_code, bad.json()), (422, {"error": "bad_guard_model"}))
        not_a_list = sealed_request(self, self.steward[1], "POST", SETTING,
                                    body={"object": OBJECT, "guard_models": "res.partner"})
        self.assertEqual((not_a_list.status_code, not_a_list.json()), (422, {"error": "bad_guard_models"}))
        generic = sealed_request(self, self.steward[1], "POST", SETTING,
                                 body={"object": OBJECT, "guard_models": [], "base_model": "res.users"})
        self.assertEqual((generic.status_code, generic.json()), (422, {"error": "unknown_field"}))
        self.assertEqual(setting.guard_models, '["res.partner"]')  # refusals wrote nothing

    def test_the_weekly_cron_is_active_and_a_clean_run_suspends_nothing(self):
        cron = self.env.ref("odoo_dashboards_bi.ir_cron_onto_parity")
        self.assertTrue(cron.active)
        self.assertEqual((cron.interval_type, cron.interval_number), ("weeks", 1))
        self.env["onto.parity.suspension"]._onto_parity_cron()
        self.assertFalse(self._suspensions().filtered(lambda row: row.signature in self._own()))
