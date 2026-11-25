# -*- coding: utf-8 -*-
"""Governed queries (1.6, contract 3.2): one QuerySpec, run for the caller with the caller's own rules.

The order follows plan A3.4. The AI switch of the companies used (D29, 403), the published managed dataset
of spec.object (404 when unpublished, engine only or of another release), the caller's groups on it (404),
read access to the base model (403), then the caller's record rules as temp tables (onto_visible, and guard
tables when the object has them). Then querygen compiles the spec (onto/compile), which returns the SQL of
the compiled request. Odoo checks that SQL (the visibility predicate, no unfiltered fallback, one SELECT),
applies the guard fragments to the view it reads, checks the field groups of the properties the query reads
(bindings), and runs the SQL under a statement timeout and a row cap, in the same transaction as the temp
tables (R5: no commit in between).

Nothing here takes SQL from a caller. Every querygen HTTP call goes through querygen_call(); the query route
calls only onto/compile (D49).
"""

import ast
import json
import logging
import math
import operator
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

import psycopg2
import requests
import pytz
from odoo.exceptions import AccessError, UserError
from psycopg2.errors import QueryCanceled
from psycopg2 import sql as pg_sql

from odoo.addons.odoo_dashboards_saas.models.sql_guard import check_select, read_only
from odoo.addons.odoo_dashboards_saas.querygen import utils as query_utils

from .onto_dataset import KEY_RE, view_name
from .onto_rules import guard_fragments, guard_model_names, visible_ids_sql
from .onto_store import onto_now, user_signature

_logger = logging.getLogger(__name__)

MAX_LIMIT = 50000  # the row cap: querygen's own spec limit
MAX_FETCH = MAX_LIMIT + 1
DEFAULT_LIMIT = 5000
QUERY_TIMEOUT_MS = 20000  # statement_timeout of one governed SELECT
COMPILE_TIMEOUT = 15  # seconds, per querygen call
DEFAULT_TZ = "Asia/Riyadh"  # the saas default when a company has no timezone
SPEC_KEYS = frozenset(
    ["release", "object", "metric", "dims", "filters", "period", "grain", "compare", "top_n", "params",
     "limit", "page"]
)
GRAINS = ("day", "week", "month", "quarter", "year")
CURRENT = "current"  # a spec release that querygen resolves to this tenant's current release
MAX_PARAMS = 20
PARAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,40}")  # fullmatch: querygen's ParamName
OPS = ("=", "!=", "<", "<=", ">", ">=", "in", "not in")
VISIBLE = re.compile(r"\b(?:WHERE|AND)\s+_onto_id\s+IN\s*\(\s*SELECT\s+id\s+FROM\s+onto_visible\s*\)", re.IGNORECASE)
UNFILTERED = re.compile(r"^\s*SELECT\s+\*\s+FROM\s+\S+?\s*$", re.IGNORECASE)
NOT_FOUND = "No published query matches this object, release or caller."
FORBIDDEN = "You may not read this query."
AI_OFF = "AI features are off for this company."
UNAVAILABLE = "The query service is unavailable. Try again."
UNEXPECTED = "The query service answered in a shape Odoo does not accept."
ARITH = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul}


