"""Compiler for the Onto map (plan 1.4; contracts 3.2, 3.2a; decisions D45-D47).

compile_object() turns one object into the SELECT of its managed view (D45). The result goes in
the provision payload (3.2a). This module never runs SQL.

compile_query() turns a checked QuerySpec into a QueryRequests-shaped dict for querygen's
/query/querygen. The dict is built in memory and never persisted: querygen stores every request
it receives (R14), so Onto's filter literals must not reach its Postgres.
"""
import ast
import hashlib
import json
import re
from datetime import timedelta
from typing import Callable

from app.ontology.schema import ODOO_16, PARAMS, Release, formula_names
from app.ontology.spec import (
    COLUMN_TYPE,
    Ctx,
    Period,
    Plan,
    QuerySpec,
    SpecError,
    check_spec,
    context,
    leaf_meta,
    typed_literal,
)

BASE = "b"
ROW_ID = "_onto_id"
BUCKET_COLUMN = "__matrix_col"  # querygen's MATRIX_COL_ALIAS (app/utils/utils.py): the bucket of each long-format row
ORG_MAP_VIEW = "onto_org_map"  # plan 1.3: one row per stock.location, joined on location_id
ORG_ROLE_COLUMN = "role"
VISIBLE = "_onto_id IN (SELECT id FROM onto_visible)"  # plan 1.6 temp table, set by Odoo
MAX_KEY = 49  # wad_ (4) + onto_ (5) + key + _view (5) stays within Postgres' 63 bytes
_OPS = {"=": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">=", "in": "IN", "not in": "NOT IN"}
_ARITH = {"Add": "+", "Sub": "-", "Mult": "*"}
_VISIBLE_PREDICATE = re.compile(r"\b(?:WHERE|AND)\s+_onto_id\s+IN\s*\(\s*SELECT\s+id\s+FROM\s+onto_visible\s*\)", re.IGNORECASE)
_UNFILTERED_FALLBACK = re.compile(r"^\s*SELECT\s+\*\s+FROM\s+\S+?\s*;?\s*$", re.IGNORECASE)


def view_name(key: str) -> str:
    if len(key) > MAX_KEY:
        raise SpecError(f"object key {key!r} is longer than {MAX_KEY} characters")
    return f"wad_onto_{key}_view"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table(model: str) -> str:
    return model.replace(".", "_")  # Odoo's own table name for a model


def check_select_shape(sql: str) -> None:
    """Structural check that sql is one SELECT: balanced parentheses, no ';', no comments outside
    literals. It is not a parser. Odoo's _validate_custom_sql checks again on provisioning."""
    depth, index, words = 0, 0, []
    while index < len(sql):
        char = sql[index]
        if char in "'\"":
            end = index + 1
            while True:
                if end >= len(sql):
                    raise SpecError("unterminated literal or identifier")
                if sql[end] == char:
                    if end + 1 < len(sql) and sql[end + 1] == char:
                        end += 2
                        continue
                    break
                end += 1
            index = end + 1
            continue
        if sql.startswith(("--", "/*"), index):
            raise SpecError("SQL comment outside a literal")
        if char == ";":
            raise SpecError("statement terminator outside a literal")
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise SpecError("unbalanced parenthesis")
        elif char.isalpha() or char == "_":
            end = index
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            words.append(sql[index:end].upper())
            index = end
            continue
        index += 1
    if depth != 0:
        raise SpecError("unbalanced parenthesis")
    if not words or words[0] != "SELECT" or "FROM" not in words:
        raise SpecError("not a single SELECT ... FROM statement")


