"""Compiler and QuerySpec checks (plan 1.4). Pure unit tests: no database, no Odoo, no SQL is run.

Golden files live in tests/onto/golden/. After a reviewed change, regenerate them with
ONTO_UPDATE_GOLDEN=1 and read the diff.
"""
import json
import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.models import QueryRequests
from app.ontology.compiler import (
    assert_visibility_predicate,
    check_select_shape,
    compile_object,
    compile_query,
)
from app.ontology.loader import load_release
from app.ontology.provision import payload
from app.ontology.spec import QuerySpec, SpecError, typed_literal

GOLDEN = Path(__file__).parent / "golden"
PERIOD = {"from": "2026-09-01", "to": "2026-09-30"}
VISIBLE = "WHERE _onto_id IN (SELECT id FROM onto_visible)"


@pytest.fixture(scope="module")
def release():
    return load_release()


def make_spec(rel, /, **kwargs) -> QuerySpec:
    base = {"release": rel.digest(), "object": "stock_position", "metric": "on_hand_qty"}
    base.update(kwargs)
    return QuerySpec.model_validate(base)


def golden(name: str, actual) -> None:
    text = json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path = GOLDEN / name
    if os.environ.get("ONTO_UPDATE_GOLDEN") == "1":
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, f"{name} changed; review the diff, then rerun with ONTO_UPDATE_GOLDEN=1"


def query_cases(release) -> dict[str, QuerySpec]:
    return {
        "stock_total": make_spec(release),
        "usable_by_warehouse_branch": make_spec(
            release, metric="usable_qty", dims=["warehouse"],
            filters=[{"prop": "location_role", "op": "in", "value": ["branch"]}]),
        "counted_lines_total": make_spec(release, metric="counted_lines"),
        "count_accuracy_by_warehouse": make_spec(release, metric="count_accuracy", dims=["warehouse"]),
        "margin_pct_by_product_compare": make_spec(
            release, object="sales_order_line", metric="margin_pct", dims=["product"],
            period=PERIOD, compare={"to": "previous_period"}),
        "revenue_by_customer_month": make_spec(
            release, object="sales_order_line", metric="revenue", dims=["customer"],
            period=PERIOD, grain="month"),
        "revenue_top_customers": make_spec(
            release, object="sales_order_line", metric="revenue", dims=["customer"],
            period=PERIOD, top_n={"n": 10, "by": "revenue", "dir": "desc"}),
        "days_of_inventory_period": make_spec(
            release, object="stock_valuation", metric="days_of_inventory", period=PERIOD),
        "near_expiry_qty_45_days_by_warehouse": make_spec(
            release, metric="near_expiry_qty", dims=["warehouse"], period=PERIOD, params={"expiry_days": 45}),
    }


# --- every object and its SELECT -------------------------------------------------

def test_every_object_of_every_template_compiles(release):
    assert len(release.objects) == 26
    for obj in release.objects:
        out = compile_object(release, obj.key)
        assert out["key"] == obj.key and out["sql"].startswith("SELECT\n")


def test_every_select_is_one_statement_with_declared_aliases(release):
    for obj in release.objects:
        sql = compile_object(release, obj.key)["sql"]
        check_select_shape(sql)
        assert ";" not in sql and "--" not in sql and "/*" not in sql
        declared = set(re.findall(r'AS "(b|j\d+)"', sql))
        used = set(re.findall(r'"(b|j\d+)"\.', sql))
        assert used <= declared, f"{obj.key} uses undeclared aliases {used - declared}"


def test_onto_id_is_projected_first_on_every_object(release):
    for obj in release.objects:
        first = compile_object(release, obj.key)["sql"].splitlines()[1].strip()
        assert first == '"b"."id" AS "_onto_id",', f"{obj.key}: {first}"


