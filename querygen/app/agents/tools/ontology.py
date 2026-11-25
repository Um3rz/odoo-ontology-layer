"""The BI Agent's tools over the ontology and Odoo (plan 3.5; contracts 3.2, 3.5, 3.6).

Each handler takes the run's Ontology and the model's arguments, and returns an Outcome: a payload the model
reads, and for answers a record the answer card is built from. A refusal of any kind is returned as
{"error": code} payload, never raised. Odoo is called only with the user's query token (OdooClient), and its
{"error": code} replies come back as the codes listed in plan 3.5. A QuerySpec is checked against the release
(check_spec) before it leaves querygen, so a bad spec costs no round trip. Nothing here calls a model.

Odoo's rows are data. Each payload keeps MODEL_ROWS rows for the model; a record keeps CARD_ROWS for the card.
"""
import ast
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from app.odoo.client import OdooError
from app.ontology.schema import Metric, Object, Release, formula_names
from app.ontology.spec import QuerySpec, SpecError, check_spec

MODEL_ROWS = 40
CARD_ROWS = 200
LOOKUP_VALUES = 20
EXPLORE_ROWS = 500
TOP_PARTS = 5
MAX_DECOMPOSE_DIMS = 3
REASONS = ("forecast", "write", "not_recorded", "no_access", "not_available")
ODOO_FALLBACK = {403: "forbidden", 404: "not_found", 408: "timeout", 422: "invalid_spec"}


@dataclass
class Ontology:
    release: Release
    digest: str                # the release hash every call of this run uses (pinned at the start)
    trust: dict                # metric key -> "certified" | "approved"; a missing key is uncertified
    suspended: set             # "object.property" keys with an open drift row (plan 2.5)
    odoo: Any                  # OdooClient, or any object with an async request(method, path, json=...)


@dataclass
class Outcome:
    payload: dict              # what the model reads
    record: dict | None = None # card material: kind measure, exploration, decomposition or refusal


class ToolError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def _metric(ont: Ontology, key: Any) -> Metric:
    found = next((m for m in ont.release.metrics if m.key == key), None)
    if found is None:
        raise ToolError("not_found", f"no metric {key!r} in the published map")
    return found


def _object(ont: Ontology, key: str) -> Object:
    found = next((o for o in ont.release.objects if o.key == key), None)
    if found is None:
        raise ToolError("not_found", f"no object {key!r} in the published map")
    return found


def _trust(ont: Ontology, key: str) -> str:
    return ont.trust.get(key, "uncertified")


def _definition(metric: Metric) -> dict:
    return {"key": metric.key, "label": metric.label, "unit": metric.unit, "object": metric.object,
            "formula": metric.derived_from, "measure": metric.measure, "aggregation": metric.aggregation,
            "additivity": metric.additivity}


async def _post(ont: Ontology, path: str, body: dict) -> dict:
    try:
        return await ont.odoo.request("POST", path, json=body)
    except OdooError as exc:
        if exc.status == 0:
            raise ToolError("odoo_unavailable") from exc
        raise ToolError(exc.code or ODOO_FALLBACK.get(exc.status, "odoo_error")) from exc


def _check_release(ont: Ontology, reply: dict) -> None:
    if reply.get("release") != ont.digest:
        raise ToolError("release_mismatch", "Odoo answered from another map release")


def _columns(reply: dict) -> list[dict]:
    return [{"key": c["key"], "label": c.get("label", c["key"]), "role": c.get("role"), "unit": c.get("unit")}
            for c in reply.get("columns") or []]


def _spec(ont: Ontology, metric: Metric, args: dict, *, compare: bool) -> QuerySpec:
    body = {"release": ont.digest, "object": metric.object, "metric": metric.key,
            "dims": list(args.get("dims") or []), "filters": list(args.get("filters") or []),
            "period": args.get("period"), "grain": args.get("grain"),
            "compare": {"to": "previous_period"} if compare else None,
            # the spec ranks groups by the queried metric, descending: the model's by and dir are not used
            "top_n": None if args.get("top_n") is None else {"n": int(args["top_n"]["n"]), "by": metric.key, "dir": "asc" if args["top_n"].get("dir") == "asc" else "desc"},
            "limit": min(int(args.get("limit") or 500), 5000),
            "params": args.get("params") or {}}
    body = {k: v for k, v in body.items() if v is not None}
    try:
        spec = QuerySpec.model_validate(body)
    except ValidationError as exc:
        error = exc.errors()[0]
        raise ToolError("invalid_spec", f"{'.'.join(map(str, error['loc']))}: {error['msg']}") from exc
    try:
        check_spec(ont.release, spec)
    except SpecError as exc:
        raise ToolError("invalid_spec", str(exc)) from exc
    return spec