class QueryError(Exception):
    """A refused query. status, code and message are what the route answers; no business value appears."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class QuerygenError(Exception):
    """querygen answered with an error status (None when it did not answer at all)."""

    def __init__(self, status, message=None):
        super().__init__(status)
        self.status = status
        self.message = message  # querygen's own reason, when it gave one


def querygen_call(env, path, payload, timeout, on_delta=None):
    """The one place Onto calls querygen: the saas client with this database's key and identity headers.
    Returns the JSON body. Raises QuerygenError. Tests patch this function, so no test reaches the network.
    on_delta, when given, asks for a streamed answer and receives its text as querygen sends it."""
    credentials = env["dashboard.api.key.saas"]
    config = env["ir.config_parameter"].sudo()
    routed = path.startswith("native-bi/v1/") or path in {
        "onto/compile", "onto/explore/compile", "onto/lookup/compile",
    }
    native_server = config.get_param("odoo_dashboards_bi.querygen_url") if routed else None
    if native_server:
        server = native_server.strip().rstrip("/")
        api_key = config.get_param("odoo_dashboards_bi.querygen_key")
        if not api_key:
            raise QuerygenError(401)
        query_utils.set_install_identity(credentials._install_identity())
    else:
        server, api_key = credentials._querygen_credentials()
    try:
        if on_delta is not None:
            return _streamed(path, payload, server, api_key, timeout, on_delta)
        return query_utils._post(path, payload, server, api_key, "%s", timeout=timeout)
    except requests.exceptions.RequestException:
        raise QuerygenError(None)
    except UserError as exc:
        raise QuerygenError(getattr(exc, "status_code", None), str(exc))
    except ValueError:  # a body that is not JSON
        raise QuerygenError(None)
    finally:
        if native_server:
            query_utils.set_install_identity({})


def _streamed(path, payload, server, api_key, timeout, on_delta):
    """A streamed querygen call: NDJSON {"delta": text} lines, then {"result": body} or {"error": status}."""
    response = query_utils._request(
        "POST", path, server, api_key, {"Content-Type": "application/json"},
        data=json.dumps({**payload, "stream": True}), timeout=timeout, stream=True,
    )
    with response:
        if response.status_code >= 400:
            raise QuerygenError(response.status_code)
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            item = json.loads(line)
            if "delta" in item:
                on_delta(item["delta"])
            elif item.get("reset"):
                on_delta(None)  # the answer is being rewritten
            elif "result" in item:
                return item["result"]
            else:
                raise QuerygenError(item.get("error"), item.get("detail"))
    raise QuerygenError(None)


def _refuse(message):
    return QueryError(422, "invalid_spec", message)


def _unexpected():
    return QueryError(502, "querygen_unexpected", UNEXPECTED)


def _text(value, name, low, high):
    if not isinstance(value, str) or not low <= len(value) <= high:
        raise _refuse("%s must be text of %d to %d characters" % (name, low, high))
    return value


def _int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise _refuse("%s must be a whole number from %d to %d" % (name, low, high))
    return value


def _choice(value, name, options):
    if value is not None and value not in options:
        raise _refuse("%s must be one of %s" % (name, ", ".join(options)))
    return value


def _dates(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"from", "to"}:
        raise _refuse("period must be an object with from and to")
    try:
        start, end = date.fromisoformat(raw["from"]), date.fromisoformat(raw["to"])
    except (TypeError, ValueError):
        raise _refuse("period dates must be ISO dates (YYYY-MM-DD)")
    if start > end:
        raise _refuse("period.from is after period.to")
    return {"from": start.isoformat(), "to": end.isoformat()}


def _filters(raw):
    if not isinstance(raw, list) or len(raw) > 20:
        raise _refuse("filters must be a list of at most 20 conditions")
    out = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"prop", "op", "value"}:
            raise _refuse("each filter needs prop, op and value and nothing else")
        out.append({"prop": _text(item["prop"], "filter prop", 1, 100),
                    "op": _choice(item["op"], "filter op", OPS), "value": item["value"]})
    return out


def _top_n(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or not set(raw) <= {"n", "by", "dir"} or "n" not in raw or "by" not in raw:
        raise _refuse("top_n needs n and by, and may have dir")
    return {"n": _int(raw["n"], "top_n.n", 1, 1000), "by": _text(raw["by"], "top_n.by", 1, 100),
            "dir": _choice(raw.get("dir", "desc"), "top_n.dir", ("asc", "desc"))}


def _compare(raw):
    if raw is None:
        return None
    if raw != {"to": "previous_period"}:
        raise _refuse('compare must be {"to": "previous_period"}')
    return {"to": "previous_period"}


def _params(raw):
    """The spec's params (contract 3.2): an object of at most 20 scalars. querygen checks the names it uses and their values."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or len(raw) > MAX_PARAMS:
        raise _refuse("params must be an object of at most %d values" % MAX_PARAMS)
    for name, value in raw.items():
        if not isinstance(name, str) or not PARAM_NAME.fullmatch(name):
            raise _refuse("a param name is lower case letters, digits and _, starting with a letter")
        if type(value) not in (int, float, str, bool) or (type(value) is float and not math.isfinite(value)):
            raise _refuse("param %s must be a whole number, a number, text, or true or false" % name)
    return dict(raw)


