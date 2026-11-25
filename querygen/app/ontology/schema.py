"""Node schema of the Onto business map (plan 1.1; contracts A3.1, D5, D6, D23, D40).

Each node is a Pydantic model that forbids unknown fields. Checks that need the Odoo
field fixture (binding paths, relations, stored columns, role types) live in loader.py,
which reports every problem at once.

Bindings are dotted Odoo field paths relative to the object's base_model, keyed by Odoo
major version ("16", D6). Every hop before the last is a many2one, and every field on
the path is stored, so the compiler can join it.

Formulas (Metric.derived_from) are arithmetic over names. They are parsed, never evaluated:
  - names that are property keys of the metric's object make a row-level formula, computed
    per row and then aggregated (revenue = quantity * price);
  - names that are metric keys of the same object make a metric-level formula, computed from
    the aggregated values (margin percent = margin / net revenue);
  - `days` is reserved: the length of the requested period, supplied by the compiler (1.4).

Additivity (A3.1, Appendix R2 s.6):
  additive       sums over every dimension and over time
  semi_additive  sums over dimensions, never over time (a balance: stock, stock value)
  non_additive   never summed or counted: ratios are recomputed from their parts, averages stay averages

Condition values may be {"param": name}, where name is one of PARAMS, filled in by the compiler.
A day parameter may carry a default: {"param": name, "default": days}. Its value comes from QuerySpec.params.
"""
import ast
import hashlib
import json
import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ODOO_16 = "16"
DAY_PARAMS = frozenset({"expiry_days"})  # supplied by QuerySpec.params: a whole number of days after as_of
PARAMS = frozenset({"as_of"}) | DAY_PARAMS  # as_of is the period end, set by the compiler, never by the caller
RESERVED = frozenset({"days"})
MAX_HOPS = 3

Role = Literal["id", "dimension", "measure", "time", "status", "link"]
Unit = Literal["qty", "money", "count", "days", "ratio", "pct"]
Additivity = Literal["additive", "semi_additive", "non_additive"]
Aggregation = Literal["sum", "count", "avg", "min", "max"]
OverTime = Literal["sum", "last", "avg", "min", "max"]
Op = Literal["=", "!=", "<", "<=", ">", ">=", "in", "not in"]
Cardinality = Literal["m:1", "1:m"]

_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_DOMAIN_KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_MODEL = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_PATH = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_VERSION = re.compile(r"^[0-9]+$")

# Nodes a formula may contain. Anything else (calls, attributes, subscripts) is refused.
_FORMULA_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load,
                  ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)


def _parse_formula(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"formula {expr!r} does not parse") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError(f"formula {expr!r}: only numbers are allowed as constants")
        elif not isinstance(node, _FORMULA_NODES):
            raise ValueError(f"formula {expr!r}: {type(node).__name__} is not allowed")
    return tree


def formula_names(expr: str) -> set[str]:
    """Every name a formula refers to (property keys, metric keys, or `days`)."""
    return {node.id for node in ast.walk(_parse_formula(expr)) if isinstance(node, ast.Name)}


def formula_is_ratio(expr: str) -> bool:
    return any(isinstance(node, ast.Div) for node in ast.walk(_parse_formula(expr)))


def _key_rule(schema: dict, model: type) -> None:
    """The key pattern the validator enforces, as JSON Schema, so the Builder's tool call is held to the same rule."""
    if "key" in schema.get("properties", {}):
        schema["properties"]["key"]["pattern"] = model._key_pattern.pattern


