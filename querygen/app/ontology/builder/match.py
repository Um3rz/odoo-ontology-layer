"""Deterministic matching for the Builder (plan 2.3, D52): template bindings against the steward's metadata, the
OrgMap draft, and the checks every LLM proposal must pass before it is stored.

Types and relations come from the introspect, fill rates from the profile (D50). No record value is read or kept.
"""
import re
from datetime import date

from pydantic import ValidationError

from app.ontology import compiler, loader
from app.ontology.builder.llm import domain_of
from app.ontology.builder.profile import Introspect, Profile
from app.ontology.schema import (ODOO_16, Link, Metric, Object, Property, Release, Rule, Template, formula_is_ratio,
                                 formula_names)
from app.ontology.spec import DIM_ROLES, Period, QuerySpec, SpecError, context, expand, fields_for

BINDING = "16"  # the Odoo major version the bindings are keyed by (D6)
THRESHOLD = 0.6  # a template is proposed when the mean confidence of its objects reaches this
NUMERIC = {"float", "integer", "monetary"}
DATES = {"date", "datetime"}
RELATIONAL = {"many2one", "one2many"}
QC_TOKENS = frozenset({"qc", "quality"})


def resolve(intro: Introspect, base_model: str, binding: str, *, link: bool = False) -> tuple:
    """The leaf field of a dotted binding read from base_model, or (None, problem). Every hop must be a stored many2one.
    `link` lets a last hop that is a one2many through, which Odoo does not store (a link property; drift reads it)."""
    model, leaf = base_model, None
    segments = binding.split(".")
    for index, name in enumerate(segments):
        leaf = intro.field(model, name)
        last = index == len(segments) - 1
        if leaf is None or not (leaf.stored or (link and last and leaf.type == "one2many")):
            return None, f"{model}.{name} is missing or not stored"
        if index < len(segments) - 1:
            if leaf.type != "many2one" or leaf.relation is None:
                return None, f"{model}.{name} is {leaf.type}, so the path cannot go on"
            model = leaf.relation
    return leaf, None


def type_fits(role: str, leaf) -> bool:
    """Whether a field's type suits its property role: a measure is numeric, a time property a date, a link relational."""
    if role == "measure":
        return leaf.type in NUMERIC
    if role == "time":
        return leaf.type in DATES
    if role == "link":
        return leaf.type in RELATIONAL
    return True


def check_binding(intro: Introspect, prof: Profile, base_model: str, binding: str, role: str) -> tuple:
    """(status, fill rate, problem) of one property bound to a dotted path: matched, missing_field, type_mismatch or no_data.
    A structural match can have unknown fill. An explicitly empty model returns no_data."""
    leaf, problem = resolve(intro, base_model, binding)
    if leaf is None:
        return "missing_field", None, problem
    if not type_fits(role, leaf):
        return "type_mismatch", None, f"{leaf.model}.{leaf.name} is {leaf.type}, which a {role} cannot use"
    holder = leaf.model
    fill = prof.fill(leaf.model, leaf.name)
    if fill is None:  # a leaf on a model the profile did not read: the first hop on the base model stands in
        holder = base_model
        fill = prof.fill(base_model, binding.split(".")[0])
    if prof.row_count.get(holder) == 0:
        return "no_data", fill, f"{holder} has no rows"
    return "matched", fill, None


def _org_binding(release: Release, obj: Object, role_key: str, intro: Introspect) -> str | None:
    """The field this object reaches a location through: the first bound property that lands on the role's object
    (the compiler joins the same way, compiler._Joins._location_prop)."""
    role = next(r for r in release.org_roles if r.key == role_key)
    target = next(o.base_model for o in release.objects if o.key == role.object)
    for prop in obj.properties:
        binding = prop.bindings.get(BINDING)
        leaf = resolve(intro, obj.base_model, binding)[0] if binding else None
        if leaf is not None and leaf.relation == target:
            return binding
    return None


