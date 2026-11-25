import hashlib
import json
import re
from collections import Counter
from decimal import Decimal

from lxml import etree

from odoo import _, fields, release as odoo_release
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression
from odoo.tools import html2plaintext

from odoo.addons.odoo_dashboards_bi.models import onto_explore, onto_query
from odoo.addons.odoo_dashboards_saas.models.ai_governance import check_ai_credits


MAX_ROWS = 200  # querygen keeps 200 rows of each observation
MAX_FIELDS = 30
PAGE_ROWS = 20
PAGE_TEXT = 1000
DATA_TYPES = {"boolean", "char", "date", "datetime", "float", "integer", "many2one", "monetary", "selection"}
MODEL_ROOTS = {
    "account", "calendar", "crm", "fleet", "hr", "helpdesk", "maintenance", "mrp", "payment",
    "pos", "product", "project", "purchase", "quality", "repair", "res", "sale", "stock", "uom",
}
RES_MODELS = {"res.partner", "res.company", "res.country", "res.currency"}
OPERATORS = {"=", "!=", "in", "not in", ">", ">=", "<", "<=", "ilike", "like", "=?"}
GROUP_AGGREGATES = {"count", "count_distinct", "sum", "avg", "min", "max"}
GRAINS = {"day", "week", "month", "quarter", "year"}
CHARTS = {"vertical_bar_chart", "horizontal_bar_chart", "line", "area_chart", "doughnut", "funnel"}
AGGREGATE_UNITS = {
    ("stock_position", "qty"): "stock_uom",
    ("stock_movement_detail", "qty"): "movement_uom",
    ("sales_order_line", "money"): "currency",
    ("sales_order_line", "pct"): "currency",
    ("sales_order_line", "qty"): "ordered_uom",
    ("purchase_order_line", "money"): "purchase_currency",
    ("billing_document", "money"): "company_currency",
    ("posted_payment", "money"): "payment_currency",
}
SENSITIVE = re.compile(r"secret|password|token|(^|_)key($|_)|bank|ssn", re.IGNORECASE)
OBJECT_DESCRIPTIONS = "odoo_dashboards_bi.object_descriptions"
RECONCILIATION = "odoo_dashboards_bi.reconciliation"


def reconciled_metrics(env, release):
    config = env["ir.config_parameter"].sudo()
    checks = json.loads(config.get_param(RECONCILIATION) or "{}")
    if not release or checks.get("release") != release:
        return {}
    per_company = [checks.get("companies", {}).get(str(company.id), {}) for company in env.companies]
    if not per_company:
        return {}
    return {key: check for key, check in per_company[0].items() if check["passed"] and
            all(company.get(key, {}).get("passed") for company in per_company)}


def json_data(value):
    return json.loads(json.dumps(value, default=lambda item: float(item) if isinstance(item, Decimal) else str(item)))


def fingerprint(value):
    sources = value if isinstance(value, list) else [value]
    # notice is model-facing text, not data: leaving it out keeps proofs saved before it existed valid.
    canonical = [{key: val for key, val in source.items() if key not in ("read_at", "id", "notice")}
                 for source in sources]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()


def scope_key(env):
    # Module upgrades can rewrite identical rules. Compare their read semantics.
    rules = env["ir.rule"].sudo().with_context(active_test=True).search([("perm_read", "=", True)], order="id")
    rights = env["ir.model.access"].sudo().with_context(active_test=True).search([("perm_read", "=", True)], order="id")
    # v3: no user write_date. Groups already cover access, and write_date moved with every avatar or language change.
    values = [env.uid, env.companies.ids, sorted(env.user.groups_id.ids)]
    values.extend((rec.model_id.id, rec.domain_force, sorted(rec.groups.ids)) for rec in rules)
    values.extend((rec.model_id.id, rec.group_id.id) for rec in rights)
    return "v3:" + hashlib.sha256(json.dumps(values).encode()).hexdigest()


def legacy_scope_key(env):
    rules = env["ir.rule"].sudo().search([], order="id")
    rights = env["ir.model.access"].sudo().search([], order="id")
    values = [env.uid, env.companies.ids, sorted(env.user.groups_id.ids), str(env.user.write_date)]
    values.extend((rec.id, str(rec.write_date)) for rec in rules)
    values.extend((rec.id, str(rec.write_date)) for rec in rights)
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def legacy_page_fingerprint(source, tool):
    """Page proofs before record columns had numeric roles."""
    sources = source if isinstance(source, list) else [source]
    role = "dimension" if tool else None
    return fingerprint([{**entry, "columns": [{**column, "role": role} for column in entry["columns"]]}
                        for entry in sources])


def check_enabled(env, companies, *, check_credits=True):
    user = env.user
    if not user.active or not user.has_group("base.group_user") or set(companies) - set(user.company_ids.ids):
        raise AccessError(_("Your company permissions changed."))
    settings = env["onto.agent.setting"].sudo().search([("company_id", "in", companies)])
    if len(settings) != len(companies) or not all(settings.mapped("ai_enabled")):
        raise UserError(_("An Onto steward must enable BI for your selected companies."))
    if any(settings.mapped("agents_paused")):
        raise UserError(_("BI has been paused for one of your selected companies."))
    if check_credits:
        check_ai_credits(env, user)


EXTRA_MODELS_PARAM = "odoo_dashboards_bi.extra_models"  # models of non-standard modules the steward mapped


def _extra_models(env):
    return set(json.loads(env["ir.config_parameter"].sudo().get_param(EXTRA_MODELS_PARAM) or "[]"))


def model_for(env, name):
    if (
        not isinstance(name, str) or name not in env
        or not (name.startswith("x_") or name.split(".")[0] in MODEL_ROOTS or name in _extra_models(env))
        or (name.startswith("res.") and name not in RES_MODELS)
        or SENSITIVE.search(name)
    ):
        raise AccessError(_("This model is not available for BI."))
    model = env[name]
    if model._abstract or model._transient:
        raise AccessError(_("This model is not available for BI."))
    model.check_access_rights("read")
    return model


def field_map(model):
    info = model.fields_get(attributes=["string", "type", "relation", "store"])
    return {
        name: spec for name, spec in info.items()
        if spec.get("store") and spec.get("type") in DATA_TYPES and not SENSITIVE.search(name)
    }


