import hashlib
import json
from unittest.mock import patch

from odoo import Command
from odoo.tests.common import BaseCase, HttpCase, tagged

from ..models import onto_query
from ..models.onto_provision import managed_dataset_unlocked, provision
from odoo.addons.odoo_dashboards_saas.tests.e2e_helpers import sealed_request

QUERY = "/onto/v1/query"
RELEASE = "onto-query-test-release"
STOCK_GROUP = "stock.group_stock_user"
MANAGER_GROUP = "stock.group_stock_manager"
POS_SQL = (
    "SELECT q.id AS _onto_id, q.quantity AS quantity, q.inventory_quantity AS inventory_quantity, "
    "q.location_id AS location_id FROM stock_quant q JOIN stock_location l ON l.id = q.location_id "
    "WHERE l.usage = 'internal'"
)
PICK_SQL = "SELECT p.id AS _onto_id, p.partner_id AS partner_id FROM stock_picking p"
WHERE = "WHERE _onto_id IN (SELECT id FROM onto_visible)"
AGG = {"SUM": "SUM", "COUNT": "COUNT"}


def _obj(key, sql, base_model, columns, groups=(STOCK_GROUP,), engine_only=False):
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


def _matrix(view, measures, dims=()):
    """A matrix request as querygen's compiler builds it, with the visibility predicate."""
    return {
        "sql_view_name": view, "analytic_type": "matrix", "pre_defined_filter_conditions": WHERE,
        "matrix_row_dims": [{"field_name": d, "field_name_as": d} for d in dims],
        "matrix_col_dim": {"field_name": "1", "grain": "value"},
        "matrix_measures": [{"field_name": f, "aggregation": agg, "alias": alias} for alias, f, agg in measures],
    }


def _tile(view, field, agg):
    return {
        "sql_view_name": view, "analytic_type": "tile", "pre_defined_filter_conditions": WHERE,
        "data_field_name": field, "arithmetic_value_type": agg,
    }


def _reply(payload, request, columns, path="matrix", snapshot=True, bindings=None, derive=(), sql=None,
           params=None):
    """A compile answer as querygen's /onto/compile gives it. Its SQL is built from the request, as querygen does.
    params are the bound values (the spec's, or none)."""
    return {"release": payload["release"], "path": path, "snapshot": snapshot, "request": request,
            "derive": list(derive), "columns": columns, "bindings": bindings or {},
            "sql": _sql_for(request) if sql is None else sql, "previous_request": None,
            "previous_sql": None, "merge": None, "params": dict(params or {})}


def _values_sql(rows, record):
    """Matrix-shaped SQL over literal (location_id, on_hand) rows, each on one visible record, with the visibility
    predicate. rows: (location_id, quantity) pairs."""
    values = ", ".join("(%d, %d, %d)" % (record, loc, qty) for loc, qty in rows)
    return ("SELECT v.loc AS location_id, SUM(v.qty) AS on_hand, 1 AS __matrix_col "
            "FROM (VALUES %s) AS v(_onto_id, loc, qty) %s GROUP BY 1" % (values, WHERE))


def _merge(previous, grain=None, absent="zero", dims=("location_id",), key="on_hand"):
    """querygen's merge block for one measure, as its _merge contract gives it."""
    return {"kind": "previous_period", "previous_period": previous, "match_dims": list(dims),
            "match_bucket": "offset" if grain else None, "bucket_column": "__matrix_col" if grain else None,
            "bucket_grain": grain, "absent": absent,
            "measures": [{"key": key, "previous": key + "_previous", "change": key + "_change",
                          "change_pct": key + "_change_pct"}]}


def _col(key, role="measure", unit="qty"):
    return {"key": key, "label": key.replace("_", " ").title(), "role": role, "unit": unit}