def test_every_property_is_a_column_except_the_one_to_many_link(release):
    skipped = set()
    for obj in release.objects:
        names = {c["name"] for c in compile_object(release, obj.key)["columns"]}
        skipped |= {(obj.key, p.key) for p in obj.properties if p.key not in names}
    assert skipped == {("product", "supplier_lines")}


def test_object_has_exactly_the_3_2a_shape(release):
    out = compile_object(release, "stock_position")
    assert set(out) == {"key", "label", "base_model", "sql", "columns", "group_xmlids", "engine_only", "spec_hash"}
    assert out["group_xmlids"] == [] and out["engine_only"] is False
    assert {"name", "type", "role", "unit", "pii"} <= set(out["columns"][0])


def test_the_payload_marks_an_object_without_groups_engine_only(release):
    # Odoo refuses group_xmlids [] unless engine_only (onto_provision._parse_object), so the payload must send True.
    objects = {o["key"]: o for o in payload(release, {"stock_position": {"group_xmlids": ["stock.group_stock_user"]}},
                                            "d")["objects"]}
    assert objects["product"]["group_xmlids"] == [] and objects["product"]["engine_only"] is True
    assert objects["stock_position"]["engine_only"] is False


def test_spec_hash_covers_groups_and_is_stable(release):
    first = compile_object(release, "stock_lot")
    assert first["spec_hash"] == compile_object(release, "stock_lot")["spec_hash"]
    granted = compile_object(release, "stock_lot", group_xmlids=["stock.group_stock_user"])
    assert granted["spec_hash"] != first["spec_hash"]


def test_org_role_joins_the_onto_org_map_view_on_location(release):
    sql = compile_object(release, "stock_position")["sql"]
    assert 'LEFT JOIN "onto_org_map" AS "j4" ON "j4"."location_id" = "b"."location_id"' in sql


def test_base_filter_keeps_internal_and_transit_locations_only(release):
    sql = compile_object(release, "stock_position")["sql"]
    assert "WHERE (\"j1\".\"usage\" IN ('internal', 'transit'))" in sql


def test_null_location_role_counts_as_usable(release):
    query = compile_query(release, make_spec(release, metric="usable_qty"))
    measure = query["request"]["matrix_measures"][0]["field_name"]
    assert "IS NULL OR" in measure and "NOT IN ('damaged', 'qc_hold', 'expired')" in measure


# --- additivity -----------------------------------------------------------------

def test_a_semi_additive_balance_refuses_a_time_grain(release):
    with pytest.raises(SpecError, match="semi_additive"):
        compile_query(release, make_spec(release, grain="month"))


def test_a_semi_additive_flow_over_a_span_of_days_is_refused(release):
    with pytest.raises(SpecError, match="semi_additive"):
        compile_query(release, make_spec(release, object="transfer", metric="open_transfer_count", period=PERIOD))


def test_a_snapshot_balance_reads_at_period_end_without_a_period_filter(release):
    out = compile_query(release, make_spec(release, period=PERIOD))
    assert out["request"]["pre_defined_filter_conditions"] == VISIBLE
    assert out["decimal"] is True and out["path"] == "matrix"


def test_a_flow_metric_is_bounded_by_the_period(release):
    out = compile_query(release, make_spec(release, object="sales_order_line", metric="units_sold", period=PERIOD))
    where = out["request"]["pre_defined_filter_conditions"]
    assert "\"order_date\" >= '2026-09-01' AND \"order_date\" < '2026-10-01'" in where


def test_a_ratio_is_recomputed_from_its_parts_never_summed(release):
    out = compile_query(release, make_spec(release, object="sales_order_line", metric="margin_pct", dims=["product"]))
    aliases = [m["alias"] for m in out["request"]["matrix_measures"]]
    assert aliases == ["margin", "net_revenue"]
    assert out["derive"] == [{"alias": "margin_pct", "formula": "margin / net_revenue", "zero_division": "null"}]


