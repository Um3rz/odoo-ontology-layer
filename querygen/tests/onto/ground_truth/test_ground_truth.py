"""Ground truth: every template metric and its dimensions against hand SQL on the Odoo 16 seed (plan 1.10).

For each case the compiler produces the SELECT Odoo would run: app.onto_routes.compile._final_sql, the same
generator the compile route uses. That SELECT runs against odoo_onto with the managed view as a CTE named like
the view, and with onto_visible as a CTE holding every base id (the all-visible case). Its rows are compared with
the hand SQL in cases.py, ignoring order. Counts are exact and money is +-0.01 (cases.TOLERANCE).

Read-only: the connection is a READ ONLY session and every statement is a WITH or SELECT.
"""
from __future__ import annotations

import ast
import re
import time
from decimal import Decimal
from pathlib import Path

import pytest

from .cases import (
    CASES,
    FINDINGS,
    GROUND_TRUTH_GAPS,
    OBJECT_BASE_COUNT,
    OBJECT_RELEASE,
    TOLERANCE,
    Case,
    hand_sql,
    previous_period,
)

ROOT = Path(__file__).resolve().parents[3]
QUESTION_BANK = ROOT / "evals" / "onto" / "question_bank.yaml"
# The template file each object comes from. The report counts results per template.
OBJECT_TEMPLATE = {
    "company": "org", "warehouse": "org", "location": "org",
    "product": "core", "product_category": "core", "partner": "core",
    "stock_position": "inventory", "stock_lot": "inventory", "stock_move": "inventory",
    "stock_movement_detail": "inventory",
    "stock_valuation": "inventory", "transfer": "inventory", "scrap": "inventory",
    "purchase_order": "purchase", "purchase_order_line": "purchase", "supplier_info": "purchase",
    "pos_order": "pos", "pos_order_line": "pos", "pos_session": "pos",
    "sales_order": "sales", "sales_order_line": "sales",
    "billing_document": "account", "billing_line": "account", "posted_payment": "account",
    "accounting_reconciliation": "account", "crm_opportunity": "crm",
}
# Steward roles simulated for the test_map variant. The real OrgMap is empty on odoo_onto (onto_org_node has 0 rows).
TEST_MAP_CTE = """onto_org_map AS (
  SELECT l.id AS location_id,
         CASE WHEN l.complete_name = 'WH/QC Hold' THEN 'qc_hold'
              WHEN l.complete_name = 'WH/Damaged' THEN 'damaged'
              WHEN l.complete_name = 'WH/Expired' THEN 'expired'
              WHEN l.complete_name = 'WH/Stock' THEN 'central'
              WHEN l.complete_name IN ('A1/Stock', 'A2/Stock', 'A3/Stock', 'B1/Stock', 'B2/Stock') THEN 'branch'
         END AS role
  FROM stock_location l)"""
SAMPLE_ROWS = 40


# --- value handling ----------------------------------------------------------------------------------
def _num(value):
    if value is None:
        return None
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def _evaluate(formula: str, env: dict, zero_null: bool):
    """A derive instruction, evaluated the way Odoo does it: a formula over component aliases, NULL in and
    NULL out, and a zero denominator gives NULL when the instruction says zero_division is null."""
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Constant):
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = walk(node.operand)
            return None if inner is None else -inner
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if right == 0:
                    if zero_null:
                        return None
                    raise ZeroDivisionError(formula)
                return left / right
        raise ValueError(f"unsupported node in formula {formula!r}")

    return walk(ast.parse(formula, mode="eval"))


def _same(a, b, tol) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(Decimal(a) - Decimal(b)) <= Decimal(str(tol))


def _tolerance(release, metric_key: str):
    metric = next(m for m in release.metrics if m.key == metric_key)
    return TOLERANCE[metric.unit]


# --- statements --------------------------------------------------------------------------------------
def _fetch(conn, sql: str, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params or None)
        names = [column.name for column in cur.description]
        rows = cur.fetchall()
    finally:
        cur.close()
    return names, rows


def _base_table(release, object_key: str) -> str:
    from app.ontology.compiler import _table

    return _table(next(o for o in release.objects if o.key == object_key).base_model)


