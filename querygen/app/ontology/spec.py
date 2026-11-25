"""QuerySpec (plan Part D 3.2) and its checks against a loaded release (plan 1.4).

A spec names one object, one metric, dimensions, typed filters, a period, a grain, a compare,
a top-N and paging. Every name must exist in the release, and every value is checked against
the Odoo 16 field it filters on. compiler.py renders literals only through typed_literal().
A refused spec raises SpecError and produces no output.
"""
import math
from datetime import date
from functools import lru_cache
from typing import Annotated, Any, Literal, NamedTuple, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.ontology.loader import load_compiler_fields, with_release_fields
from app.ontology.schema import ODOO_16, RESERVED, Metric, Object, Op, Property, Release, formula_names, is_days

Grain = Literal["day", "week", "month", "quarter", "year"]  # hour is out: tenant timezone (0.8), no matrix grain
GRAINS = get_args(Grain)
DIM_ROLES = frozenset({"dimension", "status", "time"})  # a time dim groups by its exact value; grain buckets it
MAX_LIMIT = 50000
ParamName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,40}$")]
FORBIDDEN_IN_VALUES = (";", "--", "/*", "*/", "\x00")
# Odoo 16 field type -> how a filter value is typed. one2many is neither filterable nor a column.
KIND = {"float": "number", "monetary": "number", "integer": "number", "boolean": "bool",
        "date": "date", "datetime": "datetime", "char": "text", "selection": "text", "text": "text",
        "html": "text", "many2one": "id"}
COLUMN_TYPE = {"float": "float", "monetary": "float", "integer": "integer", "boolean": "boolean",
               "date": "date", "datetime": "datetime", "char": "char", "selection": "char",
               "text": "char", "html": "char", "many2one": "integer"}
DECIMAL_TYPES = frozenset({"float", "monetary"})


class SpecError(ValueError):
    """A spec, or one of its values, is refused. The message names the part that failed."""


