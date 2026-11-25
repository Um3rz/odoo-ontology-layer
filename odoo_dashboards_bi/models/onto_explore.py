# -*- coding: utf-8 -*-
"""Uncertified exploration and value lookup for the BI Agent (3.5, plan A3.4a).

describe (POST /onto/v1/explore with "describe"): the schema graph from one model, bounded by depth and size. Only
models and stored fields the caller may read (ACL and field groups) are listed. No record values.

run_explore (POST /onto/v1/explore with an explore spec): one aggregate over a model and its many2one paths. Odoo
checks every path as the caller (ACL, field groups, three segments at most), querygen compiles the spec to one
SELECT (onto/explore/compile), and Odoo runs it. Each table the SQL may read is replaced by a CTE that holds only the
rows the caller may read in that model (its record rules and companies), so a joined row the caller cannot read reads
as NULL. Output is the group-by columns and one aggregate: no raw rows.

lookup_values (POST /onto/v1/lookup_values): the distinct values of one managed dataset column that match a partial
name, prefix matches first, at most 20. querygen compiles the column read (onto/lookup/compile) and names the field it
is bound to. Odoo checks that field (field groups), reads the managed view under the caller's record rules and guard
models (onto_query._visibility), and adds the term and the cap as parameters. A column bound to a many2one stores
record ids: the term matches the related records' display names instead, and each value is an [id, name] pair.

Nothing here takes SQL from a caller. No value is logged.
"""

import logging
import math
import re

from odoo.addons.odoo_dashboards_saas.models.sql_guard import check_select, code_only
from odoo.exceptions import AccessError, UserError

from .onto_dataset import KEY_RE, view_name
from .onto_query import (
    COMPILE_TIMEOUT,
    CURRENT,
    FORBIDDEN,
    NOT_FOUND,
    PARAM_NAME,
    UNAVAILABLE,
    QueryError,
    QuerygenError,
    _ai_gate,
    _check_fields,
    _companies,
    _fetch,
    _plain,
    _published,
    _refuse,
    _suspended,
    _unexpected,
    _visibility,
    querygen_call,
)
from .onto_rules import visible_ids_sql
from .onto_store import onto_now

_logger = logging.getLogger(__name__)

MAX_DEPTH = 2
MAX_NODES = 25
MAX_NODE_FIELDS = 60
MAX_SELECTION = 30
MAX_FIELDS = 8
MAX_FILTERS = 10
MAX_GROUP_BY = 3
MAX_SEGMENTS = 3
MAX_IN = 50
MAX_TEXT = 200
EXPLORE_LIMIT = 500
EXPLORE_DEFAULT = 100
LOOKUP_CAP = 20
LOOKUP_NAMES = 200  # ponytail: names matched before the object's own rows filter them; raise if broad terms miss
LOOKUP_TERM = 60
AGGREGATES = ("count", "sum", "avg", "min", "max")
NUMERIC = ("integer", "float", "monetary")
SCALAR_TYPES = ("char", "text", "selection", "date", "datetime", "boolean", "integer", "float", "monetary",
                "many2one")
TEXT_TYPES = ("char", "text", "selection", "date", "datetime")
FILTER_OPS = ("=", "!=", "<", "<=", ">", ">=", "in", "not in")
EXPLORE_KEYS = frozenset(("model", "fields", "filters", "group_by", "aggregate", "limit"))
LOOKUP_KEYS = frozenset(("object", "column", "term", "release"))
# Internals: Odoo's infrastructure, Onto's own ledgers, and API key tables (never listed or queried).
EXCLUDED = ("ir.", "base.", "bus.", "mail.", "onto.", "res.users.apikeys")
MODEL_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*")
PATH_RE = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*){0,2}")
TABLE_RE = re.compile(r"[a-z_][a-z0-9_]*")
IDENT = re.compile(r'(\.?)\s*"?([a-z_][a-z0-9_]*)"?')  # applied to lower-cased code (code_only keeps quoted names)
AGG_KEY = "_aggregate"
TIMEOUT_TEXT = "The query took too long. Narrow the filters or the period."


# ---------------------------------------------------------------------------
# Shared checks
# ---------------------------------------------------------------------------


def _model_ok(env, name):
    """True for a model name the caller may be shown or asked about: well formed, registered, concrete, not internal."""
    if not isinstance(name, str) or len(name) > 100 or not MODEL_RE.fullmatch(name):
        return False
    if name.startswith(EXCLUDED) or name not in env.registry:
        return False
    model = env.registry[name]
    return not (model._abstract or model._transient)