def domain_for(model, raw):
    if not isinstance(raw, list) or len(raw) > 60:
        raise UserError(_("Invalid Odoo filters."))
    for term in raw:
        if isinstance(term, str) and term in ("&", "|", "!"):
            continue
        if not isinstance(term, (list, tuple)) or len(term) != 3:
            raise UserError(_("Invalid Odoo filter."))
        name, operator, value = term
        if not isinstance(name, str) or not isinstance(operator, str) or operator not in OPERATORS:
            raise UserError(_("That filter is not available in BI."))
        current = model
        path = name.split(".")
        for index, part in enumerate(path):
            meta = field_map(current).get(part)
            if meta is None:
                raise AccessError(_("The filter references a field outside your BI access."))
            if index < len(path) - 1:
                # Arbitrary dotted filters can infer hidden related records without applying comodel rules.
                raise UserError(_("Filter on the visible relation itself using its record ID."))
        if isinstance(value, list) and len(value) > 150:
            raise UserError(_("The filter selection is too large."))
        if isinstance(value, (list, dict)) and operator not in {"in", "not in"}:
            raise UserError(_("The filter value is invalid."))
    try:
        return expression.normalize_domain(raw)
    except (AssertionError, TypeError, ValueError) as exc:
        raise UserError(_("Invalid Odoo filters.")) from exc


def attachment(raw):
    if raw in (None, False, {}):
        return {}
    if not isinstance(raw, dict) or len(json.dumps(raw)) > 16000:
        raise UserError(_("The attached view is too large."))
    kind = raw.get("kind")
    if kind in ("record", "view"):
        ids = raw.get("record_ids") or []
        if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int or i <= 0 for i in ids):
            raise UserError(_("Select up to 100 saved records."))
        if kind == "record" and len(ids) != 1:
            raise UserError(_("Open a saved record to attach it."))
        view_type = raw.get("view_type") or ("form" if kind == "record" else None)
        if view_type not in ({"form"} if kind == "record" else {None, "list", "kanban", "pivot", "graph"}):
            raise UserError(_("This page cannot be attached to BI."))
        view_id = raw.get("view_id") or None
        if view_id is not None and (type(view_id) is not int or view_id <= 0):
            raise UserError(_("This page cannot be attached to BI."))
        page_fields = raw.get("page_fields")
        if page_fields is not None and (not isinstance(page_fields, list) or len(page_fields) > 100
                                       or any(not isinstance(name, str) for name in page_fields)):
            raise UserError(_("The page fields are invalid."))
        return {"kind": kind, "model": raw.get("model"), "record_ids": ids, "page_fields": page_fields,
                "view_type": view_type, "view_id": view_id,
                "domain": raw.get("domain") or [], "groupby": raw.get("groupby") or [],
                "measures": raw.get("measures") or []}
    if kind in ("widget", "insight"):
        keys = ("dashboard_id", "widget_id", "insight_id", "range", "filters", "widget_filters", "widget_range")
        return {"kind": kind, **{key: raw[key] for key in keys if key in raw}}
    raise UserError(_("This view cannot be attached to a BI question."))


def _widget(env, attached):
    dashboard_id, widget_id = attached.get("dashboard_id"), attached.get("widget_id")
    if type(dashboard_id) is not int or type(widget_id) is not int:
        raise UserError(_("Select a single widget to attach."))
    dashboard = env["dashboards.saas"].search([("id", "=", dashboard_id)], limit=1)
    if not dashboard or not dashboard._external_visible():
        raise AccessError(_("The dashboard is not visible to you."))
    widget = dashboard._visible_analytics(dashboard, [widget_id])
    if not widget:
        raise AccessError(_("The widget is not visible to you."))
    return dashboard, widget


def _insight(env, attached):
    insight_id = attached.get("insight_id")
    if type(insight_id) is not int:
        raise UserError(_("Select a single dashboard insight."))
    insight = env["ai.insight.saas"].search([("id", "=", insight_id)], limit=1)
    if not insight or not insight.analytic_id:
        raise AccessError(_("The dashboard insight is unavailable."))
    dashboard, widget = _widget(env, {"dashboard_id": insight.dashboard_id.id, "widget_id": insight.analytic_id.id})
    return insight, dashboard, widget


def validate_references(env, attached):
    kind = attached.get("kind")
    if kind == "widget":
        _widget(env, attached)
    elif kind == "insight":
        _insight(env, attached)
    elif kind in ("record", "view"):
        model = model_for(env, attached["model"])
        domain_for(model, attached.get("domain", []))
        ids = attached.get("record_ids") or []
        if ids and model.with_context(active_test=False).search_count([("id", "in", ids)]) != len(set(ids)):
            raise AccessError(_("Some attached records are unavailable."))


def _view_fields(env, model):
    return [
        {"key": name, "label": data["string"], "type": data["type"], "relation": data.get("relation")}
        for name, data in list(field_map(model).items())[:100]
    ]


def _metric_fields(env, model, metric, obj):
    selected, readable = {}, []
    for prop in obj.get("properties") or []:
        if prop.get("pii"):
            continue
        key = prop["key"]
        path = (prop.get("bindings") or {}).get(odoo_release.series.split(".")[0])
        if path:
            try:
                if any(SENSITIVE.search(segment) for segment in path.split(".")):
                    continue
                onto_query._check_fields(env, model, {key: path})
            except (AccessError, onto_query.QueryError):
                continue
        selected[key] = prop
        # A time property groups by its exact value; use grain for period buckets.
        if prop["role"] in ("dimension", "status", "time"):
            readable.append(key)
    needed = {metric.get("measure"), metric.get("time_property")} - {None, False, ""}
    if not needed <= set(selected):
        return None
    return {**metric, "properties": list(selected.values()), "dimensions": readable}


def _resolved_metrics(rows):
    """Require readable operands, including any same-object metrics used by a formula."""
    resolved = set()
    pending = list(rows)
    while pending:
        ready = []
        for row in pending:
            operands = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", row.get("derived_from") or ""))
            properties = {prop["key"] for prop in row["properties"]}
            if all(name == "days" or name in properties or (row["object"], name) in resolved
                   for name in operands):
                ready.append((row["object"], row["key"]))
        if not ready:
            break
        resolved.update(ready)
        pending = [row for row in pending if (row["object"], row["key"]) not in resolved]
    return [row for row in rows if (row["object"], row["key"]) in resolved]