def test_every_ratio_in_the_templates_is_non_additive(release):
    ratios = [m for m in release.metrics if m.derived_from and "/" in m.derived_from]
    assert {m.key for m in ratios} >= {"count_accuracy", "turnover", "days_of_inventory", "margin_pct"}
    assert all(m.additivity == "non_additive" for m in ratios)


def test_days_in_a_formula_becomes_the_period_length(release):
    out = compile_query(release, make_spec(release, object="stock_valuation", metric="days_of_inventory", period=PERIOD))
    assert out["derive"][-1] == {"alias": "days_of_inventory", "formula": "stock_value * 30 / cogs", "zero_division": "null"}
    with pytest.raises(SpecError, match="needs a period"):
        compile_query(release, make_spec(release, object="stock_valuation", metric="days_of_inventory"))


def test_a_rule_on_as_of_needs_a_period(release):
    with pytest.raises(SpecError, match="needs a period"):
        compile_query(release, make_spec(release, metric="expired_qty"))
    out = compile_query(release, make_spec(release, metric="expired_qty", period=PERIOD))
    assert "'2026-09-30'" in out["request"]["matrix_measures"][0]["field_name"]
    with pytest.raises(SpecError, match="needs a period"):
        compile_query(release, make_spec(release, metric="near_expiry_qty", params={"expiry_days": 30}))


# --- params: a day window is a bound parameter, never a literal (plan 1.4) ------------------------

EXPIRY = '"expiry_date" <= (CAST(\'2026-09-30\' AS DATE) + %(expiry_days)s)'


def release_without_expiry_default(rel):
    """The same release, with near_expiry_lot's expiry_days placeholder declared without a default."""
    rules = []
    for rule in rel.rules:
        if rule.key == "near_expiry_lot":
            when = [c.model_copy(update={"value": {"param": "expiry_days"}})
                    if isinstance(c.value, dict) and c.value.get("param") == "expiry_days" else c
                    for c in rule.when]
            rule = rule.model_copy(update={"when": when})
        rules.append(rule)
    return rel.model_copy(update={"rules": rules})


def test_an_expiry_window_is_bound_and_its_value_stays_out_of_the_sql(release):
    out = compile_query(release, make_spec(release, metric="near_expiry_qty", period=PERIOD, params={"expiry_days": 45}))
    measure = out["request"]["matrix_measures"][0]["field_name"]
    assert EXPIRY in measure and "45" not in measure
    assert out["params"] == {"expiry_days": 45}


def test_a_day_param_left_out_takes_the_template_default(release):
    out = compile_query(release, make_spec(release, metric="near_expiry_qty", period=PERIOD))
    assert EXPIRY in out["request"]["matrix_measures"][0]["field_name"]
    assert out["params"] == {"expiry_days": 30}
    assert compile_query(release, make_spec(release))["params"] == {}


def test_a_missing_param_with_no_default_is_refused(release):
    strict = release_without_expiry_default(release)
    with pytest.raises(SpecError, match="needs param 'expiry_days'"):
        compile_query(strict, make_spec(strict, metric="near_expiry_qty", period=PERIOD))
    out = compile_query(strict, make_spec(strict, metric="near_expiry_qty", period=PERIOD, params={"expiry_days": 7}))
    assert out["params"] == {"expiry_days": 7}


def test_an_unknown_param_is_refused(release):
    for params in ({"expiry_days": 7}, {"nope": 1}, {"as_of": 1}):
        with pytest.raises(SpecError, match="unknown param"):
            compile_query(release, make_spec(release, params=params))  # on_hand_qty uses no param


@pytest.mark.parametrize("value", ["1; drop table x", -1, 1.5, True])
def test_a_day_param_must_be_a_whole_number_so_no_sql_is_built(release, value):
    spec = make_spec(release, metric="near_expiry_qty", period=PERIOD, params={"expiry_days": value})
    with pytest.raises(SpecError, match="whole number of days"):
        compile_query(release, spec)


def test_a_param_name_must_match_the_pattern(release):
    with pytest.raises(ValidationError):
        make_spec(release, params={"x; drop table x": 1})


