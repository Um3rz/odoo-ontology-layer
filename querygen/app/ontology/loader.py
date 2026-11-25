"""Loads the canonical ontology templates and checks them against Odoo field metadata (plan 1.1).

`load_release()` runs at boot (app/main.py lifespan). Any problem raises TemplateError with
every problem listed, so a bad template fails startup instead of a later query. The Odoo
metadata is the committed fixture app/ontology/data/odoo16_fields.json: model -> field ->
{type, relation, store}, exported read-only from a scratch Odoo 16 database for exactly the
paths the templates bind.
"""
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.ontology.schema import MAX_HOPS, ODOO_16, RESERVED, Link, Metric, Release, Template, formula_names

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAMES = ("core", "inventory", "purchase", "pos", "sales", "org", "account", "crm")
FIXTURES = {ODOO_16: Path(__file__).parent / "data" / "odoo16_fields.json"}
# Every stored standard business field (scripts/onto_export_odoo16_catalog.py): what the Builder may propose on.
CATALOGS = {ODOO_16: Path(__file__).parent / "data" / "odoo16_catalog.json"}
NUMERIC = {"float", "integer", "monetary"}
DATES = {"date", "datetime"}
RELATIONAL = {"many2one", "one2many"}


class TemplateError(ValueError):
    """A template or binding is wrong. The message lists every problem found."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """yaml.SafeLoader that refuses a mapping with a repeated key (PyYAML would keep the last one silently)."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            keys = [self.construct_scalar(key) for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
            repeated = sorted({key for key in keys if keys.count(key) > 1})
            if repeated:
                raise yaml.constructor.ConstructorError(None, None, f"repeated keys {repeated}", node.start_mark)
        return super().construct_mapping(node, deep=deep)


@lru_cache(maxsize=None)
def _default_fields() -> dict:
    return load_fields(ODOO_16)


def load_fields(version: str = ODOO_16) -> dict[str, dict[str, dict[str, Any]]]:
    return json.loads(FIXTURES[version].read_text(encoding="utf-8"))


def load_compiler_fields(version: str = ODOO_16) -> dict[str, dict[str, dict[str, Any]]]:
    """The exported catalogue with the template fixture over it: every field the compiler may read a binding through.
    Cached (the Builder compiles a probe per candidate): callers copy before they change it."""
    return _compiler_fields(version)


@lru_cache(maxsize=None)
def _compiler_fields(version: str) -> dict[str, dict[str, dict[str, Any]]]:
    path = CATALOGS[version]
    fields = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for model, entries in load_fields(version).items():
        fields.setdefault(model, {}).update(entries)
    return fields


def with_release_fields(release: Release, fields: dict) -> dict:
    """fields plus every object's own odoo_fields. A copy: the cached field list is never changed."""
    own = [obj.odoo_fields for obj in release.objects if obj.odoo_fields]
    if not own:
        return fields
    merged = {model: dict(entries) for model, entries in fields.items()}
    for chunk in own:
        for model, entries in chunk.items():
            known = merged.setdefault(model, {})
            for name, meta in entries.items():
                known.setdefault(name, meta)
    return merged


def load_release(template_dir: Path = TEMPLATE_DIR, fields: dict | None = None) -> Release:
    """Parse every canonical template and check it. Raises TemplateError on the first failing batch of problems."""
    problems: list[str] = []
    fields = _default_fields() if fields is None else fields
    templates = _read_templates(Path(template_dir), problems)
    release = _combine(templates, problems)
    _check_release(release, fields, problems)
    if problems:
        raise TemplateError("ontology templates are invalid:\n  " + "\n  ".join(problems))
    return release


def problems_of(release: Release, fields: dict | None = None) -> list[str]:
    """Every check load_release applies to an assembled release, as messages. Empty when it is valid."""
    problems: list[str] = []
    fields = load_compiler_fields() if fields is None else fields
    _check_release(release, with_release_fields(release, fields), problems)
    return problems


def _read_templates(directory: Path, problems: list[str]) -> list[Template]:
    found = {path.stem for path in directory.glob("*.yaml")}
    for unknown in sorted(found - set(TEMPLATE_NAMES)):
        problems.append(f"{unknown}.yaml: not a canonical template (expected {', '.join(TEMPLATE_NAMES)})")
    templates: list[Template] = []
    for name in TEMPLATE_NAMES:
        path = directory / f"{name}.yaml"
        if not path.exists():
            problems.append(f"{name}.yaml: missing canonical template")
            continue
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        except yaml.YAMLError as exc:
            problems.append(f"{name}.yaml: {exc}")
            continue
        try:
            template = Template.model_validate(raw)
        except ValidationError as exc:
            problems.extend(f"{name}.yaml: {error['msg']} at {'.'.join(map(str, error['loc']))}" for error in exc.errors())
            continue
        if template.name != name:
            problems.append(f"{name}.yaml: name is {template.name!r}, the file name must match")
        templates.append(template)
    return templates


