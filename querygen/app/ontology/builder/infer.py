"""Deterministic candidates for the Builder (ONTOLOGY_BUILDER_PLAN 1): code discovers, the LLM curates.

For each object: its own stored fields by type, one or two many2one hops to the conformed dimension targets, and the
standard metrics on its measures, with additivity set by code from object.snapshot. Every candidate is checked
(match.check_property, match._check_metric) and becomes a `property` or `metric` row. A candidate that is empty, over a
cap or fails its check is not a row: it is listed in the notes with the reason. Metadata only: types from the
introspect, fill rates from the profile.
"""
import re

from app.ontology import loader
from app.ontology.builder import match
from app.ontology.builder.llm import domain_of
from app.ontology.builder.profile import Introspect, Profile
from app.ontology.schema import Metric, Object, Property, Release

MAX_DIMS, MAX_MEASURES = 25, 15
MIN_FILL, BATCH_FILL = 0.05, 0.5
BATCH_CONFIDENCE, REVIEW_CONFIDENCE = 0.9, 0.6  # batch approval takes 0.85 or more (builder.BATCH_CONFIDENCE)
# target model -> (property key, label, synonyms). The same target gets the same key on every object, so chat can group
# any metric by "branch" with one synonym set. A partner's key comes from its role (_partner).
CONFORMED = {
    "stock.warehouse": ("warehouse", "Warehouse", ["warehouse", "branch", "store", "site"]),
    "stock.location": ("location", "Location", ["location"]),
    "product.product": ("product", "Product", ["product", "item"]),
    "product.category": ("product_category", "Product category", ["product category", "category"]),
    "res.partner": ("partner", "Partner", ["partner", "contact"]),
    "res.users": ("user", "User", ["user", "responsible"]),
    "crm.team": ("sales_team", "Sales team", ["sales team", "team"]),
    "res.company": ("company", "Company", ["company"]),
    "uom.uom": ("uom", "Unit of measure", ["unit of measure", "uom"]),
    "account.account": ("account", "Account", ["account", "gl account"]),
    "account.analytic.account": ("analytic_account", "Analytic account", ["analytic account", "cost center"]),
    "hr.department": ("department", "Department", ["department"]),
    "hr.employee": ("employee", "Employee", ["employee"]),
    "pos.config": ("point_of_sale", "Point of sale", ["point of sale", "pos"]),
    "project.project": ("project", "Project", ["project"]),
}
PARTNER_ROLES = {"customer": ("Customer", ["customer", "client"]), "vendor": ("Vendor", ["vendor", "supplier"])}
PARTNER_PARTS = (("country_id", "country"), ("state_id", "state"))  # a partner's own conformed dimensions
SALES = ("sale.", "pos.", "crm.")
ROLES = {"many2one": "dimension", "selection": "status", "boolean": "status", "date": "time", "datetime": "time",
         "monetary": "measure", "float": "measure", "integer": "measure"}
SKIP = {"id", "create_uid", "write_uid", "write_date", "__last_update", "display_name"}
SKIP_PREFIXES = ("message_", "activity_", "website_message", "access_")
SKIP_NUMERIC = ("sequence", "priority", "color", "colour")
UNIT_PRICE = ("price", "unit_cost", "cost_unit", "discount", "percent")  # averaged, never summed
MONEY = ("amount", "price", "cost", "value", "total", "margin", "balance", "debit", "credit")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def _usable(intro: Introspect, fields: dict, model: str) -> list:
    """The stored fields of a model that the introspect and the compiler field list both hold, less technical noise."""
    entry = intro.models.get(model)
    known = fields.get(model, {})
    if entry is None:
        return []
    return [f for f in entry["fields"].values()
            if f.stored and known.get(f.name, {}).get("store") and f.name not in SKIP
            and not f.name.startswith(SKIP_PREFIXES)]


def _steps(intro: Introspect, fields: dict, model: str) -> list[tuple[str, list, object]]:
    """(path, hops, field) for each many2one of a model; hops are (model, field) pairs for the fill rate."""
    out = [(f.name, [(model, f.name)], f) for f in _usable(intro, fields, model) if f.type == "many2one" and f.relation]
    if model == "product.product":
        # ponytail: product.product's _inherits of product.template is the one inherit followed, so a product reaches
        # its category in one step; follow other _inherits when a conformed target sits behind one.
        out += [(f"product_tmpl_id.{path}", [(model, "product_tmpl_id"), *hops], f)
                for path, hops, f in _steps(intro, fields, "product.template")]
    return out


