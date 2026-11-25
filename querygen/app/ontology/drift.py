"""Drift (plan 2.5, contract 9): does every binding of the tenant's current release still exist in its Odoo?

compare() is pure: one release read against one introspect reply (2.1), both metadata only (D52). A binding is broken when
its field is missing or not stored, a hop on its path is not a many2one, its type differs from the one the Odoo 16
fixture holds, or the type no longer suits the property's role. check() runs compare for the current release and stores
the result through store.drift_sync. Nothing here reads a record value.
"""
from app.ontology import compiler, store
from app.ontology.builder import match
from app.ontology.builder.profile import InputError, Introspect
from app.ontology.schema import ODOO_16, Release, formula_names
from app.ontology.spec import SpecError, context, expand, fields_for

BINDING = match.BINDING  # the Odoo major version the bindings are keyed by (D6)


def _binding(release: Release, obj, prop, intro: Introspect) -> tuple[str, str | None]:
    """(the Odoo path this property reads, its problem or None). An org-role property reads the location field the compiler joins on."""
    if prop.org_role is not None:
        try:
            path = compiler.location_binding(release, obj.key, prop.org_role)
        except SpecError as exc:
            return f"org_role:{prop.org_role}", str(exc)
    else:
        path = prop.bindings[BINDING]
    leaf, problem = match.resolve(intro, obj.base_model, path, link=prop.role == "link")
    if leaf is None:
        return path, problem
    baseline = fields_for(ODOO_16).get(leaf.model, {}).get(leaf.name)  # the type the templates were written against
    if baseline is not None and baseline["type"] != leaf.type:
        return path, f"{leaf.model}.{leaf.name} changed type from {baseline['type']} to {leaf.type}"
    if not match.type_fits(prop.role, leaf):
        return path, f"{leaf.model}.{leaf.name} is {leaf.type}, which a {prop.role} cannot use"
    return path, None


def compare(release: Release, intro: Introspect) -> tuple[set[str], list[dict], set[str]]:
    """(checked, broken, unchecked). A node is "<object>.<property>", the key a custom-field proposal uses.

    checked: the nodes of every object whose base model this Odoo has. broken: {node, binding, problem} for each
    binding that fails. unchecked: the nodes of objects whose model is not installed. D53: a module that is not
    installed is not in use, so its bindings are neither compared nor resolved.
    """
    checked: set[str] = set()
    broken: list[dict] = []
    unchecked: set[str] = set()
    for obj in release.objects:
        if not intro.has_model(obj.base_model):
            unchecked |= {f"{obj.key}.{prop.key}" for prop in obj.properties}
            continue
        for prop in obj.properties:
            node = f"{obj.key}.{prop.key}"
            checked.add(node)
            path, problem = _binding(release, obj, prop, intro)
            if problem is not None:
                broken.append({"node": node, "binding": path, "problem": problem})
    return checked, broken, unchecked


def metric_props(release: Release, metric_key: str) -> set[str]:
    """The properties a metric reads through its leaves: measures, time properties, rule conditions and row formulas.

    The compiler's own walk (spec.expand), without a query's dimensions and filters. Suspension uses it (plan 2.5).
    """
    metric = next(m for m in release.metrics if m.key == metric_key)
    ctx = context(release, metric.object)
    leaves, _ = expand(ctx, metric)
    props: set[str] = set()
    for leaf in leaves:
        props |= {leaf.measure, leaf.time_property} - {None}
        if leaf.derived_from is not None:
            props |= formula_names(leaf.derived_from) & set(ctx.props)
        for rule_key in leaf.filters:
            props |= {cond.prop for cond in ctx.rules[rule_key].when}
    return props


def check(session, tenant_id, introspect: dict) -> dict:
    """Compare the tenant's current release with one introspect reply and store the result. Returns the route's reply.

    LookupError when no release is published. InputError when the reply is not an Odoo 16 introspect.
    """
    row = store.current_release(session, tenant_id)
    if row is None:
        raise LookupError("no release published")
    intro = Introspect(introspect)
    if not intro.odoo_version.startswith("16."):
        raise InputError(f"drift reads Odoo 16 only (D6), not {intro.odoo_version}")
    release = store.load_release(session, tenant_id, row.hash)
    checked, broken, unchecked = compare(release, intro)
    resolved = store.drift_sync(session, tenant_id, row.hash, broken, unchecked)
    return {"checked": len(checked), "broken": broken, "resolved": resolved}


def counts(reply: dict) -> dict:
    """What a job run keeps of a reply: counts only, never the node or field names."""
    return {"checked": reply["checked"], "broken": len(reply["broken"]), "resolved": reply["resolved"]}