def parse_spec(raw):
    """The QuerySpec of contract 3.2, checked for shape and limits. Names and values are checked by querygen."""
    if not isinstance(raw, dict):
        raise _refuse("the query must be a JSON object")
    unknown = sorted(set(raw) - SPEC_KEYS)
    if unknown:
        raise _refuse("unknown field(s): %s" % ", ".join(unknown))
    for name in ("release", "object", "metric"):
        if name not in raw:
            raise _refuse("%s is required" % name)
    if not isinstance(raw["object"], str) or not KEY_RE.fullmatch(raw["object"]):
        raise _refuse("object must be an Onto object key")
    dims = raw.get("dims", [])
    if not isinstance(dims, list) or len(dims) > 10 or len(set(dims)) != len(dims):
        raise _refuse("dims must be a list of up to 10 distinct names")
    spec = {
        "release": _text(raw["release"], "release", 1, 128),
        "object": raw["object"],
        "metric": _text(raw["metric"], "metric", 1, 100),
        "dims": [_text(d, "dim", 1, 100) for d in dims],
        "filters": _filters(raw.get("filters", [])),
        "period": _dates(raw.get("period")),
        "grain": _choice(raw.get("grain"), "grain", GRAINS),
        "compare": _compare(raw.get("compare")),
        "top_n": _top_n(raw.get("top_n")),
        "limit": _int(raw.get("limit", DEFAULT_LIMIT), "limit", 1, MAX_LIMIT),
        "page": _int(raw.get("page", 1), "page", 1, MAX_LIMIT),
        "params": _params(raw.get("params")),
    }
    if spec["compare"] and not spec["period"]:
        raise _refuse("compare to previous_period needs a period")
    if spec["page"] * spec["limit"] > MAX_LIMIT:
        raise _refuse("page is past the row cap")
    return spec


def _companies(user, requested):
    """The companies a query may use: the token's (already checked), or the caller's own choice, or the main one."""
    if requested is None:
        return [user.company_id.id]
    if (not isinstance(requested, list) or not requested or not all(type(c) is int for c in requested)
            or not set(requested) <= set(user.company_ids.ids)):
        raise QueryError(403, "forbidden", FORBIDDEN)
    return sorted(set(requested))


def _ai_gate(env, cids):
    """D29: AI features must be switched on for every company the query uses. Off, or no setting, is 403."""
    settings = env["onto.agent.setting"].sudo().search([("company_id", "in", cids)])
    if len(settings) != len(cids) or not all(settings.mapped("ai_enabled")):
        raise QueryError(403, "ai_features_off", AI_OFF)


def _published(env, spec):
    """The active managed dataset of this object in this release. Sudo for metadata only: the caller's groups are checked next."""
    dataset = env["dataset.saas"].sudo().with_context(active_test=False).search(
        [("onto_managed", "=", True), ("onto_object_key", "=", spec["object"])], limit=1
    )
    # "current" has no release to compare here: _compile checks the release querygen resolved.
    if (not dataset or dataset.state != "active" or dataset.onto_engine_only
            or (spec["release"] != CURRENT and dataset.onto_release != spec["release"])):
        raise QueryError(404, "not_found", NOT_FOUND)
    return dataset


def _suspended(user_env, key):
    """True when the parity check (1.8) suspended this object for the caller's user signature. Sudo: a
    refusal must not depend on the caller's rules, and suspensions are keyed by signature, not company."""
    return user_env["onto.parity.suspension"].sudo().search_count(
        [("object_key", "=", key), ("signature", "=", user_signature(user_env.user))]
    ) > 0


def _tenant_today(env, now):
    """Today in the tenant timezone: the company partner's, else the saas default."""
    name = env.company.sudo().partner_id.tz or DEFAULT_TZ
    try:
        zone = pytz.timezone(name)
    except pytz.UnknownTimeZoneError:
        zone = pytz.timezone(DEFAULT_TZ)
    return pytz.utc.localize(now).astimezone(zone).date()


