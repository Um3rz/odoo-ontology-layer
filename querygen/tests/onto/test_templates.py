"""Ontology node schema and canonical YAML templates (plan 1.1). Pure tests: no database, no Odoo.

Bindings are checked against app/ontology/data/odoo16_fields.json, exported read-only from a
scratch Odoo 16 database for exactly the paths the templates bind. test_fixture_holds_exactly_
the_bound_fields fails when a template binds a field the fixture lacks, or the fixture carries one
no template binds; re-export it to match.
"""
import copy
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.ontology import loader
from app.ontology.loader import TEMPLATE_DIR, TemplateError, load_fields, load_release
from app.ontology.schema import Metric, Template

FIELDS = load_fields()


def _hops(model, path):
    """(model, field) for every hop of a dotted binding path, using the fixture."""
    pairs, current = [], model
    segments = path.split(".")
    for index, name in enumerate(segments):
        pairs.append((current, name))
        if index < len(segments) - 1:
            current = FIELDS[current][name]["relation"]
    return pairs


def _templates_copy(tmp_path: Path, edits: dict[str, tuple[str, str]]) -> Path:
    """Copy the canonical templates and apply one exact-text edit per file; each old text must occur once."""
    target = tmp_path / "templates"
    shutil.copytree(TEMPLATE_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    for name, (old, new) in edits.items():
        path = target / f"{name}.yaml"
        text = path.read_text(encoding="utf-8")
        assert text.count(old) == 1, f"{name}.yaml: expected one occurrence of {old!r}"
        path.write_text(text.replace(old, new), encoding="utf-8")
    return target


def _metric(**kwargs) -> Metric:
    base = {"key": "m", "label": "M", "object": "o", "unit": "qty", "synonyms": ["m"]}
    return Metric(**(base | kwargs))


def test_canonical_templates_load():
    release = load_release()
    keys = {obj.key for obj in release.objects}
    assert {"stock_position", "stock_valuation", "purchase_order_line", "pos_order_line",
            "sales_order_line", "warehouse", "location", "product", "partner"} <= keys
    assert release.odoo_version == "16"


@pytest.mark.parametrize("name", loader.TEMPLATE_NAMES)
def test_each_template_parses_on_its_own(name):
    raw = yaml.safe_load((TEMPLATE_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    assert Template.model_validate(raw).name == name


def test_every_node_has_synonyms_and_a_single_odoo_16_binding_key():
    release = load_release()
    for obj in release.objects:
        assert obj.synonyms, obj.key
        assert obj.base_model in FIELDS, obj.key
        for prop in obj.properties:
            assert prop.synonyms, f"{obj.key}.{prop.key}"
            if prop.org_role is None:
                assert set(prop.bindings) == {"16"}, f"{obj.key}.{prop.key}"
    for metric in release.metrics:
        assert metric.synonyms, metric.key


def test_every_binding_resolves_in_the_fixture():
    release = load_release()
    for obj in release.objects:
        for prop in obj.properties:
            if prop.bindings:
                _, error = loader._resolve(FIELDS, obj.base_model, prop.bindings["16"])
                assert error is None, f"{obj.key}.{prop.key}: {error}"
    for role in release.org_roles:
        obj = next(o for o in release.objects if o.key == role.object)
        _, error = loader._resolve(FIELDS, obj.base_model, role.joins_on["16"])
        assert error is None, f"org role {role.key}: {error}"


def test_fixture_holds_exactly_the_bound_fields():
    release = load_release()
    bound = set()
    for obj in release.objects:
        for prop in obj.properties:
            if prop.bindings:
                bound |= set(_hops(obj.base_model, prop.bindings["16"]))
    for role in release.org_roles:
        obj = next(o for o in release.objects if o.key == role.object)
        bound |= set(_hops(obj.base_model, role.joins_on["16"]))
    assert {(m, f) for m, fields in FIELDS.items() for f in fields} == bound


def test_pos_till_goes_through_the_session_because_config_id_is_not_stored():
    obj = next(o for o in load_release().objects if o.key == "pos_order")
    till = next(p for p in obj.properties if p.key == "till")
    assert till.bindings == {"16": "session_id.config_id"}


def test_a_broken_binding_fails_startup(tmp_path):
    target = _templates_copy(tmp_path, {
        "inventory": ('{"16": quantity}, synonyms: [on hand', '{"16": qty_on_hand}, synonyms: [on hand'),
    })
    with pytest.raises(TemplateError, match="qty_on_hand"):
        load_release(template_dir=target)


def test_a_binding_to_a_non_stored_field_fails_startup():
    fields = copy.deepcopy(FIELDS)
    fields["stock.quant"]["quantity"]["store"] = False
    with pytest.raises(TemplateError, match="not stored"):
        load_release(fields=fields)


def test_a_binding_path_through_a_non_relational_field_fails_startup():
    fields = copy.deepcopy(FIELDS)
    fields["stock.quant"]["location_id"]["type"] = "char"
    with pytest.raises(TemplateError, match="cannot continue"):
        load_release(fields=fields)


def test_a_binding_with_the_wrong_type_for_its_role_fails_startup():
    fields = copy.deepcopy(FIELDS)
    fields["stock.quant"]["quantity"]["type"] = "char"
    with pytest.raises(TemplateError, match="measure must bind a numeric field"):
        load_release(fields=fields)


def test_a_bound_field_for_another_odoo_version_is_refused(tmp_path):
    target = _templates_copy(tmp_path, {
        "core": ('{"16": default_code}', '{"16": default_code, "17": default_code}'),
    })
    with pytest.raises(TemplateError, match="no Odoo fixture for version"):
        load_release(template_dir=target)


def test_stock_summed_over_time_fails_startup(tmp_path):
    target = _templates_copy(tmp_path, {
        "inventory": ("over_time: last, synonyms: [on hand, stock on hand, stock level, quantity on hand]",
                      "over_time: sum, synonyms: [on hand, stock on hand, stock level, quantity on hand]"),
    })
    with pytest.raises(TemplateError, match="never summed over time"):
        load_release(template_dir=target)


def test_snapshot_less_balance_without_a_time_property_fails_startup(tmp_path):
    target = _templates_copy(tmp_path, {
        "inventory": ("    snapshot: true\n    synonyms: [valuation, stock value, cost layer, inventory value]",
                      "    snapshot: false\n    synonyms: [valuation, stock value, cost layer, inventory value]"),
    })
    with pytest.raises(TemplateError, match="needs a time_property, or a snapshot object"):
        load_release(template_dir=target)


def test_semi_additive_metric_is_never_summed_over_time():
    with pytest.raises(ValidationError, match="never summed over time"):
        _metric(additivity="semi_additive", aggregation="sum", measure="x", over_time="sum")
    with pytest.raises(ValidationError, match="never summed over time"):
        _metric(additivity="semi_additive", aggregation="sum", measure="x")
    _metric(additivity="semi_additive", aggregation="sum", measure="x", over_time="last")


def test_non_additive_metric_is_never_summed_or_counted():
    with pytest.raises(ValidationError, match="never summed or counted"):
        _metric(additivity="non_additive", aggregation="sum", measure="x")
    with pytest.raises(ValidationError, match="never summed or counted"):
        _metric(additivity="non_additive", aggregation="count")
    _metric(additivity="non_additive", aggregation="avg", measure="x")


def test_ratio_must_be_non_additive():
    with pytest.raises(ValidationError, match="ratio is non_additive"):
        _metric(additivity="additive", derived_from="a / b")
    _metric(additivity="non_additive", derived_from="a / b")


def test_additive_metric_is_summed_or_counted_and_not_over_time_averaged():
    with pytest.raises(ValidationError, match="summed or counted"):
        _metric(additivity="additive", aggregation="avg", measure="x")
    with pytest.raises(ValidationError, match="summed over time"):
        _metric(additivity="additive", aggregation="sum", measure="x", over_time="last")


def test_metric_needs_a_measure_or_a_formula():
    with pytest.raises(ValidationError, match="needs a measure"):
        _metric(additivity="additive", aggregation="sum")
    with pytest.raises(ValidationError, match="needs an aggregation or a derived_from"):
        _metric(additivity="additive")


def test_formula_grammar_is_closed():
    with pytest.raises(ValidationError, match="Call is not allowed"):
        _metric(additivity="additive", aggregation="sum", derived_from="abs(x)")
    with pytest.raises(ValidationError, match="Attribute is not allowed"):
        _metric(additivity="additive", aggregation="sum", derived_from="x.y")
    with pytest.raises(ValidationError, match="does not parse"):
        _metric(additivity="additive", aggregation="sum", derived_from="a +")


def test_unknown_formula_name_fails_startup(tmp_path):
    target = _templates_copy(tmp_path, {
        "sales": ('derived_from: "quantity * price"', 'derived_from: "quantity * prize"'),
    })
    with pytest.raises(TemplateError, match="formula names"):
        load_release(template_dir=target)


def test_metric_formula_cycle_is_refused():
    cycle = [
        _metric(key="a", additivity="additive", derived_from="b + 1"),
        _metric(key="b", additivity="additive", derived_from="a + 1"),
    ]
    problems: list[str] = []
    loader._check_metric_cycles(cycle, problems)
    assert any("formula cycle" in problem for problem in problems)


def test_org_role_values_are_checked_in_rules(tmp_path):
    target = _templates_copy(tmp_path, {
        "inventory": ("value: [damaged, qc_hold, expired]}]}", "value: [damaged, qc_hold, expired_typo]}]}"),
    })
    with pytest.raises(TemplateError, match="not a value of org role location_role"):
        load_release(template_dir=target)


def test_one_to_many_link_needs_an_aggregate_of_its_target(tmp_path):
    target = _templates_copy(tmp_path, {
        "purchase": ("cardinality: \"1:m\", aggregate: supplier_lead_time_days",
                     "cardinality: \"1:m\", aggregate: po_count"),
    })
    with pytest.raises(TemplateError, match="must be a metric of supplier_info"):
        load_release(template_dir=target)


def test_many_to_one_link_must_point_at_the_target_model(tmp_path):
    target = _templates_copy(tmp_path, {
        "inventory": ("via: product, cardinality: \"m:1\", synonyms: [product of stock]",
                      "via: location, cardinality: \"m:1\", synonyms: [product of stock]"),
    })
    with pytest.raises(TemplateError, match="points to stock.location, not product.product"):
        load_release(template_dir=target)


def test_object_key_declared_twice_across_templates_fails(tmp_path):
    target = _templates_copy(tmp_path, {
        "purchase": ("  - key: purchase_order\n", "  - key: stock_position\n"),
    })
    with pytest.raises(TemplateError, match="declared more than once"):
        load_release(template_dir=target)


def test_property_key_repeated_within_an_object_fails(tmp_path):
    target = _templates_copy(tmp_path, {
        "core": ("      - {key: sales_price,", "      - {key: code,"),
    })
    with pytest.raises(TemplateError, match="repeated property keys"):
        load_release(template_dir=target)


def test_yaml_mapping_key_repeated_in_a_node_fails(tmp_path):
    target = _templates_copy(tmp_path, {
        "core": ("- {key: code, label: Internal reference", "- {key: code, key: sku, label: Internal reference"),
    })
    with pytest.raises(TemplateError, match="repeated keys"):
        load_release(template_dir=target)


def test_templates_are_read_with_a_safe_loader_only(tmp_path):
    assert issubclass(loader._UniqueKeyLoader, yaml.SafeLoader)
    target = _templates_copy(tmp_path, {
        "core": ("name: core", 'name: !!python/object/apply:os.system ["echo unsafe"]'),
    })
    with pytest.raises(TemplateError, match="constructor"):
        load_release(template_dir=target)


def test_unknown_template_file_fails_startup(tmp_path):
    target = _templates_copy(tmp_path, {})
    (target / "extra.yaml").write_text("name: extra\n", encoding="utf-8")
    with pytest.raises(TemplateError, match="not a canonical template"):
        load_release(template_dir=target)


def test_stock_is_semi_additive_and_never_summed_over_time():
    metrics = {m.key: m for m in load_release().metrics}
    for key in ("on_hand_qty", "usable_qty", "reserved_qty", "stock_value"):
        assert metrics[key].additivity == "semi_additive", key
        assert metrics[key].over_time == "last", key


def test_sales_revenue_is_quantity_times_price_and_margin_percent_is_a_ratio():
    metrics = {m.key: m for m in load_release().metrics}
    assert metrics["revenue"].derived_from == "quantity * price"
    assert metrics["margin_pct"].additivity == "non_additive"
    assert metrics["margin_pct"].derived_from == "margin / net_revenue"


def test_pos_orders_are_counted_as_sales_only_when_paid_or_done():
    obj = next(o for o in load_release().objects if o.key == "pos_order")
    assert obj.base_filter[0].prop == "order_status"
    assert obj.base_filter[0].value == ["paid", "done", "invoiced"]


MVP_METRICS = {
    # Monitoring (G1), KPIs v1 (4.5), purchasing (G1 replenishment), POS and sales (D40).
    "on_hand_qty", "usable_qty", "available_qty", "expired_qty", "near_expiry_qty", "count_accuracy",
    "count_diff_qty", "stock_value", "cogs", "turnover", "days_of_inventory", "waste_qty", "waste_value",
    "ordered_qty", "received_qty", "open_qty", "purchase_value", "supplier_lead_time_days",
    "pos_units", "pos_net_sales", "pos_gross_sales", "pos_cost", "pos_margin",
    "order_count", "units_sold", "revenue", "net_revenue", "cost_of_sales", "margin", "margin_pct",
}


def test_mvp_metrics_named_in_the_plan_are_declared():
    assert MVP_METRICS <= {m.key for m in load_release().metrics}


def test_release_digest_ignores_node_order_and_changes_with_content():
    release = load_release()
    assert release.digest() == load_release().digest()
    reordered = release.model_copy(update={"objects": list(reversed(release.objects))})
    assert reordered.digest() == release.digest()
    edited = release.model_copy(deep=True)
    edited.metrics[0].synonyms.append("another name")
    assert edited.digest() != release.digest()