def _compiled_statement(release, case: Case, sql: str) -> str:
    """The compiled SELECT with the managed view and the visibility set as CTEs (test-map roles first when used)."""
    from app.ontology.compiler import compile_object, view_name

    ctes = [TEST_MAP_CTE] if case.roles == "test_map" else []
    ctes.append(f"{view_name(case.object)} AS (\n{compile_object(release, case.object)['sql']}\n)")
    ctes.append(f"onto_visible AS (SELECT id FROM {_base_table(release, case.object)})")
    return "WITH " + ",\n".join(ctes) + "\n" + sql


def _hand_statement(case: Case, sql: str) -> str:
    return ("WITH " + TEST_MAP_CTE + "\n" + sql) if case.roles == "test_map" else sql


def _spec(release, case: Case, period, compare: bool):
    from app.ontology.spec import QuerySpec

    body = {"release": release.digest(), "object": case.object, "metric": case.metric,
            "dims": list(case.dims),
            "filters": [{"prop": prop, "op": op, "value": value} for prop, op, value in case.filters],
            "limit": 5000}
    if period is not None:
        body["period"] = {"from": period[0], "to": period[1]}
    if case.grain:
        body["grain"] = case.grain
    if compare:
        body["compare"] = {"to": "previous_period"}
    if case.params:
        body["params"] = dict(case.params)
    return QuerySpec.model_validate(body)


# --- rows to points ----------------------------------------------------------------------------------
def _compiled_points(names, rows, case: Case, request: dict, derives: list[dict]) -> dict:
    """Compiled rows as {row key: value}. The key is the dimension values, then the bucket when grained.
    A derived metric is evaluated from its component aliases, as Odoo does when it reshapes the rows."""
    from app.utils.utils import MATRIX_COL_ALIAS

    if request["analytic_type"] == "tile":
        # The tile path is a plain COUNT: zero is a value, and the hand total (one row, no GROUP BY) returns it too.
        return {(): rows[0][0]}
    leaves = [measure["alias"] for measure in request["matrix_measures"]]
    out: dict = {}
    for row in rows:
        record = dict(zip(names, row))
        key = tuple(record[dim] for dim in case.dims)
        if case.grain:
            key += (record[MATRIX_COL_ALIAS],)
        if key in out:
            raise AssertionError(f"duplicate row key {key} in the compiled result")
        env = {leaf: _num(record[leaf]) for leaf in leaves}
        for node in derives:
            env[node["alias"]] = _evaluate(node["formula"], env, "zero_division" in node)
        out[key] = env[case.metric]
    if not rows and not case.dims and not case.grain:
        # An empty total: the matrix path returns no row, which Odoo reads as no value (NULL). The hand SUM is NULL.
        out[()] = None  # F12: a COUNT over no rows is 0 on the tile path and a missing row here
    return out


def _hand_points(names, rows, case: Case) -> dict:
    out: dict = {}
    for row in rows:
        record = dict(zip(names, row))
        key = tuple(record[f"d{i}"] for i in range(len(case.dims)))
        if case.grain:
            key += (record["bucket"],)
        if key in out:
            raise AssertionError(f"duplicate row key {key} in the hand SQL")
        out[key] = _num(record["value"])
    return out


def _mismatches(compiled: dict, hand: dict, tol) -> list[str]:
    problems = []
    for key in sorted(set(compiled) | set(hand), key=str):
        if key not in compiled:
            problems.append(f"only in hand SQL: {key} = {hand[key]}")
        elif key not in hand:
            problems.append(f"only in compiled SQL: {key} = {compiled[key]}")
        elif not _same(compiled[key], hand[key], tol):
            problems.append(f"{key}: compiled {compiled[key]} != hand {hand[key]}")
    return problems


def _text(value):
    return None if value is None else str(value)


def _sample(points: dict) -> list:
    return [[[str(part) for part in key], None if value is None else str(value)]
            for key, value in sorted(points.items(), key=lambda kv: str(kv[0]))[:SAMPLE_ROWS]]


# --- the case test -----------------------------------------------------------------------------------
def _case_params():
    params = []
    for case in CASES:
        marks = []
        if case.expect == "divergence":
            marks.append(pytest.mark.xfail(strict=True, raises=AssertionError, reason=case.note))
        params.append(pytest.param(case, id=case.id, marks=marks))
    return params