def catalog(env, attached):
    preferred = [attached.get("model")] if attached.get("model") else []
    preferred += ["sale.order", "sale.order.line", "purchase.order", "stock.quant", "stock.picking",
                  "account.move", "account.move.line", "crm.lead", "pos.order", "product.product",
                  "res.partner", "project.task", "mrp.production"]
    models = []
    for name in dict.fromkeys(preferred):
        try:
            model = model_for(env, name)
        except AccessError:
            continue
        models.append({"model": name, "label": model._description, "fields": _view_fields(env, model)})
    config = env["ir.config_parameter"].sudo()
    release = config.get_param("odoo_dashboards_bi.release") or None
    snapshot = config.get_param("odoo_dashboards_bi.catalog") or "{}"
    published = json.loads(snapshot)
    descriptions = json.loads(config.get_param(OBJECT_DESCRIPTIONS) or "{}")
    reconciled = reconciled_metrics(env, release)
    metric_rows = []
    object_rows = {}
    if release and not config.get_param("odoo_dashboards_bi.pending_release"):
        objects = {
            item["key"]: item for item in published.get("objects", [])
            if isinstance(item, dict) and item.get("key")
        }
        readable_objects = {}
        for key, obj in objects.items():
            try:
                dataset = onto_query._published(env, {"release": release, "object": key})
                model_for(env, obj["base_model"])
                if not (dataset.group_ids & env.user.groups_id) or onto_query._suspended(env, key):
                    continue
            except (AccessError, onto_query.QueryError):
                continue
            readable_objects[key] = obj
            description = descriptions.get(key, {}).get("text")
            if description:
                object_rows[key] = {"key": key, "label": obj.get("label"), "description": description}
        for metric in published.get("metrics", []):
            obj = readable_objects.get(metric.get("object"))
            if not obj:
                continue
            visible = _metric_fields(env, obj["base_model"], metric, obj)
            if visible:
                if visible["key"] in reconciled:
                    visible["trust"] = "certified"
                metric_rows.append(visible)
    return {"release": release, "metrics": _resolved_metrics(metric_rows), "models": models,
            "objects": list(object_rows.values())}


def _column(key, label=None, role=None, unit=None):
    return {"key": key, "label": label or key, "role": role, "unit": unit}


def _record_column(name, spec):
    role = "id" if name == "id" else "measure" if spec["type"] in {"integer", "float", "monetary"} else "dimension"
    return _column(name, spec["string"], role)


def _evidence(env, source_kind, source_id, columns, rows, **provenance):
    # Result order is the display order. Preserve the truncation bit for the renderer.
    truncated = bool(provenance.pop("truncated", False)) or len(rows) > MAX_ROWS
    result = {
        "source_kind": source_kind, "source_id": source_id,
        "companies": env.companies.ids, "read_at": fields.Datetime.now(),
        "columns": columns, "rows": rows[:MAX_ROWS], "truncated": truncated,
        **provenance,
    }
    if truncated:
        result["notice"] = _("The rows are incomplete: the row limit was reached. Do not total, count or list distinct "
                             "values from them. For totals or distinct values, use read_group or explore with an "
                             "aggregate, or narrow the filters.")
    return json_data(result)


def _related_field(env, model, path):
    """The field at the end of a dotted group-by path such as product_id.product_tmpl_id.categ_id. Each hop must be a
    stored many2one the user may read, into a model BI may read; the last field must be a stored field there."""
    names = path.split(".")
    if len(names) > 3:
        raise UserError(_("A related group-by path names at most three fields."))
    current = model
    for name in names[:-1]:
        hop = field_map(current).get(name)
        if hop is None or hop.get("type") != "many2one":
            raise AccessError(_("An analytical field is outside your BI access."))
        current = model_for(env, hop["relation"])
    leaf = field_map(current).get(names[-1])
    if leaf is None:
        raise AccessError(_("An analytical field is outside your BI access."))
    return leaf


def _groups(env, model, grouping, measures):
    if len(grouping) > 3 or len(measures) > 12:
        raise UserError(_("Group by up to three fields and choose at most twelve measures."))
    readable, related = field_map(model), {}
    for value in grouping + measures:
        if not isinstance(value, str):
            raise UserError(_("An analytical field is invalid."))
        key, separator, modifier = value.partition(":")
        if key in ("__count",) and not separator:
            continue
        if "." in key and value in grouping:
            if modifier:
                raise UserError(_("A related field groups by its exact value, without a time grain."))
            related[key] = _related_field(env, model, key)
            continue
        desc = readable.get(key)
        if not desc:
            raise AccessError(_("An analytical field is outside your BI access."))
        if modifier and modifier not in (GRAINS if value in grouping else GROUP_AGGREGATES):
            raise UserError(_("That aggregate or time grain is unavailable."))
    return {**readable, **related}


def _fold(op, current, value):
    """Combine one group's aggregate with another group that reaches the same target. None and False are no value."""
    if value is None or value is False:
        return current
    if current is None:
        return value
    if op == "min":
        return min(current, value)
    if op == "max":
        return max(current, value)
    return current + value  # sum and count


