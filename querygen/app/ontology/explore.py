"""SQL for exploration and lookup (plan 3.5; contracts 3.2 exploration, 3.5 lookup). Pure: no DB, no Odoo.

compile_explore() turns an explore spec and the hop map Odoo sends into one SELECT over Odoo's own tables.
Every value is a %(name)s parameter; only identifiers (quoted with the compiler's _q) reach the SQL text.
compile_lookup() reads the distinct values of one property of a published object's managed view (its
WHERE clause is the same row-visibility predicate the query path uses, so a value only comes from rows the
user may see). Both raise SpecError (422) or LookupError (404); nothing here executes SQL.
"""
import re
from datetime import date, datetime
from typing import Any

from app.ontology.compiler import VISIBLE, _bool_predicate, _q, assert_visibility_predicate, check_select_shape, \
    location_binding, view_name
from app.ontology.schema import ODOO_16, MAX_HOPS, Release
from app.ontology.spec import KIND, SpecError, context

BASE = "b"  # the compiler's base alias, so Odoo can add its own semi-join on b.id
EXPLORE_VISIBLE = f'"{BASE}"."id" IN (SELECT id FROM onto_visible)'  # the view path's predicate on the base table's id
TABLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")  # a bare table name: no dot, so no schema-qualified or foreign table
MODEL = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
FIELD = re.compile(r"^[a-z][a-z0-9_]*$")
OPS_SQL = {"=": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">=", "in": "IN", "not in": "NOT IN"}
AGGREGATES = {"count": "COUNT", "sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX"}
NUMERIC = frozenset({"float", "monetary", "integer"})
ORDERED = NUMERIC | frozenset({"date", "datetime"})  # min and max also take dates
MAX_FIELDS, MAX_GROUP, MAX_FILTERS, MAX_IN, MAX_LIMIT = 50, 10, 20, 100, 5000


def _scalar_param(kind: str, value: Any) -> Any:
    """A bound value of the right Python type, or SpecError. Booleans never reach here (see _filter)."""
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SpecError(f"expected a number, got {value!r}")
        return value
    if kind == "id":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SpecError(f"expected a record id, got {value!r}")
        return value
    if kind in ("date", "datetime"):
        if not isinstance(value, str):
            raise SpecError(f"expected an ISO date, got {value!r}")
        try:
            (datetime if kind == "datetime" else date).fromisoformat(value)
        except ValueError as exc:
            raise SpecError(f"expected an ISO date, got {value!r}") from exc
        return value
    if kind == "text":
        if not isinstance(value, str):
            raise SpecError(f"expected text, got {value!r}")
        return value
    raise SpecError(f"values of this type cannot be filtered: {kind}")


def _check_hops(path: str, hops: Any, model: str, tables: dict) -> None:
    """The hop list must spell the path, start on the base model, and chain through m2o comodels."""
    if not isinstance(hops, list) or not 1 <= len(hops) <= MAX_HOPS:
        raise SpecError(f"path {path!r} needs 1 to {MAX_HOPS} hops")
    for hop in hops:
        if not isinstance(hop, dict) or not isinstance(hop.get("field"), str) or not FIELD.match(hop["field"]):
            raise SpecError(f"path {path!r}: a hop needs a field name")
        if not isinstance(hop.get("type"), str):
            raise SpecError(f"path {path!r}: hop {hop['field']!r} needs a type")
        if hop.get("model") not in tables:
            raise SpecError(f"path {path!r}: hop model {hop.get('model')!r} has no table")
    if ".".join(hop["field"] for hop in hops) != path:
        raise SpecError(f"path {path!r} does not match its hops")
    if hops[0]["model"] != model:
        raise SpecError(f"path {path!r} does not start on {model}")
    for hop, following in zip(hops, hops[1:]):
        if hop["type"] != "many2one" or hop.get("comodel") not in tables or following["model"] != hop["comodel"]:
            raise SpecError(f"path {path!r}: hop {hop['field']!r} must be a many2one into the next hop's model")


class _Joins:
    """LEFT JOINs for the m2o prefixes of the paths, each once. Aliases j1, j2 ... follow the order they appear."""

    def __init__(self, tables: dict):
        self.tables = tables
        self.lines: list[str] = []
        self.alias: dict[tuple, str] = {(): BASE}
        self.binding: dict[str, str] = {}  # join alias -> the prefix path it reads

    def expr(self, hops: list[dict]) -> str:
        prefix: tuple = ()
        for hop in hops[:-1]:
            key = prefix + (hop["field"],)
            if key not in self.alias:
                alias = f"j{len(self.lines) + 1}"
                self.lines.append(f'LEFT JOIN {_q(self.tables[hop["comodel"]])} AS {_q(alias)} '
                                  f'ON {_q(alias)}."id" = {_q(self.alias[prefix])}.{_q(hop["field"])}')
                self.alias[key] = alias
                self.binding[alias] = ".".join(key)
            prefix = key
        return f'{_q(self.alias[prefix])}.{_q(hops[-1]["field"])}'