def _property_rows(obj: Object, release: Release, intro: Introspect, prof: Profile) -> list[dict]:
    rows = []
    for prop in obj.properties:
        if prop.org_role is not None:
            binding, role = _org_binding(release, obj, prop.org_role, intro), "dimension"
            missing = f"no field reaches a location for org role {prop.org_role}"
        else:
            binding, role, missing = prop.bindings.get(BINDING), prop.role, "no Odoo 16 binding"
        if binding is None:
            status, fill, problem = "missing_field", None, missing
        else:
            status, fill, problem = check_binding(intro, prof, obj.base_model, binding, role)
            if status == "missing_field" and prop.optional:  # 2.9f: an optional field this Odoo lacks is not a gap
                continue
        row = {"property": prop.key, "binding": binding, "status": status,
               "fill_rate": None if fill is None else round(fill, 2)}
        if problem:
            row["problem"] = problem
        rows.append(row)
    return rows


def _object_match(obj: Object, release: Release, intro: Introspect, prof: Profile) -> dict:
    rows = _property_rows(obj, release, intro, prof)
    matched = sum(row["status"] == "matched" for row in rows)
    return {"object": obj.key, "confidence": matched / len(rows) if rows else 0.0, "properties": rows}


def match_templates(templates: list[Template], release: Release, intro: Introspect, prof: Profile) -> list[dict]:
    """Every template with its objects' property statuses. Confidence is the mean of the objects' confidence; a
    template is proposed at THRESHOLD or above. Confidence is kept raw for the test and rounded for the output."""
    out = []
    for template in templates:
        objects = [_object_match(o, release, intro, prof) for o in template.objects]
        confidence = sum(o["confidence"] for o in objects) / len(objects) if objects else 0.0
        out.append({
            "template": template.name, "confidence": round(confidence, 2), "proposed": confidence >= THRESHOLD,
            "objects": [{**o, "confidence": round(o["confidence"], 2)} for o in objects],
        })
    return out


def matched_objects(results: list[dict]) -> list[str]:
    """Keys of the template objects with at least one property the steward's metadata matches (2.8c)."""
    return sorted({obj["object"] for result in results for obj in result["objects"]
                   if any(row["status"] == "matched" for row in obj["properties"])})


def orgmap_draft(intro: Introspect) -> list[dict]:
    """A draft for each active, unmapped internal or transit location. One matching rule proposes its role. No rule,
    or two or more, is ambiguous and waits for the steward. A location the Odoo steward already mapped is left out."""
    warehouses = {w["id"]: w for w in intro.warehouses}
    drafts = []
    for loc in intro.locations:
        if not loc.get("active", True) or loc.get("onto_role") or loc["usage"] not in ("internal", "transit"):
            continue
        name = loc["complete_name"]
        tokens = set(re.split(r"[^a-z0-9]+", name.lower()))
        candidates = set()
        if tokens & QC_TOKENS:
            candidates.add("qc_hold")
        if any(token.startswith(("damag", "scrap")) for token in tokens):
            candidates.add("damaged")
        if loc["usage"] == "transit" or "transit" in tokens:
            candidates.add("transit")
        warehouse = warehouses.get(loc.get("warehouse_id"))
        if warehouse and loc["usage"] == "internal" and name == f"{warehouse.get('code')}/Stock":
            candidates.add("branch")
        found = sorted(candidates)
        role = found[0] if len(found) == 1 else None
        problem = None if role else ("two rules match: " + ", ".join(found) if found else "no rule matches")
        drafts.append({"location_id": loc["id"], "complete_name": name, "proposed_role": role,
                       "confidence": 0.9 if role else 0.0, "ambiguous": role is None, "candidates": found,
                       "problem": problem})
    return drafts


# ---------------------------------------------------------------- LLM proposals (D52)


def _taken(key: str, release: Release) -> list[str]:
    keys = {n.key for group in (release.domains, release.objects, release.links, release.rules, release.metrics,
                                release.org_roles) for n in group}
    return [f"key {key!r} is already in the release"] if key in keys else []


RELEASE_KINDS = ("object", "metric", "custom_field", "property", "rule")  # kinds whose key the release holds (2.12)


def _release_keys(release: Release) -> set[str]:
    """Every key the release holds: its nodes, and each object's properties as obj.prop (a custom field's key)."""
    nodes = {n.key for group in (release.domains, release.objects, release.links, release.rules, release.metrics,
                                 release.org_roles) for n in group}
    return nodes | {f"{o.key}.{p.key}" for o in release.objects for p in o.properties}