def _grouped_through_paths(env, model, domain, groupby, query_fields, operators, descriptions):
    """read_group cannot group by a dotted path. Group by each path's first hop instead, read the target of each
    first-hop record with the user's own access, and fold the groups that reach the same target. Only aggregates
    that add up across groups can be folded: averages and distinct counts are refused."""
    if any(op not in ("sum", "count", "min", "max") for op in operators.values()):
        raise UserError(_("Averages and distinct counts cannot be grouped by a related field. Use sum, count, min or "
                          "max, or group by a field of this model."))
    first_hops = {item: item.split(".")[0] for item in groupby if "." in item}
    hops = list(dict.fromkeys(first_hops.get(item, item) for item in groupby))
    found = model.read_group(domain, query_fields, hops, lazy=False)
    targets = {}
    for item, first in first_hops.items():
        names = item.split(".")[1:]
        ids = sorted({row[first][0] for row in found if row.get(first)})
        values = {}
        for record in env[model._fields[first].comodel_name].browse(ids):
            node = record
            for name in names[:-1]:
                node = node[name]
            value = node[names[-1]]
            if descriptions[item]["type"] == "many2one":
                value = (value.id, value.display_name) if value else False
            values[record.id] = False if value is None else value
        targets[item] = values
    folded = {}
    for row in found:
        key = []
        for item in groupby:
            if item in first_hops:
                link = row.get(first_hops[item])
                key.append(targets[item].get(link[0], False) if link else False)
            else:
                key.append(row.get(item))
        slot = folded.setdefault(tuple(key), {"__count": 0})
        slot["__count"] += row.get("__count") or 0
        for alias, op in operators.items():
            slot[alias] = _fold(op, slot.get(alias, 0 if op in ("sum", "count") else None), row.get(alias))
    return [{**dict(zip(groupby, key)), **slot} for key, slot in folded.items()]


def _scoped_relation(env, desc, raw):
    if desc.get("type") != "many2one" or not isinstance(raw, (list, tuple)) or not raw:
        return raw
    try:
        target = model_for(env, desc.get("relation"))
        return raw if target.search_count([("id", "=", raw[0])]) else False
    except AccessError:
        return False


def _search_term(args):
    query = args.get("query") or ""
    if not isinstance(query, str) or len(query) > 100:
        raise UserError(_("Search with a short text."))
    return query.strip().lower()


def _list_models(env, args):
    """Every business model this user may read, so the ORM tools reach past the preloaded catalogue. A model matches
    when any word of the query is in its technical name or label; the models with the most words hit come first."""
    words = _search_term(args).split()
    scored = []
    for item in env["ir.model"].sudo().search([("transient", "=", False)], order="model"):
        text = "%s %s" % (item.model, (item.name or "").lower())
        hits = sum(1 for word in words if word in text)
        if words and not hits:
            continue
        try:
            model_for(env, item.model)
        except AccessError:
            continue
        scored.append((-hits, len(item.model), item.model, item.name))  # ties: the shorter technical name first
    rows = [[model, name] for _hits, _length, model, name in sorted(scored)]
    return _evidence(env, "exploration", "odoo:models",
                     [_column("model", _("Model"), "dimension"), _column("label", _("Label"), "dimension")],
                     rows, truncated=len(rows) > MAX_ROWS)


def _describe_model(env, args):
    """The readable stored fields of one model: what search_read, read_group and explore may name."""
    model = model_for(env, args.get("model"))
    query = _search_term(args)
    rows = []
    for name, spec in field_map(model).items():
        if query and query not in name and query not in (spec.get("string") or "").lower():
            continue
        rows.append([name, spec["string"], spec["type"], spec.get("relation")])
    columns = [_column("field", _("Field"), "dimension"), _column("label", _("Label"), "dimension"),
               _column("type", _("Type"), "dimension"), _column("relation", _("Relation"), "dimension")]
    return _evidence(env, "exploration", "odoo:" + model._name, columns, rows[:MAX_ROWS],
                     truncated=len(rows) > MAX_ROWS)


def _product_name_domain(env, model, raw):
    """Resolve a variant name through visible templates, then let the variant ORM apply its own rules."""
    if model._name != "product.product" or not isinstance(raw, list):
        return raw
    if len(raw) > 60:
        raise UserError(_("Invalid Odoo filters."))
    terms = []
    for term in raw:
        if (isinstance(term, (list, tuple)) and len(term) == 3 and term[0] == "name"
                and term[1] in ("=", "ilike", "like") and isinstance(term[2], str)):
            template = model_for(env, "product.template")
            if "name" not in field_map(template) or "product_tmpl_id" not in field_map(model):
                raise AccessError(_("The filter references a field outside your BI access."))
            matches = template.search([("name", term[1], term[2])], limit=151)
            if len(matches) > 150:
                raise UserError(_("The product name matches too many templates. Narrow the search."))
            terms.append(("product_tmpl_id", "in", matches.ids))
        else:
            terms.append(term)
    return terms