def _new_record(case: Case) -> dict:
    return {"id": case.id, "object": case.object, "metric": case.metric, "template": OBJECT_TEMPLATE[case.object],
            "dims": list(case.dims), "filters": [list(f) for f in case.filters], "period": case.period,
            "grain": case.grain, "compare": case.compare, "roles": case.roles, "release": case.release,
            "expect": case.expect, "note": case.note, "status": None, "detail": "", "compiled_rows": None,
            "hand_rows": None, "sample": [], "total": None, "bindings": {}}


@pytest.mark.parametrize("case", _case_params())
def test_case(case: Case, ground_truth):
    gt = ground_truth
    record = _new_record(case)
    started = time.monotonic()
    try:
        if gt.over_budget():
            record["status"] = "gap"
            record["detail"] = f"run budget of {gt.budget_s:.0f} s reached: not run"
            pytest.skip(record["detail"])
        _run_case(case, gt, record)
        # A divergence case that agrees is an unexpected pass: pytest reports it as XPASS(strict).
        record["status"] = {"refused": "refused", "divergence": "unexpected_pass"}.get(case.expect, "pass")
    except AssertionError as exc:
        record["status"] = "divergence" if case.expect == "divergence" else "fail"
        record["detail"] = str(exc)[:4000]
        raise
    except Exception as exc:  # a database error, a timeout or a compile error: never a silent pass
        if type(exc).__name__ == "QueryCanceled":
            record["status"] = "gap"
            record["detail"] = "statement timeout: a query ran over 120 s, so the case is a gap"
            pytest.skip(record["detail"])
        record["status"] = "fail"
        record["detail"] = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:1000]
        raise
    finally:
        record["seconds"] = round(time.monotonic() - started, 3)
        if record["status"] is None:
            record["status"] = "fail"
        gt.results.append(record)
        gt.conn.rollback()


def _run_case(case: Case, gt, record: dict) -> None:
    release = gt.release(case.release)
    if case.expect == "refused":
        from app.ontology.compiler import compile_query
        from app.ontology.spec import SpecError

        with pytest.raises(SpecError, match=re.escape(case.refusal)):
            compile_query(release, _spec(release, case, case.period, case.compare))
        record["detail"] = f"refused with {case.refusal!r}, as the gap records"
        return

    from app.ontology.compiler import compile_query, query_reads
    from app.onto_routes.compile import _final_sql

    tol = _tolerance(release, case.metric)
    compiled = compile_query(release, _spec(release, case, case.period, case.compare))
    record["bindings"] = query_reads(release, _spec(release, case, case.period, case.compare))["bindings"]
    targets = [("period", case.period, compiled["request"])]
    if case.compare:
        targets.append(("previous period", previous_period(case.period), compiled["previous_request"]))
    for label, period, request in targets:
        sql = _final_sql(request)
        names, rows = _fetch(gt.conn, _compiled_statement(release, case, sql), compiled["params"])
        points = _compiled_points(names, rows, case, request, compiled["derive"])
        hsql, params = hand_sql(case, period)
        hnames, hrows = _fetch(gt.conn, _hand_statement(case, hsql), params)
        hand = _hand_points(hnames, hrows, case)
        if label == "period":
            record["compiled_rows"], record["hand_rows"] = len(points), len(hand)
            record["sample"] = _sample(hand)
            record["total"] = {"compiled": _text(points.get(())), "hand": _text(hand.get(()))}
            record["compiled_sql"] = sql
        problems = _mismatches(points, hand, tol)
        if problems:
            raise AssertionError(f"{label} {period}: " + "; ".join(problems[:8]))
        if label == "period":
            for key, expected in case.pins:
                if not _same(hand.get(key), expected, tol):
                    raise AssertionError(f"pinned {key}: hand SQL gives {hand.get(key)}, the pin says {expected}")
        record["detail"] = f"{label} {period}: {len(points)} rows agree"