def _already_held(entry, taken: set[str], held: set[str]) -> str | None:
    """2.12: a metric, object or custom field whose key the release or an approved proposal already holds is not offered."""
    kind = entry.get("kind") if isinstance(entry, dict) else None
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if kind not in RELEASE_KINDS or not isinstance(payload, dict):
        return None
    key = _node_key(kind, payload)
    bare = payload.get("key") if kind == "rule" else key  # a rule's key is global, like a metric's
    return f"key {key!r} is already in the release or approved" if bare in taken or key in held else None


def _known_synonym(entry, release: Release) -> str | None:
    """A term whose synonym its target already has adds nothing, so it is not offered."""
    if not isinstance(entry, dict) or entry.get("kind") != "term" or not isinstance(entry.get("payload"), dict):
        return None
    payload = entry["payload"]
    node, synonym = _node_of(release, payload.get("target")), payload.get("synonym")
    if node is None or not isinstance(synonym, str) or not hasattr(node, "synonyms"):
        return None
    known = synonym.strip().lower() in {s.lower() for s in node.synonyms}
    return f"{synonym.strip()!r} is already a synonym of {node.key}" if known else None


def _foreign_filter(entry, release: Release) -> str | None:
    """A metric filters only by rules of its own object (the rules listed for it in the input). Any other filter is not
    offered, so the metric is dropped rather than stored with a filter the release refuses."""
    if not isinstance(entry, dict) or entry.get("kind") != "metric" or not isinstance(entry.get("payload"), dict):
        return None
    payload = entry["payload"]
    filters = payload.get("filters") if isinstance(payload.get("filters"), list) else []
    owners = {rule.key: rule.object for rule in release.rules}
    for name in filters:
        if owners.get(name) != payload.get("object"):
            return f"metric {payload.get('key')!r}: filter {name!r} is not a rule of {payload.get('object')}"
    return None


def _node_of(release: Release, key):
    for group in (release.domains, release.objects, release.links, release.rules, release.metrics, release.org_roles):
        for node in group:
            if node.key == key:
                return node
    return None


def own_fields(base_model: str, paths: list[str], intro: Introspect) -> dict:
    """model -> field -> {type, relation, store} for every hop of these binding paths that the compiler field list
    lacks, read from this database's introspection."""
    known = fields_for(ODOO_16)
    own: dict = {}
    for path in paths:
        model = base_model
        for name in path.split("."):
            field = intro.models.get(model, {}).get("fields", {}).get(name) if model else None
            if field is None:
                break
            if name not in known.get(model, {}):
                own.setdefault(model, {})[name] = {"type": field.type, "relation": field.relation,
                                                   "store": field.stored}
            model = field.relation
    return own


def _links_of(node: Object, release: Release, intro: Introspect) -> tuple[Object, list[Link], list[str]]:
    """The builder never proposes links, so each link property gets one here: a many2one onto the base model of exactly
    one object (of the release, or this one) becomes an m:1 link. Any other link property (a one2many, or no target
    object) is dropped from the object, with a note. A binding that does not resolve is left for the checks."""
    keep, links, notes = [], [], []
    for prop in node.properties:
        binding = prop.bindings.get(BINDING)
        leaf = resolve(intro, node.base_model, binding, link=True)[0] if prop.role == "link" and binding else None
        if leaf is None or leaf.type not in RELATIONAL:
            keep.append(prop)
            continue
        targets = [o.key for o in [*release.objects, node] if o.base_model == leaf.relation]
        if leaf.type == "many2one" and len(targets) == 1:
            keep.append(prop)
            links.append(Link(key=f"{node.key}_{prop.key}", label=prop.label, source=node.key, target=targets[0],
                              via=prop.key, cardinality="m:1"))
        else:
            why = "a one2many" if leaf.type == "one2many" else f"no single object on {leaf.relation}"
            notes.append(f"{node.key}.{prop.key}: dropped, {why} cannot be linked")
    return node.model_copy(update={"properties": keep}), links, notes