def _orm(env, attached, name, args):
    model = model_for(env, args.get("model"))
    domain = domain_for(model, _product_name_domain(env, model, args.get("domain", [])))
    if attached.get("kind") in ("record", "view") and attached["model"] == model._name:
        ids = attached.get("record_ids") or []
        if ids:
            model = model.with_context(active_test=False)
        domain = expression.AND([domain, domain_for(model, attached.get("domain") or []),
                                 [("id", "in", ids)] if ids else []])
    if name == "search_count":
        return _evidence(env, "exploration", "odoo:" + model._name, [_column("count", _("Records"), "measure")],
                         [[model.search_count(domain)]], filters=domain)
    requested = args.get("fields") or []
    if not isinstance(requested, list) or not 0 < len(requested) <= MAX_FIELDS:
        raise UserError(_("Choose up to thirty readable fields."))
    limit = args.get("limit", MAX_ROWS)
    limit = min(limit, MAX_ROWS) if type(limit) is int and limit > 0 else MAX_ROWS
    if name == "read_group":
        groupby = args.get("groupby") or []
        if not isinstance(groupby, list):
            raise UserError(_("Choose group-by fields."))
        descriptions = _groups(env, model, groupby, requested)
        dimensions = {item.partition(":")[0] for item in groupby}
        aggregates, operators = {}, {}
        for item in requested:
            key, separator, aggregate = item.partition(":")
            if key == "__count" or key in dimensions:
                continue
            if not separator:
                aggregate = "sum" if descriptions[key]["type"] in ("float", "integer", "monetary") else "count_distinct"
            aggregates[item] = "value_%s:%s(%s)" % (len(aggregates), aggregate, key)
            operators[aggregates[item].partition(":")[0]] = aggregate
        query_fields = list(aggregates.values())
        if any("." in item for item in groupby):
            found = _grouped_through_paths(env, model, domain, groupby, query_fields, operators, descriptions)
        else:
            found = model.read_group(domain, query_fields, groupby, limit=limit + 1, lazy=False)
        names = list(dict.fromkeys([*groupby, *requested]))
        columns = [_column(item, role="dimension" if item in groupby else "measure")
                   for item in names]
        rows = [[_scoped_relation(env, descriptions.get(item.partition(":")[0], {}),
                                  row.get(aggregates[item].partition(":")[0]) if item in aggregates else
                                  row.get(item, row.get(item.partition(":")[0]))) for item in names] for row in found]
    else:
        raw_domain = args.get("domain", [])
        product_label = False
        if (model._name == "product.product" and "display_name" in requested and isinstance(raw_domain, list)
                and all(isinstance(term, (list, tuple)) and len(term) == 3 for term in raw_domain)):
            name_search = any(term[0] == "name" and term[1] in ("=", "ilike", "like")
                              and isinstance(term[2], str) for term in raw_domain)
            template_ids = []
            for field, operator, value in raw_domain:
                if field == "product_tmpl_id" and operator == "=" and type(value) is int and value > 0:
                    template_ids.append(value)
                elif (field == "product_tmpl_id" and operator == "in" and isinstance(value, list)
                      and len(value) <= 150 and all(type(item) is int and item > 0 for item in value)):
                    template_ids.extend(value)
            if template_ids:
                template = model_for(env, "product.template")
                if "name" not in field_map(template) or "product_tmpl_id" not in field_map(model):
                    raise AccessError(_("An analytical field is outside your BI access."))
                visible = template.search([("id", "in", template_ids)]).ids
                domain = expression.AND([domain, [("product_tmpl_id", "in", visible)]])
            product_label = name_search or bool(template_ids)
        descriptions = _groups(env, model, [], [item for item in requested if not (product_label and item == "display_name")])
        if product_label:
            label = model.fields_get(["display_name"], attributes=["string", "type"]).get("display_name")
            if not label:
                raise AccessError(_("An analytical field is outside your BI access."))
            descriptions["display_name"] = label
        if any(":" in item or item == "__count" for item in requested):
            raise UserError(_("Choose record fields without aggregations."))
        if name == "read_records":
            ids = args.get("ids") or []
            if not isinstance(ids, list) or not 0 < len(ids) <= MAX_ROWS or any(type(i) is not int for i in ids):
                raise UserError(_("Choose up to sixty saved records."))
            domain = expression.AND([domain, [("id", "in", ids)]])
        found = model.search_read(domain, requested, limit=limit + 1, order="id")
        columns = [_record_column(item, descriptions[item]) for item in requested]
        rows = [[_scoped_relation(env, descriptions[item], row.get(item)) for item in requested] for row in found]
    source = {"model": model._name}
    if name == "read_records":
        source["record_ids"] = [item["id"] for item in found[:limit]]
    return _evidence(env, "exploration", "odoo:" + model._name, columns, rows[:limit],
                     filters=domain, truncated=len(rows) > limit, **source)


def _outstanding_purchase_lines(env, attached, args):
    """Scan visible confirmed lines before the cutoff; apply the quantity predicate before the result cap."""
    from datetime import date, datetime, time
    import pytz
    cutoff = args.get("before")
    try:
        if not isinstance(cutoff, str) or date.fromisoformat(cutoff).isoformat() != cutoff:
            raise ValueError()
    except ValueError as exc:
        raise UserError(_("Use a cutoff date in YYYY-MM-DD format.")) from exc
    mode = args.get("mode", "lines")
    if mode not in ("lines", "summary", "suppliers"):
        raise UserError(_("Choose lines, summary or suppliers."))
    limit = args.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise UserError(_("Choose a limit from 1 to 200."))
    try:
        zone = pytz.timezone(env.context.get("tz") or env.user.tz or "UTC")
        cutoff_utc = zone.localize(datetime.combine(date.fromisoformat(cutoff), time.min), is_dst=None).astimezone(pytz.UTC)
    except (pytz.UnknownTimeZoneError, pytz.InvalidTimeError) as exc:
        raise UserError(_("The cutoff cannot be resolved in your time zone.")) from exc
    model = model_for(env, "purchase.order.line")
    names = ["id", "order_id", "partner_id", "product_id", "product_qty", "qty_received", "product_uom", "date_planned"]
    descriptions = _groups(env, model, [], names + ["state"])
    domain = domain_for(model, [("state", "in", ["purchase", "done"]), ("date_planned", "<", fields.Datetime.to_string(cutoff_utc))])
    if attached.get("kind") in ("record", "view"):
        if attached.get("model") != model._name:
            raise UserError(_("This purchase-line query cannot use the attached view. Open a purchase line view or detach it."))
        ids = attached.get("record_ids") or []
        domain = expression.AND([domain, domain_for(model, attached.get("domain") or []),
                                 [("id", "in", ids)] if ids else []])
    found, last_id, line_count, counts, hidden = [], 0, 0, Counter(), False
    while mode != "lines" or len(found) <= limit:
        batch = model.search_read(expression.AND([domain, [("id", ">", last_id)]]), names, limit=200, order="id")
        if not batch:
            break
        last_id = batch[-1]["id"]
        matched = [row for row in batch if (row.get("product_qty") or 0) > (row.get("qty_received") or 0)]
        if mode == "lines":
            found.extend(matched)
        else:
            line_count += len(matched)
            for row in matched:
                supplier = _scoped_relation(env, descriptions["partner_id"], row.get("partner_id"))
                if supplier:
                    counts[tuple(supplier)] += 1
                else:
                    hidden = True
        if len(batch) < 200:
            break
    if mode == "suppliers":
        groups = sorted(counts.items(), key=lambda item: (-item[1], item[0][0]))
        return _evidence(env, "exploration", "odoo:purchase.order.line",
                         [_record_column("partner_id", descriptions["partner_id"]),
                          _column("line_count", _("Outstanding lines"), "measure", "count")],
                         [[supplier, count] for supplier, count in groups[:limit]],
                         filters={"domain": domain, "quantity_condition": "product_qty > qty_received"},
                         model=model._name, truncated=hidden or len(groups) > limit)
    if mode == "summary":
        supplier_count = len(counts) if not hidden else None
        return _evidence(env, "exploration", "odoo:purchase.order.line",
                         [_column("line_count", _("Outstanding lines"), "measure", "count"),
                          _column("supplier_count", _("Visible suppliers"), "measure", "count")],
                         [[line_count, supplier_count]], filters={"domain": domain,
                         "quantity_condition": "product_qty > qty_received"}, model=model._name, truncated=False)
    columns = [_record_column(item, descriptions[item]) for item in names]
    columns.append(_column("open_qty", _("Outstanding quantity"), "measure"))
    rows = [[*[_scoped_relation(env, descriptions[item], row.get(item)) for item in names],
             (row.get("product_qty") or 0) - (row.get("qty_received") or 0)] for row in found[:limit]]
    return _evidence(env, "exploration", "odoo:purchase.order.line", columns, rows,
                     filters={"domain": domain, "quantity_condition": "product_qty > qty_received"},
                     model=model._name, truncated=len(found) > limit)