def _readable(model, name):
    """True when the caller may read one field (ACL and field groups, Odoo's own check)."""
    try:
        model.check_field_access_rights("read", [name])
    except AccessError:
        return False
    return True


def _forbidden(path):
    return QueryError(403, "forbidden", "The query reads a field or model you may not read: %s." % path)


def _registry_tables(env):
    """Every concrete table in the registry: a query may name one of these only through the caller's CTE."""
    tables = set()
    for model in env.registry.values():
        if model._abstract or model._transient:
            continue
        table = getattr(model, "_table", None)
        if isinstance(table, str):
            tables.add(table)
    return tables


def _querygen(env, path, body):
    """One compile call. querygen's refusals map to the caller's statuses; a reply that is not an object is 502."""
    try:
        reply = querygen_call(env, path, body, COMPILE_TIMEOUT)
    except QuerygenError as exc:
        if exc.status == 404:
            raise QueryError(404, "not_found", NOT_FOUND)
        if exc.status == 422:
            raise _refuse(exc.message or "the query is not valid")
        if exc.status == 403:
            raise QueryError(403, "forbidden", FORBIDDEN)
        raise QueryError(502, "querygen_unavailable", UNAVAILABLE)
    if not isinstance(reply, dict):
        raise _unexpected()
    return reply


def _one_select(sql):
    """querygen's SQL without a trailing semicolon, which must be one SELECT."""
    if not isinstance(sql, str):
        raise _unexpected()
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not re.match(r"(?i)select\b", text):
        raise _unexpected()
    return text


def _params_ok(params):
    """querygen's bound values: named like PARAM_NAME, never Odoo's own onto_ names, and scalar."""
    for name, value in params.items():
        if not isinstance(name, str) or not PARAM_NAME.fullmatch(name) or name.startswith("onto_"):
            raise _unexpected()
        if value is not None and (type(value) not in (int, float, str, bool) or (
                type(value) is float and not math.isfinite(value))):
            raise _unexpected()


def _check_relations(sql, allowed, registry):
    """Refuse querygen SQL that could read a table other than the allowed ones: no WITH (a CTE of the same name would
    shadow the caller's filtered CTE), no schema-qualified table, no catalogue, no other registry table or managed view.
    The check is on tokens, so it is strict: a name that is also a column name is refused too."""
    code = code_only(sql, keep_names=True).lower()
    if re.search(r"\bwith\b", code):
        raise _unexpected()
    for dot, name in IDENT.findall(code):
        is_table = (name in allowed or name in registry or name.startswith(("wad_", "pg_"))
                    or name == "information_schema")
        if is_table and (dot or name not in allowed):
            raise _unexpected()


def _prefixes(path):
    parts = path.split(".")
    return {".".join(parts[: index + 1]) for index in range(len(parts))}


def _bound_relation(env, model, path):
    """The model a dotted many2one path ends on, or None when it ends on a plain field."""
    for name in path.split("."):
        field = env[model]._fields[name]
        if field.type != "many2one":
            return None
        model = field.comodel_name
    return model