def _check_object(payload: dict, release: Release, intro: Introspect, prof: Profile) -> tuple[dict, list[str]]:
    node = Object.model_validate({**payload, "odoo_fields": None})
    node, links, notes = _links_of(node, release, intro)
    paths = [p.bindings[BINDING] for p in node.properties if p.bindings.get(BINDING)]
    node = node.model_copy(update={"odoo_fields": own_fields(node.base_model, paths, intro) or None})
    problems = _taken(node.key, release)
    roles = {role.key for role in release.org_roles}
    unknown = sorted({p.org_role for p in node.properties if p.org_role and p.org_role not in roles})
    if not intro.has_model(node.base_model):
        problems.append(f"model {node.base_model} is not in this Odoo")
    elif unknown:
        problems.append(f"org roles {unknown} are not declared")
    else:
        problems += [f"{node.key}.{row['property']}: {row.get('problem', row['status'])}"
                     for row in _property_rows(node, release, intro, prof) if row["status"] != "matched"]
    if not problems:
        candidate = release.model_copy(update={"objects": [*release.objects, node], "links": [*release.links, *links]})
        problems += loader.problems_of(candidate)  # the release was valid, so every problem is this node's
        if not problems:
            try:
                compiler.compile_object(candidate, node.key)
            except SpecError as exc:
                problems.append(str(exc))
    # links and notes ride with the node; llm_row moves them beside it in the proposal payload
    return {**node.model_dump(mode="json"), "links": [link.model_dump(mode="json") for link in links],
            "notes": notes}, problems


def _probe(release: Release, metric: Metric) -> QuerySpec:
    """A spec any valid metric compiles under: a one-day period when a rule compares to as_of or a formula uses days, and
    a value for each param a rule names without a default. A query brings its own; the check only needs some."""
    ctx = context(release, metric.object, release.odoo_version)
    leaves, derives = expand(ctx, metric)
    values = [c.value for leaf in leaves for key in leaf.filters for c in ctx.rules[key].when if isinstance(c.value, dict)]
    params = {v["param"]: 30 for v in values if v["param"] != "as_of" and "default" not in v}
    dated = values or any("days" in formula_names(d.derived_from) for d in derives)
    period = Period(**{"from": date(2000, 12, 31), "to": date(2000, 12, 31)}) if dated else None
    return QuerySpec(release=release.digest(), object=metric.object, metric=metric.key, period=period, params=params)


def measure_key(obj: Object, value: str | None) -> str | None:
    """2.10a: the measure property of obj that a metric's measure names. A measure key is kept. An Odoo 16 field name is
    the measure whose binding is that field, since keys are for people and bindings hold the field. Any other value is
    returned as it was, and _check_metric reports it."""
    if value is None:
        return None
    measures = [p for p in obj.properties if p.role == "measure"]
    if any(p.key == value for p in measures):
        return value
    named = [p.key for p in measures if p.bindings.get(BINDING) == value]
    return named[0] if len(named) == 1 else value


def _check_metric(payload: dict, release: Release, intro: Introspect, prof: Profile) -> tuple[dict, list[str]]:
    node = Metric.model_validate(payload)
    problems = _taken(node.key, release)
    obj = next((o for o in release.objects if o.key == node.object), None)
    if obj is None:
        problems.append(f"object {node.object!r} is not in the release")
    elif not intro.has_model(obj.base_model):
        problems.append(f"model {obj.base_model} is not in this Odoo")
    else:
        node = node.model_copy(update={"measure": measure_key(obj, node.measure)})
        measures = {p.key: p for p in obj.properties if p.role == "measure"}
        prop = measures.get(node.measure)
        if node.measure is not None and prop is None:
            problems.append(f"{node.key}: measure {node.measure!r} is not a measure of {obj.key}; "
                            f"use one of {', '.join(sorted(measures))}")
        elif prop is not None and prop.org_role is None:
            status, _, problem = check_binding(intro, prof, obj.base_model, prop.bindings.get(BINDING) or "", "measure")
            if status != "matched":
                problems.append(f"{node.key}: measure {node.measure}: {problem}")
    if not problems:
        candidate = release.model_copy(update={"metrics": [*release.metrics, node]})
        problems += loader.problems_of(candidate)
        if not problems:
            try:
                compiler.compile_query(candidate, _probe(candidate, node))
            except SpecError as exc:
                problems.append(str(exc))
    return node.model_dump(mode="json"), problems


def additivity(snapshot: bool, aggregation: str | None, derived_from: str | None = None) -> dict:
    """Additivity set by code, never by the LLM (plan 1): an average, a minimum, a maximum or a ratio is non_additive; any
    other metric of a snapshot object is semi_additive and takes the last value over time; the rest are additive."""
    try:
        ratio = bool(derived_from) and formula_is_ratio(derived_from)
    except ValueError:
        ratio = False  # the metric check reports the formula
    if aggregation in ("avg", "min", "max") or ratio:
        return {"additivity": "non_additive", "over_time": None}
    if snapshot:
        return {"additivity": "semi_additive", "over_time": "last"}
    return {"additivity": "additive", "over_time": None}


