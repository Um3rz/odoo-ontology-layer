import hashlib
import json
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, tagged

from ..models import onto_provision
from ..models.onto_provision import managed_dataset_unlocked
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

STEWARD = "odoo_dashboards_bi.group_onto_steward"
ROUTE = "/onto/v1/provision"
STOCK_GROUP = "stock.group_stock_user"
MANAGER_GROUP = "stock.group_stock_manager"
STOCK_SQL = (
    "SELECT q.id AS _onto_id, q.product_id, q.location_id, q.quantity "
    "FROM stock_quant q JOIN stock_location l ON l.id = q.location_id WHERE l.usage = 'internal'"
)
PRODUCT_SQL = "SELECT p.id AS _onto_id, p.default_code, p.active FROM product_product p"


def _obj(key, sql, base_model, columns=("quantity",), groups=(STOCK_GROUP,), engine_only=False):
    body = {
        "key": key,
        "label": key.replace("_", " ").title(),
        "base_model": base_model,
        "sql": sql,
        "columns": [{"name": c, "type": "float", "role": "measure", "unit": "qty", "pii": False} for c in columns],
        "group_xmlids": list(groups),
        "engine_only": engine_only,
    }
    fields = ("key", "base_model", "sql", "columns", "group_xmlids", "engine_only")
    body["spec_hash"] = hashlib.sha256(
        json.dumps({f: body[f] for f in fields}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return body


@tagged("post_install", "-at_install")
class TestOntoProvision(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group_user = cls.env.ref("base.group_user")
        cls.steward = cls.env["res.users"].create({
            "name": "Onto Provision Steward", "login": "onto_provision_steward",
            "groups_id": [Command.set([group_user.id, cls.env.ref(STEWARD).id])],
        })
        cls.person = cls.env["res.users"].create({
            "name": "Onto Provision Person", "login": "onto_provision_person",
            "groups_id": [Command.set([group_user.id])],
        })
        # A stock user can read a stock-group dataset, so a refusal in the tests below comes from the lock.
        cls.stocker = cls.env["res.users"].create({
            "name": "Onto Provision Stocker", "login": "onto_provision_stocker",
            "groups_id": [Command.set([group_user.id, cls.env.ref(STOCK_GROUP).id])],
        })
        cls.steward_key = cls.env["res.users.apikeys"].with_user(cls.steward)._generate("mobile", "onto-provision")
        cls.person_key = cls.env["res.users.apikeys"].with_user(cls.person)._generate("mobile", "onto-provision")

    def _provision(self, objects, key=None):
        body = {"release": {"hash": "release-test-1"}, "objects": objects}
        return sealed_request(self, key or self.steward_key, "POST", ROUTE, body=body)

    def _managed(self, key):
        self.env.invalidate_all()
        return self.env["dataset.saas"].with_context(active_test=False).search(
            [("onto_managed", "=", True), ("onto_object_key", "=", key)]
        )

    def _oid(self, view):
        self.env.cr.execute("SELECT oid FROM pg_class WHERE relname = %s", (view,))
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _viewdef(self, view):
        self.env.cr.execute("SELECT definition FROM pg_views WHERE viewname = %s", (view,))
        return self.env.cr.fetchone()[0]

    def test_provision_creates_managed_views_for_two_objects(self):
        resp = self._provision([
            _obj("stock_position", STOCK_SQL, "stock.quant"),
            _obj("product_catalog", PRODUCT_SQL, "product.product", ("default_code", "active"), (MANAGER_GROUP,)),
        ])
        self.assertEqual(resp.status_code, 200, resp.text)
        results = resp.json()["results"]
        self.assertEqual([r["status"] for r in results], ["created", "created"])
        self.assertEqual(results[0]["view"], "wad_onto_stock_position_view")
        for key in ("stock_position", "product_catalog"):
            dataset = self._managed(key)
            self.assertEqual((dataset.name, dataset.state, dataset.use_custom_query), ("onto_" + key, "active", True))
            self.assertEqual(dataset.onto_release, "release-test-1")
            self.assertTrue(dataset.group_ids)
            self.assertTrue(self._oid(dataset.table))
            self.assertTrue(dataset.line_ids)  # sync_lines_from_view_columns ran inside the provisioner's lock
        self.assertEqual(self._managed("product_catalog").group_ids, self.env.ref(MANAGER_GROUP))

    def test_reprovision_is_unchanged_and_keeps_the_view(self):
        objects = [_obj("stock_position", STOCK_SQL, "stock.quant")]
        self.assertEqual(self._provision(objects).status_code, 200)
        oid = self._oid("wad_onto_stock_position_view")
        calls = []
        original = type(self.env["dataset.saas"]).generate_dataset

        def spy(dataset):
            calls.append(dataset.id)
            return original(dataset)

        with patch.object(type(self.env["dataset.saas"]), "generate_dataset", spy):
            resp = self._provision(objects)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual([r["status"] for r in resp.json()["results"]], ["unchanged"])
        self.assertEqual(calls, [])
        self.assertEqual(self._oid("wad_onto_stock_position_view"), oid)

    def test_a_changed_spec_rebuilds_the_view_once(self):
        self.assertEqual(self._provision([_obj("stock_position", STOCK_SQL, "stock.quant")]).status_code, 200)
        before = self._viewdef("wad_onto_stock_position_view")
        changed = [_obj("stock_position", STOCK_SQL + " AND q.quantity > 0", "stock.quant")]
        calls = []
        original = type(self.env["dataset.saas"]).generate_dataset

        def spy(dataset):
            calls.append(dataset.id)
            return original(dataset)

        with patch.object(type(self.env["dataset.saas"]), "generate_dataset", spy):
            resp = self._provision(changed)
        self.assertEqual([r["status"] for r in resp.json()["results"]], ["updated"], resp.text)
        self.assertEqual(calls, [self._managed("stock_position").id])
        # saas replaces a view in place when its columns are unchanged, which keeps the OID: compare the definition.
        after = self._viewdef("wad_onto_stock_position_view")
        self.assertNotEqual(after, before)
        self.assertIn("q.quantity > (0)", after)

    def test_a_groups_only_change_updates_group_ids_and_keeps_the_view(self):
        self.assertEqual(self._provision([_obj("stock_position", STOCK_SQL, "stock.quant")]).status_code, 200)
        oid = self._oid("wad_onto_stock_position_view")
        resp = self._provision([_obj("stock_position", STOCK_SQL, "stock.quant", groups=(MANAGER_GROUP,))])
        self.assertEqual([r["status"] for r in resp.json()["results"]], ["updated"], resp.text)
        dataset = self._managed("stock_position")
        self.assertEqual(dataset.group_ids, self.env.ref(MANAGER_GROUP))
        self.assertEqual(self._oid("wad_onto_stock_position_view"), oid)

    def test_a_build_that_failed_after_its_view_is_rebuilt_on_retry(self):
        objects = [_obj("stock_position", STOCK_SQL, "stock.quant")]
        with patch.object(type(self.env["dataset.saas"]), "sync_lines_from_view_columns", side_effect=RuntimeError("boom")):
            first = self._provision(objects)
        self.assertEqual([r["status"] for r in first.json()["results"]], ["failed"], first.text)
        resp = self._provision(objects)
        self.assertEqual([r["status"] for r in resp.json()["results"]], ["updated"], resp.text)
        dataset = self._managed("stock_position")
        self.assertEqual(dataset.state, "active")
        self.assertTrue(dataset.line_ids)

    def test_a_non_steward_gets_404(self):
        resp = self._provision([_obj("stock_position", STOCK_SQL, "stock.quant")], key=self.person_key)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(self._managed("stock_position"))

    def test_the_key_must_match_the_regex(self):
        for bad in ("Bad-Key", "1stock", "stock\n", "x" * 51):
            resp = self._provision([_obj(bad, STOCK_SQL, "stock.quant")])
            self.assertEqual(resp.status_code, 422, bad)
        self.assertFalse(self._managed("stock_position"))

    def test_a_dataset_on_the_view_name_is_a_clash(self):
        # A dataset that predates the name rules: it slugs to onto_clash_probe.
        with managed_dataset_unlocked():
            self.env["dataset.saas"].create({"name": "Onto Clash Probe"})
        resp = self._provision([_obj("clash_probe", STOCK_SQL, "stock.quant")])
        self.assertEqual((resp.status_code, resp.json()["error"]), (409, "name_clash"), resp.text)
        self.assertFalse(self._managed("clash_probe"))

    def test_a_user_dataset_named_like_a_managed_one_is_refused(self):
        Dataset = self.env["dataset.saas"]
        model = self.env["ir.model"]._get("stock.quant")
        with self.assertRaises(UserError):
            Dataset.create({"name": "Onto Stock Position", "model_id": model.id})
        plain = Dataset.create({"name": "Plain stock", "model_id": model.id})
        with self.assertRaises(UserError):
            plain.write({"name": "onto stock"})

    def test_a_managed_dataset_refuses_manual_write_and_unlink(self):
        self.assertEqual(self._provision([_obj("stock_position", STOCK_SQL, "stock.quant")]).status_code, 200)
        dataset = self._managed("stock_position")
        with self.assertRaises(UserError):
            dataset.write({"description": "hand edit"})
        with self.assertRaises(UserError):
            dataset.unlink()
        self.assertTrue(dataset.exists())

    def test_the_context_key_does_not_unlock_a_managed_dataset(self):
        self.assertEqual(self._provision([_obj("stock_position", STOCK_SQL, "stock.quant")]).status_code, 200)
        dataset = self._managed("stock_position").with_user(self.stocker).with_context(onto_provision=True)
        with self.assertRaises(UserError):
            dataset.write({"description": "hand edit"})
        with self.assertRaises(UserError):
            dataset.unlink()
        with self.assertRaises(UserError):
            self.env["dataset.saas"].with_user(self.stocker).with_context(onto_provision=True).create(
                {"name": "Onto Sneaky"}
            )
        self.assertTrue(self._managed("stock_position"))

    def test_managed_lines_refuse_every_change_outside_the_provisioner(self):
        self.assertEqual(self._provision([_obj("stock_position", STOCK_SQL, "stock.quant")]).status_code, 200)
        dataset = self._managed("stock_position")
        line = dataset.line_ids[:1]
        self.assertTrue(line)
        Line = self.env["dataset.line.saas"]
        with self.assertRaises(UserError):
            line.write({"name": "hand edit"})
        with self.assertRaises(UserError):
            line.unlink()
        with self.assertRaises(UserError):
            Line.create({"dataset_id": dataset.id, "name": "hand line"})
        with self.assertRaises(UserError):
            Line.with_user(self.stocker).with_context(onto_provision=True).create(
                {"dataset_id": dataset.id, "name": "hand line"}
            )
        self.assertTrue(line.exists())

    def test_each_object_takes_its_provision_lock_before_it_is_built(self):
        taken = []
        original = onto_provision._lock_object

        def spy(env, key):
            taken.append(key)
            return original(env, key)

        with patch.object(onto_provision, "_lock_object", spy):
            resp = self._provision([
                _obj("stock_position", STOCK_SQL, "stock.quant"),
                _obj("product_catalog", PRODUCT_SQL, "product.product", ("default_code", "active"), (MANAGER_GROUP,)),
            ])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(taken, ["stock_position", "product_catalog"])
        # The same database and key give the same lock; another database or key does not.
        self.assertEqual(onto_provision._lock_id("db", "stock_position"), onto_provision._lock_id("db", "stock_position"))
        self.assertNotEqual(onto_provision._lock_id("db", "stock_position"), onto_provision._lock_id("other", "stock_position"))
        self.assertNotEqual(onto_provision._lock_id("db", "stock_position"), onto_provision._lock_id("db", "product_catalog"))

    def test_a_missing_onto_id_is_422(self):
        resp = self._provision([_obj("no_onto_id", "SELECT q.id AS quant_id, q.quantity FROM stock_quant q", "stock.quant")])
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertIn("project the base id as _onto_id", resp.json()["message"])
        self.assertFalse(self._managed("no_onto_id"))

    def test_the_sql_may_only_read_stored_model_tables(self):
        sql = "SELECT q.id AS _onto_id, q.quantity FROM stock_quant q, pg_class c"
        resp = self._provision([_obj("sneaky", sql, "stock.quant")])
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertIn("pg_class", resp.json()["message"])
        self.assertFalse(self._managed("sneaky"))

    def test_a_bad_object_refuses_the_whole_request_before_any_write(self):
        bad_group = _obj("bad_group", STOCK_SQL, "stock.quant", groups=("stock.no_such_group",))
        empty_groups = _obj("empty_groups", STOCK_SQL, "stock.quant", groups=())
        for bad in (bad_group, empty_groups):
            resp = self._provision([_obj("stock_position", STOCK_SQL, "stock.quant"), bad])
            self.assertEqual(resp.status_code, 422, resp.text)
        self.assertFalse(self._managed("stock_position"))