def _like(term):
    """A term as a LIKE pattern body: \\, % and _ are literal."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _now_text():
    return onto_now().replace(microsecond=0).isoformat() + "Z"


# ---------------------------------------------------------------------------
# describe: the schema graph (bounded depth and size; readable fields only)
# ---------------------------------------------------------------------------


def _node(env, model_name):
    """One model of the schema graph: its readable stored fields, and the models its many2one fields point to."""
    model = env[model_name]
    fields_out, relations, truncated = [], [], False
    for name in sorted(model._fields):
        field = model._fields[name]
        if not field.store or name.startswith("_") or field.type not in SCALAR_TYPES or not _readable(model, name):
            continue
        entry = {"name": name, "label": str(field.string), "type": field.type}
        if field.type == "many2one":
            comodel = field.comodel_name
            if not _model_ok(env, comodel) or not env[comodel].check_access_rights("read", raise_exception=False):
                continue  # a relation to a model the caller cannot read is not shown at all
            entry["relation"] = comodel
        elif field.type == "selection":
            entry["selection"] = [[str(v), str(label)] for v, label in field._description_selection(env)][:MAX_SELECTION]
        if len(fields_out) >= MAX_NODE_FIELDS:
            truncated = True
            break
        fields_out.append(entry)
        if field.type == "many2one":
            relations.append(field.comodel_name)
    node = {"model": model_name, "name": model._description or model_name, "fields": fields_out,
            "truncated": truncated}
    return node, relations


def describe(env, raw, cids):
    """The schema graph from one model: its models and fields, depth relations deep (at most MAX_DEPTH), at most
    MAX_NODES models. The caller's ACL and field groups decide what is listed. Names and labels only."""
    if not isinstance(raw, dict) or not set(raw) <= {"describe", "depth"}:
        raise _refuse("describe takes a model name and an optional depth")
    depth = raw.get("depth", 1)
    if type(depth) is not int or not 1 <= depth <= MAX_DEPTH:
        raise _refuse("depth must be a whole number from 1 to %d" % MAX_DEPTH)
    name = raw.get("describe")
    if not isinstance(name, str) or len(name) > 100 or not MODEL_RE.fullmatch(name):
        raise _refuse("describe must be a model name")
    user = env.user
    cids = _companies(user, cids)
    _ai_gate(env, cids)
    env = env(context=dict(env.context, allowed_company_ids=cids))
    if not _model_ok(env, name):
        raise QueryError(404, "not_found", NOT_FOUND)
    if not env[name].check_access_rights("read", raise_exception=False):
        raise QueryError(403, "forbidden", FORBIDDEN)
    nodes, seen, queue, truncated = [], {name}, [(name, 0)], False
    while queue:
        model_name, level = queue.pop(0)
        node, relations = _node(env, model_name)
        nodes.append(node)
        truncated = truncated or node["truncated"]
        if level >= depth:
            continue
        for comodel in relations:
            if comodel in seen:
                continue
            if len(nodes) + len(queue) < MAX_NODES:
                seen.add(comodel)
                queue.append((comodel, level + 1))
            else:
                truncated = True
    return {"model": name, "depth": depth, "models": nodes, "truncated": truncated}


# ---------------------------------------------------------------------------
# explore: an aggregate spec over the schema graph
# ---------------------------------------------------------------------------


def _path(value):
    if not isinstance(value, str) or not PATH_RE.fullmatch(value):
        raise _refuse("a path is field names joined by dots, at most %d names" % MAX_SEGMENTS)
    return value


def _path_list(raw, name, cap):
    if not isinstance(raw, list) or len(raw) > cap:
        raise _refuse("%s must be a list of at most %d paths" % (name, cap))
    paths = [_path(item) for item in raw]
    if len(set(paths)) != len(paths):
        raise _refuse("%s must not repeat a path" % name)
    return paths


def _scalar(value):
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= MAX_TEXT
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _filter_list(raw):
    if not isinstance(raw, list) or len(raw) > MAX_FILTERS:
        raise _refuse("filters must be a list of at most %d conditions" % MAX_FILTERS)
    out = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"path", "op", "value"}:
            raise _refuse("each filter needs path, op and value and nothing else")
        op, value = item["op"], item["value"]
        if op not in FILTER_OPS:
            raise _refuse("filter op must be one of %s" % ", ".join(FILTER_OPS))
        if op in ("in", "not in"):
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN or not all(
                    v is not None and _scalar(v) for v in value):
                raise _refuse("an in filter needs a list of 1 to %d values" % MAX_IN)
        elif value is None:
            if op not in ("=", "!="):
                raise _refuse("only = and != can compare with null")
        elif not _scalar(value):
            raise _refuse("filter values are text, numbers, true or false")
        out.append({"path": _path(item["path"]), "op": op, "value": value})
    return out


def _aggregate(raw):
    if raw is None:
        raise _refuse("explore returns aggregates only: name one (count, sum, avg, min or max)")
    if not isinstance(raw, dict) or not set(raw) <= {"op", "path"} or raw.get("op") not in AGGREGATES:
        raise _refuse("aggregate needs op (count, sum, avg, min or max) and, except for count, a path")
    if raw["op"] == "count":
        if raw.get("path") is not None:
            raise _refuse("count takes no path")
        return {"op": "count", "path": None}
    return {"op": raw["op"], "path": _path(raw.get("path"))}