def with_additivity(payload: dict, release: Release) -> dict:
    obj = next((o for o in release.objects if o.key == payload.get("object")), None)
    return {**payload, **additivity(bool(obj and obj.snapshot), payload.get("aggregation"), payload.get("derived_from"))}


PROBE = "onto_builder_probe"


def _with_node(release: Release, object_key: str, prop: Property | None = None, rule: Rule | None = None) -> Release:
    objects = [o.model_copy(update={"properties": [*o.properties, prop]}) if prop and o.key == object_key else o
               for o in release.objects]
    return release.model_copy(update={"objects": objects, "rules": [*release.rules, *([rule] if rule else [])]})


def _compiles(release: Release, metric: Metric, dims: list[str] = ()) -> list[str]:
    """[] when a probe query of this metric, grouped by dims, compiles against the release (it holds the metric)."""
    candidate = release.model_copy(update={"metrics": [*release.metrics, metric]})
    try:
        compiler.compile_query(candidate, _probe(candidate, metric).model_copy(update={"dims": list(dims)}))
    except SpecError as exc:
        return [str(exc)]
    return []


def check_property(payload: dict, release: Release, intro: Introspect, prof: Profile) -> tuple[dict, list[str]]:
    """A property for an existing object: its path resolves through the introspect with stored many2one hops, its type
    suits its role, the release with it passes, and a probe query using it compiles."""
    binding = payload.get("binding")
    prop = Property.model_validate({**{k: payload.get(k) for k in ("key", "label", "role", "unit", "synonyms")},
                                    "bindings": {BINDING: binding}})
    obj = next((o for o in release.objects if o.key == payload.get("object")), None)
    problems = []
    if obj is None:
        problems.append(f"object {payload.get('object')!r} is not in the release")
    elif any(p.key == prop.key for p in obj.properties):
        problems.append(f"{obj.key} already has property {prop.key}")
    elif any(p.bindings.get(BINDING) == binding for p in obj.properties):
        problems.append(f"{obj.key} already binds {binding}")
    else:
        leaf, problem = resolve(intro, obj.base_model, binding)
        if leaf is None:
            problems.append(problem)
        elif not type_fits(prop.role, leaf):
            problems.append(f"{leaf.model}.{leaf.name} is {leaf.type}, which a {prop.role} cannot use")
    if not problems:
        candidate = _with_node(release, obj.key, prop=prop)
        problems += loader.problems_of(candidate)
        if not problems:
            measure = prop.role == "measure"
            probe = Metric(key=PROBE, label="Probe", object=obj.key, unit="qty" if measure else "count",
                           additivity="additive", aggregation="sum" if measure else "count",
                           measure=prop.key if measure else None,
                           time_property=prop.key if prop.role == "time" else None, synonyms=["probe"])
            problems += _compiles(candidate, probe, [prop.key] if prop.role in DIM_ROLES else [])
    return prop.model_dump(mode="json"), problems


def _check_rule(payload: dict, release: Release, intro: Introspect, prof: Profile) -> tuple[dict, list[str]]:
    """A named filter on one object: its values exist in the field's selection (a flag compares to true or false), the
    release with it passes, and a count filtered by it compiles."""
    node = Rule.model_validate(payload)
    problems = _taken(node.key, release)
    obj = next((o for o in release.objects if o.key == node.object), None)
    if obj is None:
        return node.model_dump(mode="json"), [*problems, f"object {node.object!r} is not in the release"]
    props = {p.key: p for p in obj.properties}
    for condition in node.when:
        prop = props.get(condition.prop)
        binding = prop.bindings.get(BINDING) if prop is not None else None
        leaf = resolve(intro, obj.base_model, binding)[0] if binding else None
        values = condition.value if isinstance(condition.value, list) else [condition.value]
        if leaf is not None and leaf.selection:
            allowed = {item[0] if isinstance(item, (list, tuple)) else item for item in leaf.selection}
            problems += [f"{node.key}: {value!r} is not a value of {leaf.model}.{leaf.name}"
                         for value in values if not isinstance(value, dict) and value not in allowed]
        elif leaf is not None and leaf.type == "boolean":
            problems += [f"{node.key}: {condition.prop} is a flag, so it compares to true or false"
                         for value in values if not isinstance(value, bool)]
    if not problems:
        candidate = _with_node(release, obj.key, rule=node)
        problems += loader.problems_of(candidate)
        if not problems:
            probe = Metric(key=PROBE, label="Probe", object=obj.key, unit="count", additivity="additive",
                           aggregation="count", filters=[node.key], synonyms=["probe"])
            problems += _compiles(candidate, probe)
    return node.model_dump(mode="json"), problems


