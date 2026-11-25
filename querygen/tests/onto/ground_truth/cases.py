"""Ground-truth cases for the Onto templates on the Odoo 16 seed database (plan 1.10).

Every number here comes from hand-written SQL against Odoo 16 tables. None of it is derived from the
compiler's output: the compiler is only asked to produce the SELECT that Odoo would run, and the two
results are compared row by row.

A Case is one (object, metric, dimensions, filters, period, grain, compare) combination. Its hand
SQL is generated from the HAND table below, one template per metric, plus the object's joins and
dimension expressions. Those tables are the hand SQL: read them as SQL, not as code.

Conventions for the hand side:
- A period is an inclusive pair of ISO dates. Its bounds are UTC dates, as the compiler uses.
- A balance metric (stock position, valuation) ignores the period, except through the `as_of` rule
  (expired lots), which uses the period end, as the compiler does.
- A time metric restricts rows to the period in WHERE. A metric whose leaves mix a balance with a
  period (turnover, days of inventory) applies the period inside a CASE, so the balance keeps every row.
- Totals use HAVING COUNT(*) > 0, so an empty population gives no row, as the matrix path does.

Tolerances: counts are exact. Money is +-0.01. Quantities are +-0.001. Ratios and days are +-1e-6.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# --- periods (inclusive) --------------------------------------------------------------------------
JUL = ("2026-07-01", "2026-07-31")
JUN = ("2026-06-01", "2026-06-30")
AUG = ("2026-08-01", "2026-08-21")
ASOF = ("2026-08-21", "2026-08-21")  # the seed as_of date: expiry rules compare against its end
DAY_0815 = ("2026-08-15", "2026-08-15")
DAY_0818_0821 = ("2026-08-18", "2026-08-21")
FULL = ("2025-07-01", "2026-08-21")
JUL_24 = ("2026-07-24", "2026-07-24")

# --- seed identifiers (manifest seed_manifest_odoo_onto.json, verified against odoo_onto) ----------
WH = 1  # Central Warehouse
A1, A2, A3, B1, B2 = 2, 3, 4, 5, 6  # branch warehouses
LOC_WH_STOCK, LOC_QC_HOLD, LOC_DAMAGED, LOC_EXPIRED = 8, 17, 18, 19
LOC_A1_STOCK, LOC_B1_STOCK = 22, 40
PO_P00196, PO_P00197, PO_P00198, PO_P00199 = 196, 197, 198, 199  # purchase_order ids of the planted POs


@dataclass(frozen=True)
class Case:
    id: str
    object: str
    metric: str
    dims: tuple[str, ...] = ()
    filters: tuple[tuple[str, str, object], ...] = ()  # QuerySpec filters: (prop, op, value)
    hand_where: str = ""  # the same filters written as SQL over the hand joins
    hand_measure: str = ""  # overrides the metric's hand measure (used only where the definition differs)
    period: tuple[str, str] | None = None
    grain: str | None = None
    compare: bool = False
    params: tuple[tuple[str, object], ...] = ()  # QuerySpec.params: (name, value)
    roles: str = "db"  # db: the real onto_org_map view; test_map: steward roles simulated in the test
    release: str = "templates"  # templates | no_sale_margin (unit_cost dropped, see degraded_release)
    expect: str = "match"  # match | refused | divergence
    refusal: str = ""  # substring the refusal must contain
    pins: tuple[tuple[tuple, object], ...] = ()  # ((row key), hand value) checked against the hand SQL
    note: str = ""


@dataclass(frozen=True)
class HandMetric:
    object: str
    measure: str  # SQL aggregate; {P} is the CASE period test, {DAYS} the day count
    where_time: str | None = None  # column the period filters in WHERE (time metrics)
    case_time: str | None = None  # column the period tests inside a CASE (mixed balance metrics)


# --- objects: hand FROM, base population, dimension expressions ------------------------------------
@dataclass(frozen=True)
class HandObject:
    from_: str
    base: str
    dims: dict[str, str]


POSITION = HandObject(
    from_="""FROM stock_quant q
JOIN stock_location l ON l.id = q.location_id
LEFT JOIN product_product pp ON pp.id = q.product_id
LEFT JOIN product_template pt ON pt.id = pp.product_tmpl_id
LEFT JOIN stock_lot lot ON lot.id = q.lot_id
LEFT JOIN onto_org_map om ON om.location_id = q.location_id""",
    base="l.usage IN ('internal', 'transit')",
    dims={"product": "q.product_id", "product_category": "pt.categ_id", "location": "q.location_id",
          "location_usage": "l.usage", "location_role": "om.role", "warehouse": "l.warehouse_id",
          "company": "q.company_id", "lot": "q.lot_id", "counted": "q.inventory_quantity_set"},
)
MOVE = HandObject(
    from_="""FROM stock_move m
LEFT JOIN stock_picking_type spt ON spt.id = m.picking_type_id""",
    base="m.state = 'done'",
    dims={"product": "m.product_id", "operation_code": "spt.code", "source_location": "m.location_id",
          "destination_location": "m.location_dest_id", "inventory_adjustment": "m.is_inventory",
          "scrapped": "m.scrapped", "move_status": "m.state"},
)
VALUATION = HandObject(
    from_="""FROM stock_valuation_layer v
LEFT JOIN stock_move sm ON sm.id = v.stock_move_id""",
    base="TRUE",
    dims={"product": "v.product_id", "company": "v.company_id"},
)
PICKING = HandObject(
    from_="""FROM stock_picking p
LEFT JOIN stock_picking_type spt ON spt.id = p.picking_type_id""",
    base="TRUE",
    dims={"transfer_partner": "p.partner_id", "operation_code": "spt.code", "company": "p.company_id",
          "transfer_status": "p.state", "source_location": "p.location_id",
          "destination_location": "p.location_dest_id"},
)
SCRAP = HandObject(
    from_="FROM stock_scrap s",
    base="s.state = 'done'",
    dims={"product": "s.product_id", "source_location": "s.location_id", "company": "s.company_id",
          "scrap_status": "s.state"},
)
PO_LINE = HandObject(
    from_="FROM purchase_order_line pol JOIN purchase_order po ON po.id = pol.order_id",
    base="pol.state <> 'cancel'",
    dims={"product": "pol.product_id", "supplier": "pol.partner_id", "line_status": "pol.state",
          "company": "pol.company_id"},
)
PO = HandObject(
    from_="FROM purchase_order po",
    base="TRUE",
    dims={"supplier": "po.partner_id", "buyer": "po.user_id", "po_status": "po.state",
          "company": "po.company_id"},
)
SUPPLIER_INFO = HandObject(
    from_="FROM product_supplierinfo si",
    base="TRUE",
    dims={"supplier": "si.partner_id", "product_template": "si.product_tmpl_id", "product": "si.product_id",
          "company": "si.company_id"},
)
POS_ORDER = HandObject(
    from_="FROM pos_order po LEFT JOIN pos_session ps ON ps.id = po.session_id",
    base="po.state IN ('paid', 'done', 'invoiced')",
    dims={"customer": "po.partner_id", "till": "ps.config_id", "session": "po.session_id",
          "company": "po.company_id", "order_status": "po.state"},
)
POS_LINE = HandObject(
    from_="""FROM pos_order_line pl