def _parse_explore(raw):
    """The explore spec, checked for shape and size. Names, types and access are checked against the caller next."""
    if not isinstance(raw, dict):
        raise _refuse("the explore spec must be a JSON object")
    unknown = sorted(set(raw) - EXPLORE_KEYS)
    if unknown:
        raise _refuse("unknown field(s): %s" % ", ".join(unknown))
    model = raw.get("model")
    if not isinstance(model, str) or len(model) > 100 or not MODEL_RE.fullmatch(model):
        raise _refuse("model must be a model name")
    fields = _path_list(raw.get("fields", []), "fields", MAX_FIELDS)
    group_by = _path_list(raw.get("group_by", []), "group_by", MAX_GROUP_BY)
    if "id" in group_by:
        raise _refuse("grouping by id returns one row per record; group by another field")
    if fields and not set(group_by) <= set(fields):
        raise _refuse("every group_by path must also be listed in fields")
    limit = raw.get("limit", EXPLORE_DEFAULT)
    if type(limit) is not int or not 1 <= limit <= EXPLORE_LIMIT:
        raise _refuse("limit must be a whole number from 1 to %d" % EXPLORE_LIMIT)
    return {
        "model": model,
        "fields": fields,
        "group_by": group_by,
        "filters": _filter_list(raw.get("filters", [])),
        "aggregate": _aggregate(raw.get("aggregate")),
        "limit": limit,
    }


def _resolve(env, model_name, path):
    """(hops, label, type) of a dotted path, as the caller may read it. Every hop's field must be stored, readable
    (ACL and field groups) and, except the last, a many2one whose model the caller may read. 403 when the caller may
    not read a hop; 422 when Odoo cannot use the path at all."""
    names = path.split(".")
    hops, labels, current = [], [], env[model_name]
    for index, name in enumerate(names):
        field = current._fields.get(name)
        if field is None or not field.store:
            raise _refuse("%s is not a stored field" % path)
        if not _readable(current, name):
            raise _forbidden(path)
        labels.append(str(field.string))
        if index == len(names) - 1:
            if field.type not in SCALAR_TYPES:
                raise _refuse("%s cannot be used in a query (%s)" % (path, field.type))
            hops.append({"field": name, "type": field.type, "model": current._name})
            return hops, " > ".join(labels), field.type
        if field.type != "many2one":
            raise _refuse("%s goes through %s, which is not a relation" % (path, name))
        comodel = field.comodel_name
        if not _model_ok(env, comodel) or not env[comodel].check_access_rights("read", raise_exception=False):
            raise _forbidden(path)
        hops.append({"field": name, "type": "many2one", "model": current._name, "comodel": comodel})
        current = env[comodel]
    raise _unexpected()  # unreachable: PATH_RE gives at least one name


def _check_value(path, ftype, op, value):
    """A filter value must match the type of its field (a many2one takes record ids)."""
    if value is None:
        return
    for item in (value if isinstance(value, list) else [value]):
        if ftype in TEXT_TYPES:
            ok = isinstance(item, str)
        elif ftype in ("integer", "many2one"):
            ok = type(item) is int
        elif ftype in ("float", "monetary"):
            ok = isinstance(item, (int, float)) and not isinstance(item, bool)
        elif ftype == "boolean":
            ok = isinstance(item, bool)
        else:
            ok = False
        if not ok:
            raise _refuse("the value for %s does not match its type" % path)


def _scope_ctes(env, model_names):
    """One CTE per model the SQL may read. Each holds only the rows the caller may read in that model (visible_ids_sql:
    the model's own record rules and companies), read from a temporary table made in this transaction."""
    ctes = []
    for index, name in enumerate(sorted(model_names)):
        table = env[name]._table
        if not TABLE_RE.fullmatch(table):
            raise _unexpected()
        visible_sql, params = visible_ids_sql(env, name)
        temp = "onto_scope_%d" % index
        env.cr.execute(
            "DROP TABLE IF EXISTS pg_temp.%s; CREATE TEMP TABLE %s ON COMMIT DROP AS SELECT id FROM %s t(id)"
            % (temp, temp, visible_sql),
            params,
        )
        ctes.append('"%s" AS (SELECT * FROM "%s" WHERE id IN (SELECT id FROM %s))' % (table, table, temp))
    return ctes