def _widget_rows(analytic):
    kind = analytic.get("analytic_type")
    if kind == "tile":
        amount = analytic.get("amount")
        if isinstance(amount, list) and amount and isinstance(amount[0], dict):
            amount = amount[0].get("amount", amount[0].get("value"))
        if isinstance(amount, (int, float, Decimal)) and not isinstance(amount, bool):
            return [_column("amount", analytic.get("name"), "measure", analytic.get("unit"))], [[amount]]
    rows = analytic.get("rows") or analytic.get("data")
    columns = analytic.get("columns")
    if isinstance(rows, list) and rows and all(isinstance(item, dict) for item in rows[:MAX_ROWS + 1]):
        names = [item for item in rows[0] if item in ("x", "y", "label", "name", "value", "amount",
                                                    "year_", "group_by", "category", "comparison")
                 or re.fullmatch(r"y\d+", item)]
        if names:
            return [_column(name, role="measure" if name in {"value", "amount", "y"} or re.fullmatch(r"y\d+", name)
                            else "dimension")
                    for name in names], [[row.get(name) for name in names] for row in rows[:MAX_ROWS + 1]]
    if isinstance(columns, list) and isinstance(rows, list) and len(columns) <= MAX_FIELDS:
        names = [str(col) if not isinstance(col, dict) else col.get("label") or col.get("name") or str(i)
                 for i, col in enumerate(columns)]
        if all(isinstance(item, (list, tuple)) and len(item) == len(names) for item in rows[:MAX_ROWS + 1]):
            return [_column(str(i), name) for i, name in enumerate(names)], rows[:MAX_ROWS + 1]
    raise UserError(_("This widget does not have a supported numerical result."))


def _dashboard_filters(widget, rows, is_global):
    if not isinstance(rows, list) or len(rows) > 50:
        raise UserError(_("The widget filters are invalid."))
    if is_global:
        ids = set(widget._dashboard_global_filter_groups())
    else:
        ids = set(widget.conditions_line.filtered(
            lambda condition: condition.show_filters and not condition.is_query_param and not condition.is_having_filter
        ).ids)
    for item in rows:
        if not isinstance(item, dict) or type(item.get("conditions_line_id")) is not int:
            raise UserError(_("The widget filters are invalid."))
        if item["conditions_line_id"] not in ids:
            raise AccessError(_("The filter is not visible on that widget or dashboard."))
        if not isinstance(item.get("filter_values"), dict):
            raise UserError(_("The widget filter value is invalid."))
    return rows


def _widget_evidence(env, attached):
    dashboard, widget = _widget(env, attached)
    global_filters = _dashboard_filters(widget, attached.get("filters") or [], True)
    local_filters = _dashboard_filters(widget, attached.get("widget_filters") or [], False)
    period = attached.get("widget_range") or attached.get("range")
    if period and (not isinstance(period, dict) or any(not isinstance(value, (str, bool)) for value in period.values())):
        raise UserError(_("The widget date range is invalid."))
    # Use the same filter compiler as the dashboard. The local and dashboard
    # filters are independently authorized above, then combined by SaaS.
    server, key = env["dashboard.api.key.saas"]._querygen_credentials()
    query = {
        "data_analytics_obj": widget, "range_filter": period, "filter": local_filters,
        "global_filters": global_filters, "chosen_api_server": server, "api_key": key,
    }
    dataset = widget.dataset_id
    kind = widget.analytical_type
    row = {"analytic_type": kind, "name": widget.name}
    if kind == "tile":
        row["amount"], _, _ = dataset.get_tile_data(**query)
        row["unit"] = widget.currency_symbol or None
    elif kind in CHARTS:
        _, row["data"], _, _, _ = dataset.get_bar_chart_data(**query)
    elif kind in {"table", "pivot"}:
        raw, _, _ = dataset.get_table_data(**query, apply_default_formatting=False)
        if raw:
            # The web widget uses the ID column for drill-down, not display.
            columns = [idx for idx, name in enumerate(raw[0]) if not widget.show_drilldown or name != "id"]
            row["columns"] = [raw[0][idx] for idx in columns]
            row["rows"] = [[item[idx] for idx in columns] for item in raw[1:MAX_ROWS + 2]]
    columns, rows = _widget_rows(row)
    return _evidence(env, "dashboard", "odoo:widget:%s" % widget.id, columns, rows,
                     definition=widget.name, filters={"widget": local_filters, "dashboard": global_filters},
                     period=period, dashboard_id=dashboard.id, widget_id=widget.id)


def _insight_evidence(env, attached):
    insight, dashboard, widget = _insight(env, attached)
    # The generation log holds the snapshot that produced the visible insight.
    # It and the feed rows share a transaction, so their creation times match.
    log = env["ai.generation.log.saas"].sudo().search([
        ("dashboard_id", "=", dashboard.id), ("kind", "=", "insights"), ("status", "=", "success"),
        ("create_date", "=", insight.create_date),
    ], limit=1)
    try:
        findings = json.loads(log.payload or "{}").get("findings", [])
    except (ValueError, AttributeError):
        findings = []
    row = next((item for item in findings if item.get("analytic_id") == widget.id), None)
    if not row:
        raise UserError(_("The source snapshot for this insight is no longer available."))
    names = ["current", "previous", "change_pct"]
    return _evidence(env, "dashboard", "odoo:insight:%s" % attached["insight_id"],
                     [_column(key, role="measure", unit="%" if key == "change_pct" else None) for key in names],
                     [[row[key] for key in names]], definition=row["metric"], period=row["window"],
                     dashboard_id=dashboard.id, widget_id=widget.id)


