import json
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged

from ..models import onto_token
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

RECORD = "/onto/v1/agent/record"
CHARGE = "odoo.addons.odoo_dashboards_bi.models.onto_agent_run.charge_ai_credits"


def _turn(run_id="run-1", kind="chat", thread_id=None, text="How many left?"):
    """A finished turn, shaped as contract 3.10 sends it."""
    return {
        "run": {
            "id": run_id,
            "kind": kind,
            "state": "done",
            "outcome": {"answer": 7},
            "trace": [{"seq": 1, "type": "tool", "tool": "query", "ms": 40, "summary": "7 rows"}],
            "recommendation_ids": [],
        },
        "chat": {
            "thread_id": thread_id,
            "title": "Stock",
            "messages": [
                {"role": "user", "text": text, "card": None},
                {"role": "assistant", "text": "7 units", "card": {"figures": [7]}},
            ],
        } if kind == "chat" else None,
        "usage": {
            "model": "claude-test", "input_tokens": 100, "output_tokens": 20,
            "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0, "credits": 1.5,
        },
        "idempotency_key": run_id + ":1",
    }


@tagged("post_install", "-at_install")
class TestOntoAgentRecord(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base = cls.env.ref("base.group_user")
        branch = cls.env.ref("odoo_dashboards_bi.group_onto_branch_manager")
        steward = cls.env.ref("odoo_dashboards_bi.group_onto_steward")

        def make_user(name, login, groups):
            return cls.env["res.users"].create({
                "name": name,
                "login": login,
                "groups_id": [(6, 0, [base.id] + [g.id for g in groups])],
            })

        cls.alice = make_user("Record Alice", "onto_record_alice", [branch])
        cls.bob = make_user("Record Bob", "onto_record_bob", [branch])
        cls.steward = make_user("Record Steward", "onto_record_steward", [branch, steward])
        cls.alice_key = cls.env["res.users.apikeys"].with_user(cls.alice)._generate("mobile", "onto-record-alice")

    def _token(self, key):
        resp = sealed_request(self, key, "POST", "/onto/v1/token", body={})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["token"]

    def _post(self, token, body):
        headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
        return self.opener.request(
            "POST", self.base_url() + RECORD, data=json.dumps(body), headers=headers, timeout=12
        )

    def _counts(self):
        return [
            self.env["onto.agent.run"].sudo().search_count([]),
            self.env["onto.chat.message"].sudo().search_count([]),
            self.env["onto.chat.thread"].sudo().search_count([]),
            self.env["ai.generation.log.saas"].sudo().search_count([("kind", "=", "onto_chat")]),
        ]

    def test_query_token_writes_its_own_chat_turn(self):
        token = self._token(self.alice_key)
        with patch(CHARGE) as charge:
            resp = self._post(token, _turn())
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = json.loads(resp.text)
        run = self.env["onto.agent.run"].sudo().browse(ids["run_id"])
        thread = self.env["onto.chat.thread"].sudo().browse(ids["thread_id"])
        log = self.env["ai.generation.log.saas"].sudo().browse(ids["generation_log_id"])
        self.assertEqual((run.user_id, run.kind, run.state), (self.alice, "chat", "done"))
        self.assertEqual((thread.user_id, len(thread.message_ids)), (self.alice, 2))
        self.assertEqual((log.kind, log.status, log.user_id), ("onto_chat", "success", self.alice))
        self.assertEqual(run.generation_log_id, log)
        charge.assert_called_once()
        self.assertEqual(charge.call_args.kwargs["generation_log_id"], log.id)

    def test_replay_of_the_same_turn_changes_nothing(self):
        token = self._token(self.alice_key)
        first = json.loads(self._post(token, _turn(run_id="run-2")).text)
        before = self._counts()
        again = self._post(token, _turn(run_id="run-2"))
        self.assertEqual((again.status_code, json.loads(again.text)), (200, first))
        self.assertEqual(self._counts(), before)

    def test_query_token_cannot_name_another_user_or_write_a_system_run(self):
        token = self._token(self.alice_key)
        body = _turn(run_id="run-3")
        body["user_id"] = self.bob.id
        resp = self._post(token, body)
        self.assertEqual((resp.status_code, json.loads(resp.text)), (422, {"error": "unknown_field"}))
        resp = self._post(token, _turn(run_id="run-4", kind="job"))
        self.assertEqual((resp.status_code, json.loads(resp.text)), (403, {"error": "token_kind"}))
        self.assertFalse(self.env["onto.agent.run"].sudo().search([("run_key", "in", ["run-3", "run-4"])]))

    def test_query_token_cannot_append_to_another_users_thread(self):
        thread = self.env["onto.chat.thread"].with_user(self.bob).create({"title": "Bob's"})
        resp = self._post(self._token(self.alice_key), _turn(run_id="run-5", thread_id=thread.id))
        self.assertEqual((resp.status_code, json.loads(resp.text)), (404, {"error": "not_found"}))

    def test_bot_token_records_a_system_run_only_stewards_see(self):
        bot_token = onto_token.mint_bot_token(self.env)
        resp = self._post(bot_token, _turn(run_id="job-1", kind="job"))
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = json.loads(resp.text)
        self.assertIsNone(ids["thread_id"])
        self.assertTrue(self.env["onto.agent.run"].with_user(self.steward).search([("id", "=", ids["run_id"])]))
        self.assertFalse(self.env["onto.agent.run"].with_user(self.alice).search([("id", "=", ids["run_id"])]))

    def test_a_sealed_person_cannot_write_records(self):
        resp = sealed_request(self, self.alice_key, "POST", RECORD, body=_turn(run_id="run-6"))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_ledger_row_keeps_a_summary_and_caps_the_prompt(self):
        token = self._token(self.alice_key)
        resp = self._post(token, _turn(run_id="run-7", text="q" * 9000))
        self.assertEqual(resp.status_code, 200, resp.text)
        log = self.env["ai.generation.log.saas"].sudo().browse(json.loads(resp.text)["generation_log_id"])
        self.assertEqual(len(log.prompt), 8000)
        self.assertNotIn("7 units", log.payload)

    def test_query_token_is_still_refused_on_other_write_routes(self):
        token = self._token(self.alice_key)
        with self.assertRaises(onto_token.TokenError) as ctx:
            onto_token.verify_token(self.env, token, "write")
        self.assertEqual(ctx.exception.code, "token_kind")