def run_explore(env, raw, cids):
    """An explore spec, run for the caller. Returns the query response shape with trust "uncertified" and release null."""
    user = env.user
    cids = _companies(user, cids)
    _ai_gate(env, cids)
    spec = _parse_explore(raw)
    env = env(context=dict(env.context, allowed_company_ids=cids))
    model_name = spec["model"]
    if not _model_ok(env, model_name):
        raise QueryError(404, "not_found", NOT_FOUND)
    if not env[model_name].check_access_rights("read", raise_exception=False):
        raise QueryError(403, "forbidden", FORBIDDEN)
    aggregate = spec["aggregate"]
    roles = [(path, False) for path in spec["fields"] + spec["group_by"] + [f["path"] for f in spec["filters"]]]
    if aggregate["path"]:
        roles.append((aggregate["path"], True))
    resolved = {}
    for path, _ in roles:
        if path not in resolved:
            resolved[path] = _resolve(env, model_name, path)
    for path, numeric in roles:
        if numeric and resolved[path][2] not in NUMERIC:
            raise _refuse("%s is not a number, so it cannot be summed or averaged" % path)
    for item in spec["filters"]:
        _check_value(item["path"], resolved[item["path"]][2], item["op"], item["value"])
    models = {model_name}
    for hops, _, _ in resolved.values():
        for hop in hops:
            models.add(hop["model"])
            if "comodel" in hop:
                models.add(hop["comodel"])
    authorized = set()
    for path in resolved:
        authorized |= _prefixes(path)
    tables = {name: env[name]._table for name in models}
    # querygen maps exactly the requested fields: filter and aggregate paths must be among them too.
    body = {
        "spec": {"model": model_name, "fields": list(resolved), "group_by": spec["group_by"],
                 "filters": spec["filters"], "aggregate": aggregate, "limit": spec["limit"]},
        "tables": tables,
        "paths": {path: hops for path, (hops, _, _) in resolved.items()},
    }
    reply = _querygen(env, "onto/explore/compile", body)
    sql, params, bindings = reply.get("sql"), reply.get("params", {}), reply.get("bindings", {})
    if not isinstance(params, dict) or not isinstance(bindings, dict):
        raise _unexpected()
    sql = _one_select(sql)
    _params_ok(params)
    if any(not isinstance(value, str) or value not in authorized for value in bindings.values()):
        raise _unexpected()
    _check_relations(sql, set(tables.values()), _registry_tables(env))
    ctes = _scope_ctes(env, models)
    # querygen's SQL keeps the base rows in onto_visible, which the governed query path creates; explore makes it too.
    visible_sql, visible_params = visible_ids_sql(env, model_name)
    env.cr.execute("DROP TABLE IF EXISTS pg_temp.onto_visible; CREATE TEMP TABLE onto_visible ON COMMIT DROP AS "
                   "SELECT id FROM %s t(id)" % visible_sql, visible_params)
    final = "WITH " + ", ".join(ctes) + "\n" + sql
    try:
        check_select(final)
    except UserError:
        raise _unexpected()
    fetched = _fetch(env, final, spec["limit"] + 1, params)
    if fetched is None:
        raise QueryError(408, "timeout", TIMEOUT_TEXT)
    names, rows = fetched
    group_by = spec["group_by"]
    if names != group_by + [AGG_KEY]:
        raise _unexpected()
    _logger.info("Onto explore model=%s rows=%d", model_name, len(rows))
    columns = [{"key": path, "label": resolved[path][1], "role": "dimension", "unit": None} for path in group_by]
    agg_label = "Count" if aggregate["op"] == "count" else "%s of %s" % (
        aggregate["op"].capitalize(), resolved[aggregate["path"]][1])
    columns.append({"key": AGG_KEY, "label": agg_label, "role": "measure",
                    "unit": "count" if aggregate["op"] == "count" else None})
    truncated = len(rows) > spec["limit"]
    return {
        "columns": columns,
        "rows": [[_plain(value) for value in row] for row in rows[: spec["limit"]]],
        "release": None,
        "read_at": _now_text(),
        "truncated": truncated,
        "next_page": None,
        "trust": "uncertified",
    }


# ---------------------------------------------------------------------------
# lookup_values: real values of one managed column that match a partial name
# ---------------------------------------------------------------------------