class _Node(BaseModel):
    """Shared rules: no unknown fields, snake_case keys, unique and non-empty synonyms."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_key_rule)
    _key_pattern: ClassVar[re.Pattern] = _KEY

    @field_validator("key", check_fields=False)
    @classmethod
    def _key_format(cls, value: str) -> str:
        if not cls._key_pattern.match(value):
            raise ValueError(f"key {value!r} must match {cls._key_pattern.pattern}")
        return value

    @field_validator("synonyms", check_fields=False)
    @classmethod
    def _synonym_list(cls, values: list[str]) -> list[str]:
        clean = [value.strip() for value in values]
        if any(not value for value in clean):
            raise ValueError("a synonym is empty")
        if len({value.lower() for value in clean}) != len(clean):
            raise ValueError("a synonym is repeated")
        return clean


class Domain(_Node):
    """Taxonomy path, e.g. inventory.stock under inventory."""

    _key_pattern: ClassVar[re.Pattern] = _DOMAIN_KEY
    key: str
    label: str = Field(min_length=1)
    parent: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class Condition(_Node):
    """One comparison over a property of the same object. Conditions in a list are ANDed."""

    prop: str
    op: Op
    value: Any

    @field_validator("prop")
    @classmethod
    def _prop_format(cls, value: str) -> str:
        return _check_key(value)

    @model_validator(mode="after")
    def _value_shape(self) -> "Condition":
        if self.op in ("in", "not in"):
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"{self.prop} {self.op}: needs a non-empty list")
        elif isinstance(self.value, list):
            raise ValueError(f"{self.prop} {self.op}: a list needs the in or not in operator")
        elif isinstance(self.value, dict):
            name = self.value.get("param")
            if not (set(self.value) <= {"param", "default"}) or name not in PARAMS:
                raise ValueError(f"{self.prop}: a parameter must be {{param: one of {sorted(PARAMS)}}}, with an optional default")
            if "default" in self.value and (name not in DAY_PARAMS or not is_days(self.value["default"])):
                raise ValueError(f"{self.prop}: only a day parameter takes a default, a whole number of days")
        elif self.value is None:
            raise ValueError(f"{self.prop}: a value is required")
        return self


class Property(_Node):
    """A field with a meaning. Either bound to Odoo 16 or sourced from the steward's org map."""

    key: str
    label: str = Field(min_length=1)
    role: Role
    unit: Unit | None = None
    pii: bool = False
    bindings: dict[str, str] = Field(
        default_factory=dict,
        description='Odoo 16 field path keyed "16": a dotted path from the base model, e.g. {"16": "partner_id.name"}. '
                    "Set this, not org_role.")
    org_role: str | None = None
    # A field some Odoo installs lack (sale_margin's purchase_price). The Builder leaves the property out when this Odoo
    # does not store it. Excluded from the dump, so the release digest does not change.
    optional: bool = Field(default=False, exclude=True)
    synonyms: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_source(self) -> "Property":
        if (self.org_role is None) == (not self.bindings):
            raise ValueError(f"property {self.key}: give either Odoo bindings or an org_role, not both or neither")
        for version, path in self.bindings.items():
            if not _VERSION.match(version):
                raise ValueError(f"property {self.key}: binding version {version!r} must be digits")
            if not _PATH.match(path):
                raise ValueError(f"property {self.key}: binding path {path!r} is not a dotted field path")
        return self


class Object(_Node):
    """A business thing with an Odoo base model. Every queryable object declares base_model."""

    key: str
    label: str = Field(min_length=1)
    domain: str
    base_model: str
    # A snapshot object describes the state at the query's as_of time, so its balances
    # (semi-additive metrics) need no time property.
    snapshot: bool = False
    synonyms: list[str] = Field(min_length=1)
    base_filter: list[Condition] = Field(default_factory=list)
    properties: list[Property] = Field(default_factory=list)
    # Odoo field types this object's bindings need that the compiler field list lacks: a database's own x_ fields and
    # fields of non-standard modules, read from that database (model -> field -> {type, relation, store}). Left out of
    # the dump when empty, so a release without such fields keeps its hash.
    odoo_fields: dict[str, dict[str, dict]] | None = Field(default=None, exclude_if=lambda value: not value)

    @field_validator("domain", check_fields=False)
    @classmethod
    def _domain_format(cls, value: str) -> str:
        if not _DOMAIN_KEY.match(value):
            raise ValueError(f"domain {value!r} is not a dotted domain key")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "Object":
        if not _MODEL.match(self.base_model):
            raise ValueError(f"object {self.key}: base_model {self.base_model!r} is not an Odoo model name")
        keys = [p.key for p in self.properties]
        repeated = sorted({k for k in keys if keys.count(k) > 1})
        if repeated:
            raise ValueError(f"object {self.key}: repeated property keys {repeated}")
        for condition in self.base_filter:
            if condition.prop not in keys:
                raise ValueError(f"object {self.key}: base filter uses unknown property {condition.prop}")
        return self


class Link(_Node):
    """Object to object through one property of the source. 1:m needs an aggregate metric on the target."""

    key: str
    label: str = Field(min_length=1)
    source: str
    target: str
    via: str
    cardinality: Cardinality
    aggregate: str | None = None
    synonyms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _aggregate_only_for_one_to_many(self) -> "Link":
        if self.cardinality == "1:m" and not self.aggregate:
            raise ValueError(f"link {self.key}: a 1:m link needs a declared aggregate metric on the target")
        if self.cardinality == "m:1" and self.aggregate:
            raise ValueError(f"link {self.key}: only a 1:m link takes an aggregate")
        return self