def _combine(templates: list[Template], problems: list[str]) -> Release:
    release = Release()
    for field in ("domains", "objects", "links", "rules", "metrics", "org_roles"):
        merged = [node for template in templates for node in getattr(template, field)]
        seen: set[str] = set()
        for node in merged:
            if node.key in seen:
                problems.append(f"{field}: key {node.key!r} is declared more than once")
            seen.add(node.key)
        setattr(release, field, merged)
    return release


def _resolve(fields: dict, model: str, path: str) -> tuple[dict | None, str | None]:
    """Walk a dotted path through the fixture: every field stored, every hop before the last many2one."""
    segments = path.split(".")
    if len(segments) - 1 > MAX_HOPS:
        return None, f"path {path!r} has {len(segments) - 1} joins, the limit is {MAX_HOPS}"
    current = model
    for index, name in enumerate(segments):
        if current not in fields:
            return None, f"model {current} is not in the Odoo 16 fixture"
        meta = fields[current].get(name)
        if meta is None:
            # The fixture holds only bound fields: a real but unbound field needs a re-export first.
            return None, f"{current}.{name} is not in the Odoo 16 fixture (bound fields only; re-export to add it)"
        if not meta.get("store"):
            return None, f"{current}.{name} is not stored, so it has no column to join"
        if index == len(segments) - 1:
            return {**meta, "model": current}, None
        if meta.get("type") != "many2one":
            return None, f"{current}.{name} is {meta.get('type')}, so the path cannot continue past it"
        current = meta.get("relation")
    return None, "empty path"  # unreachable: split always yields at least one segment


def _check_release(release: Release, fields: dict, problems: list[str]) -> None:
    objects = {obj.key: obj for obj in release.objects}
    domains = {domain.key for domain in release.domains}
    roles = {role.key: role for role in release.org_roles}
    rules = {rule.key: rule for rule in release.rules}
    metrics = {metric.key: metric for metric in release.metrics}

    for domain in release.domains:
        if domain.parent is not None and domain.parent not in domains:
            problems.append(f"domain {domain.key}: parent {domain.parent!r} is not a domain")
    for obj in release.objects:
        if obj.domain not in domains:
            problems.append(f"object {obj.key}: domain {obj.domain!r} is not declared")
        if obj.base_model not in fields:
            problems.append(f"object {obj.key}: base model {obj.base_model} is not in the Odoo 16 fixture")

    used_as_via = {(link.source, link.via) for link in release.links}
    for obj in release.objects:
        props = {p.key: p for p in obj.properties}
        for prop in obj.properties:
            _check_property(obj, prop, roles, fields, used_as_via, problems)
        for condition in obj.base_filter:
            _check_condition(f"object {obj.key} base filter", condition, props, roles, problems)
    for rule in release.rules:
        if rule.object not in objects:
            problems.append(f"rule {rule.key}: object {rule.object!r} is not declared")
            continue
        props = {p.key: p for p in objects[rule.object].properties}
        for condition in rule.when:
            _check_condition(f"rule {rule.key}", condition, props, roles, problems)

    for link in release.links:
        _check_link(link, objects, metrics, fields, problems)
    for role in release.org_roles:
        if role.object not in objects:
            problems.append(f"org role {role.key}: object {role.object!r} is not declared")
            continue
        _check_binding_versions(f"org role {role.key}", role.joins_on, problems)
        _check_path(f"org role {role.key}", objects[role.object].base_model, role.joins_on, fields, problems)
    for metric in release.metrics:
        _check_metric(metric, objects, rules, metrics, fields, problems)
    _check_metric_cycles(release.metrics, problems)


def _check_binding_versions(where: str, bindings: dict[str, str], problems: list[str]) -> None:
    unknown = sorted(set(bindings) - {ODOO_16})
    if unknown:
        problems.append(f"{where}: no Odoo fixture for version(s) {unknown}; only {ODOO_16} is built (D6)")
    if ODOO_16 not in bindings:
        problems.append(f"{where}: no Odoo {ODOO_16} binding")


def _check_path(where: str, model: str, bindings: dict[str, str], fields: dict, problems: list[str]) -> dict | None:
    if ODOO_16 not in bindings:
        return None
    leaf, error = _resolve(fields, model, bindings[ODOO_16])
    if error:
        problems.append(f"{where}: {error}")
    return leaf


def _check_property(obj, prop, roles, fields, used_as_via, problems: list[str]) -> None:
    where = f"property {obj.key}.{prop.key}"
    if prop.org_role is not None:
        if prop.org_role not in roles:
            problems.append(f"{where}: org role {prop.org_role!r} is not declared")
        return
    _check_binding_versions(where, prop.bindings, problems)
    leaf = _check_path(where, obj.base_model, prop.bindings, fields, problems)
    if leaf is None:
        return
    kind = leaf.get("type")
    if prop.role == "measure" and kind not in NUMERIC:
        problems.append(f"{where}: a measure must bind a numeric field, {leaf['model']} field is {kind}")
    if prop.role == "time" and kind not in DATES:
        problems.append(f"{where}: a time property must bind a date field, {leaf['model']} field is {kind}")
    if prop.role == "link" and kind not in RELATIONAL:
        problems.append(f"{where}: a link property must bind a relational field, not {kind}")
    if kind == "one2many" and (obj.key, prop.key) not in used_as_via:
        problems.append(f"{where}: a one2many property must be the via of a 1:m link")
    if prop.role == "link" and (obj.key, prop.key) not in used_as_via:
        problems.append(f"{where}: a link property is not the via of any link")


