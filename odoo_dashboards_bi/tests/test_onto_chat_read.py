import json

from odoo import Command
from odoo.tests.common import HttpCase, tagged

from ..models import onto_token
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

BRANCH = "odoo_dashboards_bi.group_onto_branch_manager"
THREAD_FIELDS = ["title", "user_id"]
MESSAGE_FIELDS = ["thread_id", "role", "text", "card"]


@tagged("post_install", "-at_install")
class TestOntoChatRead(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.alice = cls._user("Chat Read Alice", "chat_read_alice")
        cls.bob = cls._user("Chat Read Bob", "chat_read_bob")
        cls.alice_key = cls.env["res.users.apikeys"].with_user(cls.alice)._generate("mobile", "onto-chat-read-alice")
        cls.bob_key = cls.env["res.users.apikeys"].with_user(cls.bob)._generate("mobile", "onto-chat-read-bob")
        cls.alice_thread = cls.env["onto.chat.thread"].with_user(cls.alice).create({"title": "Alice chat"})
        cls.bob_thread = cls.env["onto.chat.thread"].with_user(cls.bob).create({"title": "Bob chat"})
        cls.alice_msg = cls.env["onto.chat.message"].with_user(cls.alice).create(
            {"thread_id": cls.alice_thread.id, "role": "user", "text": "Alice question"})
        cls.bob_msg = cls.env["onto.chat.message"].with_user(cls.bob).create(
            {"thread_id": cls.bob_thread.id, "role": "user", "text": "Bob question"})

    @classmethod
    def _user(cls, name, login):
        return cls.env["res.users"].create({
            "name": name,
            "login": login,
            "company_id": cls.company.id,
            "company_ids": [Command.set([cls.company.id])],
            "groups_id": [Command.set([cls.env.ref("base.group_user").id, cls.env.ref(BRANCH).id])],
        })

    def _call(self, path, token, body):
        headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
        return self.opener.request(
            "POST", self.base_url() + path, data=json.dumps(body), headers=headers, timeout=60
        )

    def _query_token(self, key):
        resp = sealed_request(self, key, "POST", "/onto/v1/token", body={})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["token"]

    def _read_ids(self, key, model, fields, domain=None):
        body = {"model": model, "fields": fields, "domain": domain or []}
        resp = self._call("/onto/v1/store/read", self._query_token(key), body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return [row["id"] for row in json.loads(resp.text)]

    def test_each_user_reads_only_their_own_threads_and_messages(self):
        self.assertEqual(self._read_ids(self.alice_key, "onto.chat.thread", THREAD_FIELDS), [self.alice_thread.id])
        self.assertEqual(self._read_ids(self.bob_key, "onto.chat.thread", THREAD_FIELDS), [self.bob_thread.id])
        self.assertEqual(self._read_ids(self.alice_key, "onto.chat.message", MESSAGE_FIELDS), [self.alice_msg.id])
        self.assertEqual(self._read_ids(self.bob_key, "onto.chat.message", MESSAGE_FIELDS), [self.bob_msg.id])

    def test_query_token_cannot_read_another_users_thread_by_id(self):
        domain = [["id", "=", self.bob_thread.id]]
        self.assertEqual(self._read_ids(self.alice_key, "onto.chat.thread", THREAD_FIELDS, domain), [])
        self.assertEqual(self._read_ids(self.bob_key, "onto.chat.thread", THREAD_FIELDS, domain), [self.bob_thread.id])

    def test_bot_token_cannot_read_chat_models(self):
        bot = onto_token.mint_bot_token(self.env)
        for model in ("onto.chat.thread", "onto.chat.message"):
            body = {"model": model, "fields": ["id"], "domain": []}
            resp = self._call("/onto/v1/store/read", bot, body)
            self.assertEqual((resp.status_code, json.loads(resp.text)), (403, {"error": "forbidden"}), resp.text)

    def test_chat_models_refuse_upsert_as_bad_model(self):
        bot = onto_token.mint_bot_token(self.env)
        for model in ("onto.chat.thread", "onto.chat.message"):
            resp = self._call("/onto/v1/store/upsert", bot, {"model": model, "key": ["id"], "rows": []})
            self.assertEqual((resp.status_code, json.loads(resp.text)), (422, {"error": "bad_model"}), resp.text)
