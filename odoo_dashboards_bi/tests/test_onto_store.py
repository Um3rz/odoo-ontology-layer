import json

from odoo import Command
from odoo.tests.common import HttpCase, tagged

from ..models import onto_token
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

STATUS_KEY = ["product_id", "location_id"]


@tagged("post_install", "-at_install")
class TestOntoStore(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.wh1 = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.loc = cls.wh1.lot_stock_id
        cls.product = cls.env["product.product"].create({"name": "Store test product"})
        cls.user = cls.env["res.users"].create(
            {
                "name": "Store Branch User",
                "login": "store_branch_user",
                "company_id": cls.company.id,
                "company_ids": [Command.set([cls.company.id])],
                "groups_id": [
                    Command.set([
                        cls.env.ref("base.group_user").id,
                        cls.env.ref("odoo_dashboards_bi.group_onto_branch_manager").id,
                    ])
                ],
                "onto_branch_ids": [Command.set([cls.wh1.id])],
            }
        )
        cls.key = cls.env["res.users.apikeys"].with_user(cls.user)._generate("mobile", "onto-store-test")
        cls.other = cls.env["res.company"].create({"name": "Store other company"})
        other_wh = cls.env["stock.warehouse"].create({"name": "Store other WH", "code": "SOW", "company_id": cls.other.id})
        cls.other_status = cls.env["onto.status"].create(
            {
                "product_id": cls.product.id,
                "location_id": other_wh.lot_stock_id.id,
                "status": "critical",
                "company_id": cls.other.id,
            }
        )
        cls.own_status = cls.env["onto.status"].create(
            {"product_id": cls.product.id, "location_id": cls.loc.id, "status": "healthy"}
        )

    def _call(self, path, token, body):
        headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
        return self.opener.request(
            "POST", self.base_url() + path, data=json.dumps(body), headers=headers, timeout=60
        )

    def _query_token(self):
        resp = sealed_request(self, self.key, "POST", "/onto/v1/token", body={})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["token"]

    def test_bot_upserts_1000_status_rows_then_writes_none(self):
        products = self.env["product.product"].create([{"name": "Bulk %d" % i} for i in range(1000)])
        rows = [{"product_id": p.id, "location_id": self.loc.id, "status": "low"} for p in products]
        body = {"model": "onto.status", "key": STATUS_KEY, "rows": rows}
        bot = onto_token.mint_bot_token(self.env)
        first = self._call("/onto/v1/store/upsert", bot, body)
        self.assertEqual((first.status_code, json.loads(first.text)), (200, {"written": 1000}), first.text)
        second = self._call("/onto/v1/store/upsert", bot, body)
        self.assertEqual((second.status_code, json.loads(second.text)), (200, {"written": 0}), second.text)

    def test_query_token_cannot_upsert(self):
        body = {"model": "onto.status", "key": STATUS_KEY, "rows": []}
        resp = self._call("/onto/v1/store/upsert", self._query_token(), body)
        self.assertEqual((resp.status_code, json.loads(resp.text)), (403, {"error": "token_kind"}))

    def test_person_cannot_upsert(self):
        body = {"model": "onto.status", "key": STATUS_KEY, "rows": []}
        resp = sealed_request(self, self.key, "POST", "/onto/v1/store/upsert", body=body)
        self.assertEqual(resp.status_code, 403)

    def test_bot_cannot_upsert_stock_history(self):
        # Odoo owns onto.stock.daily (3.10): only the stock-history writer writes it, never the bot's upsert.
        row = {"product_id": self.product.id, "location_id": self.loc.id, "date": "2026-11-01", "quantity": 5.0,
               "value": 10.0}
        body = {"model": "onto.stock.daily", "key": ["product_id", "location_id", "date"], "rows": [row]}
        resp = self._call("/onto/v1/store/upsert", onto_token.mint_bot_token(self.env), body)
        self.assertEqual((resp.status_code, json.loads(resp.text)), (422, {"error": "bad_model"}), resp.text)
        self.assertFalse(self.env["onto.stock.daily"].search([("product_id", "=", self.product.id)]))

    def test_user_reads_own_branch_only_and_other_company_reads_nothing(self):
        body = {"model": "onto.status", "fields": ["id", "status"], "domain": [["product_id", "=", self.product.id]]}
        resp = self._call("/onto/v1/store/read", self._query_token(), body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual([row["id"] for row in json.loads(resp.text)], [self.own_status.id])

    def test_upsert_refuses_other_company_and_unknown_fields(self):
        bot = onto_token.mint_bot_token(self.env)
        other_row = {"product_id": self.product.id, "location_id": self.loc.id, "status": "low", "company_id": self.other.id}
        resp = self._call("/onto/v1/store/upsert", bot, {"model": "onto.status", "key": STATUS_KEY, "rows": [other_row]})
        self.assertEqual((resp.status_code, json.loads(resp.text)), (422, {"error": "bad_company"}))
        cost_row = {"product_id": self.product.id, "location_id": self.loc.id, "status": "low", "cost": 1}
        resp = self._call("/onto/v1/store/upsert", bot, {"model": "onto.status", "key": STATUS_KEY, "rows": [cost_row]})
        self.assertEqual((resp.status_code, json.loads(resp.text)), (422, {"error": "bad_row"}))

    def test_read_refuses_fields_outside_the_allowlist(self):
        body = {"model": "onto.status", "fields": ["cost"], "domain": []}
        resp = self._call("/onto/v1/store/read", self._query_token(), body)
        self.assertEqual((resp.status_code, json.loads(resp.text)), (422, {"error": "bad_fields"}))

    def test_action_refuses_every_method_until_one_is_allowlisted(self):
        body = {"model": "onto.alert", "id": 1, "method": "unlink", "args": {}}
        resp = self._call("/onto/v1/store/action", onto_token.mint_bot_token(self.env), body)
        self.assertEqual((resp.status_code, json.loads(resp.text)), (422, {"error": "method_not_allowed"}))

    def test_store_read_filters_on_an_allowlisted_field(self):
        body = {"model": "onto.status", "fields": ["status"], "domain": [["status", "in", ["healthy"]]], "limit": 10}
        resp = self._call("/onto/v1/store/read", self._query_token(), body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(json.loads(resp.text), [{"id": self.own_status.id, "status": "healthy"}])