def test_the_spec_takes_release_current_or_a_hash_only(release):
    assert make_spec(release, release="current").release == "current"
    with pytest.raises(ValidationError):
        make_spec(release, release="CURRENT")


# --- booleans: NULL reads as false, so IS TRUE / IS NOT TRUE, never = or <> -------------------------

@pytest.mark.parametrize("op, value, expected", [
    ("=", True, '"counted" IS TRUE'),
    ("=", False, '"counted" IS NOT TRUE'),
    ("!=", True, '"counted" IS NOT TRUE'),
    ("!=", False, '"counted" IS TRUE'),
])
def test_a_boolean_filter_reads_null_as_false(release, op, value, expected):
    spec = make_spec(release, filters=[{"prop": "counted", "op": op, "value": value}])
    where = compile_query(release, spec)["request"]["pre_defined_filter_conditions"]
    assert f"({expected})" in where
    assert "= TRUE" not in where and "= FALSE" not in where and "<> TRUE" not in where


def test_a_boolean_rule_is_tested_with_is_true(release):
    out = compile_query(release, make_spec(release, metric="counted_lines"))
    assert out["request"]["data_field_name"] == 'CASE WHEN "counted" IS TRUE THEN _onto_id END'


def test_a_boolean_takes_only_equality_and_membership(release):
    with pytest.raises(SpecError, match="a boolean takes"):
        compile_query(release, make_spec(release, filters=[{"prop": "counted", "op": "<", "value": True}]))


# --- names, types and injection --------------------------------------------------

def test_a_time_property_groups_by_its_exact_value(release):
    out = compile_query(release, make_spec(release, dims=["expiry_date"]))
    assert {"field_name": "expiry_date", "field_name_as": "expiry_date"} in out["request"]["matrix_row_dims"]


@pytest.mark.parametrize("kwargs, message", [
    ({"metric": "nope"}, "unknown metric"),
    ({"object": "nope"}, "unknown object"),
    ({"dims": ["quantity"]}, "not a dimension"),
    ({"dims": ["nope"]}, "unknown dimension"),
    ({"grain": "hour"}, "grain"),
    ({"dims": ["product", "product"]}, "repeated"),
    ({"filters": [{"prop": "nope", "op": "=", "value": 1}]}, "unknown filter field"),
    ({"filters": [{"prop": "quantity", "op": "=", "value": "many"}]}, "expected a finite number"),
    ({"filters": [{"prop": "counted", "op": "=", "value": "yes"}]}, "expected true or false"),
    ({"filters": [{"prop": "location_role", "op": "in", "value": ["branch", "nowhere"]}]}, "not a value of this org role"),
    ({"filters": [{"prop": "location_usage", "op": "in", "value": []}]}, "non-empty list"),
    ({"filters": [{"prop": "location_usage", "op": "=", "value": ["internal"]}]}, "one value"),
    ({"top_n": {"n": 3, "by": "quantity"}}, "ranks by the queried metric"),
    ({"compare": {"to": "previous_period"}}, "needs a period"),
    ({"period": {"from": "2026-09-30", "to": "2026-09-01"}}, "period.from is after"),
    ({"limit": 50001}, "less than or equal"),
])
def test_unknown_or_ill_typed_parts_are_refused(release, kwargs, message):
    with pytest.raises((SpecError, ValidationError), match=message):
        compile_query(release, make_spec(release, **kwargs))


def test_a_spec_for_another_release_is_refused(release):
    with pytest.raises(SpecError, match="another release"):
        compile_query(release, make_spec(release, release="0" * 64))


def test_unknown_spec_fields_are_refused(release):
    with pytest.raises(ValidationError):
        make_spec(release, sql="SELECT 1")


