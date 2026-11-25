import json

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..models import onto_recommendation, onto_store, onto_token
from ..models.onto_store import StoreError
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

BRANCH = "odoo_dashboards_bi.group_onto_branch_manager"


def _make_user(env, company, name, branches):
    return env["res.users"].create(
        {
            "name": name,
            "login": name.lower().replace(" ", "_"),
            "company_id": company.id,
            "company_ids": [Command.set([company.id])],
            "groups_id": [Command.set([env.ref("base.group_user").id, env.ref(BRANCH).id])],
            "onto_branch_ids": [Command.set([w.id for w in branches])],
        }
    )


def _lines(partner_id, key="act-1"):
    return [
        {
            "option_key": key,
            "line_type": "action",
            "action_type": "activity",
            "params": {"res_model": "res.partner", "res_id": partner_id, "summary": "Recount the shelf"},
            "reason": "Stock is short",
        },
        {"option_key": "none", "line_type": "do_nothing", "reason": "Wait"},
    ]


@tagged("post_install", "-at_install")
class TestOntoRecommendation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.wh = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.wh_other = cls.env["stock.warehouse"].create(
            {"name": "Recommendation other branch", "code": "RCO", "company_id": cls.company.id}
        )
        cls.product = cls.env["product.product"].create({"name": "Recommendation test product"})
        cls.partner = cls.env["res.partner"].create({"name": "Recommendation activity target"})
        cls.bot = cls.env.ref("odoo_dashboards_bi.user_onto_bot")
        cls.bot_env = cls.env(user=cls.bot)
        cls.approver = _make_user(cls.env, cls.company, "Rec Approver", [cls.wh])
        cls.outsider = _make_user(cls.env, cls.company, "Rec Outsider", [cls.wh_other])

    def _propose(self, lines=None, preferred="act-1", ref="rec-1"):
        rows = [
            {
                "external_ref": ref,
                "warehouse_id": self.wh.id,
                "product_id": self.product.id,
                "location_id": self.wh.lot_stock_id.id,
                "shortage": 4.0,
                "reason_code": "below_min",
                "preferred_option_key": preferred,
                "lines": lines if lines is not None else _lines(self.partner.id),
            }
        ]
        return onto_store.onto_upsert(self.bot_env, "onto.recommendation", ["external_ref"], rows)

    def _rec(self, ref="rec-1"):
        return self.env["onto.recommendation"].search([("external_ref", "=", ref)])

    def _as_approver(self, ref="rec-1"):
        return self.env(user=self.approver)["onto.recommendation"].search([("external_ref", "=", ref)])

    def test_upsert_writes_the_proposal_with_its_options(self):
        self.assertEqual(self._propose(), {"written": 1})
        rec = self._rec()
        self.assertEqual((rec.state, rec.version, rec.requester_id), ("proposed", 1, self.bot))
        self.assertEqual(set(rec.option_ids.mapped("option_key")), {"act-1", "none"})
        self.assertEqual(rec.preferred_line_id.option_key, "act-1")

    def _refused(self, lines, preferred="act-1"):
        with self.assertRaises(StoreError) as caught:
            self._propose(lines=lines, preferred=preferred, ref="rec-frozen")
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "bad_line"))
        self.assertFalse(self._rec("rec-frozen"))

    def test_nested_lines_accept_only_the_skeleton_fields(self):
        self.assertEqual(
            onto_recommendation.LINE_KEYS,
            frozenset(("option_key", "line_type", "action_type", "params", "reason")),
        )
        action, none = _lines(self.partner.id)
        for extra in ({"figures": {"qty": 4}}, {"score": 0.9}, {"cost": 3.0}):
            self._refused([dict(action, **extra), none])
        # Free text: the skeleton's reason stops at 300 characters, and a proposal carries no activity note.
        self._refused([dict(action, reason="x" * 301), none])
        self._refused([dict(action, params=dict(action["params"], note="Customer asked about SKU 1234")), none])
        self.assertEqual(self._propose(lines=[dict(action, reason="x" * 300), none]), {"written": 1})

    def test_the_skeleton_do_nothing_line_is_accepted_with_empty_params(self):
        action = _lines(self.partner.id)[0]
        skeleton_none = {
            "option_key": "do_nothing", "line_type": "do_nothing", "params": {}, "reason": "Take no action now.",
        }
        self.assertEqual(
            self._propose(lines=[action, skeleton_none], preferred="do_nothing", ref="rec-skel"), {"written": 1}
        )
        self.assertEqual(set(self._rec("rec-skel").option_ids.mapped("option_key")), {"act-1", "do_nothing"})
        self._refused([action, dict(skeleton_none, params={"x": 1})])

    def test_re_upsert_replaces_the_options_and_bumps_version(self):
        self._propose()
        self._propose(lines=_lines(self.partner.id, key="act-2"), preferred="act-2")
        rec = self._rec()
        self.assertEqual(rec.version, 2)
        self.assertEqual(set(rec.option_ids.mapped("option_key")), {"act-2", "none"})
        self.assertEqual(rec.preferred_line_id.option_key, "act-2")
        self.assertEqual(rec.requester_id, self.bot)

    def test_option_limits_refuse_the_proposal_and_the_model(self):
        four_actions = [dict(_lines(self.partner.id)[0], option_key="a%d" % i) for i in range(4)]
        with self.assertRaises(StoreError) as caught:
            self._propose(lines=four_actions, preferred="a0", ref="rec-limit")
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "bad_line"))
        do_nothing = _lines(self.partner.id)[1]
        with self.assertRaises(StoreError) as caught:
            self._propose(lines=[do_nothing, dict(do_nothing, option_key="none2")], preferred=None, ref="rec-limit")
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "bad_line"))
        self.assertFalse(self._rec("rec-limit"))

        rec = self.env["onto.recommendation"].create({"external_ref": "rec-direct"})
        for i in range(3):
            self.env["onto.recommendation.line"].create(
                {"recommendation_id": rec.id, "option_key": "a%d" % i, "line_type": "action"}
            )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["onto.recommendation.line"].create(
                {"recommendation_id": rec.id, "option_key": "a3", "line_type": "action"}
            )

    def test_approve_runs_the_activity_as_the_approver_and_audits_it(self):
        self._propose()
        rec = self._as_approver()
        rec.action_approve(version=rec.version)
        rec = self._rec()
        self.assertEqual((rec.state, rec.chosen_by), ("done", self.approver))
        activity = self.env["mail.activity"].search(
            [("res_model", "=", "res.partner"), ("res_id", "=", self.partner.id)]
        )
        self.assertEqual(activity.user_id, self.approver)
        audit = self.env["onto.audit"].search([("res_model", "=", "onto.recommendation"), ("res_id", "=", rec.id)])
        self.assertEqual(set(audit.mapped("action")), {"onto.recommendation.approve", "onto.action.activity.create"})
        self.assertEqual(set(audit.mapped("actor_id")), {self.approver})

    def test_stale_and_decided_proposals_are_refused(self):
        self._propose()
        rec = self._as_approver()
        with self.assertRaises(StoreError) as caught:
            rec.action_approve(version=rec.version + 1)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "stale"))
        rec.action_approve(version=rec.version)
        with self.assertRaises(StoreError) as caught:
            self._as_approver().action_approve(version=2)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "locked"))
        with self.assertRaises(StoreError) as caught:
            self._propose()
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "locked"))

    def test_modify_adds_a_modified_copy_and_refuses_fields_outside_the_allowlist(self):
        self._propose()
        rec = self._as_approver()
        original = rec.preferred_line_id
        result = rec.action_modify(
            version=rec.version, line_id=original.id, changes={"summary": "Recount the back shelf"}
        )
        new = self.env["onto.recommendation.line"].browse(result["line_id"])
        self.assertEqual(new.modified_from_id, original)
        self.assertTrue(new.option_key.startswith("modified:"))
        self.assertIn("Recount the back shelf", new.params)
        self.assertEqual(self._rec().version, 2)
        with self.assertRaises(StoreError) as caught:
            self._as_approver().action_modify(version=2, line_id=original.id, changes={"res_id": 1})
        self.assertEqual((caught.exception.code, caught.exception.message), ("limit", "res_id"))

    def test_reject_records_the_reason_and_locks_the_proposal(self):
        self._propose()
        rec = self._as_approver()
        with self.assertRaises(StoreError) as caught:
            rec.action_reject(version=rec.version, reason="   ")
        self.assertEqual(caught.exception.status, 422)
        rec.action_reject(version=rec.version, reason=" Not needed this week ")
        rec = self._rec()
        self.assertEqual((rec.state, rec.decision_reason, rec.version), ("rejected", "Not needed this week", 2))
        with self.assertRaises(StoreError) as caught:
            self._propose()
        self.assertEqual(caught.exception.code, "locked")

    def test_only_onto_actions_write_and_branch_scope_hides_other_branches(self):
        self._propose()
        outsider_view = self.env(user=self.outsider)["onto.recommendation"].search([("external_ref", "=", "rec-1")])
        self.assertFalse(outsider_view)
        rec = self._as_approver()
        with self.assertRaises(AccessError):
            rec.write({"state": "done"})
        with self.assertRaises(AccessError):
            rec.option_ids[:1].write({"reason": "changed"})

    def test_escalation_marker_is_idempotent_and_keeps_the_version(self):
        self._propose()
        rec = self._rec()
        rec.onto_mark_escalated(2)
        rec.onto_mark_escalated(1)
        rec = self._rec()
        self.assertEqual((rec.escalation_level, rec.version), (2, 1))

    def test_form_button_approves_the_preferred_option_at_the_version_shown(self):
        self._propose()
        rec = self._as_approver()
        with self.assertRaises(UserError):
            rec.with_context(onto_version=rec.version + 1).onto_button_approve()
        rec.with_context(onto_version=rec.version).onto_button_approve()
        self.assertEqual(self._rec().state, "done")