def _page_evidence(env, attached):
    model = model_for(env, attached["model"])
    kind = attached["kind"]
    view_type = attached.get("view_type") or ("form" if kind == "record" else "list")
    ids = attached.get("record_ids") or []
    if ids:
        model = model.with_context(active_test=False)
    domain = expression.AND([domain_for(model, attached.get("domain") or []), [("id", "in", ids)] if ids else []])
    source_id = "odoo:record:%s:%s" % (model._name, ids[0]) if kind == "record" else "odoo:page:%s" % model._name
    label = _("%s record") % model._description if kind == "record" else _("%s page") % model._description
    if view_type in {"pivot", "graph"}:
        grouping, measures = attached.get("groupby") or [], attached.get("measures") or []
        if not isinstance(grouping, list) or not isinstance(measures, list):
            raise UserError(_("The page grouping or measures are invalid."))
        if not measures:
            measures = ["__count"]
        result = _orm(env, attached, "read_group", {
            "model": model._name, "fields": measures, "groupby": grouping, "limit": PAGE_ROWS,
        })
        return {**result, "source_kind": kind, "source_id": source_id, "label": label, "dims": grouping}

    try:
        arch = model.get_view(attached.get("view_id"), "tree" if view_type == "list" else view_type)["arch"]
    except ValueError as exc:
        raise UserError(_("This page view is unavailable.")) from exc
    root = etree.fromstring(arch.encode())
    readable = model.fields_get(attributes=["string", "type", "relation", "store"])
    allowed_types = DATA_TYPES | {"text", "html"}
    names = list(dict.fromkeys(node.get("name") for node in root.xpath(
        ".//field[@name and not(ancestor::field) and not(ancestor-or-self::*[@invisible='1' or @invisible='True'])]"
    )))
    if attached.get("page_fields") is not None:
        shown = set(attached["page_fields"])
        names = [name for name in names if name in shown]
    names = [name for name in names if readable.get(name, {}).get("store")
             and readable[name]["type"] in allowed_types and not SENSITIVE.search(name)][:MAX_FIELDS]
    if not names:
        return _evidence(env, kind, source_id, [_column("count", _("Records"), "measure")],
                         [[model.search_count(domain)]], label=label, filters=domain)
    limit = 1 if kind == "record" else PAGE_ROWS
    found = model.search_read(domain, names, limit=limit + 1, order="id")
    rows = []
    shortened = False
    text_limit = PAGE_TEXT if kind == "record" else 240
    for record in found[:limit]:
        row = []
        for name in names:
            spec = readable[name]
            value = _scoped_relation(env, spec, record.get(name))
            if spec["type"] == "html" and isinstance(value, str):
                value = html2plaintext(value)
            if isinstance(value, str) and len(value) > text_limit:
                shortened = True
                value = value[:text_limit]
            row.append(value)
        rows.append(row)
    return _evidence(env, kind, source_id, [_record_column(name, readable[name]) for name in names], rows,
                     label=label, filters=domain, record_ids=[record["id"] for record in found[:limit]],
                     truncated=len(found) > limit or shortened)


def context_for_step(env, attached):
    data = []
    if attached.get("kind") == "widget":
        data.append(_widget_evidence(env, attached))
    elif attached.get("kind") == "insight":
        data.append(_insight_evidence(env, attached))
    elif attached.get("kind") in ("record", "view"):
        data.append(_page_evidence(env, attached))
    return {"attachment": attached, "evidence": data}


def _bindings(chosen):
    return {prop["key"]: (prop.get("bindings") or {}).get(odoo_release.series.split(".")[0])
            for prop in chosen["properties"]}


def _unit_dimension(env, chosen):
    """The dimension a summed amount is split by, so no total adds two currencies or units of measure. A quantity
    metric without a listed one uses any dimension of its object bound to a unit of measure."""
    known = AGGREGATE_UNITS.get((chosen["object"], chosen.get("unit")))
    if known or chosen.get("unit") != "qty":
        return known
    bindings = _bindings(chosen)
    return next((key for key in chosen["dimensions"] if bindings.get(key)
                 and onto_explore._bound_relation(env, chosen["base_model"], bindings[key]) == "uom.uom"), None)


def _dimension_labels(env, chosen, dims, result):
    bindings = _bindings(chosen)
    names = {}
    for index, column in enumerate(result["columns"]):
        key = column["key"]
        if key not in dims or not bindings.get(key):
            continue
        related = onto_explore._bound_relation(env, chosen["base_model"], bindings[key])
        ids = {row[index] for row in result["rows"] if type(row[index]) is int}
        if not related or not ids:
            continue
        try:
            readable = model_for(env, related).search([("id", "in", list(ids))])
            names[index] = dict(readable.name_get())
        except AccessError:
            continue
    return names