async def _query(ont: Ontology, metric: Metric, args: dict, *, compare: bool) -> dict:
    spec = _spec(ont, metric, args, compare=compare)
    reply = await _post(ont, "/onto/v1/query", spec.model_dump(mode="json", by_alias=True, exclude_none=True))
    _check_release(ont, reply)
    return reply


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


async def search_ontology(ont: Ontology, args: dict) -> Outcome:
    want = _tokens(str(args.get("query", "")))
    scored = []
    nodes = [("object", o.key, o.label, [o.key, o.label, *o.synonyms], None, None) for o in ont.release.objects]
    nodes += [("metric", m.key, m.label, [m.key, m.label, *m.synonyms], m.unit, _trust(ont, m.key))
              for m in ont.release.metrics]
    for kind, key, label, words, unit, trust in nodes:
        score = len(want & set().union(*(_tokens(w) for w in words)))
        if score:
            scored.append((-score, key, {"kind": kind, "key": key, "label": label, "unit": unit, "trust": trust}))
    matches = [item for _, _, item in sorted(scored, key=lambda s: (s[0], s[1]))[:10]]
    return Outcome({"matches": matches, "note": "" if matches else "nothing matches; try other words"})


async def describe(ont: Ontology, args: dict) -> Outcome:
    key = str(args.get("key", ""))
    metric = next((m for m in ont.release.metrics if m.key == key), None)
    if metric is not None:
        obj = _object(ont, metric.object)
        dims = [p.key for p in obj.properties if p.role in ("dimension", "status") and not p.pii]
        return Outcome({"kind": "metric", "key": metric.key, "label": metric.label, "object": obj.key,
                        "unit": metric.unit, "additivity": metric.additivity, "aggregation": metric.aggregation,
                        "measure": metric.measure, "formula": metric.derived_from,
                        "time_property": metric.time_property, "dimensions": dims, "trust": _trust(ont, key)})
    obj = _object(ont, key)
    props = [{"key": p.key, "label": p.label, "role": p.role, "unit": p.unit} for p in obj.properties if not p.pii]
    return Outcome({"kind": "object", "key": obj.key, "label": obj.label, "base_model": obj.base_model,
                    "properties": props})


async def measure(ont: Ontology, args: dict) -> Outcome:
    metric = _metric(ont, args.get("metric"))
    return measure_reply(ont, metric, args, await _query(ont, metric, args, compare=False), compare=False)


async def compare(ont: Ontology, args: dict) -> Outcome:
    metric = _metric(ont, args.get("metric"))
    return measure_reply(ont, metric, args, await _query(ont, metric, args, compare=True), compare=True)


def measure_reply(ont: Ontology, metric: Metric, args: dict, reply: dict, *, compare: bool) -> Outcome:
    """Shape one query reply as a measure payload and record (shared by measure and compare)."""
    rows = reply.get("rows") or []
    record = {"kind": "measure", "metric": metric.key, "label": metric.label, "unit": metric.unit,
              "columns": _columns(reply), "rows": rows[:CARD_ROWS], "row_count": len(rows),
              "dims": list(args.get("dims") or []), "period": args.get("period"), "compare": compare,
              "grain": args.get("grain"), "definition": _definition(metric),
              "trust": _trust(ont, metric.key), "release": ont.digest, "read_at": reply.get("read_at"),
              "truncated": bool(reply.get("truncated"))}
    payload = {"metric": metric.key, "label": metric.label, "unit": metric.unit, "trust": record["trust"],
               "columns": [c["key"] for c in record["columns"]], "row_count": len(rows),
               "truncated": record["truncated"], "rows": rows[:MODEL_ROWS]}
    return Outcome(payload, record)


def quantity_property(formula: str | None, units: dict[str, str | None]) -> str:
    """Identify the single quantity input of a quantity times price formula."""
    if not formula:
        raise ToolError("not_decomposable", "only a derived formula splits into volume and price")
    node = ast.parse(formula, mode="eval").body
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult)
            and isinstance(node.left, ast.Name) and isinstance(node.right, ast.Name)):
        raise ToolError("not_decomposable", "the formula is not a quantity times a price")
    quantities = [n for n in (node.left.id, node.right.id) if units.get(n) == "qty"]
    if len(quantities) != 1:
        raise ToolError("not_decomposable", "the formula has no quantity property")
    return quantities[0]