def _check_term(payload: dict, release: Release, intro: Introspect, prof: Profile) -> tuple[dict, list[str]]:
    target, synonym = payload.get("target"), payload.get("synonym")
    node = _node_of(release, target) if isinstance(target, str) else None
    problems = [] if node is not None else [f"target {target!r} is not a node of the release"]
    if not isinstance(synonym, str) or not synonym.strip():
        problems.append("synonym must be text")
    else:
        synonym = synonym.strip()
        if node is not None and synonym.lower() in {s.lower() for s in node.synonyms}:
            problems.append(f"{synonym!r} is already a synonym of {target}")
    return {"target": target, "synonym": synonym}, problems


def _check_custom_field(payload: dict, release: Release, intro: Introspect, prof: Profile) -> tuple[dict, list[str]]:
    prop = Property.model_validate(payload.get("property"))
    obj = next((o for o in release.objects if o.key == payload.get("object")), None)
    field = prop.bindings.get(BINDING)
    problems = []
    if prop.org_role is not None or field is None:
        problems.append("a custom field binds one Odoo 16 field, not an org role")
    if obj is None:
        problems.append(f"object {payload.get('object')!r} is not in the release")
    elif any(p.key == prop.key for p in obj.properties):
        problems.append(f"{obj.key} already has property {prop.key}")
    elif field is not None:
        if "." in field:
            problems.append(f"{field}: a custom field binds one field, not a path")
        elif not intro.has_model(obj.base_model):
            problems.append(f"model {obj.base_model} is not in this Odoo")
        else:
            status, _, problem = check_binding(intro, prof, obj.base_model, field, prop.role)
            if status != "matched":
                problems.append(f"{field}: {problem}")
    own = own_fields(obj.base_model, [field], intro) if obj is not None and field else {}
    return {"object": payload.get("object"), "property": prop.model_dump(mode="json"), "odoo_fields": own}, problems


_CHECKS = {"object": _check_object, "metric": _check_metric, "term": _check_term,
           "custom_field": _check_custom_field, "rule": _check_rule}
FLAT_KINDS = ("property", "rule")  # rows whose payload holds the node's fields, not a `node` (the Studio row contract)


def _node_key(kind, payload: dict) -> str:
    if kind == "term":
        return f"term:{payload.get('target')}:{payload.get('synonym')}"
    if kind == "custom_field":
        prop = payload.get("property")
        return f"{payload.get('object')}.{prop.get('key') if isinstance(prop, dict) else None}"
    if kind == "property":
        return f"property.{payload.get('object')}.{payload.get('key')}"
    if kind == "rule":
        return f"rule.{payload.get('key')}"
    return str(payload.get("key"))


def _check(kind, payload: dict, release: Release, intro: Introspect, prof: Profile) -> tuple[dict, list[str]]:
    checker = _CHECKS.get(kind)
    if checker is None:
        return payload, [f"kind {kind!r} is not one of {', '.join(_CHECKS)}"]
    try:
        return checker(payload, release, intro, prof)
    except ValidationError as exc:
        error = exc.errors()[0]
        return payload, [f"{'.'.join(map(str, error['loc'])) or 'payload'}: {error['msg']}"]


def _outside_fixture(payload: dict, fixture: dict) -> str | None:
    """Why an object uses a model or a bound path the compiler cannot read (2.9d), or None. Its base model must be in the
    fixture, and so must every field its bindings walk through. Custom fields are not checked here: a custom_field
    proposal is checked against the steward's introspect (2.3)."""
    model = payload.get("base_model")
    if not isinstance(model, str):
        return None
    if model not in fixture:
        return f"model {model} is not in the Odoo 16 fixture"
    props = payload.get("properties")
    for prop in props if isinstance(props, list) else []:
        bindings = prop.get("bindings") if isinstance(prop, dict) else None
        path = bindings.get(BINDING) if isinstance(bindings, dict) else None
        if isinstance(path, str):
            _, problem = loader._resolve(fixture, model, path)
            if problem:
                return problem
    return None