def _metric(env, available, attached, name, args):
    chosen = next((item for item in available["metrics"] if item["key"] == args.get("metric")), None)
    if not chosen or not available["release"]:
        raise AccessError(_("The metric is outside your published catalogue."))
    if env["ir.config_parameter"].sudo().get_param("odoo_dashboards_bi.release") != available["release"]:
        raise AccessError(_("The ontology release changed. Ask your question again."))
    if attached.get("model") == chosen.get("base_model") and (attached.get("domain") or attached.get("record_ids")):
        raise UserError(_("For the exact attached selection, query the permitted Odoo records."))
    dims = args.get("dims") or []
    filters = args.get("filters") or []
    permitted_props = {item["key"] for item in chosen.get("properties") or []}
    if (
        not isinstance(dims, list) or any(not isinstance(key, str) or key not in chosen["dimensions"] for key in dims)
        or not isinstance(filters, list) or any(not isinstance(item, dict) or
            not isinstance(item.get("prop"), str) or item["prop"] not in permitted_props for item in filters)
    ):
        # A UserError goes back to the model as a failed tool call, so it can retry.
        raise UserError(_(
            "Unknown dims or filters for this metric. Dims: %(dims)s. Filter props: %(props)s. "
            "For anything else, use list_models, describe_model and the Odoo record tools.",
            dims=", ".join(chosen["dimensions"]), props=", ".join(sorted(permitted_props)),
        ))
    accepted = {"dims", "filters", "period", "grain", "top_n", "limit", "params"}
    spec = {key: value for key, value in args.items() if key in accepted}
    unit_dimension = _unit_dimension(env, chosen)
    if unit_dimension in chosen["dimensions"] and unit_dimension not in dims:
        spec["dims"] = [*dims, unit_dimension]
    spec.update(release=available["release"], object=chosen["object"], metric=chosen["key"], page=1)
    if name == "compare":
        spec["compare"] = {"to": "previous_period"}
    if not isinstance(spec.get("limit"), int) or not 0 < spec["limit"] <= MAX_ROWS:
        spec["limit"] = MAX_ROWS
    result = onto_query.run_query(env, spec, env.companies.ids, scope_related=True)
    result_dims = spec.get("dims") or []
    labels = _dimension_labels(env, chosen, result_dims, result)
    for index, readable in labels.items():
        duplicates = {label for label, count in Counter(readable.values()).items() if count > 1}
        for row in result["rows"]:
            value = row[index]
            if value in readable:
                label = readable[value]
                row[index] = "%s (#%d)" % (label, value) if label in duplicates else label
    if unit_dimension not in dims and unit_dimension in (result_dims or []) and len(result["rows"]) > 0:
        index = next((i for i, column in enumerate(result["columns"]) if column["key"] == unit_dimension), None)
        if index is not None and all(row[index] == result["rows"][0][index] for row in result["rows"]):
            unit = result["rows"][0][index]
            if isinstance(unit, str):
                for column in result["columns"]:
                    if column.get("role") == "measure" and column.get("unit") in {"money", "qty"}:
                        column["unit"] = unit
            result["columns"] = [column for i, column in enumerate(result["columns"]) if i != index]
            result["rows"] = [row[:index] + row[index + 1:] for row in result["rows"]]
            result_dims = dims
    evidence = _evidence(env, "ontology", "odoo:metric:" + chosen["key"], result["columns"],
                         result["rows"], release=result["release"], read_at=result["read_at"],
                         definition=chosen.get("description") or chosen.get("label"), label=chosen.get("label"),
                         trust=chosen.get("trust"), unreviewed=bool(chosen.get("unreviewed")),
                         filters=spec.get("filters"), dims=result_dims,
                         grain=spec.get("grain"), period=spec.get("period"), truncated=result["truncated"])
    if chosen.get("unit") == "qty" and not unit_dimension:
        evidence["notice"] = " ".join(filter(None, [evidence.get("notice"), _(
            "Each quantity is in its line's own unit of measure, and this result is not split by unit. If it covers "
            "more than one product, say the figures may add different units.")]))
    return evidence


def execute(env, available, attached, tool):
    name, args = tool.get("name"), tool.get("arguments")
    if not isinstance(args, dict) or len(json.dumps(args)) > 10000:
        raise UserError(_("Invalid BI tool input."))
    with env.cr.savepoint():
        env.cr.execute("SET LOCAL statement_timeout = '12000ms'")
        result = _execute(env, available, attached, name, args)
        env.cr.execute("SET LOCAL statement_timeout = DEFAULT")
    return result


def _execute(env, available, attached, name, args):
    try:
        if name in {"measure", "compare"}:
            return _metric(env, available, attached, name, args)
        if name == "outstanding_purchase_lines":
            return _outstanding_purchase_lines(env, attached, args)
        if name in {"search_count", "search_read", "read_records", "read_group"}:
            try:
                with env.cr.savepoint():
                    return _orm(env, attached, name, args)
            except (ValueError, TypeError) as exc:  # a malformed domain value: the model fixes it and retries
                raise UserError(_("Invalid domain: %s. Compare fields to literal values only; a domain cannot "
                                  "compare two fields.", exc)) from exc
        if name == "list_models":
            return _list_models(env, args)
        if name == "describe_model":
            return _describe_model(env, args)
        if name == "explore" and not attached.get("domain") and not attached.get("record_ids"):
            model_for(env, args.get("model"))
            result = onto_explore.run_explore(env, args, env.companies.ids)
            return _evidence(env, "exploration", "odoo:" + args["model"], result["columns"], result["rows"],
                             filters=args.get("filters"), truncated=result.get("truncated", False))
        if name == "lookup_values":
            key = args.get("object")
            permitted = {
                prop["key"] for metric in available["metrics"] if metric["object"] == key
                for prop in metric.get("properties") or []
            }
            if args.get("column") not in permitted:
                raise AccessError(_("That dimension is outside your published catalogue."))
            result = onto_explore.lookup_values(
                env, {**args, "release": available["release"]}, env.companies.ids, scope_related=True,
            )
            values = result.get("values") or []
            # A relation column answers [id, name] pairs: filter a metric with the id.
            related = any(isinstance(value, list) for value in values)
            columns = [_column("id", role="id"), _column("name", role="dimension")] if related else [
                _column("value", role="dimension")]
            return _evidence(env, "exploration", "odoo:object:" + key, columns,
                             [value if related else [value] for value in values],
                             truncated=bool(result.get("truncated")))
    except onto_query.QueryError as exc:
        if exc.status in (403, 404):
            raise AccessError(_("The requested data is outside your Odoo access.")) from exc
        if exc.code == "invalid_spec" and exc.message.startswith("insufficient data: this snapshot has no history"):
            raise UserError(_("This metric uses a current snapshot. Historical data is unavailable for that date.")) from exc
        if exc.status == 422:  # querygen's refusal of the spec: its reason is safe to show, and the model can fix it
            filters = args.get("filters") if isinstance(args.get("filters"), list) else []
            hint = ""
            if any(isinstance(item, dict) and isinstance(item.get("value"), str) for item in filters):
                hint = " " + _("Filters on a relation take record ids; resolve the name with lookup_values first.")
            raise UserError(_("The query is not valid: %s", exc.message) + hint) from exc
        raise UserError(_("The query could not be evaluated at the requested scope.")) from exc
    raise UserError(_("This analytical request is not available."))
