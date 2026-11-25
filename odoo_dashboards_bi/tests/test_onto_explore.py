import hashlib
import json
from unittest.mock import patch

from odoo import Command
from odoo.tests.common import HttpCase, tagged

from ..models import onto_explore
from ..models.onto_provision import provision
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

EXPLORE = "/onto/v1/explore"
LOOKUP = "/onto/v1/lookup_values"
RELEASE = "onto-explore-test-release"
OBJECT = "explore_quant_names"
VIEW = "wad_onto_explore_quant_names_view"
STOCK = "stock.group_stock_user"
MANAGER = "stock.group_stock_manager"
LOOKUP_SQL = (
    "SELECT q.id AS _onto_id, q.quantity AS quantity, l.complete_name AS location_name, q.product_id AS product "
    "FROM stock_quant q JOIN stock_location l ON l.id = q.location_id WHERE l.usage = 'internal'"
)
COUNT_BY_LOCATION = (
    'SELECT t0.location_id AS "location_id", COUNT(*) AS "_aggregate" FROM stock_quant AS t0 GROUP BY 1'
)
COUNT_BY_PRODUCT = (
    'SELECT t0.product_id AS "product_id", COUNT(*) AS "_aggregate" FROM stock_quant AS t0 GROUP BY 1'
)
SUM_QUANTITY = 'SELECT SUM(t0.quantity) AS "_aggregate" FROM stock_quant AS t0'
LOOKUP_REPLY = {
    "release": RELEASE,
    "sql": "SELECT location_name FROM %s" % VIEW,
    "params": {},
    "bindings": {"location_name": "location_id.complete_name"},
}