def out_of_scope(entry, domain: str, release: Release, fixture: dict) -> str | None:
    """Why a proposal is dropped before it is stored, or None. A key the release already has is taken (2.8c). An object
    names a domain key of the release and a model of the domain asked, a base model no template object already maps
    (2.9c), and only models and bound fields the compiler fixture holds (2.9d). A metric, rule or custom field names an
    object of that domain. Anything else, including a malformed entry, is kept, so the checks in llm_row report it."""
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if not isinstance(payload, dict):
        return None
    kind, key = entry.get("kind"), payload.get("key")
    if kind in ("object", "metric", "rule") and isinstance(key, str) and _taken(key, release):
        return f"key {key!r} is already in the release"
    if kind == "object":
        if isinstance(payload.get("domain"), str) and payload["domain"] not in [d.key for d in release.domains]:
            return f"domain {payload['domain']!r} is not a domain of the release"
        model = payload.get("base_model")
        if isinstance(model, str) and domain_of(model) != domain:
            return f"model {model} is not in the {domain} domain"
        owner = next((o.key for o in release.objects if o.base_model == model), None)
        if owner is not None:
            return f"model {model} is already covered by {owner}"
        return _outside_fixture(payload, fixture)
    if kind in ("metric", "custom_field", "rule"):
        obj = next((o for o in release.objects if o.key == payload.get("object")), None)
        if obj is None:
            return None  # an object the release lacks is for the checks to report
        prop = payload.get("property")
        if kind == "custom_field" and isinstance(prop, dict) and any(p.key == prop.get("key") for p in obj.properties):
            return f"{obj.key} already has property {prop.get('key')!r}"
        if domain_of(obj.base_model) != domain:
            return f"object {obj.key} is not in the {domain} domain"
    return None


def _owner(release: Release, model: str | None) -> str | None:
    """The key of the one release object on this base model, or None when there is none or more than one."""
    keys = [obj.key for obj in release.objects if obj.base_model == model]
    return keys[0] if len(keys) == 1 else None


def _object_named(entry) -> str | None:
    """The object a metric, rule or custom field names, or None for any other proposal."""
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if not isinstance(payload, dict) or entry.get("kind") not in ("metric", "custom_field", "rule"):
        return None
    return payload.get("object") if isinstance(payload.get("object"), str) else None


def _field_owner(entry, release: Release, intro: Introspect) -> str | None:
    """2.10c: for a custom field whose object name is unknown, the one release object whose base model holds the field's
    Odoo 16 field (the steward's introspect). None when no object, or more than one, holds it."""
    prop = entry["payload"].get("property")
    bindings = prop.get("bindings") if isinstance(prop, dict) else None
    field = bindings.get(BINDING) if isinstance(bindings, dict) else None
    if entry.get("kind") != "custom_field" or not isinstance(field, str):
        return None
    owners = [obj.key for obj in release.objects if intro.field(obj.base_model, field) is not None]
    return owners[0] if len(owners) == 1 else None


def _offered_field(entry, bases: dict[str, str], intro: Introspect) -> str | None:
    """2.11: why a custom field is dropped, or None. Its Odoo field must be a stored field of the steward's introspect on
    the base model of the object it names, the fields the Builder offered. A field the LLM guessed is dropped with this
    reason, not shown as an invalid row. An object whose base model is not known here is left to the checks."""
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None  # a malformed payload is reported by the checks
    prop = payload.get("property")
    bindings = prop.get("bindings") if isinstance(prop, dict) else None
    field = bindings.get(BINDING) if isinstance(bindings, dict) else None
    object_key = payload.get("object")
    model = bases.get(object_key) if isinstance(object_key, str) else None
    if not isinstance(field, str) or model is None:
        return None
    found = intro.field(model, field)
    if found is None or not found.stored:
        return f"{model}.{field} is not a stored field in the Odoo introspect, so the LLM guessed it"
    return None