def _related_visibility(env, model, column_path, alias):
    """Require each record behind a relational property to pass its own Odoo read rules."""
    field = env[model]._fields[column_path[0]]
    if field.type != "many2one" or not field.store:
        raise _refuse("the relation has no stored visibility binding")
    target = field.comodel_name
    visible, values = visible_ids_sql(env, target)
    reference = pg_sql.SQL("{}.{}").format(pg_sql.Identifier(alias), pg_sql.Identifier(field.name))
    if len(column_path) == 1:
        return pg_sql.SQL("({} IS NULL OR {} IN {})").format(reference, reference, pg_sql.SQL(visible)), values
    child_alias = alias + "_related"
    child, child_params = _related_visibility(env, target, column_path[1:], child_alias)
    filtered = pg_sql.SQL(
        "SELECT {alias}.id FROM {table} {alias} WHERE {alias}.id IN {visible} AND {child}"
    ).format(alias=pg_sql.Identifier(child_alias), table=pg_sql.Identifier(env[target]._table),
             visible=pg_sql.SQL(visible), child=child)
    return pg_sql.SQL("({} IS NULL OR {} IN ({}))").format(reference, reference, filtered), [*values, *child_params]


def _native_visible(env, model, related_bindings, visible_sql, params):
    scoped = pg_sql.SQL("SELECT id FROM {} native_base(id)").format(pg_sql.SQL(visible_sql))
    if related_bindings:
        paths = set()
        for binding in related_bindings.values():
            parts = binding.split(".")
            current, relation = model, []
            for name in parts:
                field = env[current]._fields[name]
                if field.type != "many2one":
                    break
                relation.append(name)
                current = field.comodel_name
            if relation:
                paths.add(tuple(relation))
        checks = []
        for path in sorted(paths):
            if any(len(other) > len(path) and other[:len(path)] == path for other in paths):
                continue  # the longer guard already includes this relation
            try:
                check, values = _related_visibility(env, model, path, "bi_source")
            except AccessError as exc:
                raise QueryError(403, "forbidden", FORBIDDEN) from exc
            checks.append(check)
            params.extend(values)
        if checks:
            scoped = pg_sql.SQL(
                "SELECT bi_source.id FROM {table} bi_source JOIN ({scope}) native_scope "
                "ON native_scope.id = bi_source.id WHERE {checks}"
            ).format(table=pg_sql.Identifier(env[model]._table), scope=scoped,
                     checks=pg_sql.SQL(" AND ").join(checks))
    return scoped.as_string(env.cr._obj)


def _visibility(env, model, dataset, key, related_bindings=None):
    """Creates onto_visible (the caller's visible ids of the base model) and any guard tables, in this
    transaction. Returns the guard fragments to AND into the query (empty when the object has no guards)."""
    visible_sql, params = visible_ids_sql(env, model)
    select = (
        _native_visible(env, model, related_bindings, visible_sql, params)
        if related_bindings else "SELECT id FROM %s t(id)" % visible_sql
    )
    env.cr.execute(
        "DROP TABLE IF EXISTS pg_temp.onto_visible; "
        "CREATE TEMP TABLE onto_visible ON COMMIT DROP AS " + select,
        params,
    )
    setting = env["onto.object.setting"].sudo().search(
        [("name", "=", key), ("company_id", "in", env.companies.ids)], limit=1
    )
    if not setting:
        return []
    try:
        names = guard_model_names(setting)
    except ValueError as exc:
        raise _refuse(str(exc))
    if not names:
        return []
    if setting.base_model != model:
        raise _refuse("the guard models of %s do not match its base model" % key)
    columns = {name for name, _ in dataset._view_column_types()}
    try:
        items = guard_fragments(env, setting, columns)
    except ValueError as exc:
        raise _refuse(str(exc))
    fragments = []
    for item in items:
        env.cr.execute(item["create"][0], item["create"][1])
        fragments.extend(item["fragments"])
    return fragments