def _check_condition(where: str, condition, props: dict, roles: dict, problems: list[str]) -> None:
    prop = props.get(condition.prop)
    if prop is None:
        problems.append(f"{where}: condition uses unknown property {condition.prop!r}")
        return
    value = condition.value
    if isinstance(value, dict):
        if prop.role != "time":
            problems.append(f"{where}: parameter {value['param']!r} compares to {prop.key}, which is not a time property")
        return
    if prop.org_role is not None and prop.org_role in roles:
        allowed = set(roles[prop.org_role].values)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item not in allowed:
                problems.append(f"{where}: {item!r} is not a value of org role {prop.org_role}")


def _check_link(link: Link, objects: dict, metrics: dict, fields: dict, problems: list[str]) -> None:
    where = f"link {link.key}"
    source, target = objects.get(link.source), objects.get(link.target)
    if source is None or target is None:
        problems.append(f"{where}: source or target object is not declared")
        return
    via = next((p for p in source.properties if p.key == link.via), None)
    if via is None:
        problems.append(f"{where}: {link.source} has no property {link.via!r}")
        return
    if via.org_role is not None:
        problems.append(f"{where}: an org role property cannot be a link")
        return
    leaf = _check_path(where, source.base_model, via.bindings, fields, problems)
    if leaf is None:
        return
    wanted = "many2one" if link.cardinality == "m:1" else "one2many"
    if leaf.get("type") != wanted:
        problems.append(f"{where}: {link.cardinality} needs a {wanted} field, {link.via} is {leaf.get('type')}")
    if leaf.get("relation") != target.base_model:
        problems.append(f"{where}: {link.via} points to {leaf.get('relation')}, not {target.base_model}")
    if link.aggregate is not None:
        aggregate = metrics.get(link.aggregate)
        if aggregate is None or aggregate.object != target.key:
            problems.append(f"{where}: aggregate {link.aggregate!r} must be a metric of {target.key}")


def _check_metric(metric: Metric, objects: dict, rules: dict, metrics: dict, fields: dict, problems: list[str]) -> None:
    where = f"metric {metric.key}"
    obj = objects.get(metric.object)
    if obj is None:
        problems.append(f"{where}: object {metric.object!r} is not declared")
        return
    props = {p.key: p for p in obj.properties}
    if metric.measure is not None:
        measure = props.get(metric.measure)
        if measure is None or measure.role != "measure":
            problems.append(f"{where}: measure {metric.measure!r} is not a measure property of {obj.key}")
    for name in metric.filters:
        rule = rules.get(name)
        if rule is None or rule.object != obj.key:
            problems.append(f"{where}: filter {name!r} is not a rule of {obj.key}")
    if metric.time_property is not None:
        time_prop = props.get(metric.time_property)
        if time_prop is None or time_prop.role != "time":
            problems.append(f"{where}: time_property {metric.time_property!r} is not a time property of {obj.key}")
    if metric.additivity == "semi_additive" and metric.time_property is None and not obj.snapshot:
        problems.append(f"{where}: a semi_additive metric needs a time_property, or a snapshot object")
    if metric.derived_from is None:
        return
    names = formula_names(metric.derived_from) - RESERVED
    local_metrics = {m.key for m in metrics.values() if m.object == obj.key}
    if names & set(props) and names & local_metrics:
        problems.append(f"{where}: formula name is both a property and a metric of {obj.key}")
    if names and names <= local_metrics:
        if metric.aggregation is not None:
            problems.append(f"{where}: a metric-level formula takes no aggregation")
    elif names and names <= set(props):
        if metric.aggregation is None:
            problems.append(f"{where}: a row-level formula needs an aggregation")
        for name in names:
            if props[name].role != "measure":
                problems.append(f"{where}: formula name {name!r} must be a measure property")
    else:
        unknown = sorted(names - set(props) - local_metrics)
        problems.append(f"{where}: formula names {unknown or sorted(names)} are not properties or metrics of {obj.key}")


def _check_metric_cycles(metrics: list[Metric], problems: list[str]) -> None:
    by_key = {metric.key: metric for metric in metrics}
    done: set[str] = set()

    def visit(key: str, path: tuple[str, ...]) -> None:
        if key in path:
            problems.append(f"metric {key}: formula cycle {' -> '.join(path + (key,))}")
            return
        if key in done or key not in by_key or by_key[key].derived_from is None:
            return
        for name in formula_names(by_key[key].derived_from) - RESERVED:
            if name in by_key:
                visit(name, path + (key,))
        done.add(key)

    for metric in metrics:
        visit(metric.key, ())