@pytest.mark.parametrize("value", ["x'; DROP TABLE stock_quant; --", "a /* b */", "1; select 1", "a--b", "x */"])
def test_sql_tokens_in_a_text_value_are_refused(release, value):
    spec = make_spec(release, filters=[{"prop": "location_usage", "op": "=", "value": value}])
    with pytest.raises(SpecError, match="forbidden"):
        compile_query(release, spec)


def test_a_quote_in_a_value_is_escaped_not_refused(release):
    assert typed_literal("text", "O'Brien") == "'O''Brien'"
    spec = make_spec(release, filters=[{"prop": "location_usage", "op": "=", "value": "O'Brien"}])
    where = compile_query(release, spec)["request"]["pre_defined_filter_conditions"]
    assert "(\"location_usage\" = 'O''Brien')" in where


def test_a_boolean_and_a_date_are_typed_literals(release):
    assert typed_literal("bool", True) == "TRUE" and typed_literal("date", "2026-09-01") == "'2026-09-01'"
    with pytest.raises(SpecError, match="ISO date"):
        typed_literal("date", "2026-09-01; drop")


# --- visibility predicate ---------------------------------------------------------

def test_every_request_is_filtered_by_the_visibility_predicate(release):
    for name, spec in query_cases(release).items():
        out = compile_query(release, spec)
        for request in (out["request"], out["previous_request"]):
            if request is not None:
                assert request["pre_defined_filter_conditions"].startswith(VISIBLE), name


def test_user_filters_are_anded_into_the_same_where(release):
    spec = make_spec(release, dims=["warehouse"], filters=[{"prop": "location_role", "op": "in", "value": ["branch"]}])
    where = compile_query(release, spec)["request"]["pre_defined_filter_conditions"]
    assert where.startswith(VISIBLE + " AND (")
    assert where.count("WHERE") == 1


def test_guard_accepts_filtered_sql_and_refuses_fallback_or_missing_predicate(release):
    request = compile_query(release, make_spec(release))["request"]
    view = request["sql_view_name"]
    assert_visibility_predicate(f"SELECT * FROM {view} {request['pre_defined_filter_conditions']}")
    with pytest.raises(SpecError, match="fallback"):
        assert_visibility_predicate(f"SELECT * FROM {view};")
    with pytest.raises(SpecError, match="missing"):
        assert_visibility_predicate(f"SELECT quantity FROM {view} WHERE (quantity > 0)")
    with pytest.raises(SpecError, match="no SQL"):
        assert_visibility_predicate("   ")


def test_request_fields_are_all_querygen_query_request_fields(release):
    fields = set(QueryRequests.model_fields)
    for name, spec in query_cases(release).items():
        out = compile_query(release, spec)
        for request in (out["request"], out["previous_request"]):
            if request is not None:
                assert set(request) <= fields, name
                assert request["analytic_type"] in ("tile", "matrix")


# --- compare, top-N and row cap ----------------------------------------------------

REVENUE_MEASURES = [{"key": "revenue", "previous": "revenue_previous", "change": "revenue_change",
                     "change_pct": "revenue_change_pct"}]


def test_previous_period_is_the_same_length_immediately_before(release):
    out = compile_query(release, make_spec(release, object="sales_order_line", metric="revenue",
                                           dims=["product"], period=PERIOD, compare={"to": "previous_period"}))
    assert out["merge"] == {"kind": "previous_period",
                            "previous_period": {"from": "2026-08-02", "to": "2026-08-31"},
                            "match_dims": ["product"], "match_bucket": None, "bucket_column": None,
                            "bucket_grain": None, "absent": "zero", "measures": REVENUE_MEASURES}
    previous = out["previous_request"]["pre_defined_filter_conditions"]
    assert "\"order_date\" >= '2026-08-02' AND \"order_date\" < '2026-09-01'" in previous
    assert "'2026-08-02'" not in out["request"]["pre_defined_filter_conditions"]