def _check_fields(env, model, bindings):
    """The caller must read every field the query reads, along each dotted path (403)."""
    for prop, path in bindings.items():
        current = env[model]
        parts = path.split(".")
        for position, name in enumerate(parts):
            field = current._fields.get(name)
            if field is None:
                raise _unexpected()
            try:
                current.check_field_access_rights("read", [name])
            except AccessError:
                raise QueryError(403, "forbidden", "The query reads a field you may not read: %s." % prop)
            if position < len(parts) - 1:
                if field.type != "many2one":
                    raise _unexpected()
                current = env[field.comodel_name]


def _list_of(value, keys):
    return isinstance(value, list) and all(isinstance(item, dict) and all(k in item for k in keys) for item in value)


def _previous_period(period):
    """The previous period: as long as period, ending the day before it (the rule querygen's compile applies)."""
    start, end = date.fromisoformat(period["from"]), date.fromisoformat(period["to"])
    prev_end = start - timedelta(days=1)
    return {"from": (prev_end - timedelta(days=(end - start).days)).isoformat(), "to": prev_end.isoformat()}


def _merge_ok(merge, spec):
    """querygen's merge block (its _merge) for this spec: the previous period, the match keys and the measure names."""
    measures = merge.get("measures") if isinstance(merge, dict) else None
    if not isinstance(measures, list) or len(measures) != 1 or not isinstance(measures[0], dict):
        return False
    measure = measures[0]
    return (merge.get("kind") == "previous_period" and merge.get("previous_period") == _previous_period(spec["period"])
            and merge.get("match_dims") == spec["dims"] and merge.get("bucket_grain") == spec["grain"]
            and merge.get("match_bucket") == ("offset" if spec["grain"] else None)
            and merge.get("absent") in ("zero", "null") and measure.get("key") == spec["metric"]
            and all(isinstance(measure.get(k), str) for k in ("previous", "change", "change_pct")))


def _compile(env, spec, dataset, today):
    """querygen's compile of the spec, checked against this object, its view and the caller (403, then 422).
    The reply's release is the one querygen resolved (a "current" spec too). With compare, the previous period's
    SQL and merge are checked as well."""
    body = {"release": spec["release"], "spec": {k: v for k, v in spec.items() if v is not None}}
    try:
        reply = querygen_call(env, "onto/compile", body, COMPILE_TIMEOUT)
    except QuerygenError as exc:
        if exc.status == 404:
            raise QueryError(404, "not_found", NOT_FOUND)
        if exc.status == 422:
            raise _refuse(exc.message or "the query is not valid for this object")
        if exc.status == 403:
            raise QueryError(403, "forbidden", FORBIDDEN)
        raise QueryError(502, "querygen_unavailable", UNAVAILABLE)
    if not isinstance(reply, dict):
        raise _unexpected()
    request, release = reply.get("request"), reply.get("release")
    if (not isinstance(release, str) or (spec["release"] != CURRENT and release != spec["release"])
            or not isinstance(request, dict)
            or request.get("sql_view_name") != view_name(spec["object"])
            or reply.get("path") not in ("tile", "matrix") or not isinstance(reply.get("snapshot"), bool)
            or (reply["path"] == "matrix" and not isinstance(request.get("matrix_col_dim"), dict))
            or not isinstance(reply.get("sql"), str) or not isinstance(reply.get("params"), dict)
            or not isinstance(reply.get("bindings"), dict) or not _list_of(reply.get("columns"), ["key"])
            or not reply["columns"] or not _list_of(reply.get("derive"), ["alias", "formula"])):
        raise _unexpected()
    if dataset.onto_release != release:  # the release querygen resolved is not one this object is published in
        raise QueryError(404, "not_found", NOT_FOUND)
    compare = spec["compare"] is not None
    previous, merge = reply.get("previous_request"), reply.get("merge")
    if compare:
        if not isinstance(previous, dict) or not isinstance(reply.get("previous_sql"), str) or not _merge_ok(merge, spec):
            raise _unexpected()
    elif previous is not None or reply.get("previous_sql") is not None or merge is not None:
        raise _unexpected()
    for part in ([request, previous] if compare else [request]):
        pre = part.get("pre_defined_filter_conditions")
        if not isinstance(pre, str) or not VISIBLE.search(pre):
            raise _unexpected()
    if any(not isinstance(name, str) or name.startswith("onto_") for name in reply["params"]):
        raise _unexpected()  # onto_ names belong to Odoo's own wrapper parameters
    _check_fields(env, dataset.model_id.model, reply["bindings"])
    # A snapshot (stock on hand) has no history before today: a past period would silently be today's stock.
    if reply["snapshot"] and spec["period"]:
        ends = [spec["period"]["to"]] + ([merge["previous_period"]["to"]] if compare else [])
        if min(date.fromisoformat(end) for end in ends) < today:
            raise _refuse("insufficient data: this snapshot has no history before today")
    return reply