def lookup_values(env, raw, cids, *, scope_related=False):
    """The distinct values of one managed column that contain the term (case-insensitive), prefix matches first,
    at most LOOKUP_CAP. Groups, record rules, guard models and field groups apply, as for /onto/v1/query."""
    if not isinstance(raw, dict) or not set(raw) <= LOOKUP_KEYS:
        raise _refuse("lookup takes object, column, term and optionally release")
    key, column, term = raw.get("object"), raw.get("column"), raw.get("term")
    release = raw.get("release", CURRENT)
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise _refuse("object must be an Onto object key")
    if not isinstance(column, str) or not 1 <= len(column) <= 100:
        raise _refuse("column must be the name of a managed column")
    if not isinstance(term, str) or not 1 <= len(term.strip()) <= LOOKUP_TERM:
        raise _refuse("term must be text of 1 to %d characters" % LOOKUP_TERM)
    if not isinstance(release, str) or not 1 <= len(release) <= 128:
        raise _refuse('release must be a release hash or "current"')
    term = term.strip()
    user = env.user
    cids = _companies(user, cids)
    _ai_gate(env, cids)
    env = env(context=dict(env.context, allowed_company_ids=cids))
    dataset = _published(env, {"object": key, "release": release})
    if not dataset.group_ids & user.groups_id or _suspended(env, key):
        raise QueryError(404, "not_found", NOT_FOUND)
    model = dataset.model_id.model
    try:
        env[model].check_access_rights("read")
    except AccessError:
        raise QueryError(403, "forbidden", FORBIDDEN)
    if column.startswith("_") or column not in {name for name, _ in dataset._view_column_types()}:
        raise QueryError(404, "not_found", NOT_FOUND)
    if not scope_related:
        fragments = _visibility(env, model, dataset, key)
    reply = _querygen(env, "onto/lookup/compile", {"release": release, "object": key, "column": column})
    reply_release, bindings = reply.get("release"), reply.get("bindings", {})
    if not isinstance(reply_release, str) or (release != CURRENT and reply_release != release):
        raise _unexpected()
    if dataset.onto_release != reply_release:
        raise QueryError(404, "not_found", NOT_FOUND)
    if not isinstance(bindings, dict) or any(not isinstance(v, str) for v in bindings.values()):
        raise _unexpected()
    params = reply.get("params", {})
    if not isinstance(params, dict):
        raise _unexpected()
    inner = _one_select(reply.get("sql"))
    _params_ok(params)
    _check_fields(env, model, bindings)  # the field the column is bound to: field groups apply (403)
    if scope_related:
        fragments = _visibility(env, model, dataset, key, bindings)
    view = view_name(key)
    _check_relations(inner, {view}, _registry_tables(env))
    # A relation column stores record ids, so the term is matched on the related records' display names (their
    # record rules apply), and only the ids this object's visible rows use are kept.
    related = _bound_relation(env, model, bindings[column])
    names = {}
    if related:
        try:
            names = dict(env[related].name_search(term, limit=LOOKUP_NAMES))
        except AccessError:
            raise QueryError(403, "forbidden", FORBIDDEN)
    match = "= ANY(%(onto_ids)s)" if related else "ILIKE %(onto_pattern)s"
    guard = " AND ".join(["_onto_id IN (SELECT id FROM onto_visible)"] + list(fragments))
    text = (
        'WITH "%s" AS (SELECT * FROM "%s" WHERE %s)' % (view, view, guard)
        + "\nSELECT onto_d.value FROM (\nSELECT DISTINCT CAST(onto_v.value AS text) AS value FROM (\n"
        + inner
        + "\n) AS onto_v(value)\nWHERE onto_v.value IS NOT NULL"
        + " AND CAST(onto_v.value AS text) " + match
        + "\n) AS onto_d\nORDER BY (onto_d.value ILIKE %(onto_prefix)s) DESC, onto_d.value"
        + "\nLIMIT %(onto_limit)s"
    )
    pattern = _like(term)
    values_params = dict(params, onto_pattern="%" + pattern + "%", onto_prefix=pattern + "%",
                         onto_ids=[str(record_id) for record_id in names], onto_limit=LOOKUP_CAP + 1)
    fetched = _fetch(env, text, LOOKUP_CAP + 1, values_params)
    if fetched is None:
        raise QueryError(408, "timeout", TIMEOUT_TEXT)
    _, rows = fetched
    values = [[int(row[0]), names[int(row[0])]] if related else row[0] for row in rows]
    return {
        "object": key,
        "column": column,
        "values": values[:LOOKUP_CAP],
        "truncated": len(values) > LOOKUP_CAP,
        "release": reply_release,
        "read_at": _now_text(),
    }