def _fill(prof: Profile, hops: list) -> float | None:
    # ponytail: a path's fill rate is the product of each hop's own fill rate, not a count on the path itself; a hop on
    # a model the profile did not read leaves it unknown. Replace with the path profile endpoint (plan 6) once it exists.
    rate = 1.0
    for model, name in hops:
        hop = prof.fill(model, name)
        if hop is None:
            return None
        rate *= hop
    return rate


def _path_key(path: str) -> str:
    return "_".join(part.removesuffix("_id") for part in path.split(".") if part != "product_tmpl_id")


def _partner(base: str, path: str) -> tuple[str, str, list[str]]:
    """customer, vendor or partner, from the field name and then the base model's app."""
    role = ("vendor" if any(word in path for word in ("vendor", "supplier")) else
            "customer" if "customer" in path else
            "vendor" if base.startswith("purchase.") else "customer" if base.startswith(SALES) else None)
    if role is None:
        return CONFORMED["res.partner"]
    label, synonyms = PARTNER_ROLES[role]
    return role, label, synonyms


def _conformed(base: str, target: str, path: str) -> tuple[str, str, list[str]]:
    if target == "res.partner":
        return _partner(base, path)
    if target == "res.users" and base.startswith(SALES):
        return "salesperson", "Salesperson", ["salesperson", "sales rep", "seller"]
    return CONFORMED[target]


def _unit(field) -> str:
    if field.type == "monetary" or any(word in field.name for word in MONEY):
        return "money"
    if "qty" in field.name or "quantity" in field.name:
        return "qty"
    return "count" if field.type == "integer" else "qty"


def _candidate(key, label, role, unit, path, synonyms, fill, conformed, caveat) -> dict:
    notes = [caveat] if caveat else []
    if fill is None:
        notes.append("fill rate unknown: a model on the path is not profiled")
    return {"key": key, "label": label, "role": role, "unit": unit, "binding": path,
            "synonyms": list(dict.fromkeys(s.lower() for s in synonyms)),
            "fill_rate": None if fill is None else round(fill, 3), "notes": notes, "conformed": conformed}


def candidates(obj: Object, intro: Introspect, prof: Profile, fields: dict,
               caveats: dict[str, str]) -> tuple[list[dict], list[str]]:
    """The ranked, capped candidates of one object that it does not bind yet, and a note for each one left out."""
    base = obj.base_model
    bound = {p.bindings.get(match.BINDING) for p in obj.properties}
    used = {p.key for p in obj.properties}
    out, notes = [], []

    def add(key, label, role, unit, path, synonyms, fill, conformed=False):
        if path in bound:
            return
        if key in used and conformed:
            return  # the object already has this conformed dimension under its key
        if key in used or not _KEY.match(key):
            key = _path_key(path)
            if key in used or not _KEY.match(key):
                return
        if fill is not None and fill < MIN_FILL:
            notes.append(f"{obj.key}.{key} ({path}): dropped, fill rate {fill:.0%} is under {MIN_FILL:.0%}")
            return
        used.add(key)
        out.append(_candidate(key, label, role, unit, path, synonyms, fill, conformed, caveats.get(f"{base}.{path}")))

    first = _steps(intro, fields, base)
    paths = [(1, *step) for step in first]
    paths += [(2, f"{p1}.{p2}", h1 + h2, f2) for _, p1, h1, f1 in paths for p2, h2, f2 in _steps(intro, fields, f1.relation)]
    best: dict[str, tuple] = {}  # target model -> its shortest path, ties to the best fill rate, then the shortest name
    for steps, path, hops, leaf in paths:
        if leaf.relation in CONFORMED and leaf.relation != base:
            fill = _fill(prof, hops)
            rank = (steps, -(fill if fill is not None else -1), len(path))
            if leaf.relation not in best or rank < best[leaf.relation][0]:
                best[leaf.relation] = (rank, path, hops, fill)
    for target, (_, path, hops, fill) in sorted(best.items(), key=lambda item: item[1][0]):
        key, label, synonyms = _conformed(base, target, path)
        add(key, label, "dimension", None, path, synonyms, fill, conformed=True)
        if target == "res.partner" and path.count(".") == 0:  # a partner's country and state, still two hops
            for name, part in PARTNER_PARTS:
                leaf = intro.field(target, name)
                if leaf is not None and leaf.stored and fields.get(target, {}).get(name, {}).get("store"):
                    add(f"{key}_{part}", f"{label} {part}", "dimension", None, f"{path}.{name}",
                        [f"{label} {part}", f"{part}"], _fill(prof, [*hops, (target, name)]), conformed=True)
    conformed_paths = {best[target][1] for target in best}
    for f in _usable(intro, fields, base):
        role = ROLES.get(f.type)
        if role is None or f.name in conformed_paths:
            continue
        if role == "measure" and any(word in f.name for word in SKIP_NUMERIC):
            continue
        add(f.name.removesuffix("_id"), f.label, role, _unit(f) if role == "measure" else None, f.name,
            [f.label], prof.fill(base, f.name))

    def rank(c):
        return -(c["fill_rate"] if c["fill_rate"] is not None else -1), not c["conformed"]

    dims = sorted((c for c in out if c["role"] != "measure"), key=rank)
    measures = sorted((c for c in out if c["role"] == "measure"), key=rank)
    for kept, cap in ((dims, MAX_DIMS), (measures, MAX_MEASURES)):
        notes += [f"{obj.key}.{c['key']} ({c['binding']}): dropped, past the cap of {cap}" for c in kept[cap:]]
    return dims[:MAX_DIMS] + measures[:MAX_MEASURES], notes