class Rule(_Node):
    """A named, tunable predicate: conditions over one object's properties, ANDed."""

    key: str
    label: str = Field(min_length=1)
    object: str
    when: list[Condition] = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)


class Metric(_Node):
    """A measure over one object. Additivity is declared and enforced here (plan 1.1, step 2)."""

    key: str
    label: str = Field(min_length=1)
    object: str
    unit: Unit
    additivity: Additivity
    aggregation: Aggregation | None = Field(
        default=None, description="sum, count, avg, min or max. Leave it unset for a derived_from formula.")
    measure: str | None = Field(
        default=None,
        description="The property to aggregate. Required for sum, avg, min and max. A count takes no measure, and a "
                    "derived_from formula takes none either.")
    filters: list[str] = Field(default_factory=list)
    over_time: OverTime | None = None
    time_property: str | None = None
    derived_from: str | None = None
    synonyms: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape_and_additivity(self) -> "Metric":
        if self.derived_from is None:
            if self.aggregation is None:
                raise ValueError(f"metric {self.key}: needs an aggregation or a derived_from formula")
            if self.aggregation != "count" and self.measure is None:
                raise ValueError(f"metric {self.key}: needs a measure property")
        else:
            if self.measure is not None:
                raise ValueError(f"metric {self.key}: a derived_from formula takes no measure")
            formula_names(self.derived_from)  # raises on syntax the formula grammar does not allow
            if formula_is_ratio(self.derived_from) and self.additivity != "non_additive":
                raise ValueError(f"metric {self.key}: a ratio is non_additive, it cannot be summed")
        if self.aggregation == "count" and self.measure is not None:
            raise ValueError(f"metric {self.key}: a count takes no measure")

        if self.additivity == "additive":
            if self.aggregation not in ("sum", "count", None):
                raise ValueError(f"metric {self.key}: an additive metric is summed or counted")
            if self.over_time not in (None, "sum"):
                raise ValueError(f"metric {self.key}: an additive metric is summed over time")
        elif self.additivity == "semi_additive":
            if self.aggregation not in ("sum", "count", None):
                raise ValueError(f"metric {self.key}: a semi_additive metric is summed or counted over dimensions")
            if self.over_time in (None, "sum"):
                raise ValueError(
                    f"metric {self.key}: a semi_additive metric is never summed over time; "
                    "give over_time last, avg, min or max"
                )
        else:
            if self.aggregation in ("sum", "count"):
                raise ValueError(f"metric {self.key}: a non_additive metric is never summed or counted")
            if self.over_time == "sum":
                raise ValueError(f"metric {self.key}: a non_additive metric is never summed over time")
        return self


class OrgRole(_Node):
    """Closed vocabulary of a steward-set location role (onto_org_map, 1.3), joined on a location key."""

    key: str
    label: str = Field(min_length=1)
    object: str
    values: list[str] = Field(min_length=1)
    joins_on: dict[str, str] = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _values_unique(self) -> "OrgRole":
        if len(set(self.values)) != len(self.values):
            raise ValueError(f"org role {self.key}: repeated values")
        return self


class Template(_Node):
    """One canonical YAML file: core, inventory, purchase, pos, sales or org."""

    name: str
    domains: list[Domain] = Field(default_factory=list)
    objects: list[Object] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    org_roles: list[OrgRole] = Field(default_factory=list)
    # Builder notes for a path that compiles and fills but answers another question: "model.path: note" (plan 7).
    caveats: dict[str, str] = Field(default_factory=dict)


class Release(BaseModel):
    """Every node the canonical templates declare, for one Odoo version. 1.2 stores it as onto_release."""

    model_config = ConfigDict(extra="forbid")

    odoo_version: str = ODOO_16
    domains: list[Domain] = Field(default_factory=list)
    objects: list[Object] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    org_roles: list[OrgRole] = Field(default_factory=list)

    def digest(self) -> str:
        """SHA-256 of the canonical node set. Order of nodes and of list items does not change it."""
        canonical = json.dumps(_canonical(self.model_dump(mode="json")), sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_days(value: Any) -> bool:
    """A whole number of days: an int, not a bool, and not negative."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_key(value: str) -> str:
    if not _KEY.match(value):
        raise ValueError(f"key {value!r} must be snake_case")
    return value


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((_canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value