def _volume_metric(ont: Ontology, metric: Metric) -> tuple[Metric, str]:
    """Find the sum metric of the quantity input on the same filters."""
    quantity = quantity_property(metric.derived_from, {p.key: p.unit for p in _object(ont, metric.object).properties})
    volume = next((m for m in ont.release.metrics if m.object == metric.object and m.derived_from is None
                   and m.aggregation == "sum" and m.measure == quantity and m.filters == metric.filters), None)
    if volume is None:
        raise ToolError("not_decomposable", f"no sum of {quantity} on the same filters")
    return volume, quantity


def _sum(reply: dict, column: str) -> float:
    keys = [c["key"] for c in reply.get("columns") or []]
    index = keys.index(column)
    return sum(float(row[index] or 0) for row in reply.get("rows") or [])


async def decompose(ont: Ontology, args: dict) -> Outcome:
    metric = _metric(ont, args.get("metric"))
    volume, _ = _volume_metric(ont, metric)
    period = args.get("period")
    if not isinstance(period, dict):
        raise ToolError("invalid_spec", "period with from and to is required")
    obj = _object(ont, metric.object)
    dims = list(args.get("dims") or []) or [p.key for p in obj.properties
                                             if p.role == "dimension" and not p.pii][:MAX_DECOMPOSE_DIMS]
    base = {"period": period, "filters": list(args.get("filters") or [])}
    total_m = await _query(ont, metric, base, compare=True)
    total_v = await _query(ont, volume, base, compare=True)
    by_dim = {d: await _query(ont, metric, {**base, "dims": [d]}, compare=True) for d in dims}
    decomposition = decompose_comparisons(metric.key, metric.label, metric.unit, volume.key, period,
                                          total_m, total_v, by_dim)
    record = {"kind": "decomposition", "metric": metric.key, "label": metric.label, "unit": metric.unit,
              "trust": _trust(ont, metric.key), "definition": _definition(metric), "release": ont.digest,
              "read_at": total_m.get("read_at"), "decomposition": decomposition}
    return Outcome(decomposition, record)


def decompose_comparisons(key: str, label: str, unit: str, volume_key: str, period: dict,
                          total_m: dict, total_v: dict, by_dim: dict) -> dict:
    """Compute price, volume and grouped effects from Odoo comparisons."""
    revenue_now, revenue_before = _sum(total_m, key), _sum(total_m, f"{key}_previous")
    qty_now, qty_before = _sum(total_v, volume_key), _sum(total_v, f"{volume_key}_previous")
    if qty_now == 0 or qty_before == 0:
        raise ToolError("not_decomposable", "the quantity is zero in one of the periods")
    price_now, price_before = revenue_now / qty_now, revenue_before / qty_before
    volume_effect = (qty_now - qty_before) * price_before
    price_effect = qty_now * (price_now - price_before)
    change = revenue_now - revenue_before
    tol = 1e-6 * max(1.0, abs(revenue_now), abs(revenue_before))

    dimension_parts = []
    for dim, reply in by_dim.items():
        keys = [c["key"] for c in reply.get("columns") or []]
        now_i, before_i, dim_i = keys.index(key), keys.index(f"{key}_previous"), keys.index(dim)
        items = [("(none)" if row[dim_i] is None else str(row[dim_i]),
                  float(row[now_i] or 0) - float(row[before_i] or 0)) for row in reply.get("rows") or []]
        listed = sum(c for _, c in items)
        items.sort(key=lambda item: -abs(item[1]))
        parts = [{"name": n, "change": c} for n, c in items[:TOP_PARTS]]
        if len(items) > TOP_PARTS:
            parts.append({"name": "other", "change": sum(c for _, c in items[TOP_PARTS:])})
        dimension_parts.append({"dimension": dim, "parts": parts, "sums_to_total": abs(listed - change) <= tol,
                                "truncated": bool(reply.get("truncated"))})

    return {
        "metric": key, "label": label, "unit": unit, "period": period,
        "total": {"current": revenue_now, "previous": revenue_before, "change": change,
                  "change_pct": None if revenue_before == 0 else change / revenue_before},
        "volume": {"quantity_metric": volume_key, "quantity_current": qty_now, "quantity_previous": qty_before,
                   "effect": volume_effect},
        "price": {"unit_price_current": price_now, "unit_price_previous": price_before, "effect": price_effect},
        "checks": {"volume_plus_price_equals_total": abs(volume_effect + price_effect - change) <= tol},
        "dimensions": dimension_parts,
    }