def _sql_for(request):
    """SQL of a request, shaped like querygen's matrix and tile output, without the trailing semicolon."""
    view, where = request["sql_view_name"], request["pre_defined_filter_conditions"]
    if request.get("analytic_type") == "tile":
        return "SELECT %s(%s) FROM %s %s" % (
            request["arithmetic_value_type"], request["data_field_name"], view, where)
    dims = [d["field_name"] for d in request["matrix_row_dims"]]
    keys = dims + ["1 AS __matrix_col"]
    measures = ["%s(%s) AS %s" % (AGG[m["aggregation"]], m["field_name"], m["alias"])
                for m in request["matrix_measures"]]
    ordinals = ", ".join(str(i + 1) for i in range(len(keys)))
    return "SELECT %s FROM %s %s GROUP BY %s ORDER BY %s" % (
        ", ".join(keys + measures), view, where, ordinals, ordinals)


@tagged("post_install", "-at_install")
class TestOntoQuery(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.wh1 = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.wh2 = cls.env["stock.warehouse"].create(
            {"name": "Query test branch 2", "code": "QT2", "company_id": cls.company.id}
        )
        cls.loc1, cls.loc2 = cls.wh1.lot_stock_id, cls.wh2.lot_stock_id
        cls.product = cls.env["product.product"].create({"name": "Query test product", "type": "product"})
        for location in (cls.loc1, cls.loc2):
            cls.env["stock.quant"]._update_available_quantity(cls.product, location, 5)
        restricted = cls.env["res.groups"].create({"name": "Onto query test: restricted"})
        cls.env["ir.rule"].create({
            "name": "Onto query test: warehouse one",
            "model_id": cls.env["ir.model"]._get("stock.quant").id,
            "groups": [Command.set([restricted.id])],
            "domain_force": str([("location_id.warehouse_id", "in", [cls.wh1.id])]),
        })
        cls.restricted = cls._user("onto_query_restricted", [cls.env.ref(STOCK_GROUP), restricted])
        cls.open = cls._user("onto_query_open", [cls.env.ref(STOCK_GROUP)])
        cls.manager = cls._user("onto_query_manager", [cls.env.ref(MANAGER_GROUP)])
        cls.nobody = cls._user("onto_query_nobody", [])
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
        return user, cls.env["res.users.apikeys"].with_user(user)._generate("mobile", "onto-query-test")

    def setUp(self):
        super().setUp()
        # provision commits per object. On the test cursor a commit releases the cursor's own savepoint,
        # which sits outside test_0 and would take the test's savepoint with it. A fresh cursor nests inside
        # test_0, as each HTTP request does, so the commits stay contained.
        with self.registry.cursor() as cr:
            result = provision(self.env(cr=cr), {"hash": RELEASE}, [
                _obj("stock_position", POS_SQL, "stock.quant", ("quantity", "inventory_quantity", "location_id")),
                _obj("deliveries", PICK_SQL, "stock.picking", ("partner_id",)),
                _obj("engine_stock", POS_SQL, "stock.quant", ("quantity",), groups=(), engine_only=True),
            ])
        self.assertEqual([r["status"] for r in result["results"]], ["created"] * 3)

    def _spec(self, **extra):
        return dict({"release": RELEASE, "object": "stock_position", "metric": "on_hand", "limit": 5000}, **extra)

    def _post(self, user, body=None, **kw):
        return sealed_request(self, user[1], "POST", QUERY, body=body or self._spec(**kw))

    def _serve(self, compile_fn):
        """Patches querygen_call: onto/compile answers compile_fn(payload). Any other querygen path fails the
        request, because the route takes its SQL from the compile reply. Returns the patch and the paths called."""
        calls = []

        def fake(env, path, payload, timeout):
            calls.append(path)
            if path != "onto/compile":
                raise AssertionError("querygen path %s must not be called" % path)
            return compile_fn(payload)

        return patch.object(onto_query, "querygen_call", side_effect=fake), calls

    def _total_on_hand(self, sql=None):
        """A compile answer for the on-hand total of stock_position, with its SQL (or the given SQL)."""
        def compile_fn(payload):
            return _reply(payload, _matrix("wad_onto_stock_position_view", [("on_hand", "quantity", "SUM")]),
                          [_col("on_hand")], bindings={"on_hand": "quantity"}, sql=sql)
        return compile_fn

    def test_the_same_query_gives_each_user_their_own_total(self):
        patcher, _ = self._serve(self._total_on_hand())
        with patcher:
            restricted = self._post(self.restricted)
            open_user = self._post(self.open)
        self.assertEqual((restricted.status_code, open_user.status_code), (200, 200), open_user.text)
        totals = {}
        for user, resp in ((self.restricted[0], restricted), (self.open[0], open_user)):
            groups = self.env["stock.quant"].with_user(user).read_group(
                [("location_id.usage", "=", "internal")], ["quantity:sum"], ["location_id"]
            )
            expected = sum(group["quantity"] for group in groups)
            self.assertAlmostEqual(resp.json()["rows"][0][0], expected, places=2)
            totals[user.login] = resp.json()["rows"][0][0]
        self.assertNotEqual(totals["onto_query_restricted"], totals["onto_query_open"])
        body = open_user.json()
        self.assertEqual([c["key"] for c in body["columns"]], ["on_hand"])
        self.assertEqual(body["release"], RELEASE)
        self.assertFalse(body["truncated"])
        self.assertIsNone(body["next_page"])
        self.assertTrue(body["read_at"].endswith("Z"))

    def test_querygen_is_asked_only_to_compile(self):
        # The SQL comes from the compile reply. /query/querygen is never called (the fake fails if it is).
        patcher, calls = self._serve(self._total_on_hand())
        with patcher:
            resp = self._post(self.open)
        self.assertEqual((resp.status_code, calls), (200, ["onto/compile"]), resp.text)

    def test_ai_features_off_is_403_and_nothing_runs(self):
        patcher, calls = self._serve(self._total_on_hand())
        with patcher:
            self.setting.ai_enabled = False
            off = self._post(self.open)
            self.setting.ai_enabled = True
            on = self._post(self.open)
        self.assertEqual((off.status_code, off.json()["error"]), (403, "ai_features_off"), off.text)
        self.assertEqual(on.status_code, 200, on.text)
        self.assertEqual(calls, ["onto/compile"])  # the refused query was not even compiled

    def test_an_unpublished_or_engine_only_object_is_404(self):
        patcher, calls = self._serve(self._total_on_hand())
        with patcher:
            missing = self._post(self.open, self._spec(object="no_such_object"))
            engine = self._post(self.open, self._spec(object="engine_stock"))
            dataset = self.env["dataset.saas"].search([("onto_object_key", "=", "stock_position")])
            with managed_dataset_unlocked():
                dataset.state = "draft"
            draft = self._post(self.open)
        self.assertEqual([missing.status_code, engine.status_code, draft.status_code], [404, 404, 404])
        self.assertEqual(calls, [])  # refused before querygen is asked for anything

    def test_a_user_outside_the_dataset_groups_is_404(self):
        patcher, calls = self._serve(self._total_on_hand())
        with patcher:
            resp = self._post(self.nobody)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(calls, [])

    def test_a_restricted_field_without_its_group_is_403(self):
        # Odoo 16 has no stored manager-only field on stock.quant (inventory_quantity is open to stock users),
        # so the prop is bound to the manager-only inventory_quantity_auto_apply. The check reads the binding.
        def compile_fn(payload):
            return _reply(payload, _matrix("wad_onto_stock_position_view", [("inv", "inventory_quantity", "SUM")]),
                          [_col("inv")], bindings={"inv": "inventory_quantity_auto_apply"})
        patcher, calls = self._serve(compile_fn)
        with patcher:
            refused = self._post(self.open)
            allowed = self._post(self.manager)
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(calls, ["onto/compile"] * 2)  # both were compiled; the refused one got no SQL run

    def test_sql_without_the_visibility_predicate_is_refused(self):
        for sql in ("SELECT SUM(quantity) AS on_hand FROM wad_onto_stock_position_view",
                    "SELECT * FROM wad_onto_stock_position_view"):
            patcher, _ = self._serve(self._total_on_hand(sql=sql))
            with patcher:
                resp = self._post(self.open)
            self.assertEqual((resp.status_code, resp.json()["error"]), (502, "querygen_unexpected"), sql)

    def test_a_compile_reply_without_sql_is_refused(self):
        def compile_fn(payload):
            reply = self._total_on_hand()(payload)
            del reply["sql"]
            return reply
        patcher, _ = self._serve(compile_fn)
        with patcher:
            resp = self._post(self.open)
        self.assertEqual((resp.status_code, resp.json()["error"]), (502, "querygen_unexpected"), resp.text)

    def test_a_past_period_on_a_snapshot_is_insufficient_data(self):
        def compile_fn(snapshot):
            def answer(payload):
                return _reply(payload, _matrix("wad_onto_stock_position_view", [("on_hand", "quantity", "SUM")]),
                              [_col("on_hand")], snapshot=snapshot, bindings={"on_hand": "quantity"})
            return answer
        past = {"from": "2020-01-01", "to": "2020-01-31"}
        patcher, calls = self._serve(compile_fn(True))
        with patcher:
            refused = self._post(self.open, self._spec(period=past))
            future = self._post(self.open, self._spec(period={"from": "2099-01-01", "to": "2099-01-31"}))
        self.assertEqual((refused.status_code, refused.json()["error"]), (422, "invalid_spec"), refused.text)
        self.assertIn("insufficient data", refused.json()["message"])
        self.assertEqual(calls, ["onto/compile"] * 2)  # the future period was answered as of now
        self.assertEqual(future.status_code, 200, future.text)
        patcher, _ = self._serve(compile_fn(False))
        with patcher:
            flow = self._post(self.open, self._spec(period=past))
        self.assertEqual(flow.status_code, 200, flow.text)

    def test_a_zero_denominator_derive_is_null(self):
        def compile_fn(payload):
            request = _matrix("wad_onto_stock_position_view",
                              [("on_hand", "quantity", "SUM"), ("inv", "inventory_quantity", "SUM")])
            # Only this test's quants. Their counted quantity is unset (NULL) on the seed, or 0, so inv is NULL or 0.
            request["pre_defined_filter_conditions"] += (
                " AND _onto_id IN (SELECT id FROM stock_quant WHERE product_id = %d)" % self.product.id)
            return _reply(payload, request, [_col("on_hand"), _col("inv"), _col("fill", unit="ratio")],
                          bindings={"on_hand": "quantity", "inv": "inventory_quantity"},
                          derive=[{"alias": "fill", "formula": "on_hand / inv", "zero_division": "null"}])
        patcher, _ = self._serve(compile_fn)
        with patcher:
            resp = self._post(self.open)
        self.assertEqual(resp.status_code, 200, resp.text)
        on_hand, inv, fill = resp.json()["rows"][0]
        self.assertIn(inv, (0, 0.0, None))
        self.assertIsNone(fill)
        self.assertGreater(on_hand, 0)

    def test_derive_arithmetic_and_nulls(self):
        self.assertEqual(onto_query._evaluate("a / b", {"a": 6, "b": 3}), 2)
        self.assertIsNone(onto_query._evaluate("a / b", {"a": 6, "b": 0}))
        self.assertIsNone(onto_query._evaluate("a / b", {"a": None, "b": 3}))
        self.assertEqual(onto_query._evaluate("(a - b) * 2", {"a": 5, "b": 1.5}), 7.0)
        with self.assertRaises(onto_query.QueryError):
            onto_query._evaluate("__import__('os')", {})

    def test_a_grained_query_keys_the_bucket_by_its_time_property(self):
        def compile_fn(payload):
            request = _matrix("wad_onto_stock_position_view", [("on_hand", "quantity", "SUM")])
            request["matrix_col_dim"] = {"field_name": "day", "grain": "day"}
            sql = ("SELECT SUM(quantity) AS on_hand, DATE '2026-01-01' AS __matrix_col "
                   "FROM wad_onto_stock_position_view " + WHERE + " GROUP BY 2")
            return _reply(payload, request, [_col("day", role="time", unit=None), _col("on_hand")], sql=sql,
                          bindings={"day": "in_date", "on_hand": "quantity"})
        patcher, _ = self._serve(compile_fn)
        with patcher:
            resp = self._post(self.open, self._spec(grain="day"))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual([c["key"] for c in body["columns"]], ["day", "on_hand"])
        self.assertEqual(body["rows"][0][0], "2026-01-01")
        self.assertGreater(body["rows"][0][1], 0)

    def test_truncation_and_paging_walk_every_group(self):
        def compile_fn(payload):
            return _reply(payload, _matrix("wad_onto_stock_position_view", [("on_hand", "quantity", "SUM")],
                                           dims=["location_id"]),
                          [_col("location_id", role="dimension", unit=None), _col("on_hand")],
                          bindings={"location_id": "location_id", "on_hand": "quantity"})
        groups = self.env["stock.quant"].with_user(self.open[0]).read_group(
            [("location_id.usage", "=", "internal")], ["quantity:sum"], ["location_id"]
        )
        self.assertGreaterEqual(len(groups), 2)
        patcher, _ = self._serve(compile_fn)
        rows, page, pages = [], 1, []
        with patcher:
            while page:
                resp = self._post(self.open, self._spec(dims=["location_id"], limit=1, page=page))
                self.assertEqual(resp.status_code, 200, resp.text)
                body = resp.json()
                rows.extend(body["rows"])
                pages.append(body["truncated"])
                page = body["next_page"]
        self.assertEqual(len(rows), len(groups))
        self.assertEqual(pages, [True] * (len(groups) - 1) + [False])
        self.assertAlmostEqual(sum(r[1] for r in rows), sum(g["quantity"] for g in groups), places=2)

    def test_a_guard_model_hides_rows_of_customers_the_user_cannot_read(self):
        partner_a = self.env["res.partner"].create({"name": "Query guard A"})
        partner_b = self.env["res.partner"].create({"name": "Query guard B"})
        self.env["ir.rule"].create({
            "name": "Onto query test: guard customer",
            "model_id": self.env["ir.model"]._get("res.partner").id,
            "domain_force": str([("id", "!=", partner_b.id)]),
        })
        customer = self.env.ref("stock.stock_location_customers")
        for partner in (partner_a, partner_b, self.env["res.partner"]):
            self.env["stock.picking"].create({
                "picking_type_id": self.wh1.out_type_id.id, "location_id": self.loc1.id,
                "location_dest_id": customer.id, "partner_id": partner.id or False,
            })
        user = self.open[0]
        visible = self.env["res.partner"].with_user(user).search([]).ids

        def tile(payload):
            request = _tile("wad_onto_deliveries_view", "_onto_id", "COUNT")
            return _reply(payload, request, [_col("deliveries", unit=None)], path="tile")

        expected_all = self.env["stock.picking"].with_user(user).search_count([])
        expected_guarded = self.env["stock.picking"].with_user(user).search_count(
            ["|", ("partner_id", "=", False), ("partner_id", "in", visible)]
        )
        self.assertLess(expected_guarded, expected_all)
        patcher, _ = self._serve(tile)
        with patcher:
            off = self._post(self.open, self._spec(object="deliveries", metric="deliveries"))
            self.env["onto.object.setting"].create(
                {"name": "deliveries", "base_model": "stock.picking", "guard_models": '["res.partner"]'}
            )
            on = self._post(self.open, self._spec(object="deliveries", metric="deliveries"))
        self.assertEqual(off.json()["rows"], [[expected_all]], off.text)
        self.assertEqual(on.json()["rows"], [[expected_guarded]], on.text)

    def test_a_query_that_runs_past_the_timeout_is_408(self):
        slow = ("SELECT SUM(quantity) AS on_hand FROM wad_onto_stock_position_view "
                "CROSS JOIN generate_series(1, 20000000) AS g(n) WHERE _onto_id IN (SELECT id FROM onto_visible)")
        patcher, _ = self._serve(self._total_on_hand(sql=slow))
        with patcher, patch.object(onto_query, "QUERY_TIMEOUT_MS", 1):
            resp = self._post(self.open)
        self.assertEqual((resp.status_code, resp.json()["error"]), (408, "timeout"), resp.text)

    def test_params_are_forwarded_and_run_with_the_reply_dict(self):
        # The SQL reads %(expiry_days)s and a doubled %%. Both run through the params dict of the reply.
        seen = []
        sql = ("SELECT SUM(quantity) AS on_hand FROM wad_onto_stock_position_view " + WHERE +
               " AND (1 + %(expiry_days)s) > 0 AND '50%%' = '50%%'")

        def compile_fn(payload):
            seen.append(payload["spec"]["params"])
            request = _matrix("wad_onto_stock_position_view", [("on_hand", "quantity", "SUM")])
            return _reply(payload, request, [_col("on_hand")], bindings={"on_hand": "quantity"}, sql=sql,
                          params=payload["spec"]["params"])
        patcher, calls = self._serve(compile_fn)
        with patcher:
            kept = self._post(self.open, self._spec(params={"expiry_days": 5}))
            dropped = self._post(self.open, self._spec(params={"expiry_days": -10}))
        self.assertEqual((kept.status_code, dropped.status_code), (200, 200), kept.text)
        self.assertEqual(seen, [{"expiry_days": 5}, {"expiry_days": -10}])
        self.assertIsNotNone(kept.json()["rows"][0][0])
        self.assertEqual(dropped.json()["rows"], [[None]])  # the predicate read -10: no row
        self.assertEqual(calls, ["onto/compile"] * 2)

    def test_the_current_release_is_resolved_by_querygen(self):
        seen = []

        def compile_fn(payload):
            seen.append(payload["release"])
            reply = self._total_on_hand()(payload)
            reply["release"] = RELEASE  # querygen resolved "current" to this release and returns its hash
            return reply
        patcher, _ = self._serve(compile_fn)
        with patcher:
            resp = self._post(self.open, self._spec(release="current"))
        self.assertEqual((resp.status_code, seen), (200, ["current"]), resp.text)
        self.assertEqual(resp.json()["release"], RELEASE)

    def test_no_current_release_is_404(self):
        def compile_fn(payload):
            raise onto_query.QuerygenError(404)  # querygen: 404 {"detail": "no_current_release"}
        patcher, calls = self._serve(compile_fn)
        with patcher:
            resp = self._post(self.open, self._spec(release="current"))
        self.assertEqual((resp.status_code, resp.json()["error"]), (404, "not_found"), resp.text)
        self.assertEqual(calls, ["onto/compile"])

    def test_compare_pairs_by_dims_and_keeps_missing_sides(self):
        record = self.env["stock.quant"].search([("product_id", "=", self.product.id)], limit=1).id
        current = [(1, 10), (2, 2), (4, 7)]
        previous = [(1, 4), (2, 0), (3, 3)]
        prev_period = {"from": "2026-01-04", "to": "2026-01-31"}

        def compile_fn(payload):
            request = _matrix("wad_onto_stock_position_view", [("on_hand", "quantity", "SUM")], dims=["location_id"])
            reply = _reply(payload, request, [_col("location_id", role="dimension", unit=None), _col("on_hand")],
                           bindings={"location_id": "location_id", "on_hand": "quantity"}, snapshot=False,
                           sql=_values_sql(current, record))
            reply.update(previous_request=dict(request), previous_sql=_values_sql(previous, record),
                         merge=_merge(prev_period))
            return reply
        patcher, calls = self._serve(compile_fn)
        with patcher:
            resp = self._post(self.open, self._spec(dims=["location_id"], period={"from": "2026-02-01", "to": "2026-02-28"},
                                                    compare={"to": "previous_period"}))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual([c["key"] for c in body["columns"]],
                         ["location_id", "on_hand", "on_hand_previous", "on_hand_change", "on_hand_change_pct"])
        self.assertEqual(body["rows"], [
            [1, 10, 4, 6, 1.5],    # matched
            [2, 2, 0, 2, None],    # previous is 0: no percentage
            [4, 7, 0, 7, None],    # missing previous: absent is zero
            [3, 0, 3, -3, -1.0],   # missing current (previous-only group, after the current rows): absent is zero
        ])
        self.assertEqual(calls, ["onto/compile"])

    def test_compare_on_a_snapshot_with_a_past_period_is_insufficient_data(self):
        def compile_fn(payload):
            reply = self._total_on_hand()(payload)
            reply["snapshot"] = True
            reply.update(previous_request=_matrix("wad_onto_stock_position_view", [("on_hand", "quantity", "SUM")]),
                         previous_sql=reply["sql"], merge=_merge({"from": "2019-12-01", "to": "2019-12-31"}, dims=()))
            return reply
        patcher, _ = self._serve(compile_fn)
        with patcher:
            resp = self._post(self.open, self._spec(period={"from": "2020-01-01", "to": "2020-01-31"},
                                                    compare={"to": "previous_period"}))
        self.assertEqual((resp.status_code, resp.json()["error"]), (422, "invalid_spec"), resp.text)
        self.assertIn("insufficient data", resp.json()["message"])

    def test_bad_specs_are_422_and_a_bearer_key_is_refused(self):
        patcher, calls = self._serve(self._total_on_hand())
        with patcher:
            for body in (self._spec(as_of="2026-01-01"), self._spec(compare={"to": "previous_period"}),
                         self._spec(compare={"to": "weekly"}), self._spec(object="Stock Position"),
                         self._spec(limit=0), self._spec(page=2, limit=50000),
                         self._spec(period={"from": "2026-02-01", "to": "2026-01-01"}),
                         self._spec(params={"Bad Name": 1}), self._spec(params={"x": [1]}),
                         self._spec(params={"p%d" % i: 1 for i in range(21)}), self._spec(params=[1])):
                resp = self._post(self.open, body)
                self.assertEqual(resp.status_code, 422, body)
            refused = sealed_request(self, None, "POST", QUERY, body=self._spec())
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(calls, [])


@tagged("post_install", "-at_install")
class TestOntoMerge(BaseCase):
    """The compare merge on its own: querygen's _merge contract, applied to records, no database."""

    def test_the_previous_period_is_as_long_and_ends_the_day_before(self):
        self.assertEqual(onto_query._previous_period({"from": "2026-02-01", "to": "2026-02-28"}),
                         {"from": "2026-01-04", "to": "2026-01-31"})

    def test_a_week_compare_pairs_by_offset_when_the_bucket_counts_differ(self):
        # Current 2026-03-03..03-15 has two Monday-start weeks; previous 2026-02-18..03-02 has three.
        spec = {"period": {"from": "2026-03-03", "to": "2026-03-15"}, "dims": [], "grain": "week"}
        compiled = {"merge": _merge({"from": "2026-02-18", "to": "2026-03-02"}, grain="week", dims=()),
                    "request": {"matrix_col_dim": {"field_name": "day", "grain": "week"}}}
        current = [{"day": "2026-03-02", "on_hand": 10}, {"day": "2026-03-09", "on_hand": 4}]
        previous = [{"day": "2026-02-16", "on_hand": 6}, {"day": "2026-02-23", "on_hand": 0},
                    {"day": "2026-03-02", "on_hand": 8}]
        rows = onto_query._merge_rows(compiled, spec, current, previous)
        # The third previous week has no current pair: it follows, with its bucket moved to the current period.
        self.assertEqual([r["day"] for r in rows], ["2026-03-02", "2026-03-09", "2026-03-16"])
        self.assertEqual([r["on_hand"] for r in rows], [10, 4, 0])
        self.assertEqual([r["on_hand_previous"] for r in rows], [6, 0, 8])
        self.assertEqual([r["on_hand_change"] for r in rows], [4, 4, -8])
        self.assertAlmostEqual(rows[0]["on_hand_change_pct"], 4 / 6)
        self.assertEqual([r["on_hand_change_pct"] for r in rows[1:]], [None, -1.0])

    def test_the_merged_columns_are_named_after_the_measure(self):
        compiled = {"columns": [{"key": "location_id", "label": "Location", "role": "dimension", "unit": None},
                                {"key": "on_hand", "label": "On hand", "role": "measure", "unit": "qty"}],
                    "merge": _merge({"from": "2026-01-04", "to": "2026-01-31"})}
        columns = onto_query._out_columns(compiled)
        self.assertEqual([c["key"] for c in columns],
                         ["location_id", "on_hand", "on_hand_previous", "on_hand_change", "on_hand_change_pct"])
        self.assertEqual(columns[-1]["unit"], "ratio")