def standard_metrics(obj: Object, release: Release, fills: dict[str, float | None]) -> list[dict]:
    """A count, a sum of each measure and an average of each unit-price-like one, unless the release has it. Additivity
    is set by code (match.additivity). `fills` holds the fill rate of each property that may be the time_property."""
    have = {(m.aggregation, m.measure) for m in release.metrics
            if m.object == obj.key and not m.filters and m.derived_from is None}
    times = [p.key for p in obj.properties if p.role == "time"]
    time = None if obj.snapshot or not times else max(times, key=lambda k: fills.get(k) or 0)
    noun = obj.label.lower()
    out = []
    if ("count", None) not in have:
        out.append({"key": f"{obj.key}_count", "label": f"{obj.label} count", "aggregation": "count", "measure": None,
                    "unit": "count", "synonyms": [f"number of {noun}", f"{noun} count"]})
    for prop in (p for p in obj.properties if p.role == "measure" and p.bindings.get(match.BINDING)):
        leaf = prop.bindings[match.BINDING].rsplit(".", 1)[-1]
        avg = any(word in leaf or word in prop.key for word in UNIT_PRICE)
        aggregation = "avg" if avg else "sum"
        if (aggregation, prop.key) in have:
            continue
        label = f"{'Average' if avg else 'Total'} {prop.label[:1].lower()}{prop.label[1:]}"
        out.append({"key": f"{obj.key}_{prop.key}_{'avg' if avg else 'total'}", "label": label,
                    "aggregation": aggregation, "measure": prop.key, "unit": prop.unit or "qty",
                    "synonyms": [label.lower()]})
    return [{**m, "object": obj.key, "time_property": time, "filters": [],
             **match.additivity(obj.snapshot, m["aggregation"])} for m in out]


def _with_properties(release: Release, key: str, props: list[Property]) -> Release:
    return release.model_copy(update={"objects": [
        o.model_copy(update={"properties": [*o.properties, *props]}) if o.key == key else o for o in release.objects]})


def property_row(object_key: str, base_model: str, c: dict, run_id: str, batch: bool, source: str = "infer") -> dict:
    payload = {"kind": "property", "source": source, "run_id": run_id, "object": object_key,
               **{name: c[name] for name in ("key", "label", "role", "unit", "binding", "synonyms", "fill_rate",
                                             "notes")}, "batch_approvable": batch}
    return {"kind": "property", "domain": domain_of(base_model), "node_key": f"property.{object_key}.{c['key']}",
            "confidence": BATCH_CONFIDENCE if batch else REVIEW_CONFIDENCE, "payload": payload,
            "validation": {"ok": True, "problems": []}, "batch_approvable": batch}


def metric_row(base_model: str, node: dict, run_id: str, batch: bool) -> dict:
    domain = domain_of(base_model)
    return {"kind": "metric", "domain": domain, "node_key": node["key"],
            "confidence": BATCH_CONFIDENCE if batch else REVIEW_CONFIDENCE,
            "payload": {"kind": "metric", "source": "infer", "domain": domain, "run_id": run_id, "node": node,
                        "batch_approvable": batch},
            "validation": {"ok": True, "problems": []}, "batch_approvable": batch}