async def health(ont: Ontology, args: dict) -> Outcome:
    metric = _metric(ont, args.get("metric"))
    names = {metric.measure, metric.time_property} - {None}
    if metric.derived_from:
        names |= formula_names(metric.derived_from)
    props = {p.key for p in _object(ont, metric.object).properties}
    suspended = sorted(n for n in names & props if f"{metric.object}.{n}" in ont.suspended)
    return Outcome({"metric": metric.key, "status": "suspended" if suspended else "ok", "suspended": suspended,
                    "trust": _trust(ont, metric.key), "release": ont.digest})


async def lookup_values(ont: Ontology, args: dict) -> Outcome:
    body = {"object": str(args.get("object", "")), "column": str(args.get("column", "")),
            "term": str(args.get("term", ""))[:80], "release": ont.digest}
    reply = await _post(ont, "/onto/v1/lookup_values", body)
    _check_release(ont, reply)
    values = reply.get("values") or []
    return Outcome({"object": body["object"], "column": body["column"], "values": values[:LOOKUP_VALUES],
                    "truncated": bool(reply.get("truncated")) or len(values) > LOOKUP_VALUES})


async def describe_model(ont: Ontology, args: dict) -> Outcome:
    model = str(args.get("model", ""))
    depth = args.get("depth") if args.get("depth") in (1, 2) else 1
    reply = await _post(ont, "/onto/v1/explore", {"describe": model, "depth": depth})
    return Outcome({"model": model, "depth": depth, "description": reply})


async def explore(ont: Ontology, args: dict) -> Outcome:
    body = {"model": str(args.get("model", "")), "fields": list(args.get("fields") or []),
            "filters": list(args.get("filters") or []), "group_by": list(args.get("group_by") or []),
            "aggregate": args.get("aggregate") or {"op": "count", "path": None},
            "limit": min(int(args.get("limit") or 100), EXPLORE_ROWS)}
    reply = await _post(ont, "/onto/v1/explore", body)
    rows = reply.get("rows") or []
    record = {"kind": "exploration", "model": body["model"], "spec": body, "columns": _columns(reply),
              "rows": rows[:CARD_ROWS], "row_count": len(rows), "trust": "uncertified", "release": None,
              "read_at": reply.get("read_at"), "truncated": bool(reply.get("truncated"))}
    payload = {"trust": "uncertified", "columns": [c["key"] for c in record["columns"]], "row_count": len(rows),
               "truncated": record["truncated"], "rows": rows[:MODEL_ROWS]}
    return Outcome(payload, record)


async def refuse(_ont: Ontology, args: dict) -> Outcome:
    reason = args.get("reason")
    if reason not in REASONS:
        raise ToolError("bad_reason", f"reason must be one of {', '.join(REASONS)}")
    message = str(args.get("message", ""))[:300]
    return Outcome({"refused": reason}, {"kind": "refusal", "reason": reason, "message": message})


HANDLERS: dict[str, Callable[[Ontology, dict], Awaitable[Outcome]]] = {
    "search_ontology": search_ontology, "describe": describe, "measure": measure, "compare": compare,
    "decompose": decompose, "drill": measure, "health": health, "lookup_values": lookup_values,
    "describe_model": describe_model, "explore": explore, "refuse": refuse,
}


async def call(ont: Ontology, name: str, args: dict) -> Outcome:
    """Run one tool. Refusals and Odoo errors become {"error": code} payloads; anything else is a bug and raises."""
    try:
        if name not in HANDLERS:
            raise ToolError("unknown_tool")
        return await HANDLERS[name](ont, args if isinstance(args, dict) else {})
    except ToolError as exc:
        return Outcome({"error": exc.code, "detail": exc.detail[:300]})
    except (TypeError, ValueError, KeyError, IndexError) as exc:  # the model's arguments do not fit the call
        return Outcome({"error": "invalid_arguments", "detail": type(exc).__name__})