def _obj(key, sql, base_model, columns):
    """A provision object as the provisioner reads it (the same shape as test_onto_query)."""
    body = {
        "key": key,
        "label": key.replace("_", " ").title(),
        "base_model": base_model,
        "sql": sql,
        "columns": [{"name": name, "type": kind, "role": "measure", "unit": "qty", "pii": False}
                    for name, kind in columns],
        "group_xmlids": [STOCK],
        "engine_only": False,
    }
    fields = ("key", "base_model", "sql", "columns", "group_xmlids", "engine_only")
    body["spec_hash"] = hashlib.sha256(
        json.dumps({f: body[f] for f in fields}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return body


def _reply(sql, params=None, bindings=None):
    """A querygen compile reply: the SQL, its bound values and the field paths it reads."""
    return {"sql": sql, "params": params or {}, "bindings": bindings or {}}


def _spec(**extra):
    return dict({"model": "stock.quant"}, **extra)


@tagged("post_install", "-at_install")
class TestOntoExplore(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.wh1 = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.wh2 = cls.env["stock.warehouse"].create(
            {"name": "Explore test branch 2", "code": "EX2", "company_id": cls.company.id}
        )
        cls.product_a = cls.env["product.product"].create({"name": "Explore product A", "type": "product"})
        cls.product_b = cls.env["product.product"].create({"name": "Explore product B", "type": "product"})
        cls.product_c = cls.env["product.product"].create({"name": "Explore product C", "type": "product"})
        quant = cls.env["stock.quant"]
        quant._update_available_quantity(cls.product_a, cls.wh1.lot_stock_id, 5)
        quant._update_available_quantity(cls.product_a, cls.wh2.lot_stock_id, 5)
        quant._update_available_quantity(cls.product_b, cls.wh1.lot_stock_id, 3)
        quant._update_available_quantity(cls.product_c, cls.wh1.lot_stock_id, 2)
        # Twenty-five internal bins under warehouse one, and one bin under warehouse two.
        for number in range(1, 26):
            location = cls.env["stock.location"].create({
                "name": "Lookup bin %02d" % number, "location_id": cls.wh1.lot_stock_id.id,
                "usage": "internal", "company_id": cls.company.id,
            })
            quant._update_available_quantity(cls.product_a, location, 1)
        other = cls.env["stock.location"].create({
            "name": "Other bin", "location_id": cls.wh2.lot_stock_id.id,
            "usage": "internal", "company_id": cls.company.id,
        })
        quant._update_available_quantity(cls.product_a, other, 1)
        restricted = cls.env["res.groups"].create({"name": "Onto explore test: warehouse one"})
        cls.env["ir.rule"].create({
            "name": "Onto explore test: warehouse one",
            "model_id": cls.env["ir.model"]._get("stock.quant").id,
            "groups": [Command.set([restricted.id])],
            "domain_force": str([("location_id.warehouse_id", "in", [cls.wh1.id])]),
        })
        # Odoo 16 gives all internal users quant read access. Remove that default
        # only in this fixture so the no-stock user exercises an actual ACL denial.
        all_users_acl = cls.env.ref("stock.access_stock_quant_all", raise_if_not_found=False)
        if all_users_acl:
            all_users_acl.active = False
        cls.open = cls._user("onto_explore_open", [cls.env.ref(STOCK)])
        cls.restricted = cls._user("onto_explore_restricted", [cls.env.ref(STOCK), restricted])
        cls.manager = cls._user("onto_explore_manager", [cls.env.ref(MANAGER)])
        cls.nobody = cls._user("onto_explore_nobody", [])
        # D29: AI features are off until a steward switches them on. The tests run with them on.
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
        return user, cls.env["res.users.apikeys"].with_user(user)._generate("mobile", "onto-explore-test")

    def setUp(self):
        super().setUp()
        # provision commits per object: a fresh cursor nests inside the test, as each HTTP request does.
        with self.registry.cursor() as cr:
            result = provision(self.env(cr=cr), {"hash": RELEASE}, [
                _obj(OBJECT, LOOKUP_SQL, "stock.quant", (("quantity", "float"), ("location_name", "char"),
                                                                 ("product", "integer"))),
            ])
        self.assertEqual([r["status"] for r in result["results"]], ["created"])
        # Field groups: quantity is readable by stock managers only (a stored field, so explore can read it).
        field = self.env["stock.quant"]._fields["quantity"]
        previous = field.groups
        field.groups = MANAGER
        self.addCleanup(setattr, field, "groups", previous)

    def _post(self, user, path, body):
        return sealed_request(self, user[1], "POST", path, body=body)

    def _serve(self, explore=None, lookup=None):
        """Patches querygen's compile calls. A path with no handler fails the test, so a refused request must not reach
        querygen. Returns the patch and the list of paths called."""
        calls = []

        def fake(env, path, payload, timeout):
            calls.append(path)
            handler = {"onto/explore/compile": explore, "onto/lookup/compile": lookup}.get(path)
            if handler is None:
                raise AssertionError("querygen path %s must not be called here" % path)
            return handler(payload)

        return patch.object(onto_explore, "querygen_call", side_effect=fake), calls

    # --- describe: the schema graph -----------------------------------------------------------------------

    def test_describe_lists_only_the_fields_the_caller_may_read(self):
        open_resp = self._post(self.open, EXPLORE, {"describe": "stock.quant"})
        manager_resp = self._post(self.manager, EXPLORE, {"describe": "stock.quant"})
        nobody_resp = self._post(self.nobody, EXPLORE, {"describe": "stock.quant"})
        self.assertEqual((open_resp.status_code, manager_resp.status_code), (200, 200), open_resp.text)
        self.assertEqual(nobody_resp.status_code, 403, nobody_resp.text)
        open_fields = {f["name"]: f for f in open_resp.json()["models"][0]["fields"]}
        manager_fields = {f["name"]: f for f in manager_resp.json()["models"][0]["fields"]}
        self.assertNotIn("quantity", open_fields)  # field group: no leak through the schema either
        self.assertIn("quantity", manager_fields)
        self.assertEqual(open_fields["location_id"]["relation"], "stock.location")

    def test_describe_refuses_internal_and_unknown_models_and_bad_depth(self):
        codes = [self._post(self.open, EXPLORE, {"describe": name}).status_code
                 for name in ("ir.config_parameter", "onto.agent.setting", "no.such.model")]
        self.assertEqual(codes, [404, 404, 404])
        resp = self._post(self.open, EXPLORE, {"describe": "stock.quant", "depth": 3})
        self.assertEqual(resp.status_code, 422, resp.text)

    # --- explore: aggregates over the caller's own rows ---------------------------------------------------

    def test_explore_totals_follow_each_users_record_rules(self):
        patcher, calls = self._serve(explore=lambda p: _reply(COUNT_BY_LOCATION))
        with patcher:
            open_resp = self._post(self.open, EXPLORE, _spec(group_by=["location_id"], aggregate={"op": "count"}))
            restricted_resp = self._post(self.restricted, EXPLORE,
                                         _spec(group_by=["location_id"], aggregate={"op": "count"}))
        self.assertEqual((open_resp.status_code, restricted_resp.status_code), (200, 200), open_resp.text)
        totals = {}
        for user, resp in ((self.open, open_resp), (self.restricted, restricted_resp)):
            totals[user[0].login] = sum(row[1] for row in resp.json()["rows"])
            expected = self.env["stock.quant"].with_user(user[0]).search_count([])
            self.assertEqual(totals[user[0].login], expected)
        self.assertNotEqual(totals["onto_explore_open"], totals["onto_explore_restricted"])
        body = open_resp.json()
        self.assertEqual([c["key"] for c in body["columns"]], ["location_id", "_aggregate"])
        self.assertEqual(body["trust"], "uncertified")
        self.assertIsNone(body["release"])
        self.assertEqual(calls, ["onto/explore/compile"] * 2)

    def test_a_field_the_caller_may_not_read_is_refused_in_a_sum_or_a_filter(self):
        patcher, calls = self._serve(explore=lambda p: _reply(SUM_QUANTITY))
        with patcher:
            refused_sum = self._post(self.open, EXPLORE, _spec(aggregate={"op": "sum", "path": "quantity"}))
            refused_filter = self._post(self.open, EXPLORE, _spec(
                group_by=["location_id"], aggregate={"op": "count"},
                filters=[{"path": "quantity", "op": ">", "value": 0}]))
            allowed = self._post(self.manager, EXPLORE, _spec(aggregate={"op": "sum", "path": "quantity"}))
        self.assertEqual((refused_sum.status_code, refused_filter.status_code, allowed.status_code),
                         (403, 403, 200), allowed.text)
        self.assertEqual(calls, ["onto/explore/compile"])  # only the manager's query was compiled

    def test_explore_returns_aggregates_only_and_caps_the_rows(self):
        refused = [
            _spec(group_by=["location_id"]),  # no aggregate: raw rows are not returned
            _spec(group_by=["id"], aggregate={"op": "count"}),  # one row per record
            _spec(aggregate={"op": "sum"}),  # a sum needs a path
            _spec(aggregate={"op": "count"}, limit=501),
        ]
        patcher, calls = self._serve(explore=lambda p: _reply(COUNT_BY_PRODUCT))
        with patcher:
            codes = [self._post(self.open, EXPLORE, body).status_code for body in refused]
            capped = self._post(self.open, EXPLORE, _spec(
                group_by=["product_id"], aggregate={"op": "count"}, limit=2))
        self.assertEqual(codes, [422, 422, 422, 422])
        self.assertEqual(capped.status_code, 200, capped.text)
        self.assertEqual(len(capped.json()["rows"]), 2)
        self.assertTrue(capped.json()["truncated"])
        self.assertEqual(calls, ["onto/explore/compile"])

    def test_querygen_sql_that_could_read_other_tables_is_refused(self):
        bad_sql = [
            'SELECT t0.location_id AS "location_id", COUNT(*) AS "_aggregate" FROM res_users AS t0 GROUP BY 1',
            'WITH x AS (SELECT * FROM stock_quant) SELECT x.location_id AS "location_id", '
            'COUNT(*) AS "_aggregate" FROM x GROUP BY 1',
            'SELECT t0.location_id AS "location_id", COUNT(*) AS "_aggregate" FROM public.stock_quant AS t0 GROUP BY 1',
            COUNT_BY_LOCATION + "; DELETE FROM stock_quant",
        ]
        for sql in bad_sql:
            patcher, _ = self._serve(explore=lambda p, sql=sql: _reply(sql))
            with patcher:
                resp = self._post(self.open, EXPLORE, _spec(group_by=["location_id"], aggregate={"op": "count"}))
            self.assertEqual((resp.status_code, resp.json()["error"]), (502, "querygen_unexpected"), sql)

    def test_a_value_is_bound_as_a_parameter_not_as_sql(self):
        value = "x'); DROP TABLE stock_quant; --"
        sql = ('SELECT t0.location_id AS "location_id", COUNT(*) AS "_aggregate" FROM stock_quant AS t0 '
               'JOIN stock_location AS t1 ON t1.id = t0.location_id WHERE t1.name = %(f0)s GROUP BY 1')
        patcher, _ = self._serve(explore=lambda p: _reply(sql, params={"f0": value}))
        with patcher:
            resp = self._post(self.open, EXPLORE, _spec(
                group_by=["location_id"], aggregate={"op": "count"},
                filters=[{"path": "location_id.name", "op": "=", "value": value}]))
        self.assertEqual((resp.status_code, resp.json()["rows"]), (200, []), resp.text)
        still = self.env["stock.quant"].search_count([])
        self.assertGreater(still, 0)  # the table is still there

    def test_ai_off_refuses_before_querygen_is_called(self):
        patcher, calls = self._serve(explore=lambda p: _reply(COUNT_BY_LOCATION),
                                     lookup=lambda p: LOOKUP_REPLY)
        with patcher:
            self.setting.ai_enabled = False
            try:
                codes = [
                    self._post(self.open, EXPLORE, {"describe": "stock.quant"}).status_code,
                    self._post(self.open, EXPLORE, _spec(group_by=["location_id"], aggregate={"op": "count"})).status_code,
                    self._post(self.open, LOOKUP, {"object": OBJECT, "column": "location_name", "term": "bin"}).status_code,
                ]
            finally:
                self.setting.ai_enabled = True
        self.assertEqual(codes, [403, 403, 403])
        self.assertEqual(calls, [])

    # --- lookup_values: real values of one managed column -------------------------------------------------

    def test_lookup_returns_at_most_twenty_values_and_says_so(self):
        patcher, calls = self._serve(lookup=lambda p: LOOKUP_REPLY)
        with patcher:
            resp = self._post(self.open, LOOKUP, {"object": OBJECT, "column": "location_name", "term": "lookup bin"})
        body = resp.json()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(body["values"]), 20)
        self.assertTrue(body["truncated"])
        self.assertTrue(all("lookup bin" in value.lower() for value in body["values"]))
        self.assertEqual(calls, ["onto/lookup/compile"])

    def test_a_relation_column_matches_names_and_answers_id_name_pairs(self):
        unused = self.env["product.product"].create({"name": "Explore product D"})  # on no quant: not offered
        by_product = dict(LOOKUP_REPLY, sql="SELECT product FROM %s" % VIEW, bindings={"product": "product_id"})
        patcher, _ = self._serve(lookup=lambda p: by_product)
        with patcher:
            resp = self._post(self.open, LOOKUP, {"object": OBJECT, "column": "product", "term": "explore product"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(sorted(map(tuple, resp.json()["values"])), [
            (self.product_a.id, "Explore product A"), (self.product_b.id, "Explore product B"),
            (self.product_c.id, "Explore product C")])
        self.assertNotIn(unused.id, [value[0] for value in resp.json()["values"]])

    def test_lookup_applies_each_users_record_rules(self):
        patcher, _ = self._serve(lookup=lambda p: LOOKUP_REPLY)
        with patcher:
            open_resp = self._post(self.open, LOOKUP, {"object": OBJECT, "column": "location_name", "term": "other bin"})
            restricted_resp = self._post(self.restricted, LOOKUP,
                                         {"object": OBJECT, "column": "location_name", "term": "other bin"})
        self.assertEqual(open_resp.status_code, 200, open_resp.text)
        self.assertEqual(len(open_resp.json()["values"]), 1)
        self.assertEqual(restricted_resp.json()["values"], [])  # the other bin sits under warehouse two

    def test_lookup_refuses_a_column_bound_to_a_field_the_caller_may_not_read(self):
        bound_to_quantity = dict(LOOKUP_REPLY, bindings={"location_name": "quantity"})
        patcher, calls = self._serve(lookup=lambda p: bound_to_quantity)
        with patcher:
            refused = self._post(self.open, LOOKUP, {"object": OBJECT, "column": "location_name", "term": "bin"})
            allowed = self._post(self.manager, LOOKUP, {"object": OBJECT, "column": "location_name", "term": "bin"})
        self.assertEqual((refused.status_code, allowed.status_code), (403, 200), allowed.text)
        self.assertEqual(calls, ["onto/lookup/compile"] * 2)

    def test_a_column_outside_the_view_is_404_before_querygen(self):
        patcher, calls = self._serve(lookup=lambda p: LOOKUP_REPLY)
        with patcher:
            codes = [
                self._post(self.open, LOOKUP, {"object": OBJECT, "column": column, "term": "bin"}).status_code
                for column in ("location_name; DROP TABLE stock_quant", "_onto_id", "no_such_column")
            ]
        self.assertEqual(codes, [404, 404, 404])
        self.assertEqual(calls, [])

    def test_the_term_is_matched_literally(self):
        patcher, _ = self._serve(lookup=lambda p: LOOKUP_REPLY)
        with patcher:
            resps = [self._post(self.open, LOOKUP, {"object": OBJECT, "column": "location_name", "term": term})
                     for term in ("%", "_", "'", "\\", "bin'); DROP TABLE stock_quant; --")]
        for resp in resps:
            self.assertEqual((resp.status_code, resp.json()["values"]), (200, []), resp.text)
        self.assertGreater(self.env["stock.quant"].search_count([]), 0)

    def test_lookup_refuses_sql_that_reads_other_tables(self):
        for sql in ("SELECT name FROM res_users", "SELECT location_name FROM wad_onto_other_view"):
            patcher, _ = self._serve(lookup=lambda p, sql=sql: dict(LOOKUP_REPLY, sql=sql))
            with patcher:
                resp = self._post(self.open, LOOKUP, {"object": OBJECT, "column": "location_name", "term": "bin"})
            self.assertEqual((resp.status_code, resp.json()["error"]), (502, "querygen_unexpected"), sql)