class _Joins:
    """The LEFT JOINs of one object: each many2one hop once, the org-role join once, in dependency order."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.lines: list[str] = []
        self._alias: dict[tuple, str] = {(): BASE}

    def _new_alias(self) -> str:
        return f"j{len(self.lines) + 1}"

    def path(self, binding: str) -> str:
        *hops, last = binding.split(".")
        model, prefix = self.ctx.obj.base_model, ()
        for name in hops:
            meta = self.ctx.fields[model][name]
            key = prefix + (name,)
            if key not in self._alias:
                alias = self._new_alias()
                self.lines.append(
                    f'LEFT JOIN {_q(_table(meta["relation"]))} AS {_q(alias)} '
                    f'ON {_q(alias)}."id" = {_q(self._alias[prefix])}.{_q(name)}'
                )
                self._alias[key] = alias
            prefix, model = key, meta["relation"]
        return f"{_q(self._alias[prefix])}.{_q(last)}"

    def expr(self, prop) -> str:
        if prop.org_role is None:
            return self.path(prop.bindings[ODOO_16])
        key = ("org", prop.org_role)
        if key not in self._alias:
            location_expr = self.path(self._location_prop(prop.org_role).bindings[ODOO_16])
            alias = self._new_alias()
            self.lines.append(f'LEFT JOIN {_q(ORG_MAP_VIEW)} AS {_q(alias)} ON {_q(alias)}."location_id" = {location_expr}')
            self._alias[key] = alias
        return f"{_q(self._alias[key])}.{_q(ORG_ROLE_COLUMN)}"

    def _location_prop(self, role_key: str):
        role = self.ctx.roles[role_key]
        target = next(o for o in self.ctx.release.objects if o.key == role.object).base_model
        for prop in self.ctx.obj.properties:
            if prop.org_role is None and self.ctx.kinds[prop.key] == "id" and leaf_meta(self.ctx.obj, prop, self.ctx.fields)["relation"] == target:
                return prop
        raise SpecError(f"{self.ctx.obj.key}: no property points at {target}, so org role {role_key} cannot join")


def _bool_predicate(col: str, op: str, value) -> str:
    """A boolean test that reads NULL as false, as Odoo does: `= TRUE` and `<> TRUE` would drop NULL rows."""
    if op not in ("=", "!=", "in", "not in"):
        raise SpecError(f"a boolean takes =, !=, in or not in, not {op}")

    def test(flag: bool) -> str:
        return f"{col} IS TRUE" if flag else f"{col} IS NOT TRUE"

    if op in ("in", "not in"):
        flags = [typed_literal("bool", item) == "TRUE" for item in value]
        if op == "in":
            return "(" + " OR ".join(test(flag) for flag in flags) + ")"
        return "(" + " AND ".join(test(not flag) for flag in flags) + ")"
    flag = typed_literal("bool", value) == "TRUE"
    return test(flag if op == "=" else not flag)


def _predicate(ctx: Ctx, prop, col: str, op: str, value, as_of) -> str:
    """One comparison. A negative test on an org role counts an unmapped location (NULL role) as usable (D43).
    A day parameter is bound: CAST(as_of) + %(name)s, so its value never reaches the SQL text (plan 1.4)."""
    if isinstance(value, dict):
        name = value.get("param")
        if as_of is None or name not in PARAMS:
            raise SpecError(f"{prop.key}: parameter {name!r} has no value here")
        day = typed_literal("date", as_of.isoformat())
        if name == "as_of":
            return f"{col} {_OPS[op]} {day}"
        return f"{col} {_OPS[op]} (CAST({day} AS DATE) + %({name})s)"
    kind = ctx.kinds[prop.key]
    if kind == "bool":
        return _bool_predicate(col, op, value)
    allowed = frozenset(ctx.roles[prop.org_role].values) if prop.org_role else None
    if op in ("in", "not in"):
        rhs = "(" + ", ".join(typed_literal(kind, item, allowed) for item in value) + ")"
    else:
        rhs = typed_literal(kind, value, allowed)
    text = f"{col} {_OPS[op]} {rhs}"
    if prop.org_role and op in ("!=", "not in"):
        return f"({col} IS NULL OR {text})"
    return text


def _column_type(ctx: Ctx, prop) -> str:
    return "char" if prop.org_role else COLUMN_TYPE[leaf_meta(ctx.obj, prop, ctx.fields)["type"]]


def location_binding(release: Release, object_key: str, role_key: str) -> str:
    """The Odoo 16 path an org-role property reads its location through: the field the compiler joins on (_Joins)."""
    return _Joins(context(release, object_key))._location_prop(role_key).bindings[ODOO_16]


def compile_object(release: Release, object_key: str, odoo_version: str = ODOO_16, *,
                   group_xmlids=(), engine_only: bool = False) -> dict:
    """The provision-payload object (3.2a): one SELECT from the base table, joins along the bound
    paths, the base filter, every property as a named column, and `id AS _onto_id` first."""
    ctx = context(release, object_key, odoo_version)
    obj = ctx.obj
    view_name(obj.key)
    joins = _Joins(ctx)
    where = [_predicate(ctx, ctx.props[c.prop], joins.expr(ctx.props[c.prop]), c.op, c.value, None)
             for c in obj.base_filter]
    select = [f"{_q(BASE)}.{_q('id')} AS {_q(ROW_ID)}"]
    columns = []
    for prop in obj.properties:
        if ctx.kinds[prop.key] is None:  # one2many: a link, reached through its aggregate metric, not a column
            continue
        select.append(f"{joins.expr(prop)} AS {_q(prop.key)}")
        columns.append({"name": prop.key, "type": _column_type(ctx, prop), "role": prop.role,
                        "unit": prop.unit, "pii": prop.pii})
    lines = ["SELECT", ",\n".join(f"  {item}" for item in select),
             f"FROM {_q(_table(obj.base_model))} AS {_q(BASE)}", *joins.lines]
    if where:
        lines.append("WHERE " + " AND ".join(f"({item})" for item in where))
    sql = "\n".join(lines)
    check_select_shape(sql)
    group_xmlids = sorted(group_xmlids)
    canonical = {"key": obj.key, "base_model": obj.base_model, "sql": sql, "columns": columns,
                 "group_xmlids": group_xmlids, "engine_only": engine_only}
    spec_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"key": obj.key, "label": obj.label, "base_model": obj.base_model, "sql": sql,
            "columns": columns, "group_xmlids": group_xmlids, "engine_only": engine_only,
            "spec_hash": spec_hash}


def _formula(text: str, col: Callable[[str], str]) -> str:
    """Arithmetic over view columns, from the parsed AST (schema.py admits nothing else)."""
    def walk(node) -> str:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Div):
                return f"({left} / NULLIF({right}, 0))"
            return f"({left} {_ARITH[type(node.op).__name__]} {right})"
        if isinstance(node, ast.UnaryOp):
            inner = walk(node.operand)
            return f"(-{inner})" if isinstance(node.op, ast.USub) else inner
        if isinstance(node, ast.Name):
            return col(node.id)
        return str(node.value)
    return walk(ast.parse(text, mode="eval"))


class _Days(ast.NodeTransformer):
    def __init__(self, days: int):
        self.days = days

    def visit_Name(self, node):
        return ast.copy_location(ast.Constant(self.days), node) if node.id == "days" else node


def _derive_text(text: str, days: int | None) -> str:
    return ast.unparse(_Days(days).visit(ast.parse(text, mode="eval")).body)


def _period(key: str, period: Period) -> str:
    col = _q(key)
    end = period.to + timedelta(days=1)  # exclusive upper bound: correct for date and datetime columns
    return f"{col} >= {typed_literal('date', period.from_.isoformat())} AND {col} < {typed_literal('date', end.isoformat())}"


def _previous(period: Period) -> Period:
    length = (period.to - period.from_).days + 1
    end = period.from_ - timedelta(days=1)
    return Period.model_validate({"from": (end - timedelta(days=length - 1)).isoformat(), "to": end.isoformat()})


def _measure(plan: Plan, leaf, period: Period | None) -> dict:
    ctx = plan.ctx
    if leaf.derived_from is None:
        value = ROW_ID if leaf.aggregation == "count" else leaf.measure
    else:
        value = _formula(leaf.derived_from, _q)
    conds = []
    for rule_key in leaf.filters:
        for cond in ctx.rules[rule_key].when:
            prop = ctx.props[cond.prop]
            conds.append(_predicate(ctx, prop, _q(prop.key), cond.op, cond.value, period.to if period else None))
    if plan.mode == "case" and plan.classes[leaf.key] == "time" and period is not None:
        conds.append(_period(leaf.time_property, period))
    if conds:
        value = f"CASE WHEN {' AND '.join(conds)} THEN {value} END"
    return {"field_name": value, "aggregation": leaf.aggregation, "alias": leaf.key}


def _request(plan: Plan, spec: QuerySpec, period: Period | None) -> dict:
    """One QueryRequests-shaped dict for one period. Every row is filtered by the visibility predicate."""
    ctx = plan.ctx
    as_of = period.to if period else None
    parts = [_predicate(ctx, ctx.props[f.prop], _q(f.prop), f.op, f.value, as_of) for f in spec.filters]
    if plan.mode == "where" and period is not None:
        parts.append(_period(plan.time_prop, period))
    where = "WHERE " + " AND ".join([VISIBLE, *(f"({part})" for part in parts)])
    measures = [_measure(plan, leaf, period) for leaf in plan.leaves]
    base = {"sql_view_name": view_name(ctx.obj.key), "pre_defined_filter_conditions": where}
    if plan.tile:
        return base | {"analytic_type": "tile", "data_field_name": measures[0]["field_name"],
                       "arithmetic_value_type": "COUNT"}
    col_dim = ({"field_name": plan.time_prop, "grain": spec.grain} if spec.grain
               else {"field_name": "1", "grain": "value"})  # a constant column: one total per group
    return base | {
        "analytic_type": "matrix",
        "matrix_row_dims": [{"field_name": d, "field_name_as": d} for d in spec.dims],
        "matrix_col_dim": col_dim,
        "matrix_measures": [{"field_name": m["field_name"], "aggregation": m["aggregation"], "alias": m["alias"]}
                            for m in measures],
    }


def _merge(plan: Plan, spec: QuerySpec, prev: Period) -> dict:
    """The contract for joining a compare's two results (plan 1.4). Odoo builds the previous-period merge from it.

    `sql` and `previous_sql` each return long rows, one per combination of match_dims values and bucket
    (bucket_column). Pair a current row with a previous row when every column in match_dims is equal and,
    when match_bucket is "offset", their offsets are equal. A row's offset counts whole bucket_grain steps from
    the first bucket of its own period, which is what shifts the previous buckets onto the current ones: the
    current period's first bucket is the grain unit that holds period.from, and the previous period's is the one
    that holds previous_period.from. Day counts days, week counts Monday-start weeks, and month, quarter and year
    count calendar units. Pairing is by this key, never by row position, so periods with different bucket counts
    still line up.
    A key with no row on one side takes `absent` for that side: 0 when absent is "zero", null when it is "null".
    For each entry of measures, with cur and prev the two values of the metric column:
      <key>_previous     prev
      <key>_change       cur - prev, or null when either is null
      <key>_change_pct   (cur - prev) / prev as a fraction (0.25 is +25%), or null when prev is null or 0
    The metric column is the queried metric after its derive formulas, which apply to both results.
    """
    key = plan.root.key
    return {
        "kind": "previous_period",
        "previous_period": {"from": prev.from_.isoformat(), "to": prev.to.isoformat()},
        "match_dims": list(spec.dims),
        "match_bucket": "offset" if spec.grain else None,
        "bucket_column": BUCKET_COLUMN if spec.grain else None,
        "bucket_grain": spec.grain,
        "absent": "null" if plan.root.additivity == "non_additive" else "zero",
        "measures": [{"key": key, "previous": f"{key}_previous", "change": f"{key}_change",
                      "change_pct": f"{key}_change_pct"}],
    }


def compile_query(release: Release, spec: QuerySpec) -> dict:
    """A checked spec as querygen request(s), in memory only. The matrix path is unrounded: any decimal
    measure (or a derived one) must use it, because the tile and chart paths wrap values in ROUND().
    `params` are the bound values; the SQL refers to them as %(name)s and they are never inlined."""
    plan = check_spec(release, spec)
    period = spec.period
    days = (period.to - period.from_).days + 1 if period else None
    current = _request(plan, spec, period)
    previous = merge = None
    if spec.compare is not None:
        prev = _previous(period)
        previous = _request(plan, spec, prev)
        merge = _merge(plan, spec, prev)
    derive = []
    for node in plan.derives:
        text = _derive_text(node.derived_from, days)
        entry = {"alias": node.key, "formula": text}
        if "/" in text:
            entry["zero_division"] = "null"
        derive.append(entry)
    return {
        "release": release.digest(),
        "object": plan.ctx.obj.key,
        "metric": plan.root.key,
        "path": "tile" if plan.tile else "matrix",
        "decimal": plan.decimal,
        "request": current,
        "previous_request": previous,
        "merge": merge,
        "derive": derive,
        "top_n": spec.top_n.model_dump() if spec.top_n else None,
        "row_cap": spec.limit,
        "page": spec.page,
        "period": {"from": period.from_.isoformat(), "to": period.to.isoformat()} if period else None,
        "params": plan.params,
    }


def query_reads(release: Release, spec: QuerySpec) -> dict:
    """What a checked spec reads (D49): each property it touches with its Odoo 16 path (an org-role
    property is read through its location field), and whether a balance is read at the period end
    (snapshot), so Odoo can apply the as_of rule. Built from the same plan as compile_query."""
    plan = check_spec(release, spec)
    ctx = plan.ctx
    keys = set(spec.dims) | {f.prop for f in spec.filters}
    for leaf in plan.leaves:
        if plan.classes[leaf.key] == "time":
            keys.add(leaf.time_property)
        if leaf.measure is not None:
            keys.add(leaf.measure)
        if leaf.derived_from is not None:
            keys |= formula_names(leaf.derived_from) & set(ctx.props)
        for rule_key in leaf.filters:
            keys |= {cond.prop for cond in ctx.rules[rule_key].when}
    joins = _Joins(ctx)
    bindings = {}
    for key in sorted(keys):
        prop = ctx.props[key]
        source = joins._location_prop(prop.org_role) if prop.org_role else prop
        bindings[key] = source.bindings[ODOO_16]
    return {"bindings": bindings, "snapshot": any(c == "snapshot" for c in plan.classes.values())}


def assert_visibility_predicate(sql: str) -> str:
    """Refuse a querygen response whose SQL lacks the row-visibility predicate, or is the unfiltered
    fallback SELECT * FROM <view> (R2): querygen silently drops filters it does not know."""
    if not isinstance(sql, str) or not sql.strip():
        raise SpecError("querygen returned no SQL")
    if _UNFILTERED_FALLBACK.match(sql):
        raise SpecError("querygen returned the unfiltered fallback SELECT * FROM <view>")
    if not _VISIBLE_PREDICATE.search(sql):
        raise SpecError("the row-visibility predicate is missing from the returned SQL")
    return sql