def settle(entries: list, domain: str, release: Release, fixture: dict,
           intro: Introspect, held: set[str] = frozenset()) -> tuple[list, list[str | None]]:
    """Each proposal with its drop reason, None when kept (2.10b). out_of_scope drops a proposed object (2.9c, 2.9d). A
    metric or custom field that names a dropped object is pointed at the release object with that object's base model:
    `quants` on stock.quant becomes stock_position. A model name goes to its release object the same way (2.10c), and
    a custom field whose object name is unknown goes to the one release object that holds its field. With no such
    object it is dropped. A name that is an object kept in this call is left to the checks: the release lacks it yet.
    A custom field whose Odoo field the introspect does not hold as stored is dropped too (2.11), so the model is never
    shown an invented field as an invalid row. A metric, object or custom field whose key the release or an approved
    proposal (`held`) already holds is dropped too (2.12)."""
    taken = _release_keys(release)
    reasons = [out_of_scope(entry, domain, release, fixture) for entry in entries]
    models: dict[str, str] = {}  # key of each dropped object proposal -> its base model
    kept: set[str] = set()  # key of each object proposal kept in this call
    bases = {obj.key: obj.base_model for obj in release.objects}  # base model of each object a custom field can name
    for entry, reason in zip(entries, reasons):
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if isinstance(entry, dict) and entry.get("kind") == "object" and isinstance(payload, dict):
            key, model = payload.get("key"), payload.get("base_model")
            if isinstance(key, str) and isinstance(model, str):
                if reason is None:
                    kept.add(key)
                    bases.setdefault(key, model)
                else:
                    models[key] = model
    keys = {obj.key for obj in release.objects}
    settled, final = [], []
    for entry, reason in zip(entries, reasons):
        name = _object_named(entry)
        if reason is None and name is not None and name not in keys and name not in kept:
            owner = _owner(release, models.get(name, name))
            if owner is None and name not in models and "." not in name:  # an object key the LLM invented
                owner = _field_owner(entry, release, intro)
            if owner is None:
                reason = f"object {name!r} is not in the release, and no release object has its base model"
            else:
                entry = {**entry, "payload": {**entry["payload"], "object": owner}}
                reason = out_of_scope(entry, domain, release, fixture)
        if reason is None and isinstance(entry, dict) and entry.get("kind") == "custom_field":
            reason = _offered_field(entry, bases, intro)
        if reason is None:
            reason = _foreign_filter(entry, release)
        if reason is None:
            reason = _already_held(entry, taken, held)
        if reason is None:
            reason = _known_synonym(entry, release)
        settled.append(entry)
        final.append(reason)
    return settled, final


def llm_row(entry, domain: str, run_id: str, release: Release, intro: Introspect, prof: Profile) -> dict:
    """One LLM proposal, checked (D52). It is kept even when it fails: validation.ok is false and the problems say why.
    An LLM-only proposal is never batch-approvable (D54): the steward reviews each one. A metric's additivity is set by
    code (additivity), whatever the reply said. A rule's row holds the rule's fields (FLAT_KINDS)."""
    problems: list[str] = []
    kind = entry.get("kind") if isinstance(entry, dict) else None
    kind = kind if isinstance(kind, str) else None
    payload = entry.get("payload") if isinstance(entry, dict) else None
    confidence = entry.get("confidence") if isinstance(entry, dict) else None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        problems.append("confidence must be a number from 0 to 1")
        confidence = 0.0
    if isinstance(payload, dict):
        if kind == "metric":
            payload = with_additivity(payload, release)
        node_key = _node_key(kind, payload)
        node, found = _check(kind, payload, release, intro, prof)
        problems += found
        extra = {name: node.pop(name) for name in ("links", "notes") if kind == "object" and name in node}
    else:
        node_key, node, extra = f"unnamed:{domain}", {}, {}
        problems.append("the payload must be an object")
    ok = not problems
    if kind in FLAT_KINDS:
        body = {"kind": kind, "source": "llm", "run_id": run_id, **node, "notes": [], "batch_approvable": False}
    else:
        body = {"kind": kind, "source": "llm", "domain": domain, "run_id": run_id, "node": node,
                "batch_approvable": False, **extra}
    return {"kind": kind, "domain": domain, "node_key": node_key, "confidence": float(confidence), "payload": body,
            "validation": {"ok": ok, "problems": problems}, "batch_approvable": False}
