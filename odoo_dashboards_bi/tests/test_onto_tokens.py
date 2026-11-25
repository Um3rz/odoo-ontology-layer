import json
import os
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests.common import HttpCase, tagged

from odoo.addons.odoo_dashboards_saas.models import e2e

from ..models import onto_token
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

VECTOR_PATH = os.path.join(os.path.dirname(__file__), "onto_token_vector.json")
ONTO_TOKEN_MODULE = "odoo.addons.odoo_dashboards_bi.models.onto_token"


@tagged("post_install", "-at_install")
class TestOntoTokens(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env["res.users"].create(
            {
                "name": "Onto Token User",
                "login": "onto_token_user",
                "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
            }
        )
        cls.key = cls.env["res.users.apikeys"].with_user(cls.user)._generate("mobile", "onto-token-test")

    def _bearer(self, method, path, token, body=None):
        headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
        data = json.dumps(body) if body is not None else None
        return self.opener.request(method, self.base_url() + path, data=data, headers=headers, timeout=12)

    def _query_token(self, key=None, body=None):
        resp = sealed_request(self, key or self.key, "POST", "/onto/v1/token", body=body or {})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["token"]

    def _payload_of(self, token):
        return json.loads(e2e.b64d(token.split(".")[0]).decode("utf-8"))

    def _forge(self, payload):
        """A token signed with the database's real secret: only the payload is wrong."""
        secret = self.env["ir.config_parameter"].sudo().get_param(onto_token.SECRET_PARAM).encode("utf-8")
        body = onto_token._canonical(payload)
        return "%s.%s" % (e2e.b64e(body), e2e.b64e(onto_token._sign(secret, body)))

    def test_query_token_reads_whoami_as_the_user(self):
        token = self._query_token()
        resp = self._bearer("GET", "/onto/v1/whoami", token)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = json.loads(resp.text)
        self.assertEqual(body["user"]["id"], self.user.id)
        self.assertFalse(body["ai_enabled"])
        self.assertEqual(
            body["database"]["uuid"], self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        )

    def test_whoami_works_for_a_sealed_person_too(self):
        resp = sealed_request(self, self.key, "GET", "/onto/v1/whoami")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["user"]["id"], self.user.id)
        # Data Health (2.6): a stock manager's group is reported, so the web app can show the Health page.
        stock_manager = self.env.ref("stock.group_stock_manager")
        self.user.groups_id = [Command.link(stock_manager.id)]
        self.addCleanup(lambda: self.user.write({"groups_id": [Command.unlink(stock_manager.id)]}))
        self.env.flush_all()
        resp = sealed_request(self, self.key, "GET", "/onto/v1/whoami")
        self.assertIn("stock.group_stock_manager", resp.json()["groups"])

    def test_whoami_reply_has_the_frozen_keys_and_the_active_company(self):
        # Contract (D58): querygen reads active_company_id for the kill switch, so the key set is frozen.
        expected = {"user", "groups", "companies", "active_company_id", "odoo_version", "database",
                    "module_version", "ai_enabled"}
        body = json.loads(self._bearer("GET", "/onto/v1/whoami", self._query_token()).text)
        self.assertEqual(set(body), expected)
        self.assertEqual(body["active_company_id"], self.user.company_id.id)
        sealed = sealed_request(self, self.key, "GET", "/onto/v1/whoami").json()
        self.assertEqual(set(sealed), expected)
        self.assertEqual(sealed["active_company_id"], self.user.company_id.id)

    def test_token_is_readable_only_in_the_sealed_response(self):
        resp = sealed_request(self, self.key, "POST", "/onto/v1/token", body={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(set(json.loads(resp.raw.text)), {"v", "iv", "ct"})
        self.assertNotIn("token", resp.raw.text)
        self.assertTrue(onto_token.looks_like_token(resp.json()["token"]))

    def test_expired_token_is_refused(self):
        token = self._query_token()
        later = fields.Datetime.now() + timedelta(minutes=16)
        with patch(ONTO_TOKEN_MODULE + ".onto_now", return_value=later):
            resp = self._bearer("GET", "/onto/v1/whoami", token)
        self.assertEqual((resp.status_code, json.loads(resp.text)), (401, {"error": "expired"}))

    def test_token_for_another_database_is_refused(self):
        payload = self._payload_of(self._query_token())
        payload["db"] = "00000000-0000-4000-8000-000000000000"
        resp = self._bearer("GET", "/onto/v1/whoami", self._forge(payload))
        self.assertEqual((resp.status_code, json.loads(resp.text)), (401, {"error": "bad_token"}))

    def test_tampered_signature_is_refused(self):
        token = self._query_token()
        head, sig = token.split(".")
        tampered = head + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
        resp = self._bearer("GET", "/onto/v1/whoami", tampered)
        self.assertEqual((resp.status_code, json.loads(resp.text)), (401, {"error": "bad_token"}))

    def test_revoked_or_rotated_key_kills_its_tokens_at_once(self):
        other_key = self.env["res.users.apikeys"].with_user(self.user)._generate("mobile", "to-revoke")
        row = self.env["res.users.apikeys"].sudo().search(
            [("user_id", "=", self.user.id)], order="id desc", limit=1
        )
        token = self._query_token(other_key)
        self.assertEqual(self._bearer("GET", "/onto/v1/whoami", token).status_code, 200)
        row.sudo().unlink()
        resp = self._bearer("GET", "/onto/v1/whoami", token)
        self.assertEqual((resp.status_code, json.loads(resp.text)), (401, {"error": "bad_token"}))

    def test_raw_api_key_in_bearer_is_refused_on_onto_routes(self):
        resp = self._bearer("GET", "/onto/v1/whoami", self.key)
        self.assertEqual((resp.status_code, json.loads(resp.text)), (401, {"error": "sealed_required"}))

    def test_token_endpoint_refuses_a_bearer_token(self):
        token = self._query_token()
        resp = self._bearer("POST", "/onto/v1/token", token, body={})
        self.assertEqual((resp.status_code, json.loads(resp.text)), (401, {"error": "sealed_required"}))

    def test_onto_route_without_credentials_is_refused(self):
        resp = sealed_request(self, None, "GET", "/onto/v1/whoami")
        self.assertEqual((resp.status_code, resp.json()["error"]), (401, "sealed_required"))

    def test_token_refuses_companies_the_user_does_not_have(self):
        other = self.env["res.company"].create({"name": "Onto token other company"})
        resp = sealed_request(self, self.key, "POST", "/onto/v1/token", body={"cids": [other.id]})
        self.assertEqual(resp.status_code, 403)

    def test_query_token_is_refused_on_write_routes_and_bot_token_is_not(self):
        token = self._query_token()
        with self.assertRaises(onto_token.TokenError) as ctx:
            onto_token.verify_token(self.env, token, "write")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (403, "token_kind"))
        bot_token = onto_token.mint_bot_token(self.env)
        self.assertEqual(onto_token.verify_token(self.env, bot_token, "write")["typ"], "bot")

    def test_bot_token_is_minted_inside_odoo_and_reads_as_the_bot(self):
        resp = self._bearer("GET", "/onto/v1/whoami", onto_token.mint_bot_token(self.env))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(json.loads(resp.text)["user"]["name"], "Onto AI Bot")

    def test_cross_repo_vector_matches_the_signing_code(self):
        with open(VECTOR_PATH, encoding="utf-8") as handle:
            vector = json.load(handle)
        secret = vector["secret_test_only"].encode("utf-8")
        for name in ("query", "bot"):
            entry = vector[name]
            self.assertEqual(onto_token._canonical(entry["payload"]).decode("utf-8"), entry["signed_bytes"])
            body = entry["signed_bytes"].encode("utf-8")
            expected = "%s.%s" % (e2e.b64e(body), e2e.b64e(onto_token._sign(secret, body)))
            self.assertEqual(expected, entry["token"])
            self.assertTrue(onto_token.looks_like_token(entry["token"]))
        self.assertFalse(onto_token.looks_like_token(self.key))