def test_a_grained_compare_pairs_buckets_by_offset_not_by_position(release):
    out = compile_query(release, make_spec(release, object="sales_order_line", metric="revenue",
                                           dims=["product"], period=PERIOD, grain="month", compare={"to": "previous_period"}))
    assert out["merge"] == {"kind": "previous_period",
                            "previous_period": {"from": "2026-08-02", "to": "2026-08-31"},
                            "match_dims": ["product"], "match_bucket": "offset", "bucket_column": "__matrix_col",
                            "bucket_grain": "month", "absent": "zero", "measures": REVENUE_MEASURES}


def test_a_ratio_compare_reads_an_absent_side_as_null(release):
    out = compile_query(release, make_spec(release, object="sales_order_line", metric="margin_pct",
                                           dims=["product"], period=PERIOD, compare={"to": "previous_period"}))
    assert out["merge"]["absent"] == "null"
    assert out["merge"]["measures"][0]["key"] == "margin_pct"


def test_top_n_needs_dimensions_and_no_grain(release):
    spec = make_spec(release, object="sales_order_line", metric="revenue", dims=["customer"],
                     period=PERIOD, grain="month", top_n={"n": 10, "by": "revenue"})
    with pytest.raises(SpecError, match="no grain"):
        compile_query(release, spec)


def test_row_cap_and_page_are_passed_to_odoo_not_into_sql(release):
    out = compile_query(release, make_spec(release, dims=["warehouse"], limit=200, page=2))
    assert out["row_cap"] == 200 and out["page"] == 2
    assert "LIMIT" not in json.dumps(out["request"]) and "OFFSET" not in json.dumps(out["request"])


# --- decimal measures take the matrix path ------------------------------------------

def test_decimal_measures_take_the_unrounded_matrix_path(release):
    for name in ("stock_total", "usable_by_warehouse_branch", "count_accuracy_by_warehouse",
                 "margin_pct_by_product_compare", "revenue_by_customer_month"):
        out = compile_query(release, query_cases(release)[name])
        assert out["decimal"] is True and out["path"] == "matrix", name
        assert out["request"]["analytic_type"] == "matrix", name


def test_only_a_plain_count_total_may_use_the_tile_path(release):
    total = compile_query(release, make_spec(release, metric="counted_lines"))
    assert total["path"] == "tile" and total["decimal"] is False
    assert total["request"]["analytic_type"] == "tile"
    grouped = compile_query(release, make_spec(release, metric="counted_lines", dims=["warehouse"]))
    assert grouped["path"] == "matrix"


def test_no_decimal_request_is_ever_a_tile(release):
    for name, spec in query_cases(release).items():
        out = compile_query(release, spec)
        if out["decimal"]:
            assert out["path"] == "matrix" and out["request"]["analytic_type"] == "matrix", name


def test_a_total_uses_a_constant_column_so_all_rows_form_one_group(release):
    out = compile_query(release, make_spec(release))
    assert out["request"]["matrix_col_dim"] == {"field_name": "1", "grain": "value"}


# --- golden files -------------------------------------------------------------------

def test_cogs_leaves_scrapped_layers_out_and_waste_takes_only_them(release):
    # F1 (plan 1.10): Odoo 16 posts a scrap to the location valuation account, not to the stock output account that
    # carries COGS (addons/stock_account/models/stock_move.py:394-398). COGS must skip scrapped outgoing layers.
    cogs = compile_query(release, make_spec(release, object="stock_valuation", metric="cogs", period=PERIOD))
    waste = compile_query(release, make_spec(release, object="stock_valuation", metric="waste_value", period=PERIOD))
    assert '"scrapped" IS NOT TRUE' in cogs["request"]["matrix_measures"][0]["field_name"]
    assert '"scrapped" IS TRUE' in waste["request"]["matrix_measures"][0]["field_name"]


def test_golden_objects(release):
    golden("objects.json", {obj.key: compile_object(release, obj.key) for obj in release.objects})


def test_golden_queries(release):
    golden("queries.json", {name: compile_query(release, spec) for name, spec in sorted(query_cases(release).items())})