def run(objects: list[Object], release: Release, intro: Introspect, prof: Profile, run_id: str, held: set[str],
        caveats: dict[str, str]) -> tuple[list[dict], list[str], Release]:
    """The checked property and metric rows of these objects (each already in `release`), the notes, and the release
    with every row applied: the candidate release a metric of the same run is checked against."""
    rows, notes = [], []
    fields = loader.with_release_fields(release, loader.load_compiler_fields())
    for start in objects:
        if not intro.has_model(start.base_model) or prof.row_count.get(start.base_model) == 0:
            continue
        found, dropped = candidates(start, intro, prof, fields, caveats)
        notes += dropped
        pending, fills = [], {}
        for c in found:
            if f"property.{start.key}.{c['key']}" in held:
                continue
            node, problems = match.check_property({"object": start.key, **c}, release, intro, prof)
            if problems:
                notes.append(f"{start.key}.{c['key']} ({c['binding']}): {problems[0]}")
                continue
            batch = c["conformed"] and c["fill_rate"] is not None and c["fill_rate"] >= BATCH_FILL
            rows.append(property_row(start.key, start.base_model, c, run_id, batch))
            pending.append(Property.model_validate(node))
            fills[c["key"]] = c["fill_rate"]
        if pending:
            release = _with_properties(release, start.key, pending)
        obj = next(o for o in release.objects if o.key == start.key)
        for p in obj.properties:
            if p.role == "time" and p.key not in fills and p.bindings.get(match.BINDING):
                fills[p.key] = prof.fill(obj.base_model, p.bindings[match.BINDING].split(".")[0])
        new = {p.key for p in pending}
        for metric in standard_metrics(obj, release, fills):
            if metric["key"] in held:
                continue
            node, problems = match._check_metric(metric, release, intro, prof)
            if problems:
                notes.append(problems[0])  # a metric's problem names its key
                continue
            batch = not ({metric["measure"], metric["time_property"]} & new)  # it stands without a pending property
            rows.append(metric_row(obj.base_model, node, run_id, batch))
            release = release.model_copy(update={"metrics": [*release.metrics, Metric.model_validate(node)]})
    return rows, notes, release


def _clean(synonyms) -> list[str]:
    out: dict[str, str] = {}
    for text in synonyms if isinstance(synonyms, list) else []:
        if isinstance(text, str) and text.strip():
            out.setdefault(text.strip().lower(), text.strip())
    return list(out.values())


def curate(rows: list[dict], labels: list, drops: list) -> tuple[list[dict], list[str]]:
    """The LLM's curation applied to the inferred rows: a label and synonyms replace the code's, and a drop removes a
    row with its reason (and the metrics standing on a dropped property). A batch-approvable row is never dropped:
    the conformed dimensions are the code's to decide."""
    by_key = {row["node_key"]: row for row in rows}
    for item in labels if isinstance(labels, list) else []:
        row = by_key.get(item.get("node_key")) if isinstance(item, dict) else None
        label = item.get("label") if row else None
        if row is None or not isinstance(label, str) or not label.strip():
            continue
        target = row["payload"] if row["kind"] == "property" else row["payload"]["node"]
        target["label"] = label.strip()
        target["synonyms"] = _clean([*_clean(item.get("synonyms")), *target["synonyms"]]) or [label.strip().lower()]
    gone, notes = set(), []
    for item in drops if isinstance(drops, list) else []:
        row = by_key.get(item.get("node_key")) if isinstance(item, dict) else None
        if row is None or row["batch_approvable"] or row["node_key"] in gone:
            continue
        gone.add(row["node_key"])
        notes.append(f"{row['node_key']}: dropped by the LLM: {str(item.get('reason') or 'no reason given')[:200]}")
    dropped = {(r["payload"]["object"], r["payload"]["key"]) for r in rows if r["node_key"] in gone and r["kind"] == "property"}
    for row in rows:
        node = row["payload"].get("node") or {}
        if row["kind"] == "metric" and row["node_key"] not in gone and \
                {(node.get("object"), node.get("measure")), (node.get("object"), node.get("time_property"))} & dropped:
            gone.add(row["node_key"])
            notes.append(f"{row['node_key']}: dropped with the property it stands on")
    return [row for row in rows if row["node_key"] not in gone], notes