def _guarded_sql(sql, view, fragments):
    """The compiled SQL as one read-only SELECT with the visibility predicate and no unfiltered fallback (D46,
    R2), without the trailing semicolon. The guard fragments then filter the view it reads: a CTE of the same
    name reads the real view under them, so they apply to base rows before any grouping."""
    if not isinstance(sql, str) or not sql.strip():
        raise _unexpected()
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if UNFILTERED.match(text) or not VISIBLE.search(text):
        _logger.warning("Onto query refused querygen SQL without the visibility predicate")
        raise _unexpected()
    if fragments:
        text = "WITH %s AS (SELECT * FROM %s WHERE %s)\n%s" % (view, view, " AND ".join(fragments), text)
    try:
        check_select(text)
    except UserError:
        raise _unexpected()
    return text


def _fetch(env, sql, fetch, params):
    """(column names, rows) of up to `fetch` rows, or None on timeout. One read-only savepoint with a
    statement timeout (the saas _bounded_query pattern, which does not return column names). The SQL always runs
    with a parameter dict, even an empty one: querygen's %(name)s reads the reply's params, and %% is a percent sign."""
    cr = env.cr
    try:
        with read_only(cr):
            cr.execute("SET LOCAL statement_timeout = %s", (int(QUERY_TIMEOUT_MS),))
            cr.execute("SELECT * FROM (\n" + sql + "\n) AS onto_q LIMIT %(onto_fetch)s", dict(params, onto_fetch=fetch))
            names = [column[0] for column in cr.description]
            rows = cr.fetchall()
    except QueryCanceled:
        return None
    except KeyError:  # psycopg2 raises KeyError for a %(name)s that the params dict lacks
        raise _unexpected()
    except psycopg2.Error:
        _logger.warning("Onto query failed in the database")
        raise QueryError(502, "query_failed", "The query could not run. Try again.")
    return names, rows


def _plain(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _evaluate(formula, values):
    """A derive instruction (arithmetic over aliases, from querygen). None propagates, and a zero divisor is NULL."""
    try:
        node = ast.parse(formula, mode="eval").body
    except SyntaxError:
        raise _unexpected()
    return _walk(node, values)


def _walk(node, values):
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _walk(node.operand, values)
        if inner is None:
            return None
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left, right = _walk(node.left, values), _walk(node.right, values)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Div):
            return None if right == 0 else left / right
        return ARITH[type(node.op)](left, right)
    raise _unexpected()


def _records(compiled, names, rows):
    """One dict per row: the SQL columns by name (the tile path has one unnamed value), then the derived values.
    A grained matrix's bucket column (__matrix_col) is keyed by its time property, as the compiled columns name it.
    A plain matrix's bucket is a constant that no column uses."""
    columns, records = compiled["columns"], []
    for row in rows:
        if compiled["path"] == "tile":
            values = {columns[0]["key"]: _plain(row[0])}
        else:
            values = {name: _plain(value) for name, value in zip(names, row)}
            dim = compiled["request"]["matrix_col_dim"]
            if dim["grain"] != "value":
                if "__matrix_col" not in values:
                    raise _unexpected()
                values[dim["field_name"]] = values.pop("__matrix_col")
        for node in compiled["derive"]:
            values[node["alias"]] = _evaluate(node["formula"], values)
        records.append(values)
    return records