@tagged("post_install", "-at_install")
class TestOntoRecommendationRoutes(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.wh = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.product = cls.env["product.product"].create({"name": "Route recommendation product"})
        cls.partner = cls.env["res.partner"].create({"name": "Route activity target"})
        cls.approver = _make_user(cls.env, cls.company, "Route Approver", [cls.wh])
        cls.key = cls.env["res.users.apikeys"].with_user(cls.approver)._generate("mobile", "onto-rec-route-test")
        cls.bot = cls.env.ref("odoo_dashboards_bi.user_onto_bot")
        rows = [
            {
                "external_ref": "rec-route",
                "warehouse_id": cls.wh.id,
                "product_id": cls.product.id,
                "location_id": cls.wh.lot_stock_id.id,
                "shortage": 2.0,
                "preferred_option_key": "act-1",
                "lines": _lines(cls.partner.id),
            }
        ]
        onto_store.onto_upsert(cls.env(user=cls.bot), "onto.recommendation", ["external_ref"], rows)
        cls.rec = cls.env["onto.recommendation"].search([("external_ref", "=", "rec-route")])

    def _post(self, path, token, body):
        headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
        return self.opener.request(
            "POST", self.base_url() + path, data=json.dumps(body), headers=headers, timeout=60
        )

    def test_sealed_approve_acts_as_the_person_and_bot_tokens_cannot_decide(self):
        path = "/onto/v1/recommendations/%d/approve" % self.rec.id
        bot_resp = self._post(path, onto_token.mint_bot_token(self.env), {"version": 1, "line_id": None})
        self.assertEqual(bot_resp.status_code, 403, bot_resp.text)
        stale = sealed_request(self, self.key, "POST", path, body={"version": 2, "line_id": None})
        self.assertEqual((stale.status_code, stale.json()), (409, {"error": "stale"}))
        resp = sealed_request(self, self.key, "POST", path, body={"version": 1, "line_id": None})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["state"], "done")
        rec = self.env["onto.recommendation"].browse(self.rec.id)
        rec.invalidate_recordset()
        self.assertEqual(rec.chosen_by, self.approver)