class Period(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: date = Field(alias="from")
    to: date

    @model_validator(mode="after")
    def _ordered(self) -> "Period":
        if self.from_ > self.to:
            raise ValueError("period.from is after period.to")
        return self


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prop: str
    op: Op
    value: Any


class Compare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Literal["previous_period"]


class TopN(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(ge=1, le=1000)
    by: str
    dir: Literal["asc", "desc"] = "desc"


class QuerySpec(BaseModel):
    """Part D 3.2. `release` is the onto_release hash, or "current", which the route resolves to one, so a spec is
    never compiled against another map. `params` fills the {param: name} placeholders of the metric's rules. Each
    value is a SQL bind parameter (%(name)s) and is never written into the SQL."""

    model_config = ConfigDict(extra="forbid")
    release: str = Field(pattern=r"^(current|[0-9a-f]{64})$")
    object: str
    metric: str
    dims: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    period: Period | None = None
    grain: Grain | None = None
    compare: Compare | None = None
    top_n: TopN | None = None
    limit: int = Field(default=5000, ge=1, le=MAX_LIMIT)
    page: int = Field(default=1, ge=1)
    params: dict[ParamName, int | float | str | bool] = Field(default_factory=dict)


class Ctx(NamedTuple):
    """One object of a release, with the fixture and the kind of each property resolved once."""
    release: Release
    obj: Object
    fields: dict
    props: dict[str, Property]
    kinds: dict[str, str | None]  # number, bool, date, datetime, text, id, org; None for one2many
    rules: dict[str, Any]
    roles: dict[str, Any]


class Plan(NamedTuple):
    """A checked spec: what the compiler will read, and which WHERE/CASE shape the period needs."""
    ctx: Ctx
    root: Metric
    leaves: tuple[Metric, ...]   # base and row-level metrics that become measures
    derives: tuple[Metric, ...]  # metric-level formulas, dependencies first, root last
    classes: dict[str, str]      # leaf key -> time | snapshot | none
    mode: str                    # where: period in WHERE; case: period per measure; none: no period filter
    time_prop: str | None
    decimal: bool
    tile: bool                   # a plain count total: the only shape that may use the tile path
    params: dict                 # {param: name} -> value (the spec's, else the template default); bound, never inlined


@lru_cache(maxsize=None)
def fields_for(odoo_version: str) -> dict:
    if odoo_version != ODOO_16:
        raise SpecError(f"no Odoo {odoo_version} field fixture; only {ODOO_16} is built (D6)")
    return load_compiler_fields(odoo_version)


def leaf_meta(obj: Object, prop: Property, fields: dict) -> dict:
    """The Odoo 16 fixture entry of a bound property's last field."""
    model, meta = obj.base_model, None
    for name in prop.bindings[ODOO_16].split("."):
        meta = fields[model][name]
        model = meta.get("relation")
    return meta


def _bound_kind(obj: Object, prop: Property, fields: dict) -> str | None:
    odoo_type = leaf_meta(obj, prop, fields)["type"]
    if odoo_type == "one2many":
        return None
    if odoo_type not in KIND:
        raise SpecError(f"{obj.key}.{prop.key}: Odoo type {odoo_type} is not supported")
    return KIND[odoo_type]


def context(release: Release, object_key: str, odoo_version: str = ODOO_16) -> Ctx:
    if release.odoo_version != odoo_version:
        raise SpecError(f"release is Odoo {release.odoo_version}, asked for {odoo_version}")
    fields = with_release_fields(release, fields_for(odoo_version))
    obj = next((o for o in release.objects if o.key == object_key), None)
    if obj is None:
        raise SpecError(f"unknown object {object_key!r}")
    kinds = {p.key: "org" if p.org_role else _bound_kind(obj, p, fields) for p in obj.properties}
    return Ctx(release, obj, fields, {p.key: p for p in obj.properties}, kinds,
               {r.key: r for r in release.rules}, {r.key: r for r in release.org_roles})


def typed_literal(kind: str, value: Any, allowed: frozenset | None = None) -> str:
    """The SQL literal of one value, or SpecError. Text is quoted with '' escapes after the
    forbidden-token check, so no caller text reaches SQL unescaped."""
    if kind == "bool":
        if not isinstance(value, bool):
            raise SpecError(f"expected true or false, got {value!r}")
        return "TRUE" if value else "FALSE"
    if kind in ("date", "datetime"):
        try:
            return "'" + date.fromisoformat(value).isoformat() + "'"
        except (TypeError, ValueError) as exc:
            raise SpecError(f"expected an ISO date (YYYY-MM-DD), got {value!r}") from exc
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SpecError(f"expected a finite number, got {value!r}")
        return str(value) if isinstance(value, int) else repr(float(value))
    if kind == "id":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SpecError(f"expected a record id, got {value!r}")
        return str(value)
    if kind in ("text", "org"):
        if not isinstance(value, str):
            raise SpecError(f"expected text, got {value!r}")
        if any(token in value for token in FORBIDDEN_IN_VALUES):
            raise SpecError(f"value {value!r} contains a forbidden SQL token")
        if allowed is not None and value not in allowed:
            raise SpecError(f"{value!r} is not a value of this org role")
        # % is doubled as well: Odoo runs the final SQL with a parameter dict, where a bare % starts a placeholder.
        return "'" + value.replace("'", "''").replace("%", "%%") + "'"
    raise SpecError(f"unknown value kind {kind!r}")


def expand(ctx: Ctx, metric: Metric) -> tuple[list[Metric], list[Metric]]:
    """Split a metric into the leaves that read the view and the metric-level derives over them."""
    metrics = {m.key: m for m in ctx.release.metrics}
    leaves: dict[str, Metric] = {}
    derives: list[Metric] = []

    def visit(node: Metric, trail: tuple[str, ...]) -> None:
        if node.key in trail:
            raise SpecError(f"formula cycle through {node.key}")
        names = formula_names(node.derived_from) - RESERVED if node.derived_from else set()
        if node.derived_from is None or names <= set(ctx.props):
            leaves.setdefault(node.key, node)
            return
        for name in sorted(names):
            if name not in metrics:
                raise SpecError(f"formula of {node.key} names {name!r}, which is not a metric")
            visit(metrics[name], trail + (node.key,))
        derives.append(node)

    visit(metric, ())
    return list(leaves.values()), derives


def leaf_class(obj: Object, leaf: Metric) -> str:
    if leaf.time_property:
        return "time"
    return "snapshot" if obj.snapshot else "none"


def _decimal(ctx: Ctx, leaf: Metric) -> bool:
    if leaf.derived_from is not None:
        return True
    if leaf.aggregation == "count":
        return False
    return leaf_meta(ctx.obj, ctx.props[leaf.measure], ctx.fields)["type"] in DECIMAL_TYPES


def _allowed(ctx: Ctx, prop: Property) -> frozenset | None:
    return frozenset(ctx.roles[prop.org_role].values) if prop.org_role else None


def _bind_params(root: Metric, given: dict, required: set[str], defaults: dict) -> dict:
    """The value of each day placeholder the metric's rules name: the spec's own, else the template's default.
    A spec param that no rule names is unknown. A placeholder with no default must be given (plan 1.4)."""
    missing = sorted(required - set(given))
    if missing:
        raise SpecError(f"{root.key} needs param {missing[0]!r}, which the spec does not carry")
    unknown = sorted(set(given) - required - set(defaults))
    if unknown:
        raise SpecError(f"unknown param {unknown[0]!r}: no rule of {root.key} uses it")
    bound = {name: given.get(name, defaults.get(name)) for name in sorted(required | set(defaults))}
    for name, value in bound.items():
        if not is_days(value):
            raise SpecError(f"param {name} must be a whole number of days, got {value!r}")
    return bound


def check_spec(release: Release, spec: QuerySpec) -> Plan:
    """Every name, value and additivity rule of the spec, against the release. Raises SpecError."""
    if spec.release != release.digest():
        raise SpecError("the spec names another release; compile it against the release it was written for")
    ctx = context(release, spec.object, release.odoo_version)
    obj = ctx.obj
    root = next((m for m in release.metrics if m.key == spec.metric), None)
    if root is None or root.object != obj.key:
        raise SpecError(f"unknown metric {spec.metric!r} on {obj.key}")

    if len(set(spec.dims)) != len(spec.dims):
        raise SpecError("a dimension is repeated")
    for key in spec.dims:
        prop = ctx.props.get(key)
        if prop is None:
            raise SpecError(f"unknown dimension {key!r} on {obj.key}")
        if prop.role not in DIM_ROLES or ctx.kinds[key] is None:
            raise SpecError(f"{key!r} is not a dimension (role {prop.role})")
    for filt in spec.filters:
        prop = ctx.props.get(filt.prop)
        if prop is None:
            raise SpecError(f"unknown filter field {filt.prop!r} on {obj.key}")
        if ctx.kinds[filt.prop] is None:
            raise SpecError(f"{filt.prop!r} is a one-to-many link and cannot be filtered")
        is_list = filt.op in ("in", "not in")
        if is_list != isinstance(filt.value, list) or (is_list and not filt.value):
            raise SpecError(f"{filt.prop} {filt.op}: needs {'a non-empty list' if is_list else 'one value'}")
        for value in (filt.value if is_list else [filt.value]):
            typed_literal(ctx.kinds[filt.prop], value, _allowed(ctx, prop))

    if spec.compare is not None and spec.period is None:
        raise SpecError("compare to previous_period needs a period")
    if spec.top_n is not None:
        if spec.top_n.by != root.key:
            raise SpecError(f"top_n ranks by the queried metric {root.key!r} only")
        if not spec.dims or spec.grain is not None:
            raise SpecError("top_n ranks dimension groups over one period: it needs dims and no grain")

    leaves, derives = expand(ctx, root)
    classes = {leaf.key: leaf_class(obj, leaf) for leaf in leaves}
    as_of = spec.period.to if spec.period else None
    required: set[str] = set()
    defaults: dict[str, Any] = {}
    for leaf in leaves:
        if leaf.derived_from and "days" in formula_names(leaf.derived_from):
            raise SpecError(f"{leaf.key}: days is only for metric-level formulas")
        for key in leaf.filters:
            for cond in ctx.rules[key].when:
                if isinstance(cond.value, dict):
                    if as_of is None:
                        raise SpecError(f"{leaf.key} compares to as_of, so it needs a period")
                    name = cond.value["param"]
                    if name != "as_of":
                        if "default" in cond.value:
                            defaults.setdefault(name, cond.value["default"])
                        else:
                            required.add(name)
    params = _bind_params(root, spec.params, required, defaults)
    if as_of is None and any("days" in formula_names(d.derived_from) for d in derives):
        raise SpecError(f"{root.key} uses days, so it needs a period")

    # Additivity guard (plan 1.1 step 2, 1.4 done-when): a balance is never summed over time.
    for leaf in leaves:
        cls = classes[leaf.key]
        if spec.grain is not None:
            if leaf.additivity == "semi_additive":
                raise SpecError(f"{leaf.key} is semi_additive: it is never summed over a time dimension, so it takes no grain")
            if cls != "time":
                raise SpecError(f"{leaf.key} has no time property to bucket by grain {spec.grain}")
        if spec.period is not None and cls == "none":
            raise SpecError(f"{leaf.key} has no time property, so a period cannot apply to it")
        if (leaf.additivity == "semi_additive" and cls == "time" and spec.period is not None
                and spec.period.from_ != spec.period.to):
            raise SpecError(f"{leaf.key} is semi_additive: summed over a period it is wrong; use one day")

    time_props = {leaf.time_property for leaf in leaves if classes[leaf.key] == "time"}
    if spec.grain is not None:
        if len(time_props) != 1:
            raise SpecError("a grain needs one time property shared by the metric")
        mode, time_prop = "where", next(iter(time_props))
    elif spec.period is None or all(c == "snapshot" for c in classes.values()):
        mode, time_prop = "none", None
    elif all(c == "time" for c in classes.values()) and len(time_props) == 1:
        mode, time_prop = "where", next(iter(time_props))
    else:
        mode, time_prop = "case", None

    decimal = bool(derives) or any(_decimal(ctx, leaf) for leaf in leaves)
    tile = root.derived_from is None and root.aggregation == "count" and not spec.dims and spec.grain is None
    return Plan(ctx, root, tuple(leaves), tuple(derives), classes, mode, time_prop, decimal, tile, params)
