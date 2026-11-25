import json
from unittest.mock import patch
from types import SimpleNamespace

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase, new_test_user
from odoo.addons.odoo_dashboards_bi.models import onto_query

from ..models import evidence


@tagged("post_install", "-at_install")
class TestBiAnswerStream(HttpCase):
    def test_only_owner_in_original_company_scope_receives_answer_chunks(self):
        context = {"no_reset_password": True}
        owner = new_test_user(self.env, login="bi_stream_owner", password="stream-fixture", context=context)
        other = new_test_user(self.env, login="bi_stream_other", password="stream-fixture", context=context)
        self.env["onto.agent.setting"].onto_switches({"ai_enabled": True})
        threads = self.env["native.bi.thread"].with_user(owner)
        thread = threads.create_thread()
        turn = threads.start_turn(thread["id"], "Count the sales orders", "stream-1")
        text = "Here are the checked figures from the records you can see.\nSecond line."
        self.env["native.bi.turn"].sudo().browse(turn["id"])._finish("done", card={"text": text})
        path = "/native-bi/chat/%s/answer" % turn["id"]

        self.authenticate(owner.login, "stream-fixture")
        response = self.url_open(path + "?companies=%s" % owner.company_id.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/x-ndjson", response.headers["Content-Type"])
        chunks = [json.loads(line)["text"] for line in response.text.splitlines()]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertEqual(self.url_open(path + "?companies=999999999").status_code, 404)
        self.authenticate(other.login, "stream-fixture")
        self.assertEqual(self.url_open(path).status_code, 404)


@tagged("post_install", "-at_install")
class TestNativeBi(TransactionCase):
    def test_catalog_offers_metrics_after_first_hundred(self):
        metrics = [{"key": "metric_%03d" % i, "object": "test_object"} for i in range(145)]
        metrics.append({"key": "derived", "object": "test_object", "derived_from": "metric_144"})
        config = self.env["ir.config_parameter"].sudo()
        config.set_param("odoo_dashboards_bi.release", "test-release")
        config.set_param("odoo_dashboards_bi.pending_release", "")
        config.set_param("odoo_dashboards_bi.catalog", json.dumps({
            "objects": [{"key": "test_object", "base_model": "res.partner"}], "metrics": metrics,
        }))
        dataset = SimpleNamespace(group_ids=self.env.user.groups_id)
        with patch.object(evidence, "model_for", return_value=SimpleNamespace(_description="Test")), \
             patch.object(evidence, "_view_fields", return_value=[]), \
             patch.object(evidence, "reconciled_metrics", return_value=set()), \
             patch.object(evidence.onto_query, "_published", return_value=dataset), \
             patch.object(evidence.onto_query, "_suspended", return_value=False), \
             patch.object(evidence, "_metric_fields", side_effect=lambda env, model, metric, obj:
                          {**metric, "properties": [], "dimensions": []}):
            result = evidence.catalog(self.env, {})
        self.assertEqual([row["key"] for row in result["metrics"]], [row["key"] for row in metrics])
        self.assertLess(len(json.dumps(result).encode()), 1_500_000)

    def test_formula_catalog_keeps_only_readable_dependencies(self):
        def metric(key, formula=None, properties=()):
            return {"key": key, "object": "stock_position", "derived_from": formula,
                    "properties": [{"key": name} for name in properties]}

        rows = [metric("available", "on_hand - reserved"), metric("on_hand", "quantity", ["quantity"]),
                metric("reserved"), metric("hidden", "secret_operands")]
        self.assertEqual([row["key"] for row in evidence._resolved_metrics(rows)], ["available", "on_hand", "reserved"])
        self.assertEqual([row["key"] for row in evidence._resolved_metrics(rows[1:2] + rows[3:])], ["on_hand"])

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.first = new_test_user(cls.env, login="native_bi_first", groups="base.group_user")
        cls.second = new_test_user(cls.env, login="native_bi_second", groups="base.group_user")
        cls.Thread = cls.env["native.bi.thread"].with_user(cls.first)
        cls.Turn = cls.env["native.bi.turn"].sudo()
        cls.env["onto.agent.setting"].onto_switches({"ai_enabled": True})

    def test_owner_and_idempotency(self):
        thread = self.Thread.create_thread()
        turn = self.Thread.start_turn(thread["id"], "Count records", "n1")
        self.assertEqual(turn, self.Thread.start_turn(thread["id"], "Count records", "n1"))
        with self.assertRaises(UserError):
            self.Thread.start_turn(thread["id"], "Sales", "n2")
        with self.assertRaises(AccessError):
            self.Thread.with_user(self.second).events_after(turn["id"])
        self.assertEqual(self.Thread.cancel_turn(turn["id"])["status"], "cancelled")

    def test_tool_dispatch_and_card_replay(self):
        thread = self.Thread.create_thread()
        contact = self.env["res.partner"].create({"name": "Customer for BI"})
        attachment = {"kind": "record", "model": "res.partner", "record_ids": [contact.id],
                      "page_fields": ["name", "display_name"]}
        turn = self.Thread.start_turn(thread["id"], "Count this selection", "n3", attachment)
        calls = []

        def querygen(_env, path, body, timeout, on_delta=None):
            self.assertEqual(path, "native-bi/v1/step")
            calls.append(body)
            if body["step"] == 0:
                source, = body["context"]["evidence"]
                self.assertEqual(source["source_id"], "odoo:record:res.partner:%s" % contact.id)
                self.assertEqual(source["rows"], [["Customer for BI"]])
                return {
                    "status": "tools", "state": {"opaque": True},
                    "tools": [{"id": "count", "name": "search_count", "arguments": {"model": "res.partner"}}],
                }
            self.assertEqual(body["tool_results"][0]["result"]["rows"], [[1]])
            return {"status": "done", "card": {"text": "Found the selected records."}}

        turn_record = self.Turn.browse(turn["id"])
        with patch("odoo.addons.odoo_dashboards_bi.models.thread.querygen_call", side_effect=querygen):
            turn_record._advance()
            turn_record._advance()
            answer = self.Thread.get_thread(thread["id"])["messages"][-1]
            self.assertEqual(answer["card"]["text"], "Found the selected records.")
            self.assertEqual([source["tool"] for source in answer["sources_used"]], ["attachment", "search_count"])
            self.assertEqual(answer["sources_used"][1]["reference"], "odoo:res.partner")
            events = self.Thread.events_after(turn["id"])["events"]
            self.assertEqual([event["data"] for event in events if event["kind"] == "answer"], [{}])
        self.assertEqual([item["step"] for item in calls], [0, 1])
        # A profile edit (avatar, language) keeps history; a data change shows the answer marked stale.
        self.Thread.env.user.sudo().write({"signature": "<p>profile edit</p>"})
        self.assertFalse(self.Thread.get_thread(thread["id"])["messages"][-1]["stale"])
        contact.name = "Changed after the answer"
        answer = self.Thread.get_thread(thread["id"])["messages"][-1]
        self.assertEqual((answer["card"]["text"], answer["stale"]), ("Found the selected records.", True))
        contact.name = "Customer for BI"
        # Older answers survive presentation changes when all page evidence replays.
        original = evidence.context_for_step(self.Thread.env, turn_record.attachment)["evidence"]
        legacy_proofs = [{"fingerprint": evidence.legacy_page_fingerprint(original, None)}, *turn_record.proofs[1:]]
        turn_record.write({"scope": "legacy-history-scope", "proofs": legacy_proofs})
        self.assertEqual(self.Thread.get_thread(thread["id"])["messages"][-1]["card"]["text"],
                         "Found the selected records.")
        contact.name = "Updated after the answer"
        self.assertNotIn("card", self.Thread.get_thread(thread["id"])["messages"][-1])
        events = self.Thread.events_after(turn["id"])["events"]
        self.assertEqual([event["data"] for event in events if event["kind"] == "source"], [{}, {}])

    def test_page_snapshot_uses_filters(self):
        visible = self.env["res.partner"].create({"name": "Visible page partner"})
        self.env["res.partner"].create({"name": "Outside page"})
        attached = evidence.attachment({"kind": "view", "view_type": "list", "model": "res.partner",
                                        "domain": [["id", "=", visible.id]]})
        source, = evidence.context_for_step(self.Thread.env, attached)["evidence"]
        self.assertEqual(source["record_ids"], [visible.id])
        self.assertEqual(source["source_id"], "odoo:page:res.partner")
        self.assertEqual(len(source["rows"]), 1)

    def test_steward_description_replaces_ai_and_survives_regeneration(self):
        steward = new_test_user(self.env, login="native_bi_description_steward",
                                groups="base.group_user,base.group_system")
        ontology = self.env["native.bi.ontology"].with_user(steward)
        status = {"objects": [{"key": "partner"}], "draft_objects": [{"key": "sales_order"}]}

        def request(_self, path, payload, **_kwargs):
            if path == "/status":
                return {group: [dict(obj) for obj in objects] for group, objects in status.items()}
            self.assertEqual(path, "/descriptions")
            return {"descriptions": {key: "AI proposal for " + key for key in payload["keys"]}}

        with patch.object(type(ontology), "_request", request):
            self.assertEqual(ontology.generate_descriptions(), {"saved": 2})
            self.assertEqual(ontology.get_status()["objects"][0]["description_source"], "ai")
            ontology.save_description("partner", "A customer or supplier we do business with.")
            self.assertEqual(ontology.generate_descriptions(), {"saved": 0})
            partner = ontology.get_status()["objects"][0]
            self.assertEqual(partner["description_source"], "owner")
            self.assertEqual(partner["description"], "A customer or supplier we do business with.")
            with self.assertRaises(AccessError):
                ontology.with_user(self.first).save_description("partner", "Unauthorized edit")

    def test_ad_hoc_never_exposes_technical_models(self):
        with self.assertRaises(AccessError):
            evidence.execute(self.Thread.env, {}, {}, {
                "name": "search_read",
                "arguments": {"model": "ir.config_parameter", "fields": ["value"]},
            })

    def test_settings_only_for_admin(self):
        with self.assertRaises(AccessError):
            self.env["native.bi.ontology"].with_user(self.first).set_enabled(True)

    def test_ontology_activates_only_after_successful_provision(self):
        digest = "a" * 64
        steward = new_test_user(self.env, login="native_bi_steward", groups="base.group_user,base.group_system")
        ontology = self.env["native.bi.ontology"].with_user(steward)
        record = {"key": "partner_count"}
        object_spec = {"key": "contacts"}
        candidate = {
            "release": digest, "metrics": [record], "objects": [object_spec],
            "provision": {"release": {"hash": digest}, "objects": [object_spec]},
        }
        calls = []

        def querygen(_env, path, payload, timeout):
            calls.append(path)
            return {"release": digest} if path.endswith("/activate") else candidate

        def provision(_env, release, objects):
            self.assertEqual(calls, ["native-bi/v1/ontology/publish"])
            self.assertEqual(release, {"hash": digest})
            self.assertEqual(objects, [object_spec])
            return {"results": [{"status": "created"}]}

        source = "odoo.addons.odoo_dashboards_bi.models.ontology"
        with patch(source + ".querygen_call", side_effect=querygen), patch(
            source + ".onto_provision.provision", side_effect=provision,
        ):
            self.assertEqual(ontology.publish(), {"release": digest, "status": "active"})
            self.assertEqual(calls[-1], "native-bi/v1/ontology/activate")
        config = self.env["ir.config_parameter"].sudo()
        self.assertEqual(config.get_param("odoo_dashboards_bi.release"), digest)
        self.assertFalse(config.get_param("odoo_dashboards_bi.pending_release"))

    def test_product_name_lookup_respects_template_and_variant_rules(self):
        visible = self.env["product.product"].create({"name": "BI lookup visible"})
        hidden_template = self.env["product.product"].create({"name": "BI lookup hidden template"})
        hidden_variant = self.env["product.product"].create({"name": "BI lookup hidden variant"})
        for model, hidden in (("product.template", hidden_template.product_tmpl_id),
                              ("product.product", hidden_variant)):
            self.env["ir.rule"].create({
                "name": "BI restrict " + model, "model_id": self.env["ir.model"]._get_id(model),
                "domain_force": repr([("id", "!=", hidden.id)]),
            })
        result = evidence._orm(self.Thread.env, {}, "search_read", {
            "model": "product.product", "domain": [["name", "ilike", "BI lookup"]],
            "fields": ["id", "display_name", "default_code"],
        })
        self.assertEqual([row[0] for row in result["rows"]], [visible.id])

    def test_a_quantity_metric_is_split_by_its_unit_of_measure_dimension(self):
        def chosen(obj, unit):
            return {"object": obj, "unit": unit, "base_model": "stock.quant", "dimensions": ["product", "uom"],
                    "properties": [{"key": "product", "bindings": {"16": "product_id"}},
                                   {"key": "uom", "bindings": {"16": "product_id.uom_id"}}]}
        self.assertEqual(evidence._unit_dimension(self.env, chosen("any_object", "qty")), "uom")
        self.assertIsNone(evidence._unit_dimension(self.env, chosen("any_object", "money")))
        self.assertEqual(evidence._unit_dimension(self.env, chosen("stock_position", "qty")), "stock_uom")

    def test_outstanding_purchase_scans_past_nonmatching_batch(self):
        if "purchase.order" not in self.env:
            self.skipTest("Purchase is not installed")
        supplier = self.env["res.partner"].create({"name": "Scan supplier"})
        product = self.env["product.product"].create({"name": "Scan product", "type": "service"})
        order = self.env["purchase.order"].create({"partner_id": supplier.id})
        values = {"order_id": order.id, "name": "Scan", "product_id": product.id,
                  "product_qty": 1, "product_uom": product.uom_id.id, "price_unit": 1,
                  "date_planned": "2026-10-01 00:00:00"}
        self.env["purchase.order.line"].create([dict(values, qty_received_manual=1) for _ in range(200)])
        target = self.env["purchase.order.line"].create(values)
        order.write({"state": "purchase"})
        attached = {"kind": "view", "model": "purchase.order.line", "domain": [["order_id", "=", order.id]]}
        result = evidence._outstanding_purchase_lines(self.env, attached, {"before": "2026-11-01"})
        self.assertEqual([row[0] for row in result["rows"]], [target.id])
        self.assertFalse(result["truncated"])
        summary = evidence._outstanding_purchase_lines(self.env, attached, {"before": "2026-11-01", "mode": "summary"})
        self.assertEqual(summary["rows"], [[1, 1]])
        with self.assertRaises(UserError):
            evidence._outstanding_purchase_lines(self.env, {"kind": "record", "model": "purchase.order"},
                                                 {"before": "2026-11-01"})

    def test_governed_join_respects_related_record_rules(self):
        parent = self.env["res.partner"].create({"name": "Restricted parent", "is_company": True})
        child = self.env["res.partner"].create({"name": "Visible record", "parent_id": parent.id})
        self.env["ir.rule"].create({
            "name": "Restrict only the parent",
            "model_id": self.env["ir.model"]._get_id("res.partner"),
            "domain_force": repr([("id", "!=", parent.id)]),
        })
        env = self.Thread.env
        self.assertTrue(env["res.partner"].search([("id", "=", child.id)]))
        onto_query._visibility(
            env, "res.partner", env["dataset.saas"], "native_bi_test",
            {"parent_name": "parent_id.name"},
        )
        env.cr.execute("SELECT id FROM onto_visible WHERE id = %s", (child.id,))
        self.assertFalse(env.cr.fetchone())

    def test_widget_keeps_both_filter_scopes_and_excludes_drilldown_ids(self):
        observed = {}

        def fetch(**options):
            observed.update(options)
            return [[{"amount": 24, "record_ids": [1234]}], 0, {}]

        config = SimpleNamespace(ids=[3], filtered=lambda predicate: SimpleNamespace(ids=[3]))
        widget = SimpleNamespace(
            id=5, analytical_type="tile", name="Visible widget",
            dataset_id=SimpleNamespace(get_tile_data=fetch), currency_symbol="USD",
            conditions_line=config, _dashboard_global_filter_groups=lambda: {9: object()},
        )
        dashboard = SimpleNamespace(id=15)
        local = {"conditions_line_id": 3, "filter_values": {"backend": "done"}}
        global_filter = {"conditions_line_id": 9, "filter_values": {"backend": "west"}}
        with patch.object(evidence, "_widget", return_value=(dashboard, widget)):
            result = evidence._widget_evidence(self.Thread.env, {
                "widget_filters": [local], "filters": [global_filter],
            })
            self.assertEqual(result["rows"], [[24]])
            self.assertEqual(result["dashboard_id"], 15)
            self.assertNotIn("record_ids", result)
            self.assertEqual(observed["filter"], [local])
            self.assertEqual(observed["global_filters"], [global_filter])
            with self.assertRaises(AccessError):
                evidence._widget_evidence(self.Thread.env, {"filters": [local]})

    def test_build_auto_approves_then_publishes_and_reports_a_failed_publish(self):
        steward = new_test_user(self.env, login="native_bi_build_steward", groups="base.group_user,base.group_system")
        ontology = self.env["native.bi.ontology"].with_user(steward)
        Ontology = type(ontology)
        calls = []

        def request(_self, path, payload, **_kwargs):
            calls.append((path, payload))
            return {"approved": {"object": 2, "metric": 5}, "skipped": [], "failed": [9]}

        published = {"release": "b" * 64, "status": "active"}
        with patch.object(Ontology, "_request", request), patch.object(Ontology, "publish", return_value=published):
            result = ontology._build_and_go({"proposals": []})
        self.assertEqual(calls, [("/auto-approve", {"uid": steward.id, "org_locations": []})])
        self.assertEqual(result["auto"]["approved"], {"object": 2, "metric": 5})
        self.assertEqual(result["publish"], published)

        with patch.object(Ontology, "_request", request), \
             patch.object(Ontology, "publish", side_effect=UserError("Could not provision sales_order")):
            result = ontology._build_and_go({"proposals": []})
        self.assertEqual(result["publish"], {"status": "failed", "error": "Could not provision sales_order"})
        self.assertIn("auto", result)  # the approvals stay; only the publish failed