JOIN pos_order po ON po.id = pl.order_id
LEFT JOIN pos_session ps ON ps.id = po.session_id""",
    base="po.state IN ('paid', 'done', 'invoiced')",
    dims={"product": "pl.product_id", "till": "ps.config_id", "session": "po.session_id",
          "order_status": "po.state"},
)
POS_SESSION = HandObject(
    from_="FROM pos_session ps",
    base="TRUE",
    dims={"till": "ps.config_id", "cashier": "ps.user_id", "session_status": "ps.state"},
)
SALES_ORDER = HandObject(
    from_="FROM sale_order so",
    base="so.state IN ('sale', 'done')",
    dims={"customer": "so.partner_id", "salesperson": "so.user_id", "sales_team": "so.team_id",
          "warehouse": "so.warehouse_id", "company": "so.company_id", "order_status": "so.state"},
)
SALES_LINE = HandObject(
    from_="FROM sale_order_line sl JOIN sale_order so ON so.id = sl.order_id",
    base="so.state IN ('sale', 'done')",
    dims={"product": "sl.product_id", "customer": "sl.order_partner_id", "order_status": "so.state"},
)

OBJECTS = {
    "stock_position": POSITION, "stock_move": MOVE, "stock_valuation": VALUATION,
    "transfer": PICKING, "scrap": SCRAP, "purchase_order_line": PO_LINE, "purchase_order": PO,
    "supplier_info": SUPPLIER_INFO, "pos_order": POS_ORDER, "pos_order_line": POS_LINE,
    "pos_session": POS_SESSION, "sales_order": SALES_ORDER, "sales_order_line": SALES_LINE,
    "stock_movement_detail": HandObject(
        "FROM stock_move_line ml JOIN stock_move m ON m.id = ml.move_id", "m.state = 'done'",
        {"movement_uom": "ml.product_uom_id"}),
    "billing_document": HandObject(
        "FROM account_move am", "am.state = 'posted' AND am.move_type IN ('out_invoice','out_refund','in_invoice','in_refund')",
        {"billing_partner": "am.partner_id"}),
    "posted_payment": HandObject(
        "FROM account_payment ap JOIN account_move am ON am.id = ap.move_id", "am.state = 'posted'",
        {"payment_currency": "ap.currency_id"}),
    "crm_opportunity": HandObject(
        "FROM crm_lead lead", "lead.type = 'opportunity'", {"opportunity_team": "lead.team_id"}),
}

# --- metrics: one hand template each (all 40 template metrics) -----------------------------------
HAND: dict[str, HandMetric] = {
    # inventory: stock position (balance; as_of rules use the period end)
    "on_hand_qty": HandMetric("stock_position", "SUM(q.quantity)"),
    "reserved_qty": HandMetric("stock_position", "SUM(q.reserved_quantity)"),
    "usable_qty": HandMetric(
        "stock_position",
        "SUM(CASE WHEN (om.role IS NULL OR om.role NOT IN ('damaged', 'qc_hold', 'expired')) THEN q.quantity END)"),
    "available_qty": HandMetric("stock_position", "(SUM(q.quantity) - SUM(q.reserved_quantity))"),
    "expired_qty": HandMetric("stock_position", "SUM(CASE WHEN lot.expiration_date < %(asof)s THEN q.quantity END)"),
    "near_expiry_qty": HandMetric(
        "stock_position",
        "SUM(CASE WHEN lot.expiration_date >= %(asof)s AND lot.expiration_date <= %(asof)s + 14 THEN q.quantity END)"),
    "counted_lines": HandMetric("stock_position", "COUNT(CASE WHEN q.inventory_quantity_set = TRUE THEN q.id END)"),
    "matched_lines": HandMetric(
        "stock_position",
        "COUNT(CASE WHEN q.inventory_quantity_set = TRUE AND q.inventory_diff_quantity = 0 THEN q.id END)"),
    "count_accuracy": HandMetric(
        "stock_position",
        "(COUNT(CASE WHEN q.inventory_quantity_set = TRUE AND q.inventory_diff_quantity = 0 THEN q.id END)::numeric"
        " / NULLIF(COUNT(CASE WHEN q.inventory_quantity_set = TRUE THEN q.id END), 0))"),
    "count_diff_qty": HandMetric(
        "stock_position", "SUM(CASE WHEN q.inventory_quantity_set = TRUE THEN q.inventory_diff_quantity END)"),
    # inventory: valuation layers
    "stock_value": HandMetric("stock_valuation", "SUM(v.remaining_value)"),
    "valued_qty": HandMetric("stock_valuation", "SUM(v.remaining_qty)"),
    "cogs": HandMetric(
        "stock_valuation",
        "SUM(CASE WHEN v.quantity < 0 AND sm.scrapped IS NOT TRUE THEN -v.value END)", where_time="v.create_date"),
    "waste_value": HandMetric(
        "stock_valuation", "SUM(CASE WHEN sm.scrapped = TRUE THEN -v.value END)", where_time="v.create_date"),
    "turnover": HandMetric(
        "stock_valuation",
        "(SUM(CASE WHEN {P} AND v.quantity < 0 AND sm.scrapped IS NOT TRUE THEN -v.value END)"
        " / NULLIF(SUM(v.remaining_value), 0))",
        case_time="v.create_date"),
    "days_of_inventory": HandMetric(
        "stock_valuation",
        "(SUM(v.remaining_value) * {DAYS} / NULLIF(SUM(CASE WHEN {P} AND v.quantity < 0 AND sm.scrapped IS NOT TRUE"
        " THEN -v.value END), 0))",
        case_time="v.create_date"),
    # inventory: movements (done moves)
    "goods_received_qty": HandMetric(
        "stock_move", "SUM(CASE WHEN spt.code = 'incoming' THEN m.quantity_done END)", where_time="m.date"),
    "goods_shipped_qty": HandMetric(
        "stock_move", "SUM(CASE WHEN spt.code = 'outgoing' THEN m.quantity_done END)", where_time="m.date"),
    "open_transfer_count": HandMetric(
        "transfer", "COUNT(CASE WHEN p.state NOT IN ('done', 'cancel') THEN p.id END)",
        where_time="p.scheduled_date"),
    # inventory: waste (done scrap orders)
    "waste_qty": HandMetric("scrap", "SUM(s.scrap_qty)", where_time="s.date_done"),
    # purchasing
    "po_count": HandMetric("purchase_order", "COUNT(CASE WHEN po.state IN ('purchase','done') THEN po.id END)",
                           where_time="po.date_approve"),
    "rfq_count": HandMetric("purchase_order", "COUNT(CASE WHEN po.state IN ('draft','sent','to approve') THEN po.id END)",
                            where_time="po.date_order"),
    "ordered_qty": HandMetric("purchase_order_line", "SUM(pol.product_qty)", where_time="pol.date_planned"),
    "received_qty": HandMetric("purchase_order_line", "SUM(pol.qty_received)", where_time="pol.date_planned"),
    "open_qty": HandMetric(
        "purchase_order_line", "SUM(pol.product_qty - pol.qty_received)", where_time="pol.date_planned"),
    "purchase_value": HandMetric("purchase_order_line",
                                  "SUM(CASE WHEN pol.state IN ('purchase','done') THEN pol.price_subtotal END)",
                                  where_time="po.date_approve"),
    "supplier_lead_time_days": HandMetric("supplier_info", "AVG(si.delay)"),
    # point of sale
    "pos_order_count": HandMetric("pos_order", "COUNT(po.id)", where_time="po.date_order"),
    "pos_units": HandMetric("pos_order_line", "SUM(pl.qty)", where_time="po.date_order"),
    "pos_net_sales": HandMetric("pos_order_line", "SUM(pl.price_subtotal)", where_time="po.date_order"),
    "pos_gross_sales": HandMetric("pos_order_line", "SUM(pl.price_subtotal_incl)", where_time="po.date_order"),
    "pos_cost": HandMetric("pos_order_line", "SUM(pl.total_cost)", where_time="po.date_order"),
    "pos_margin": HandMetric("pos_order_line", "SUM(pl.price_subtotal - pl.total_cost)", where_time="po.date_order"),
    "pos_session_count": HandMetric("pos_session", "COUNT(ps.id)", where_time="ps.start_at"),
    # sales
    "order_count": HandMetric("sales_order", "COUNT(so.id)", where_time="so.date_order"),
    "units_sold": HandMetric("sales_order_line", "SUM(sl.product_uom_qty)", where_time="so.date_order"),
    "revenue": HandMetric("sales_order_line", "SUM(sl.product_uom_qty * sl.price_unit)", where_time="so.date_order"),
    "net_revenue": HandMetric("sales_order_line", "SUM(sl.price_subtotal)", where_time="so.date_order"),
    "sales_value_including_tax": HandMetric("sales_order_line", "SUM(sl.price_total)", where_time="so.date_order"),
    "moved_detail_qty": HandMetric("stock_movement_detail", "SUM(ml.qty_done)", where_time="ml.date"),
    "net_invoiced_sales": HandMetric("billing_document",
                                     "SUM(CASE WHEN am.move_type IN ('out_invoice','out_refund') THEN am.amount_untaxed_signed END)",
                                     where_time="am.invoice_date"),
    "open_receivable_balance": HandMetric("billing_document",
                                          "SUM(CASE WHEN am.move_type IN ('out_invoice','out_refund') THEN am.amount_residual_signed END)"),
    "net_vendor_bill_value": HandMetric("billing_document",
                                        "SUM(CASE WHEN am.move_type IN ('in_invoice','in_refund') THEN -am.amount_untaxed_signed END)",
                                        where_time="am.invoice_date"),
    "posted_customer_receipts": HandMetric("posted_payment",
                                           "SUM(CASE WHEN ap.payment_type = 'inbound' AND ap.partner_type = 'customer' THEN ap.amount END)",
                                           where_time="am.date"),
    "active_opportunity_count": HandMetric("crm_opportunity", "COUNT(CASE WHEN lead.active THEN lead.id END)"),
}

# Object base populations, for the per-object view check (base table rows after the base filter).
OBJECT_BASE_COUNT = {
    "company": "SELECT count(*) FROM res_company",
    "warehouse": "SELECT count(*) FROM stock_warehouse",
    "location": "SELECT count(*) FROM stock_location",
    "product": "SELECT count(*) FROM product_product pp JOIN product_template pt ON pt.id = pp.product_tmpl_id"
               " WHERE pt.type = 'product'",
    "product_category": "SELECT count(*) FROM product_category",
    "partner": "SELECT count(*) FROM res_partner",
    "stock_position": "SELECT count(*) FROM stock_quant q JOIN stock_location l ON l.id = q.location_id"
                      " WHERE l.usage IN ('internal', 'transit')",
    "stock_lot": "SELECT count(*) FROM stock_lot",
    "stock_move": "SELECT count(*) FROM stock_move WHERE state = 'done'",
    "stock_valuation": "SELECT count(*) FROM stock_valuation_layer",
    "transfer": "SELECT count(*) FROM stock_picking",
    "scrap": "SELECT count(*) FROM stock_scrap WHERE state = 'done'",
    "purchase_order": "SELECT count(*) FROM purchase_order",
    "purchase_order_line": "SELECT count(*) FROM purchase_order_line WHERE state <> 'cancel'",
    "supplier_info": "SELECT count(*) FROM product_supplierinfo",
    "pos_order": "SELECT count(*) FROM pos_order WHERE state IN ('paid', 'done', 'invoiced')",
    "pos_order_line": "SELECT count(*) FROM pos_order_line pl JOIN pos_order po ON po.id = pl.order_id"
                      " WHERE po.state IN ('paid', 'done', 'invoiced')",
    "pos_session": "SELECT count(*) FROM pos_session",
    "sales_order": "SELECT count(*) FROM sale_order WHERE state IN ('sale', 'done')",
    "sales_order_line": "SELECT count(*) FROM sale_order_line sl JOIN sale_order so ON so.id = sl.order_id"
                        " WHERE so.state IN ('sale', 'done')",
    "stock_movement_detail": "SELECT count(*) FROM stock_move_line ml JOIN stock_move m ON m.id = ml.move_id WHERE m.state = 'done'",
    "billing_document": "SELECT count(*) FROM account_move WHERE state = 'posted'"
                        " AND move_type IN ('out_invoice','out_refund','in_invoice','in_refund')",
    "billing_line": "SELECT count(*) FROM account_move_line l JOIN account_move m ON m.id = l.move_id"
                    " WHERE m.state = 'posted' AND m.move_type IN ('out_invoice','out_refund','in_invoice','in_refund')"
                    " AND l.display_type = 'product'",
    "posted_payment": "SELECT count(*) FROM account_payment p JOIN account_move m ON m.id = p.move_id WHERE m.state = 'posted'",
    "accounting_reconciliation": "SELECT count(*) FROM account_partial_reconcile",
    "crm_opportunity": "SELECT count(*) FROM crm_lead WHERE type = 'opportunity'",
}
# The sales object is provisioned without the sale_margin cost column (see degraded_release).
OBJECT_RELEASE = {"sales_order_line": "no_sale_margin"}

# Metrics that the degraded release drops: the cost column is absent on odoo_onto (sale_margin not installed).
DROPPED_PROPERTIES = {("sales_order_line", "unit_cost")}


def hand_period(period: tuple[str, str] | None) -> dict:
    """Parameters for an inclusive period: d0 start, d1 exclusive end, asof = end, days = length."""
    if period is None:
        return {}
    from datetime import date, timedelta

    start, end = date.fromisoformat(period[0]), date.fromisoformat(period[1])
    return {"d0": start, "d1": end + timedelta(days=1), "asof": end, "days": (end - start).days + 1}


def previous_period(period: tuple[str, str]) -> tuple[str, str]:
    from datetime import date, timedelta

    start, end = date.fromisoformat(period[0]), date.fromisoformat(period[1])
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return ((prev_end - timedelta(days=length - 1)).isoformat(), prev_end.isoformat())


def hand_sql(case: Case, period: tuple[str, str] | None) -> tuple[str, dict]:
    """The hand SQL of one case for one period. Returns (sql, params) for a psycopg2 cursor."""
    metric = HAND[case.metric]
    obj = OBJECTS[metric.object]
    params = hand_period(period)
    period_cond = "TRUE"
    if period is not None and metric.case_time:
        period_cond = f"({metric.case_time} >= %(d0)s AND {metric.case_time} < %(d1)s)"
    template = case.hand_measure or metric.measure
    measure = template.replace("{P}", period_cond).replace("{DAYS}", str(params.get("days", 0)))
    where = [obj.base]
    if case.hand_where:
        where.append(case.hand_where)
    if period is not None and metric.where_time:
        where.append(f"{metric.where_time} >= %(d0)s AND {metric.where_time} < %(d1)s")
    select = [f"{obj.dims[dim]} AS d{i}" for i, dim in enumerate(case.dims)]
    if case.grain:
        select.append(f"date_trunc('{case.grain}', {metric.where_time})::date AS bucket")
    group = [str(i + 1) for i in range(len(select))]
    sql = f"SELECT {', '.join(select + [measure + ' AS value'])}\n{obj.from_}\nWHERE " + "\n  AND ".join(
        f"({w})" for w in where)
    if group:
        sql += "\nGROUP BY " + ", ".join(group)  # a total has no GROUP BY: one row, COUNT 0 or SUM NULL when empty
    return sql, params


# --- cases ----------------------------------------------------------------------------------------
def C(id, object, metric, **kw) -> Case:  # noqa: N802 - short constructor for the table below
    return Case(id=id, object=object, metric=metric, **kw)


CASES: list[Case] = [
    # ---- stock position: on hand (balance, no period) ----
    C("on_hand_qty.total", "stock_position", "on_hand_qty"),
    C("on_hand_qty.by_product", "stock_position", "on_hand_qty", dims=("product",)),
    C("on_hand_qty.by_product_category", "stock_position", "on_hand_qty", dims=("product_category",)),
    C("on_hand_qty.by_warehouse", "stock_position", "on_hand_qty", dims=("warehouse",)),
    C("on_hand_qty.by_location", "stock_position", "on_hand_qty", dims=("location",)),
    C("on_hand_qty.by_location_role", "stock_position", "on_hand_qty", dims=("location_role",)),
    C("on_hand_qty.by_warehouse_x_category", "stock_position", "on_hand_qty",
      dims=("warehouse", "product_category")),
    C("on_hand_qty.qc_hold_product155", "stock_position", "on_hand_qty",
      filters=(("location", "=", LOC_QC_HOLD), ("product", "=", 155)),
      hand_where=f"q.location_id = {LOC_QC_HOLD} AND q.product_id = 155",
      note="planted QC-hold stock: 30 units of product 155 in WH/QC Hold"),
    C("on_hand_qty.negative_damaged_product140", "stock_position", "on_hand_qty",
      filters=(("location", "=", LOC_DAMAGED), ("product", "=", 140)),
      hand_where=f"q.location_id = {LOC_DAMAGED} AND q.product_id = 140",
      note="planted negative quant: -5 of product 140 in WH/Damaged"),
    C("on_hand_qty.overstock_product145_central", "stock_position", "on_hand_qty",
      filters=(("location", "=", LOC_WH_STOCK), ("product", "=", 145)),
      hand_where=f"q.location_id = {LOC_WH_STOCK} AND q.product_id = 145",
      note="planted overstock: 2000 units of product 145 in WH/Stock"),
    C("on_hand_qty.dead_stock_product150", "stock_position", "on_hand_qty",
      filters=(("product", "=", 150),), hand_where="q.product_id = 150",
      note="planted dead stock: 400 units of product 150, last move 2025-07-12"),
    C("on_hand_qty.role_branch_empty_when_unmapped", "stock_position", "on_hand_qty",
      filters=(("location_role", "in", ["branch"]),), hand_where="om.role IN ('branch')",
      note="no org roles on odoo_onto: a branch-role filter is empty"),
    C("on_hand_qty.role_not_damaged_counts_unmapped", "stock_position", "on_hand_qty",
      filters=(("location_role", "not in", ["damaged"]),), hand_where="(om.role IS NULL OR om.role NOT IN ('damaged'))",
      note="D43: an unmapped (NULL) role counts as usable, so a not-in filter keeps every row"),
    C("on_hand_qty.grain_refused", "stock_position", "on_hand_qty", grain="month", expect="refused",
      refusal="semi_additive", note="semi-additive: never summed over a time grain"),

    # ---- reserved, usable, available ----
    C("reserved_qty.total", "stock_position", "reserved_qty",
      note="weak: reserved_quantity is 0 on every quant of the seed, so this checks the SQL shape only"),
    C("reserved_qty.by_warehouse", "stock_position", "reserved_qty", dims=("warehouse",)),
    C("usable_qty.total", "stock_position", "usable_qty",
      note="D43: with no org roles every NULL role is usable, so usable equals on hand"),
    C("usable_qty.by_warehouse", "stock_position", "usable_qty", dims=("warehouse",)),
    C("usable_qty.total_test_map", "stock_position", "usable_qty", roles="test_map",
      note="steward roles simulated in the test: QC hold, damaged and expired excluded"),
    C("usable_qty.by_location_role_test_map", "stock_position", "usable_qty", dims=("location_role",),
      roles="test_map"),
    C("usable_qty.qc_hold_product155_test_map", "stock_position", "usable_qty", roles="test_map",
      filters=(("product", "=", 155),), hand_where="q.product_id = 155",
      note="planted QC hold: the 30 units in WH/QC Hold are not usable"),
    C("available_qty.total", "stock_position", "available_qty", note="derived: on_hand_qty - reserved_qty"),
    C("available_qty.by_warehouse", "stock_position", "available_qty", dims=("warehouse",)),

    # ---- expiry (as_of = period end) ----
    C("expired_qty.total_asof", "stock_position", "expired_qty", period=ASOF),
    C("expired_qty.by_product_asof", "stock_position", "expired_qty", dims=("product",), period=ASOF),
    C("expired_qty.soon_lot_product157_asof", "stock_position", "expired_qty", period=ASOF,
      filters=(("product", "=", 157),), hand_where="q.product_id = 157",
      note="planted expiring lot SOON-260821 expires 2026-08-26, after the as_of date: not expired yet"),
    C("near_expiry_qty.total_asof", "stock_position", "near_expiry_qty", period=ASOF, params=(("expiry_days", 14),),
      note="expiry_days 14 is bound as a parameter; planted lot SOON-260821 (expires 2026-08-26) is inside the window"),
    C("near_expiry_qty.by_warehouse_asof", "stock_position", "near_expiry_qty", dims=("warehouse",), period=ASOF,
      params=(("expiry_days", 14),)),

    # ---- count accuracy ----
    C("counted_lines.total", "stock_position", "counted_lines"),
    C("counted_lines.by_warehouse", "stock_position", "counted_lines", dims=("warehouse",)),
    C("matched_lines.total", "stock_position", "matched_lines",
      note="planted count discrepancy: both counted lines differ by -17, so none matches"),
    C("matched_lines.by_product", "stock_position", "matched_lines", dims=("product",)),
    C("count_accuracy.total", "stock_position", "count_accuracy"),
    C("count_accuracy.by_warehouse", "stock_position", "count_accuracy", dims=("warehouse",),
      note="warehouses with no counted line give a NULL ratio (zero denominator)"),
    C("count_diff_qty.total", "stock_position", "count_diff_qty"),
    C("count_diff_qty.by_product", "stock_position", "count_diff_qty", dims=("product",)),

    # ---- valuation ----
    C("stock_value.total", "stock_valuation", "stock_value"),
    C("stock_value.by_product", "stock_valuation", "stock_value", dims=("product",)),
    C("stock_value.by_company", "stock_valuation", "stock_value", dims=("company",)),
    C("stock_value.product145_cash_tied", "stock_valuation", "stock_value",
      filters=(("product", "=", 145),), hand_where="v.product_id = 145",
      note="planted overstock cash: 2133 units of product 145 valued at 11.2 each"),
    C("valued_qty.total", "stock_valuation", "valued_qty",
      note="cross-check: valued quantity equals on-hand quantity for the whole seed"),
    C("valued_qty.by_product", "stock_valuation", "valued_qty", dims=("product",)),
    C("cogs.total", "stock_valuation", "cogs",
      note="outgoing layers, scrapped layers excluded (Odoo 16 posts scrap to the location valuation account)"),
    C("cogs.period_jul", "stock_valuation", "cogs", period=JUL),
    C("cogs.period_aug_by_product", "stock_valuation", "cogs", dims=("product",), period=AUG),
    C("cogs.grain_month_full", "stock_valuation", "cogs", period=FULL, grain="month"),
    C("waste_value.total", "stock_valuation", "waste_value", note="scrapped layers only, valued at -value"),
    C("waste_value.period_jul", "stock_valuation", "waste_value", period=JUL),
    C("waste_value.by_product", "stock_valuation", "waste_value", dims=("product",)),
    C("turnover.period_jul", "stock_valuation", "turnover", period=JUL,
      note="derived: cogs / stock_value; stock_value is the current balance"),
    C("turnover.period_jul_by_product", "stock_valuation", "turnover", dims=("product",), period=JUL,
      note="products with no July cogs give a NULL turnover (zero denominator)"),
    C("days_of_inventory.period_jul", "stock_valuation", "days_of_inventory", period=JUL,
      note="derived: stock_value * days / cogs with days = 31"),
    C("days_of_inventory.period_aug", "stock_valuation", "days_of_inventory", period=AUG,
      note="derived: days = 21"),
    C("days_of_inventory.period_jul_by_product", "stock_valuation", "days_of_inventory", dims=("product",), period=JUL,
      note="products with no July cogs give a NULL days value (zero denominator)"),

    # ---- stock movements (done moves) ----
    C("goods_received_qty.total", "stock_move", "goods_received_qty"),
    C("goods_received_qty.period_jul", "stock_move", "goods_received_qty", period=JUL),
    C("goods_received_qty.by_product_jul", "stock_move", "goods_received_qty", dims=("product",), period=JUL),
    C("goods_received_qty.by_source_location_jul", "stock_move", "goods_received_qty",
      dims=("source_location",), period=JUL),
    C("goods_received_qty.product121_wrong_product_receipt", "stock_move", "goods_received_qty",
      filters=(("product", "=", 121),), hand_where="m.product_id = 121",
      note="planted wrong-product receipt: product 121 arrived on PO P00198, which ordered product 148"),
    C("goods_received_qty.grain_month_full", "stock_move", "goods_received_qty", period=FULL, grain="month"),
    C("goods_shipped_qty.total", "stock_move", "goods_shipped_qty"),
    C("goods_shipped_qty.period_jul", "stock_move", "goods_shipped_qty", period=JUL),
    C("goods_shipped_qty.by_product_jul", "stock_move", "goods_shipped_qty", dims=("product",), period=JUL),
    C("goods_shipped_qty.by_destination_location_jul", "stock_move", "goods_shipped_qty",
      dims=("destination_location",), period=JUL),
    C("goods_shipped_qty.product143_b1_abnormal", "stock_move", "goods_shipped_qty",
      filters=(("product", "=", 143), ("source_location", "=", LOC_B1_STOCK)),
      hand_where=f"m.product_id = 143 AND m.location_id = {LOC_B1_STOCK}", period=DAY_0818_0821,
      note="planted abnormal consumption: product 143 at B1 from 2026-08-18"),
    C("open_transfer_count.total", "transfer", "open_transfer_count",
      note="two receipts are assigned (open); the rest are done or cancelled"),
    C("open_transfer_count.by_operation_code", "transfer", "open_transfer_count", dims=("operation_code",)),
    C("open_transfer_count.scheduled_on_asof_day", "transfer", "open_transfer_count", period=ASOF,
      note="definition: counts open transfers scheduled on the day (not open as of the day); see FINDINGS"),

    # ---- waste ----
    C("waste_qty.total", "scrap", "waste_qty"),
    C("waste_qty.period_jul", "scrap", "waste_qty", period=JUL),
    C("waste_qty.by_product_jul", "scrap", "waste_qty", dims=("product",), period=JUL),
    C("waste_qty.by_source_location_jul", "scrap", "waste_qty", dims=("source_location",), period=JUL),
    C("waste_qty.a1_spike_jul", "scrap", "waste_qty", period=JUL,
      filters=(("source_location", "=", LOC_A1_STOCK),), hand_where=f"s.location_id = {LOC_A1_STOCK}",
      note="planted waste spike at A1/Stock on 2026-07-24"),
    C("waste_qty.by_company", "scrap", "waste_qty", dims=("company",)),

    # ---- purchasing ----
    C("po_count.total", "purchase_order", "po_count"),
    C("po_count.period_jul_by_supplier", "purchase_order", "po_count", dims=("supplier",), period=JUL),
    C("po_count.by_buyer", "purchase_order", "po_count", dims=("buyer",)),
    C("ordered_qty.total", "purchase_order_line", "ordered_qty"),
    C("ordered_qty.period_jul_by_supplier", "purchase_order_line", "ordered_qty", dims=("supplier",), period=JUL),
    C("ordered_qty.by_product", "purchase_order_line", "ordered_qty", dims=("product",)),
    C("received_qty.total", "purchase_order_line", "received_qty"),
    C("received_qty.by_product_jul", "purchase_order_line", "received_qty", dims=("product",), period=JUL),
    C("open_qty.total_template_net", "purchase_order_line", "open_qty",
      note="the template contract: ordered - received summed over lines, over-receipts net off"),
    C("open_qty.total_outstanding_clamped", "purchase_order_line", "open_qty", expect="divergence",
      hand_measure="SUM(GREATEST(pol.product_qty - pol.qty_received, 0))",
      note="Odoo semantics: outstanding supply per line is never negative, so P00197's -12 counts as 0; "
           "the template cannot clamp (FINDINGS F2)"),
    C("open_qty.by_supplier", "purchase_order_line", "open_qty", dims=("supplier",)),
    C("open_qty.partial_receipt_P00196", "purchase_order_line", "open_qty",
      filters=(("order", "=", PO_P00196),), hand_where=f"pol.order_id = {PO_P00196}",
      note="planted partial receipt: 120 ordered, 70 received"),
    C("open_qty.over_receipt_P00197", "purchase_order_line", "open_qty",
      filters=(("order", "=", PO_P00197),), hand_where=f"pol.order_id = {PO_P00197}",
      note="planted over-receipt: 100 ordered, 112 received, so the net open quantity is -12"),
    C("open_qty.late_po_P00199_on_expected_day", "purchase_order_line", "open_qty", period=DAY_0815,
      filters=(("order", "=", PO_P00199),), hand_where=f"pol.order_id = {PO_P00199}",
      note="planted late PO: 600 of product 142 expected 2026-08-15, nothing received"),
    C("purchase_value.total", "purchase_order_line", "purchase_value"),
    C("purchase_value.period_jul", "purchase_order_line", "purchase_value", period=JUL),
    C("purchase_value.period_jul_by_supplier", "purchase_order_line", "purchase_value", dims=("supplier",), period=JUL),
    C("supplier_lead_time_days.total", "supplier_info", "supplier_lead_time_days"),
    C("supplier_lead_time_days.by_supplier", "supplier_info", "supplier_lead_time_days", dims=("supplier",)),
    C("supplier_lead_time_days.by_product_template", "supplier_info", "supplier_lead_time_days",
      dims=("product_template",)),

    # ---- point of sale ----
    C("pos_order_count.total", "pos_order", "pos_order_count"),
    C("pos_order_count.period_jul", "pos_order", "pos_order_count", period=JUL),
    C("pos_order_count.period_jul_by_till", "pos_order", "pos_order_count", dims=("till",), period=JUL),
    C("pos_order_count.by_customer", "pos_order", "pos_order_count", dims=("customer",),
      note="no POS order has a customer on the seed, so every order is one NULL group"),
    C("pos_units.total", "pos_order_line", "pos_units"),
    C("pos_units.period_jul_by_till", "pos_order_line", "pos_units", dims=("till",), period=JUL),
    C("pos_units.by_product_jul", "pos_order_line", "pos_units", dims=("product",), period=JUL),
    C("pos_net_sales.total", "pos_order_line", "pos_net_sales"),
    C("pos_net_sales.period_jul", "pos_order_line", "pos_net_sales", period=JUL),
    C("pos_net_sales.period_jul_by_till", "pos_order_line", "pos_net_sales", dims=("till",), period=JUL),
    C("pos_net_sales.grain_week_jul", "pos_order_line", "pos_net_sales", period=JUL, grain="week"),
    C("pos_net_sales.grain_month_full", "pos_order_line", "pos_net_sales", period=FULL, grain="month"),
    C("pos_gross_sales.total", "pos_order_line", "pos_gross_sales"),
    C("pos_gross_sales.period_jul_vs_june", "pos_order_line", "pos_gross_sales", period=JUL, compare=True,
      note="compare: the previous-period SQL is checked too; Odoo merges the two results"),
    C("pos_gross_sales.by_session_jul", "pos_order_line", "pos_gross_sales", dims=("session",), period=JUL),
    C("pos_cost.total", "pos_order_line", "pos_cost",
      note="no data: total_cost is NULL on every POS line of the seed, so the expected value is NULL"),
    C("pos_cost.period_jul", "pos_order_line", "pos_cost", period=JUL,
      note="no data (see pos_cost.total)"),
    C("pos_margin.total", "pos_order_line", "pos_margin",
      note="no data: depends on total_cost, which is NULL on every POS line"),
    C("pos_session_count.total", "pos_session", "pos_session_count"),
    C("pos_session_count.period_jul", "pos_session", "pos_session_count", period=JUL),
    C("pos_session_count.period_jul_by_till", "pos_session", "pos_session_count", dims=("till",), period=JUL),
    C("pos_session_count.period_jul_by_cashier", "pos_session", "pos_session_count", dims=("cashier",), period=JUL),

    # ---- sales ----
    C("order_count.total", "sales_order", "order_count"),
    C("order_count.period_jul", "sales_order", "order_count", period=JUL),
    C("order_count.period_jul_by_salesperson", "sales_order", "order_count", dims=("salesperson",), period=JUL),
    C("order_count.period_jul_by_sales_team", "sales_order", "order_count", dims=("sales_team",), period=JUL),
    C("order_count.by_warehouse", "sales_order", "order_count", dims=("warehouse",)),
    C("units_sold.total", "sales_order_line", "units_sold", release="no_sale_margin"),
    C("units_sold.period_jul_by_product", "sales_order_line", "units_sold", dims=("product",), period=JUL,
      release="no_sale_margin"),
    C("units_sold.period_jul_by_customer", "sales_order_line", "units_sold", dims=("customer",), period=JUL,
      release="no_sale_margin"),
    C("revenue.total", "sales_order_line", "revenue", release="no_sale_margin",
      note="quantity x price, before the line discount (D40); see net_revenue for the discounted total"),
    C("revenue.period_jul", "sales_order_line", "revenue", period=JUL, release="no_sale_margin"),
    C("revenue.period_jul_by_product", "sales_order_line", "revenue", dims=("product",), period=JUL,
      release="no_sale_margin"),
    C("revenue.period_jul_by_customer", "sales_order_line", "revenue", dims=("customer",), period=JUL,
      release="no_sale_margin"),
    C("net_revenue.total", "sales_order_line", "net_revenue", release="no_sale_margin",
      note="price_subtotal: after the 37 discounted lines"),
    C("net_revenue.period_jul", "sales_order_line", "net_revenue", period=JUL, release="no_sale_margin"),
    C("net_revenue.period_jul_by_product", "sales_order_line", "net_revenue", dims=("product",), period=JUL,
      release="no_sale_margin"),
    C("net_revenue.grain_month_full", "sales_order_line", "net_revenue", period=FULL, grain="month",
      release="no_sale_margin"),
    C("cost_of_sales.total_refused", "sales_order_line", "cost_of_sales", release="no_sale_margin",
      expect="refused", refusal="unknown metric",
      note="GAP: needs sale_margin (sale_order_line.purchase_price), which odoo_onto does not have"),
    C("margin.total_refused", "sales_order_line", "margin", release="no_sale_margin", expect="refused",
      refusal="unknown metric", note="GAP: depends on the sale_margin unit cost"),
    C("margin_pct.total_refused", "sales_order_line", "margin_pct", release="no_sale_margin", expect="refused",
      refusal="unknown metric", note="GAP: depends on the sale_margin unit cost"),
    C("rfq_count.total", "purchase_order", "rfq_count"),
    C("rfq_count.by_supplier", "purchase_order", "rfq_count", dims=("supplier",)),
    C("sales_value_including_tax.total", "sales_order_line", "sales_value_including_tax", release="no_sale_margin"),
    C("sales_value_including_tax.by_product", "sales_order_line", "sales_value_including_tax",
      dims=("product",), release="no_sale_margin"),
    C("moved_detail_qty.total", "stock_movement_detail", "moved_detail_qty"),
    C("moved_detail_qty.by_unit", "stock_movement_detail", "moved_detail_qty", dims=("movement_uom",)),
    C("net_invoiced_sales.total", "billing_document", "net_invoiced_sales"),
    C("net_invoiced_sales.by_partner", "billing_document", "net_invoiced_sales", dims=("billing_partner",)),
    C("open_receivable_balance.total", "billing_document", "open_receivable_balance"),
    C("open_receivable_balance.by_partner", "billing_document", "open_receivable_balance", dims=("billing_partner",)),
    C("net_vendor_bill_value.total", "billing_document", "net_vendor_bill_value"),
    C("net_vendor_bill_value.by_partner", "billing_document", "net_vendor_bill_value", dims=("billing_partner",)),
    C("posted_customer_receipts.total", "posted_payment", "posted_customer_receipts"),
    C("posted_customer_receipts.by_currency", "posted_payment", "posted_customer_receipts", dims=("payment_currency",)),
    C("active_opportunity_count.total", "crm_opportunity", "active_opportunity_count"),
    C("active_opportunity_count.by_team", "crm_opportunity", "active_opportunity_count", dims=("opportunity_team",)),
]

# Pins: totals that the raw tables give (counts and sums run directly on the seed, not through the hand SQL). A pin
# fails if the hand SQL disagrees with the raw figure, so the hand side is checked too.
PINS: dict[str, str] = {
    "on_hand_qty.total": "8428",                       # internal quants: 454 rows, quantity sum
    "on_hand_qty.qc_hold_product155": "30",            # QC-REJECT lot at WH/QC Hold
    "on_hand_qty.negative_damaged_product140": "-5",   # quant 1168 at WH/Damaged
    "on_hand_qty.overstock_product145_central": "2000",
    "on_hand_qty.dead_stock_product150": "400",
    "valued_qty.total": "8428",                        # valued quantity equals on hand for the whole seed
    "counted_lines.total": "2",                        # quants 5 and 11 with inventory_quantity_set
    "matched_lines.total": "0",
    "count_diff_qty.total": "-34",                     # -17 on each counted line
    "stock_value.total": "71866.09",                   # sum of remaining_value over the valuation layers
    "stock_value.product145_cash_tied": "23889.60",
    "cogs.total": "558769.58",                         # outgoing layer value 561156.52 less scrapped 2386.94
    "waste_value.total": "2386.94",                    # scrapped outgoing layers, valued at -value
    "waste_qty.total": "530",                          # done scrap orders (31)
    "open_qty.partial_receipt_P00196": "50",
    "open_qty.over_receipt_P00197": "-12",
    "open_qty.late_po_P00199_on_expected_day": "600",
    "po_count.total": "198",
    "pos_order_count.total": "1742",
    "pos_session_count.total": "292",
    "order_count.total": "91",
    "revenue.total": "109669.17",                      # sum of qty * price_unit over sale and done orders
    "net_revenue.total": "108293.94",                  # sum of price_subtotal over the same lines
}
assert set(PINS) <= {case.id for case in CASES}, "a pin names no case"
CASES = [replace(case, pins=(((), PINS[case.id]),)) if case.id in PINS else case for case in CASES]

# Metrics whose expected value cannot be reproduced on odoo_onto. Each has a probe test that asserts the
# limitation is still present, so the gap closes loudly when the data or the compiler changes.
GROUND_TRUTH_GAPS: list[dict] = [
    {"metric": "cost_of_sales", "object": "sales_order_line", "kind": "missing_on_database",
     "reason": "sale_margin is not installed on odoo_onto, so sale_order_line.purchase_price does not exist. The "
               "sales_order_line view that carries it cannot be created here. The degraded release drops the cost "
               "property and these metrics; the other sales metrics run on that release.",
     "probe": "cost_of_sales.total_refused"},
    {"metric": "margin", "object": "sales_order_line", "kind": "missing_on_database",
     "reason": "Same as cost_of_sales: the margin needs the sale_margin unit cost.", "probe": "margin.total_refused"},
    {"metric": "margin_pct", "object": "sales_order_line", "kind": "missing_on_database",
     "reason": "Same as cost_of_sales: the margin percent needs the sale_margin unit cost.",
     "probe": "margin_pct.total_refused"},
    {"metric": "pos_cost", "object": "pos_order_line", "kind": "no_data",
     "reason": "pos_order_line.total_cost is NULL on all 26,443 lines of odoo_onto. The hand and compiled results "
               "agree on NULL, which proves wiring only, not a cost figure.", "probe": "pos_cost.total"},
    {"metric": "pos_margin", "object": "pos_order_line", "kind": "no_data",
     "reason": "Depends on total_cost, which is NULL on every line (see pos_cost).", "probe": "pos_margin.total"},
    {"metric": "reserved_qty", "object": "stock_position", "kind": "no_data",
     "reason": "reserved_quantity is 0 on every quant of odoo_onto, so reserved_qty and available_qty equal on hand. "
               "The case proves the SQL is wired, not that reservations are subtracted.", "probe": "reserved_qty.total"},
    {"metric": "usable_qty", "object": "stock_position", "kind": "missing_on_database",
     "reason": "onto_org_node is empty, so every location role is NULL and the real usable quantity equals on hand, "
               "30 units in WH/QC Hold included. The role exclusion is checked only with simulated roles "
               "(usable_qty.total_test_map).", "probe": "usable_qty.total_test_map"},
]

# Machine-readable findings for the report. status: fixed | open. kind: bug | definition | unit | data | caveat.
FINDINGS: list[dict] = [
    {"id": "F1", "status": "fixed", "kind": "bug", "metric": "cogs",
     "summary": "outgoing_layer (quantity < 0) counted scrapped layers as cost of goods sold. Odoo 16 posts a "
                "scrap to the location's valuation account, not to the stock output account that carries COGS "
                "(addons/stock_account/models/stock_move.py:394-398). The rule now also requires scrapped = FALSE. "
                "turnover and days_of_inventory inherit the fix."},
    {"id": "F2", "status": "open", "kind": "definition", "metric": "open_qty",
     "summary": "open_qty nets over-receipts: P00197 (100 ordered, 112 received) contributes -12. Outstanding supply "
                "per line should be max(0, ordered - received). A product formula cannot clamp, so the template "
                "needs a new construct, not a one-line fix. The clamped case is a strict xfail."},
    {"id": "F3", "status": "open", "kind": "unit", "metric": "count_accuracy",
     "summary": "count_accuracy and margin_pct carry unit pct, but the derived value is a fraction (0 to 1). "
                "Either the unit becomes ratio or the derive multiplies by 100. Decision needed."},
    {"id": "F4", "status": "open", "kind": "definition", "metric": "open_transfer_count",
     "summary": "With a period, the count keeps transfers scheduled on that day that are open. It does not count "
                "transfers open as of that day. Odoo state history is not kept, so this needs a decision."},
    {"id": "F5", "status": "open", "kind": "data", "metric": "cost_of_sales",
     "summary": "The sales_order_line object binds purchase_price (sale_margin). The fixture was exported from a "
                "database with sale_margin installed, so the loader accepts it, but odoo_onto has no such column. "
                "Revenue metrics cannot provision on that database. Split the cost into its own object or make the "
                "property optional."},
    {"id": "F6", "status": "open", "kind": "data", "metric": "pos_cost",
     "summary": "POS cost is NULL on all lines of the seed (total_cost not populated), so pos_cost and pos_margin "
                "have no data to check."},
    {"id": "F7", "status": "open", "kind": "caveat", "metric": "usable_qty",
     "summary": "onto_org_node is empty on odoo_onto, so every location role is NULL and usable equals on hand, "
                "including 30 units in WH/QC Hold. The role-exclusion path is exercised by simulated roles "
                "(roles=test_map), not by the real OrgMap."},
    {"id": "F8", "status": "open", "kind": "caveat", "metric": "stock_value",
     "summary": "stock_value, valued_qty and days_of_inventory read the current balance even when a past period "
                "is given. Odoo refuses past snapshots (3.10), so these numbers are only as-of today."},
    {"id": "F9", "status": "open", "kind": "caveat", "metric": "goods_received_qty",
     "summary": "Period bounds are UTC dates. The tenant timezone (0.8) is not applied, so a day near midnight "
                "can land in a different bucket from a local-time report."},
    {"id": "F10", "status": "open", "kind": "caveat", "metric": "reserved_qty",
     "summary": "reserved_quantity is zero on every quant, so reserved_qty and available_qty equal on hand. The test "
                "proves the SQL, not the arithmetic."},
    {"id": "F11", "status": "open", "kind": "caveat", "metric": "cogs",
     "summary": "900 valuation layers have no stock move, quantity 0, and create_date 2026-11-01 (the seed run). "
                "They carry no quantity, so no metric changes, but they distort any per-date view of the layers."},
    {"id": "F12", "status": "open", "kind": "definition", "metric": "on_hand_qty",
     "summary": "An empty total is not one value across paths. A plain COUNT with no rows is 0 on the tile path, but the "
                "matrix path (a total with a filter, or a SUM) returns no row, and the hand SUM is NULL. The harness "
                "reads the missing row as NULL. Consumers must treat a missing total as no value, and a COUNT total "
                "should be 0. Decide whether the matrix path should always return one row for a total."},
]

# Tolerance per unit (template unit). Counts are exact; money +-0.01; quantities +-0.001; ratios and days +-1e-6.
TOLERANCE = {"count": 0, "money": 0.01, "qty": 0.001, "pct": 1e-6, "ratio": 1e-6, "days": 1e-6}