_FILTER = {"type": "object", "properties": {
    "prop": {"type": "string", "description": "a property key of the metric's object"},
    "op": {"type": "string", "enum": ["=", "!=", "<", "<=", ">", ">=", "in", "not in"]},
    "value": {"description": "a value, or a list for in and not in"}}, "required": ["prop", "op", "value"]}
_PERIOD = {"type": "object", "properties": {"from": {"type": "string", "description": "YYYY-MM-DD"},
                                            "to": {"type": "string", "description": "YYYY-MM-DD"}},
           "required": ["from", "to"]}
_DIMS = {"type": "array", "items": {"type": "string"}, "description": "property keys to group by"}
_MEASURE = {"type": "object", "properties": {
    "metric": {"type": "string"}, "dims": _DIMS, "filters": {"type": "array", "items": _FILTER},
    "period": _PERIOD, "grain": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
    "top_n": {"type": "object", "description": "Keep the n groups ranked by this metric: highest first, or lowest "
                                               "with dir asc. Needs dims and no grain.",
              "properties": {"n": {"type": "integer"}, "dir": {"type": "string", "enum": ["asc", "desc"]}},
              "required": ["n"]},
    "limit": {"type": "integer"}}, "required": ["metric"]}

SCHEMAS = [
    {"name": "search_ontology", "description": "Find metrics, objects and business terms by words. Start here.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "describe", "description": "Definition of one metric (formula, dimensions, trust) or one object.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "measure", "description": "Run a published metric, optionally grouped by dims and filtered. A total is a "
                                       "measure with no dims. Returns rows; never add rows yourself.",
     "parameters": _MEASURE},
    {"name": "compare", "description": "A metric for a period and the previous period of the same length, with the "
                                       "change for each row.", "parameters": _MEASURE},
    {"name": "decompose", "description": "Why a sales metric changed: volume and price effects, then the split by "
                                         "dimension. Only for metrics whose formula is quantity times price.",
     "parameters": {"type": "object", "properties": {"metric": {"type": "string"}, "period": _PERIOD,
                                                     "dims": _DIMS, "filters": {"type": "array", "items": _FILTER}},
                    "required": ["metric", "period"]}},
    {"name": "drill", "description": "Break a result down further: a metric with more dims or narrower filters.",
     "parameters": _MEASURE},
    {"name": "health", "description": "Whether a metric's inputs are suspended by a schema change.",
     "parameters": {"type": "object", "properties": {"metric": {"type": "string"}}, "required": ["metric"]}},
    {"name": "lookup_values", "description": "Real stored values of one dimension that contain a partial text ('did you "
                                             "mean'). For a relation dimension (product, customer, warehouse) the text "
                                             "matches the record's name and each value is an [id, name] pair: filter "
                                             "the metric with the id.",
     "parameters": {"type": "object", "properties": {"object": {"type": "string"}, "column": {"type": "string"},
                                                     "term": {"type": "string"}},
                    "required": ["object", "column", "term"]}},
    {"name": "describe_model", "description": "Columns and relations of one Odoo model or table this user may read. "
                                             "Use before explore.",
     "parameters": {"type": "object", "properties": {"model": {"type": "string", "description": "e.g. stock.picking"},
                                                     "depth": {"type": "integer", "enum": [1, 2]}},
                    "required": ["model"]}},
    {"name": "explore", "description": "Uncertified read of Odoo fields when no published metric fits. The answer is "
                                       "labelled Uncertified.",
     "parameters": {"type": "object", "properties": {
         "model": {"type": "string"}, "fields": {"type": "array", "items": {"type": "string"}},
         "filters": {"type": "array", "items": {"type": "object", "properties": {
             "path": {"type": "string"}, "op": {"type": "string"}, "value": {}},
             "required": ["path", "op", "value"]}},
         "group_by": {"type": "array", "items": {"type": "string"}},
         "aggregate": {"type": "object", "properties": {"op": {"type": "string",
                                                              "enum": ["count", "sum", "avg", "min", "max"]},
                                                       "path": {"type": ["string", "null"]}}},
         "limit": {"type": "integer"}}, "required": ["model", "fields"]}},
    {"name": "refuse", "description": "Say plainly that this cannot be answered. Use for forecasts, requests to change "
                                     "data, data Odoo does not record, or a metric this user cannot read.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string", "enum": list(REASONS)},
                                                     "message": {"type": "string"}}, "required": ["reason"]}},
]