def _bucket_day(value):
    """A bucket value (ISO text, as _plain gives it) as its day."""
    if not isinstance(value, str):
        raise _unexpected()
    return date.fromisoformat(value[:10])


def _floor(day, grain):
    """The first day of the grain unit that holds day. Weeks start on Monday."""
    if grain == "week":
        return day - timedelta(days=day.weekday())
    if grain == "month":
        return day.replace(day=1)
    if grain == "quarter":
        return date(day.year, (day.month - 1) // 3 * 3 + 1, 1)
    if grain == "year":
        return date(day.year, 1, 1)
    return day


def _unit(day, grain):
    """How many whole grain units lie between the epoch and the unit that holds day."""
    if grain == "week":
        return _floor(day, grain).toordinal() // 7  # every Monday has the same residue mod 7
    if grain == "month":
        return day.year * 12 + day.month - 1
    if grain == "quarter":
        return day.year * 4 + (day.month - 1) // 3
    if grain == "year":
        return day.year
    return day.toordinal()


def _step(start, grain, steps):
    """The first day of the unit `steps` grain units after the unit that starts on start."""
    if grain in ("day", "week"):
        return start + timedelta(days=steps * (7 if grain == "week" else 1))
    total = start.year * 12 + start.month - 1 + steps * {"month": 1, "quarter": 3, "year": 12}[grain]
    return date(total // 12, total % 12 + 1, 1)


def _join(base, cur, prev, measure, absent):
    """One output row: base carries the dims and the bucket; cur and prev are the matched records, None when missing."""
    key = measure["key"]
    now = absent if cur is None else cur[key]
    before = absent if prev is None else prev[key]
    change = None if now is None or before is None else now - before
    row = dict(base)
    row[key] = now
    row[measure["previous"]] = before
    row[measure["change"]] = change
    row[measure["change_pct"]] = None if change is None or before == 0 else change / before
    return row


def _merge_rows(compiled, spec, current, previous):
    """The current records joined to the previous ones, as querygen's _merge contract says. A pair shares every
    match_dims value and, for an offset merge, the same bucket offset from its own period's first bucket. Never by
    position. Current rows keep their order and come first; previous-only rows follow, with the current side absent
    and their bucket moved onto the current period. Paging and top_n run after this, on the current values."""
    merge = compiled["merge"]
    measure, dims, grain = merge["measures"][0], merge["match_dims"], merge["bucket_grain"]
    offset = merge["match_bucket"] == "offset"
    bucket = compiled["request"]["matrix_col_dim"]["field_name"]
    absent = None if merge["absent"] == "null" else 0
    cur_from = date.fromisoformat(spec["period"]["from"])
    prev_from = date.fromisoformat(merge["previous_period"]["from"])

    def pair(rec, first):
        """The match key: the dims' values, then the bucket offset from the period's first bucket."""
        key = tuple(rec[d] for d in dims)
        if offset:
            key += (_unit(_bucket_day(rec[bucket]), grain) - _unit(first, grain),)
        return key

    previous_of = {pair(rec, prev_from): rec for rec in previous}
    rows, matched = [], set()
    for rec in current:
        key = pair(rec, cur_from)
        matched.add(key)
        rows.append(_join(rec, rec, previous_of.get(key), measure, absent))
    for rec in previous:
        if pair(rec, prev_from) in matched:
            continue
        base = dict(rec)
        if offset:
            base[bucket] = _step(_floor(cur_from, grain), grain, pair(rec, prev_from)[-1]).isoformat()
        rows.append(_join(base, None, rec, measure, absent))
    return rows


def _rank(records, by, desc):
    """Records by one value, largest first when desc; NULLs last. top_n ranks groups, so it needs all of them."""
    if records and by not in records[0]:
        raise _unexpected()
    have = [r for r in records if r[by] is not None]
    lack = [r for r in records if r[by] is None]
    have.sort(key=lambda r: r[by], reverse=desc)
    return have + lack


def _out_columns(compiled):
    """The compiled columns, then three per measure when the query compares: previous, change and change_pct."""
    columns = list(compiled["columns"])
    merge = compiled.get("merge")
    for measure in (merge["measures"] if merge else []):
        base = next((c for c in compiled["columns"] if c["key"] == measure["key"]), {})
        label, unit = base.get("label") or measure["key"], base.get("unit")
        columns += [
            {"key": measure["previous"], "label": label + " (previous)", "role": "measure", "unit": unit},
            {"key": measure["change"], "label": label + " change", "role": "measure", "unit": unit},
            {"key": measure["change_pct"], "label": label + " change %", "role": "measure", "unit": "ratio"},
        ]
    return columns


def _page(compiled, spec, records):
    """(rows, truncated) for the requested page. Rows follow the output columns, in their order."""
    top = spec["top_n"]
    if top:
        records = _rank(records, top["by"], top["dir"] == "desc")[: top["n"]]
    end = spec["page"] * spec["limit"]
    keys = [column["key"] for column in _out_columns(compiled)]
    window = records[end - spec["limit"]:end]
    if any(key not in record for record in window for key in keys):
        raise _unexpected()
    return [[record[key] for key in keys] for record in window], len(records) > end


def run_query(env, raw, cids, *, scope_related=False):
    """A QuerySpec, run for the caller in env (their user). cids: the companies the caller selected, or None."""
    user = env.user
    cids = _companies(user, cids)
    _ai_gate(env, cids)
    spec = parse_spec(raw)
    env = env(context=dict(env.context, allowed_company_ids=cids))
    now = onto_now()
    dataset = _published(env, spec)
    if not dataset.group_ids & user.groups_id:
        raise QueryError(404, "not_found", NOT_FOUND)
    if _suspended(env, spec["object"]):
        raise QueryError(404, "not_found", NOT_FOUND)  # the parity check suspended it for this signature (1.8)
    model = dataset.model_id.model
    try:
        env[model].check_access_rights("read")
    except AccessError:
        raise QueryError(403, "forbidden", FORBIDDEN)
    if scope_related:
        compiled = _compile(env, spec, dataset, _tenant_today(env, now))
        fragments = _visibility(env, model, dataset, spec["object"], compiled["bindings"])
    else:
        fragments = _visibility(env, model, dataset, spec["object"])
        compiled = _compile(env, spec, dataset, _tenant_today(env, now))
    view, params = view_name(spec["object"]), compiled["params"]
    compare, top = spec["compare"] is not None, spec["top_n"]
    # A compare or a top_n needs every group before paging, so it fetches the row cap; a plain page fetches one page.
    fetch = MAX_FETCH if top or compare else spec["page"] * spec["limit"] + 1
    fetched = _fetch(env, _guarded_sql(compiled["sql"], view, fragments), fetch, params)
    # The previous period runs after the current one, in the same transaction and under the same guard.
    previous = []
    if compare and fetched:
        previous = _fetch(env, _guarded_sql(compiled["previous_sql"], view, fragments), MAX_FETCH, params)
    if fetched is None or previous is None:
        raise QueryError(408, "timeout", "The query took too long. Narrow the filters or the period.")
    names, rows = fetched
    if (top or compare) and len(rows) > MAX_LIMIT or (compare and len(previous[1]) > MAX_LIMIT):
        raise QueryError(422, "too_many_groups", "Too many groups to rank or compare. Add a filter.")
    _logger.info("Onto query object=%s rows=%d", spec["object"], len(rows))
    records = _records(compiled, names, rows)
    if compare:
        records = _merge_rows(compiled, spec, records, _records(compiled, previous[0], previous[1]))
    data, truncated = _page(compiled, spec, records)
    return {
        "columns": [{"key": c["key"], "label": c.get("label"), "role": c.get("role"), "unit": c.get("unit")}
                    for c in _out_columns(compiled)],
        "rows": data,
        "release": compiled["release"],
        "read_at": now.replace(microsecond=0).isoformat() + "Z",
        "truncated": truncated,
        "next_page": spec["page"] + 1 if truncated else None,
    }