# --- object views: every object's compiled view runs and matches the base population ------------------
@pytest.mark.parametrize("object_key", sorted(OBJECT_BASE_COUNT))
def test_object_view_matches_base_population(object_key: str, ground_truth):
    from app.ontology.compiler import compile_object, view_name

    gt = ground_truth
    release = gt.release(OBJECT_RELEASE.get(object_key, "templates"))
    record = {"id": f"view.{object_key}", "object": object_key, "metric": None,
              "template": OBJECT_TEMPLATE[object_key], "expect": "match", "status": None, "detail": "",
              "compiled_rows": None, "hand_rows": None}
    try:
        view = compile_object(release, object_key)["sql"]
        base = _fetch(gt.conn, OBJECT_BASE_COUNT[object_key])[1][0][0]
        _, rows = _fetch(gt.conn, f"WITH {view_name(object_key)} AS (\n{view}\n)\n"
                                  f"SELECT count(*), count(DISTINCT _onto_id) FROM {view_name(object_key)}")
        view_rows, distinct_ids = rows[0]
        record["compiled_rows"], record["hand_rows"] = view_rows, base
        problems = []
        if view_rows != base:
            problems.append(f"view has {view_rows} rows, the base population has {base} (join fan-out or a filter)")
        if distinct_ids != view_rows:
            problems.append(f"_onto_id is not unique: {distinct_ids} distinct ids for {view_rows} rows")
        if problems:
            record["status"], record["detail"] = "fail", "; ".join(problems)
            raise AssertionError(record["detail"])
        record["status"], record["detail"] = "pass", f"{view_rows} rows, ids unique"
    except AssertionError:
        raise
    except Exception as exc:
        record["status"] = "fail"
        record["detail"] = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:1000]
        raise
    finally:
        gt.results.append(record)
        gt.conn.rollback()


# --- coverage and gap probes -------------------------------------------------------------------------
def test_every_template_metric_has_a_total_and_a_dimension_case(ground_truth):
    metrics = {m.key for m in ground_truth.templates.metrics}
    gaps = {g["metric"] for g in GROUND_TRUTH_GAPS}
    covered = {c.metric for c in CASES}
    assert metrics - covered - gaps == set(), "metrics without a case or a gap"
    assert covered - metrics == set(), "cases name metrics the templates do not define"
    for metric in sorted(metrics - gaps):
        cases = [c for c in CASES if c.metric == metric]
        assert any(not c.dims for c in cases), f"{metric}: no total case"
        assert any(c.dims for c in cases), f"{metric}: no case by a dimension"
    for metric in sorted(gaps):
        assert metric in metrics, f"gap {metric} is not a template metric"


def test_gap_probes_exist_and_the_limitations_still_hold(ground_truth):
    """Each gap names a probe case, and the limitation it records must still hold on this database."""
    ids = {c.id for c in CASES}
    for gap in GROUND_TRUTH_GAPS:
        assert gap["probe"] in ids, f"gap {gap['metric']} names a missing probe {gap['probe']}"
    gt = ground_truth
    _, rows = _fetch(gt.conn, "SELECT count(*) FROM information_schema.columns "
                              "WHERE table_name = 'sale_order_line' AND column_name = 'purchase_price'")
    assert rows[0][0] == 0, "sale_order_line.purchase_price now exists: move the sales cost gap to passing cases"
    _, rows = _fetch(gt.conn, "SELECT count(total_cost), count(*) FROM pos_order_line")
    assert rows[0][0] == 0, "pos_order_line.total_cost now has values: the POS cost gap no longer holds"
    gt.conn.rollback()


def test_findings_are_recorded_with_a_status():
    assert FINDINGS, "the report needs the findings list"
    for finding in FINDINGS:
        assert finding["status"] in {"fixed", "open"}, finding["id"]
        assert finding["kind"] in {"bug", "definition", "unit", "data", "caveat"}, finding["id"]


# --- the question bank references (no database needed) ------------------------------------------------
def test_question_bank_maps_to_cases_or_gaps():
    import yaml

    questions = yaml.safe_load(QUESTION_BANK.read_text(encoding="utf-8"))["questions"]
    assert len(questions) >= 30, "the question bank starts with 30 seed questions"
    ids = [q["id"] for q in questions]
    assert len(set(ids)) == len(ids), "question ids must be unique"
    known = {c.id for c in CASES} | {g["probe"] for g in GROUND_TRUTH_GAPS}
    by_id = {c.id: c for c in CASES}
    for question in questions:
        source = question["expected_number_source"]
        assert source in known, f"{question['id']} points at {source}, which is not a case or a gap probe"
        if source in by_id:
            case = by_id[source]
            expected = question["expected"]
            assert expected["metric"] == case.metric, f"{question['id']}: metric differs from {source}"
            assert expected["object"] == case.object, f"{question['id']}: object differs from {source}"
            assert sorted(expected["dims"]) == sorted(case.dims), f"{question['id']}: dims differ from {source}"
