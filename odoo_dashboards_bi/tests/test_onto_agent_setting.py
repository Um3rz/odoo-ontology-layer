import json

from odoo import Command
from odoo.tests.common import HttpCase, tagged

from ..models import onto_token
from ..models.onto_token import BOT_XMLID
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

SETTING_FIELDS = ["company_id", "ai_enabled", "agents_paused"]


@tagged("post_install", "-at_install")
class TestOntoAgentSetting(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other = cls.env["res.company"].create({"name": "Agent setting other company"})
        cls.steward = cls._user("Setting Steward", "setting_steward", "odoo_dashboards_bi.group_onto_steward")
        cls.branch = cls._user("Setting Branch", "setting_branch", "odoo_dashboards_bi.group_onto_branch_manager")
        cls.steward_key = cls.env["res.users.apikeys"].with_user(cls.steward)._generate(
            "mobile", "onto-agent-setting-steward")
        cls.branch_key = cls.env["res.users.apikeys"].with_user(cls.branch)._generate(
            "mobile", "onto-agent-setting-branch")
        cls.setting = cls.env["onto.agent.setting"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.setting = cls.setting or cls.env["onto.agent.setting"].create({"company_id": cls.company.id})
        cls.setting.write({"ai_enabled": False, "agents_paused": False})
        cls.other_setting = cls.env["onto.agent.setting"].create({"company_id": cls.other.id, "agents_paused": True})

    @classmethod
    def _user(cls, name, login, group_xmlid):
        return cls.env["res.users"].create({
            "name": name,
            "login": login,
            "company_id": cls.company.id,
            "company_ids": [Command.set([cls.company.id])],
            "groups_id": [Command.set([cls.env.ref("base.group_user").id, cls.env.ref(group_xmlid).id])],
        })

    def _bearer(self, path, token, body):
        headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
        return self.opener.request(
            "POST", self.base_url() + path, data=json.dumps(body), headers=headers, timeout=60
        )

    def _query_token(self, key):
        resp = sealed_request(self, key, "POST", "/onto/v1/token", body={})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["token"]

    def _switch(self, body, key=None):
        return sealed_request(self, key or self.steward_key, "POST", "/onto/v1/agent_setting", body=body)

    def _audit_count(self, flag):
        return self.env["onto.audit"].search_count([("action", "=", "onto.agent.%s" % flag)])

    def test_kill_switch_defaults_off_and_each_change_is_audited_once(self):
        fresh = self.env["res.company"].create({"name": "Kill switch default company"})
        setting = self.env["onto.agent.setting"].create({"company_id": fresh.id})
        self.assertFalse(setting.agents_paused)
        before = self._audit_count("agents_paused")
        setting.agents_paused = True
        self.assertEqual(self._audit_count("agents_paused"), before + 1)
        setting.agents_paused = True  # no change, so no audit row
        self.assertEqual(self._audit_count("agents_paused"), before + 1)

    def test_store_read_returns_the_switches_for_the_callers_company_only(self):
        body = {"model": "onto.agent.setting", "fields": SETTING_FIELDS, "domain": []}
        resp = self._bearer("/onto/v1/store/read", self._query_token(self.steward_key), body)
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = json.loads(resp.text)
        self.assertEqual([row["id"] for row in rows], [self.setting.id])
        self.assertEqual(rows[0]["company_id"][0], self.company.id)
        self.assertFalse(rows[0]["agents_paused"])

    def test_bot_reads_the_switches_and_cannot_write_them(self):
        bot = onto_token.mint_bot_token(self.env)
        body = {"model": "onto.agent.setting", "fields": SETTING_FIELDS, "domain": []}
        read = self._bearer("/onto/v1/store/read", bot, body)
        self.assertEqual(read.status_code, 200, read.text)
        ids = [row["id"] for row in json.loads(read.text)]
        self.assertIn(self.setting.id, ids)
        self.assertNotIn(self.other_setting.id, ids)
        upsert = self._bearer(
            "/onto/v1/store/upsert", bot, {"model": "onto.agent.setting", "key": ["company_id"], "rows": []}
        )
        self.assertEqual((upsert.status_code, json.loads(upsert.text)), (422, {"error": "bad_model"}))

    def test_steward_reads_then_sets_the_switches_with_one_audit_row_per_change(self):
        read = self._switch({})
        expected = {"company_id": self.company.id, "ai_enabled": False, "agents_paused": False}
        self.assertEqual((read.status_code, read.json()), (200, expected), read.text)
        before = self._audit_count("agents_paused")
        paused = self._switch({"agents_paused": True})
        expected["agents_paused"] = True
        self.assertEqual((paused.status_code, paused.json()), (200, expected), paused.text)
        self.assertEqual(self._audit_count("agents_paused"), before + 1)
        again = self._switch({"agents_paused": True})
        self.assertEqual((again.status_code, again.json()), (200, expected), again.text)
        self.assertEqual(self._audit_count("agents_paused"), before + 1)
        ai_before = self._audit_count("ai_enabled")
        ai = self._switch({"ai_enabled": True, "agents_paused": False})
        expected.update({"ai_enabled": True, "agents_paused": False})
        self.assertEqual((ai.status_code, ai.json()), (200, expected), ai.text)
        self.assertEqual(self._audit_count("ai_enabled"), ai_before + 1)
        self.assertEqual(self._audit_count("agents_paused"), before + 2)

    def test_branch_manager_gets_404_query_token_403_and_bot_404(self):
        self.assertEqual(self._switch({}, key=self.branch_key).status_code, 404)
        refused = self._bearer("/onto/v1/agent_setting", self._query_token(self.steward_key), {})
        self.assertEqual((refused.status_code, json.loads(refused.text)), (403, {"error": "token_kind"}))
        bot = onto_token.mint_bot_token(self.env)
        self.assertEqual(self._bearer("/onto/v1/agent_setting", bot, {}).status_code, 404)

    def test_bad_bodies_are_refused(self):
        not_bool = self._switch({"agents_paused": "yes"})
        self.assertEqual((not_bool.status_code, not_bool.json()), (422, {"error": "bad_value"}))
        unknown = self._switch({"company_id": self.other.id})
        self.assertEqual((unknown.status_code, unknown.json()), (422, {"error": "unknown_field"}))

    def test_store_reads_agent_runs_by_the_record_rules(self):
        bot_user = self.env.ref(BOT_XMLID)
        self.env["onto.agent.run"].create({
            "run_key": "killswitch-run-1",
            "idempotency_key": "killswitch-run-1:1",
            "user_id": bot_user.id,
            "kind": "playbook",
            "state": "done",
            "trigger": "nightly",
            "credits": 0.5,
        })
        body = {
            "model": "onto.agent.run",
            "fields": ["run_key", "kind", "state", "credits"],
            "domain": [["run_key", "=", "killswitch-run-1"]],
        }
        steward = self._bearer("/onto/v1/store/read", self._query_token(self.steward_key), body)
        self.assertEqual(steward.status_code, 200, steward.text)
        self.assertEqual([row["run_key"] for row in json.loads(steward.text)], ["killswitch-run-1"])
        branch = self._bearer("/onto/v1/store/read", self._query_token(self.branch_key), body)
        self.assertEqual(branch.status_code, 200, branch.text)
        self.assertEqual(json.loads(branch.text), [])