def compile_explore(body: dict) -> dict:
    """{"spec", "tables", "paths"} (Odoo's request) -> {"sql", "params", "bindings"}. Raises SpecError."""
    spec, tables, paths = body.get("spec"), body.get("tables"), body.get("paths")
    if not isinstance(spec, dict) or not isinstance(tables, dict) or not isinstance(paths, dict):
        raise SpecError("spec, tables and paths are required")
    for model, table in tables.items():
        if not isinstance(model, str) or not MODEL.match(model) or not isinstance(table, str) or not TABLE.match(table):
            raise SpecError(f"table for {model!r} must be a bare table name")
    model = spec.get("model")
    if not isinstance(model, str) or model not in tables:
        raise SpecError("spec.model must be a model named in tables")

    fields = spec.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS or not all(isinstance(f, str) for f in fields):
        raise SpecError(f"fields must be 1 to {MAX_FIELDS} paths")
    for path in fields:
        if path not in paths:
            raise SpecError(f"field {path!r} has no hop map")
        _check_hops(path, paths[path], model, tables)
    for path in paths:  # every mapped path is a requested field; the map holds nothing else
        if path not in fields:
            raise SpecError(f"hop map names {path!r}, which is not a requested field")

    joins = _Joins(tables)
    bindings: dict[str, str] = {}

    def hop_expr(path: str) -> str:
        return joins.expr(paths[path])

    group_by = spec.get("group_by", [])
    if not isinstance(group_by, list) or len(group_by) > MAX_GROUP or len(set(group_by)) != len(group_by):
        raise SpecError(f"group_by must be up to {MAX_GROUP} distinct paths")
    select, group_exprs = [], []
    for path in group_by:
        if path not in fields:
            raise SpecError(f"group_by {path!r} is not a requested field")
        expr = hop_expr(path)
        select.append(f"{expr} AS {_q(path)}")
        group_exprs.append(expr)
        bindings[path] = path

    aggregate = spec.get("aggregate") or {"op": "count", "path": None}
    op, agg_path = aggregate.get("op"), aggregate.get("path")
    if op not in AGGREGATES:
        raise SpecError(f"aggregate op must be one of {sorted(AGGREGATES)}")
    if op == "count" and agg_path is None:
        agg_sql = "COUNT(*)"
    else:
        if not isinstance(agg_path, str) or agg_path not in fields:
            raise SpecError(f"aggregate {op} needs a requested path")
        numeric_types = NUMERIC if op in ("sum", "avg") else ORDERED
        if op != "count" and paths[agg_path][-1]["type"] not in numeric_types:
            raise SpecError(f"aggregate {op} does not take {agg_path!r}")
        agg_sql = f"{AGGREGATES[op]}({hop_expr(agg_path)})"
        bindings["_aggregate"] = agg_path
    select.append(f"{agg_sql} AS {_q('_aggregate')}")

    params: dict[str, Any] = {}
    predicates = [EXPLORE_VISIBLE]
    filters = spec.get("filters", [])
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise SpecError(f"filters: up to {MAX_FILTERS}")
    for i, item in enumerate(filters):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] not in fields:
            raise SpecError(f"filter {i}: path must be a requested field")
        op_name, value, path = item.get("op"), item.get("value"), item["path"]
        if op_name not in OPS_SQL:
            raise SpecError(f"filter {i}: unknown op {op_name!r}")
        hops = paths[path]
        kind = KIND.get(hops[-1]["type"])
        col = hop_expr(path)
        bindings[f"f{i}"] = path
        if kind == "bool":
            predicates.append(_bool_predicate(col, op_name, value))
            continue
        if kind is None:
            raise SpecError(f"filter {i}: type {hops[-1]['type']!r} cannot be filtered")
        listed = op_name in ("in", "not in")
        values = value if listed else [value]
        if listed and (not isinstance(value, list) or not 1 <= len(value) <= MAX_IN):
            raise SpecError(f"filter {i}: {op_name} takes a list of 1 to {MAX_IN} values")
        if not listed and value is None:
            raise SpecError(f"filter {i}: a value is required")
        names = [f"f{i}_{j}" if listed else f"f{i}" for j in range(len(values))]
        for name, item_value in zip(names, values):
            params[name] = _scalar_param(kind, item_value)
        if listed:
            predicates.append(f"{col} {OPS_SQL[op_name]} ({', '.join(f'%({n})s' for n in names)})")
        else:
            predicates.append(f"{col} {OPS_SQL[op_name]} %({names[0]})s")

    limit = spec.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise SpecError(f"limit must be 1 to {MAX_LIMIT}")

    # The select list is built first: it registers the joins the rest of the statement needs.
    lines = ["SELECT " + ", ".join(select), f"FROM {_q(tables[model])} AS {_q(BASE)}", *joins.lines,
             "WHERE " + " AND ".join(f"({p})" for p in predicates)]
    if group_exprs:
        lines.append("GROUP BY " + ", ".join(group_exprs))
    lines.append(f"LIMIT {limit}")
    for alias, path in joins.binding.items():
        bindings[alias] = path
    sql = "\n".join(lines)
    check_select_shape(sql)
    if EXPLORE_VISIBLE not in sql:
        raise SpecError("the row-visibility predicate is missing from the explore SQL")
    return {"sql": sql, "params": params, "bindings": bindings}


def compile_lookup(release: Release, object_key: str, column: str) -> dict:
    """The distinct-value read of one property of a published object. Raises LookupError (404) or SpecError (422)."""
    if not any(o.key == object_key for o in release.objects):
        raise LookupError(object_key)
    ctx = context(release, object_key)
    prop = ctx.props.get(column)
    if prop is None or ctx.kinds[column] is None:  # unknown, or a one2many link that has no column
        raise LookupError(column)
    if prop.pii:
        raise SpecError(f"{column} is personal data and is not looked up")
    path = location_binding(release, object_key, prop.org_role) if prop.org_role else prop.bindings[ODOO_16]
    sql = f"SELECT {_q(column)} FROM {_q(view_name(object_key))} WHERE {VISIBLE}"
    assert_visibility_predicate(sql)
    return {"sql": sql, "params": {}, "bindings": {column: path}}
