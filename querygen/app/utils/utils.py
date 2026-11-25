import re
import datetime
from typing import List, Optional, Tuple
from app.models.models import QueryRequests

# ---------------------------------------------------------------------------
# SQL injection prevention helpers
# ---------------------------------------------------------------------------

# Allowed aggregation functions
_ALLOWED_AGGREGATIONS = frozenset(["SUM", "AVG", "COUNT", "MIN", "MAX"])

# Allowed time-bucket values for bar chart aggregation
_ALLOWED_TIME_AGGREGATIONS = frozenset(
    ["hour", "day", "week", "month", "year", "day_of_week"]
)

# Allowed EXTRACT / comparison grouping categories
_ALLOWED_COMPARISON_GROUPINGS = frozenset(
    ["year", "month", "day", "hour", "minute", "second", "quarter"]
)

# A safe SQL identifier: starts with letter/underscore, followed by word chars or dots
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")

# Result column for a chart stacked by an expression rather than a column.
STACK_EXPRESSION_ALIAS = "stack_group"

# Dangerous patterns that must not appear in raw SQL fragments
_DANGEROUS_SQL_RE = re.compile(r"--|/\*|\*/|;")


def _validate_identifier(value: Optional[str], field_name: str = "field") -> None:
    """Raises ValueError if *value* is not a safe SQL identifier.

    Values prefixed with ``$`` are treated as raw SQL expressions and are
    checked only for dangerous comment/terminator sequences instead.
    """
    if value is None:
        return
    if not isinstance(value, str):
        # Non-string falsy values (e.g. False sent by JS clients) mean "not provided";
        # treat them as None — the downstream code already guards with `if field:`.
        return
    # JS clients sometimes send the literal string "none"/"null" for unset fields
    if value.strip().lower() in ("none", "null", ""):
        return
    if value.startswith("$"):
        # Raw expression path – at minimum block comments and terminators
        _validate_sql_fragment(value[1:], field_name)
        return
    if not _IDENTIFIER_RE.match(value):
        # Value is not a simple identifier (e.g. a CASE WHEN expression).
        # Fall back to fragment-level validation instead of hard-rejecting.
        _validate_sql_fragment(value, field_name)
        return


def _validate_allowlist(
    value: Optional[str], allowed: frozenset, field_name: str
) -> None:
    """Raises ValueError if *value* is not in the *allowed* set."""
    if value is None:
        return
    if not isinstance(value, str):
        return
    # JS clients sometimes send the literal string "none"/"null" for unset fields
    if value.strip().lower() in ("none", "null", ""):
        return
    if value.upper() not in {v.upper() for v in allowed}:
        raise ValueError(
            f"Invalid value for '{field_name}': {value!r}. "
            f"Allowed values: {sorted(allowed)}"
        )


def _validate_sql_fragment(value: Optional[str], field_name: str = "condition") -> None:
    """Raises ValueError if *value* contains SQL comment or terminator sequences."""
    if value is None:
        return
    if not isinstance(value, str):
        return
    if _DANGEROUS_SQL_RE.search(value):
        raise ValueError(
            f"Potentially unsafe SQL content in '{field_name}': "
            "SQL comments (-- / /* */) and statement terminators (;) are not allowed."
        )


def _validate_comma_separated_identifiers(
    value: Optional[str], field_name: str
) -> None:
    """Validates each comma-separated part as a safe SQL identifier."""
    if value is None:
        return
    if not isinstance(value, str):
        return
    for part in value.split(","):
        _validate_identifier(part.strip(), field_name)


def _split_select_columns(columns: str) -> List[str]:
    """Split a SELECT column list on top-level commas.

    Commas inside parentheses or quotes are ignored so expressions like
    ``COALESCE(amount, 0)`` and quoted aliases stay intact.
    """
    parts: List[str] = []
    cur: List[str] = []
    depth = 0
    in_single = False
    in_double = False
    for ch in columns:
        if in_single:
            cur.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            cur.append(ch)
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
            cur.append(ch)
        elif ch == '"':
            in_double = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur)
    if tail.strip():
        parts.append(tail)
    return parts


# Trailing ``AS <alias>`` of a SELECT column (quoted or bare identifier)
_COLUMN_ALIAS_RE = re.compile(r'(?i)\sas\s+("[^"]+"|[a-zA-Z_][\w$]*)\s*$')


def _normalize_alias(alias: str | None) -> str | None:
    """Normalize an alias for comparison: strip quotes/whitespace, lowercase."""
    if not alias:
        return None
    return alias.strip().strip('"').lower()


def _column_alias(column: str) -> str | None:
    """Return the normalized trailing alias of a SELECT column, or ``None``."""
    match = _COLUMN_ALIAS_RE.search(column.strip())
    return _normalize_alias(match.group(1)) if match else None


def _wants_opening(col: dict) -> bool:
    """Whether a cumulative-column entry opts in to the opening-balance fold."""
    return bool(col.get("opening"))


def _opening_filter_clause(opening_filter: str | None) -> str:
    """Normalize ``opening_filter_conditions`` into an AND-able suffix for the opening
    balance's outer WHERE. Returns ``" AND (...)"`` or ``""`` when empty.

    These conditions are not passed to the main query generation; they only restrict
    which preceding rows are summed into the opening balance. A leading "WHERE" (as
    produced by the pre-defined filter helpers) is stripped before injecting.
    """
    if not opening_filter:
        return ""
    opening_filter = re.sub(
        r"^\s*WHERE\s+", "", opening_filter.strip(), flags=re.IGNORECASE
    )
    return f" AND ({opening_filter})" if opening_filter else ""


def _opening_boundary_literal(opening_boundary: str | None) -> str | None:
    """Validate a client-supplied opening-balance boundary (the start of the
    selected period) and return it as a safe typed literal (``::timestamp`` when a
    time part is present, otherwise ``::date``).

    When supplied, this literal is the cut-off for the opening balance instead of
    ``MIN(order_field)`` over the filtered rows: the opening must be the balance as
    at the start of the period, which is well-defined even when the period contains
    no rows (where ``MIN`` would be NULL and collapse the opening to nothing).

    Returns ``None`` when absent/blank so callers fall back to the ``MIN`` boundary.
    Anything that is not an ISO date is rejected so the value is safe to embed.
    """
    if not opening_boundary or not isinstance(opening_boundary, str):
        return None
    text = opening_boundary.strip()
    if text.lower() in ("none", "null", ""):
        return None
    # Accept a plain ISO date or an ISO datetime and re-emit a canonical literal
    # built from parsed components (nothing from the raw input is embedded verbatim),
    # preserving any time part so the cut-off exactly complements the period's lower
    # bound for datetime columns.
    try:
        dt = datetime.datetime.fromisoformat(text)
        return f"'{dt.isoformat(sep=' ')}'::timestamp"
    except ValueError:
        pass
    try:
        return f"'{datetime.date.fromisoformat(text[:10]).isoformat()}'::date"
    except ValueError:
        raise ValueError(f"Invalid opening boundary date: {opening_boundary!r}")


def _opening_cutoff_clause(
    order_data_by_field: str,
    sql_view_name: str | None,
    where: str | None,
    opening_boundary: str | None,
    boundary_column: str | None,
) -> str:
    """The ``<`` predicate that selects the rows preceding the current period.

    When an ``opening_boundary`` (period start) is supplied the cut-off is
    ``<boundary_column> < <literal>`` — compared against the *date* column being
    range-filtered, which may differ from ``order_data_by_field`` (e.g. ordering by
    ``id`` while filtering by ``order_date``). Falls back to ``boundary_column``'s
    default of ``order_data_by_field``. Without a boundary it falls back to the
    minimum order value present in the current (filtered) result.
    """
    boundary = _opening_boundary_literal(opening_boundary)
    if boundary:
        column = boundary_column or order_data_by_field
        return f"{column} < {boundary}"
    where_clause = where if where else ""
    return (
        f"{order_data_by_field} < "
        f"(SELECT MIN({order_data_by_field}) FROM {sql_view_name} {where_clause})"
    )


def _opening_total_subquery(
    field_name: str,
    order_data_by_field: str,
    sql_view_name: str | None,
    where: str | None,
    opening_filter: str | None = None,
    opening_boundary: str | None = None,
    boundary_column: str | None = None,
) -> str:
    """Scalar subquery: sum of *field_name* over all rows that precede the current
    dataset. Used to fold the opening balance into a running-total column (query 1)
    and as the standalone opening query (query 2).

    ``opening_filter`` is ANDed onto the outer WHERE so the fold matches the
    standalone opening query.
    """
    cutoff = _opening_cutoff_clause(
        order_data_by_field, sql_view_name, where, opening_boundary, boundary_column
    )
    return (
        f"(SELECT COALESCE({field_name}, 0) FROM {sql_view_name} "
        f"WHERE {cutoff}"
        f"{_opening_filter_clause(opening_filter)})"
    )


def _build_cumulative_columns(
    cumulative_columns,
    order_data_by_field: str | None,
    order_data_by_desc: bool,
    sql_view_name: str | None = None,
    where: str | None = None,
    opening_filter: str | None = None,
    opening_boundary: str | None = None,
    boundary_column: str | None = None,
) -> Tuple[str, set]:
    """Build the SELECT fragment for cumulative (running-total) window columns.

    Each entry is a ``{"field_name", "field_name_as"}`` mapping where ``field_name``
    is a full window-function expression (e.g. ``SUM(amount)``) interpolated verbatim.
    The window's ORDER BY mirrors the outer query's ORDER BY; when no order field is
    supplied it falls back to the physical row id (``ctid``).

    An entry with a truthy ``"opening"`` key has the opening balance (sum of its
    expression over all rows preceding the current dataset) folded into the column,
    so it becomes a continuous running balance. This requires ``order_data_by_field``.

    Returns ``(fragment, aliases)`` where *fragment* is the comma-joined window
    columns (no leading comma) and *aliases* is the set of normalized aliases the
    cumulative columns occupy, used to drop colliding base columns. Both are empty
    when there are no valid entries.
    """
    if not isinstance(cumulative_columns, list) or not cumulative_columns:
        return "", set()

    over_order_by = (
        order_data_by_field + (" DESC" if order_data_by_desc else "")
        if order_data_by_field
        else "ctid"
    )

    rendered: List[str] = []
    aliases: set = set()
    for col in cumulative_columns:
        if not isinstance(col, dict):
            continue
        field_name = col.get("field_name")
        field_name_as = col.get("field_name_as")
        if not field_name or not field_name_as:
            continue
        _validate_sql_fragment(field_name, "cumulative_columns[].field_name")
        _validate_identifier(field_name_as, "cumulative_columns[].field_name_as")
        window = (
            f"{field_name} OVER (ORDER BY {over_order_by} "
            f"ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
        )
        # Fold the opening balance in (continuous running balance) when requested
        # and both an order field (to define "before the current dataset") and a
        # source view (to sum the prior rows from) are available.
        if _wants_opening(col) and order_data_by_field and sql_view_name:
            opening = _opening_total_subquery(
                field_name,
                order_data_by_field,
                sql_view_name,
                where,
                opening_filter,
                opening_boundary,
                boundary_column,
            )
            window = f"{opening} + {window}"
        rendered.append(f"{window} AS {field_name_as}")
        aliases.add(_normalize_alias(field_name_as))

    if not rendered:
        return "", set()

    return ",\n".join(rendered), aliases


def _apply_cumulative_columns(
    data_field_name: str | None,
    cumulative_columns,
    order_data_by_field: str | None,
    order_data_by_desc: bool,
    sql_view_name: str | None = None,
    where: str | None = None,
    opening_filter: str | None = None,
    opening_boundary: str | None = None,
    boundary_column: str | None = None,
) -> str | None:
    """Return the SELECT column list with cumulative window columns applied.

    A cumulative column whose alias matches an existing base column (same alias)
    replaces that base column rather than being appended, so a column the client
    sends in both ``data_field_name`` and ``cumulative_columns`` is emitted once.
    """
    fragment, aliases = _build_cumulative_columns(
        cumulative_columns,
        order_data_by_field,
        order_data_by_desc,
        sql_view_name,
        where,
        opening_filter,
        opening_boundary,
        boundary_column,
    )
    if not fragment:
        return data_field_name

    base_columns = ""
    if data_field_name and data_field_name.strip():
        kept = [
            col
            for col in _split_select_columns(data_field_name)
            if _column_alias(col) not in aliases
        ]
        base_columns = ",".join(kept).strip()

    if base_columns:
        return base_columns + ",\n" + fragment
    return fragment


def generate_table_opening_query(query_request: QueryRequests) -> Optional[str]:
    """Standalone query returning the opening total(s) for cumulative columns that
    opted in via ``"opening": true``. One row, one column per such cumulative column
    (aliased by its ``field_name_as``), summing the column's expression over all rows
    that precede the current dataset.

    Returns ``None`` when there is nothing to compute (no opening columns, or no
    ``order_data_by_field`` to define the boundary).
    """
    cumulative_columns = query_request.cumulative_columns
    order_data_by_field = query_request.order_data_by_field
    sql_view_name = query_request.sql_view_name
    where = query_request.pre_defined_filter_conditions
    opening_filter = query_request.opening_filter_conditions
    # Opening cut-off: the period-start value and the date column it is compared
    # against, which may differ from ``order_data_by_field`` (e.g. ordering by ``id``
    # while range-filtering ``order_date``).
    opening_boundary = query_request.opening_boundary
    boundary_column = query_request.opening_boundary_column
    cumulative_columns = _apply_filter_date_tokens_to_columns(
        cumulative_columns, query_request.filter_dates
    )
    order_data_by_field = _apply_filter_date_tokens(
        order_data_by_field, query_request.filter_dates
    )

    if (
        not isinstance(cumulative_columns, list)
        or not order_data_by_field
        or not sql_view_name
    ):
        return None

    # --- SQL injection prevention ---
    _validate_identifier(sql_view_name, "sql_view_name")
    _validate_identifier(order_data_by_field, "order_data_by_field")
    _validate_identifier(boundary_column, "opening_boundary_column")
    _validate_sql_fragment(where, "pre_defined_filter_conditions")
    _validate_sql_fragment(opening_filter, "opening_filter_conditions")

    select_cols: List[str] = []
    for col in cumulative_columns:
        if not isinstance(col, dict) or not _wants_opening(col):
            continue
        field_name = col.get("field_name")
        field_name_as = col.get("field_name_as")
        if not field_name or not field_name_as:
            continue
        _validate_sql_fragment(field_name, "cumulative_columns[].field_name")
        _validate_identifier(field_name_as, "cumulative_columns[].field_name_as")
        select_cols.append(f"COALESCE({field_name}, 0) AS {field_name_as}")

    if not select_cols:
        return None

    opening_clause = _opening_filter_clause(opening_filter)
    cutoff = _opening_cutoff_clause(
        order_data_by_field, sql_view_name, where, opening_boundary, boundary_column
    )

    return f"""
            SELECT
            {",".join(select_cols)}
            FROM {sql_view_name}
            WHERE {cutoff}{opening_clause}
            ;
            """


def _is_count_aggregation(arithmetic_value_type: str | None) -> bool:
    return (
        arithmetic_value_type is not None and arithmetic_value_type.upper() == "COUNT"
    )


def _build_aggregate_expression(
    field_name: str | None, arithmetic_value_type: str | None
) -> str | None:
    if not arithmetic_value_type:
        return None

    if field_name and field_name[0] == "$":
        return field_name[1:]

    if _is_count_aggregation(arithmetic_value_type):
        aggregation_name = arithmetic_value_type.upper()
        return (
            f"{aggregation_name}({field_name})"
            if field_name
            else f"{aggregation_name}(*)"
        )

    if not field_name:
        return None

    return f"ROUND({arithmetic_value_type}({field_name}))"


def _build_chart_measure_columns(chart_measures: Optional[List[dict]]) -> Optional[str]:
    """Build the SELECT measure columns for a multi-series chart:
    ``<agg-expr> AS y, <agg-expr> AS y1, …``.

    Each measure reuses ``_build_aggregate_expression`` so a series behaves
    exactly like the single-series ``y`` (ROUND wrapping, COUNT(*), ``$`` raw
    exprs). Returns None when there are no measures so callers fall back to the
    single ``y`` column.
    """
    if not chart_measures:
        return None
    parts: List[str] = []
    for measure in chart_measures:
        field_name = (measure.get("field_name") or "").strip() or None
        aggregation = measure.get("aggregation")
        alias = (measure.get("alias") or "").strip()
        _validate_identifier(field_name, "chart_measures[].field_name")
        _validate_allowlist(
            aggregation, _ALLOWED_AGGREGATIONS, "chart_measures[].aggregation"
        )
        # Aliases are interpolated unquoted as `AS {alias}`; require a plain
        # identifier so an expression can't slip through.
        if not _IDENTIFIER_RE.match(alias):
            raise ValueError(
                f"Invalid chart measure alias {alias!r}: must be a plain identifier"
            )
        expr = _build_aggregate_expression(field_name, aggregation)
        if not expr:
            raise ValueError(
                "chart_measures[] needs a field_name or a COUNT aggregation "
                "that yields an expression"
            )
        parts.append(f"{expr} AS {alias}")
    return ", ".join(parts)


def generate_tile_query(query_request: QueryRequests) -> str:
    sql_view_name = query_request.sql_view_name
    pre_defined_filter_conditions = query_request.pre_defined_filter_conditions
    tile_column_name = query_request.data_field_name
    arithmetic_value_type = query_request.arithmetic_value_type
    show_drilldown = query_request.show_drilldown
    show_metrics = query_request.show_metrics
    drill_down_field = query_request.drill_down_field
    having_conditions = query_request.having_conditions

    # --- SQL injection prevention ---
    _validate_identifier(sql_view_name, "sql_view_name")
    _validate_identifier(tile_column_name, "data_field_name")
    _validate_identifier(drill_down_field, "drill_down_field")
    _validate_allowlist(
        arithmetic_value_type, _ALLOWED_AGGREGATIONS, "arithmetic_value_type"
    )
    _validate_sql_fragment(
        pre_defined_filter_conditions, "pre_defined_filter_conditions"
    )
    _validate_sql_fragment(having_conditions, "having_conditions")

    drilldown_exp = (
        f"ARRAY_AGG({drill_down_field}),"
        if show_drilldown and drill_down_field
        else "ARRAY_AGG(id)," if show_drilldown else ""
    )
    tile_value_expression = _build_aggregate_expression(
        tile_column_name, arithmetic_value_type
    )

    # When metrics are enabled, append COUNT/MIN/MAX/AVG of the data field as
    # extra trailing columns. Requires a concrete field (skip for raw "$" exprs).
    metrics_exp = ""
    if show_metrics and tile_column_name and tile_column_name[0] != "$":
        # Cast AVG to numeric: ROUND(double precision, int) does not exist in
        # Postgres, only ROUND(numeric, int).
        metrics_exp = (
            f", COUNT({tile_column_name})"
            f", ROUND(MIN({tile_column_name})::numeric, 2)"
            f", ROUND(MAX({tile_column_name})::numeric, 2)"
            f", ROUND(AVG({tile_column_name})::numeric, 2)"
        )

    query = ""

    if tile_value_expression:
        query = f"""
                    SELECT {drilldown_exp}
                            {tile_value_expression}{metrics_exp}
                    FROM {sql_view_name}
                    {pre_defined_filter_conditions if pre_defined_filter_conditions is not None else ""}
                    {having_conditions if having_conditions else ""}
                """

    return query


def generate_chart_query(query_request: QueryRequests) -> str:
    sql_view_name = query_request.sql_view_name
    pre_defined_filter_conditions = query_request.pre_defined_filter_conditions
    data_field_name = query_request.data_field_name
    date_field_name = query_request.date_field_name
    x_axis_field_name = query_request.x_axis_field_name
    bar_chart_aggregation_type = query_request.bar_chart_aggregation_type
    comparison_grouping_category = query_request.comparison_grouping_category
    arithmetic_value_type = query_request.arithmetic_value_type
    stacking_group_by = query_request.stacking_group_by
    bar_chart_group_by = query_request.bar_chart_group_by
    order_data_by_field = query_request.order_data_by_field
    order_data_by_desc = query_request.order_data_by_desc
    limit_data = query_request.limit_data
    show_drilldown = query_request.show_drilldown
    drill_down_field = query_request.drill_down_field
    having_conditions = query_request.having_conditions
    chart_measures = query_request.chart_measures

    # --- SQL injection prevention ---
    _validate_identifier(sql_view_name, "sql_view_name")
    _validate_identifier(data_field_name, "data_field_name")
    _validate_identifier(date_field_name, "date_field_name")
    _validate_identifier(x_axis_field_name, "x_axis_field_name")
    _validate_identifier(stacking_group_by, "stacking_group_by")
    _validate_identifier(drill_down_field, "drill_down_field")
    _validate_identifier(order_data_by_field, "order_data_by_field")
    _validate_comma_separated_identifiers(bar_chart_group_by, "bar_chart_group_by")
    _validate_allowlist(
        arithmetic_value_type, _ALLOWED_AGGREGATIONS, "arithmetic_value_type"
    )
    _validate_allowlist(
        bar_chart_aggregation_type,
        _ALLOWED_TIME_AGGREGATIONS,
        "bar_chart_aggregation_type",
    )
    _validate_allowlist(
        comparison_grouping_category,
        _ALLOWED_COMPARISON_GROUPINGS,
        "comparison_grouping_category",
    )
    _validate_sql_fragment(
        pre_defined_filter_conditions, "pre_defined_filter_conditions"
    )
    _validate_sql_fragment(having_conditions, "having_conditions")
    if limit_data is not None and limit_data < 0:
        raise ValueError("'limit_data' must be a non-negative integer")

    # An expression stack (a custom field) gets a fixed alias, since Postgres would
    # name the column "to_char"/"case"; the chart renderer reads it back by that alias.
    stack_select = ""
    stack_group_by = ""
    if stacking_group_by:
        stack_group_by = f", {stacking_group_by}"
        stack_select = (
            stack_group_by
            if _IDENTIFIER_RE.match(stacking_group_by)
            else f", {stacking_group_by} AS {STACK_EXPRESSION_ALIAS}"
        )

    query = ""

    # drilldown expresssion
    drilldown_exp = (
        f", ARRAY_AGG({drill_down_field}) AS id"
        if show_drilldown and drill_down_field
        else ", ARRAY_AGG(id) AS id" if show_drilldown else ""
    )
    data_value_expression = _build_aggregate_expression(
        data_field_name, arithmetic_value_type
    )

    # Multi-series: one aggregate column per measure (aliased y, y1, …) replaces
    # the single `y`. The primary measure (data_field_name/value_type) still
    # selects the branch below and is included as alias `y` by the caller.
    measure_columns = _build_chart_measure_columns(chart_measures)
    y_columns = measure_columns if measure_columns else f"{data_value_expression} as y"

    if (
        data_value_expression
        and date_field_name
        and bar_chart_aggregation_type
        and comparison_grouping_category
        and arithmetic_value_type
        and not x_axis_field_name
    ):
        """
        SELECT
            TO_CHAR(DATE_TRUNC('month', date), 'MONTH') AS x,
            EXTRACT(year FROM date) AS year_,
            SUM(amount_untaxed) AS total
        FROM wad_pos_orders_runtime_view
        GROUP BY DATE_TRUNC('month', date), EXTRACT(year FROM date)
        ORDER BY EXTRACT(year FROM date), DATE_TRUNC('month', date) ASC;
        """
        query = f"""
            SELECT 
                TO_CHAR(DATE_TRUNC('{bar_chart_aggregation_type}', {date_field_name}), '{bar_chart_aggregation_type}') AS x,
                EXTRACT({comparison_grouping_category} FROM {date_field_name}) as comparison_grouping_category,
                {y_columns}
                {drilldown_exp}
                {stack_select}
            FROM {sql_view_name}
            {pre_defined_filter_conditions if pre_defined_filter_conditions != None else ""}
            GROUP BY DATE_TRUNC('{bar_chart_aggregation_type}', {date_field_name}), EXTRACT({comparison_grouping_category} FROM {date_field_name}) {stack_group_by}
            {having_conditions if having_conditions else ""}
            ORDER BY DATE_TRUNC('{bar_chart_aggregation_type}', {date_field_name}), EXTRACT({comparison_grouping_category} FROM {date_field_name});
            """

    elif (
        data_value_expression
        and date_field_name
        and bar_chart_aggregation_type
        and arithmetic_value_type
        and not x_axis_field_name
    ):
        if bar_chart_aggregation_type == "day_of_week":
            aggregation_case = f"TO_CHAR({date_field_name}, 'Day')"

            query = f"""
            SELECT
                {aggregation_case} AS x, -- Extract the name of the day (e.g., 'Sunday')
                {y_columns}
                {drilldown_exp}
                {stack_select}
            FROM {sql_view_name}
            {pre_defined_filter_conditions if pre_defined_filter_conditions != None else ""}
            GROUP BY x,
                CASE 
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Sunday   ' THEN 1
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Monday   ' THEN 2
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Tuesday  ' THEN 3
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Wednesday' THEN 4
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Thursday ' THEN 5
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Friday   ' THEN 6
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Saturday ' THEN 7
                END
            {stack_group_by}
            {having_conditions if having_conditions else ""}
            ORDER BY
                CASE 
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Sunday   ' THEN 1
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Monday   ' THEN 2
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Tuesday  ' THEN 3
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Wednesday' THEN 4
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Thursday ' THEN 5
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Friday   ' THEN 6
                    WHEN TO_CHAR({date_field_name}, 'Day') = 'Saturday ' THEN 7
                END;
            """
        else:
            output_date_format = (
                "HH12 AM"
                if bar_chart_aggregation_type == "hour"
                else (
                    "DD-Mon"
                    if bar_chart_aggregation_type == "day"
                    else (
                        '"WEEK" IW'
                        if bar_chart_aggregation_type == "week"
                        else (
                            "Mon YY"
                            if bar_chart_aggregation_type == "month"
                            else ("YYYY")
                        )
                    )
                )
            )

            allow_time_zone = False
            if bar_chart_aggregation_type == "hour":
                allow_time_zone = True
            tz_string = " AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Riyadh'"

            aggregation_case = f"TO_CHAR(DATE_TRUNC('{bar_chart_aggregation_type}', {date_field_name + tz_string if allow_time_zone else date_field_name} ), '{output_date_format}')"

            month_ = (
                "'month'"
                if bar_chart_aggregation_type == "month"
                else "'hour'" if bar_chart_aggregation_type == "hour" else "'day'"
            )

            timezone_ = "'Asia/Riyadh'"
            code_ = "'UTC'"

            query = f"""
            SELECT 
                {aggregation_case} AS x,
                {y_columns}
                {drilldown_exp}
                {stack_select}
            FROM {sql_view_name}
            {pre_defined_filter_conditions if pre_defined_filter_conditions != None else ""}
            GROUP BY {f'DATE_TRUNC({month_}, {date_field_name})' if bar_chart_aggregation_type == 'month' or bar_chart_aggregation_type == 'day' else 'x'} {stack_group_by}
            {having_conditions if having_conditions else ""}
            ORDER BY {f'DATE_TRUNC({month_}, {date_field_name})' if bar_chart_aggregation_type == 'month' or bar_chart_aggregation_type == 'day' else f'MIN(DATE_TRUNC({month_}, {date_field_name} AT TIME ZONE {code_} AT TIME ZONE {timezone_}))' if bar_chart_aggregation_type == 'hour' else 'x'} ASC;
            """
    elif data_value_expression and x_axis_field_name and arithmetic_value_type:
        query = f"""
            SELECT 
                {x_axis_field_name} AS x,
                {y_columns}
                {drilldown_exp}
                {stack_select}
            FROM {sql_view_name}
            {pre_defined_filter_conditions if pre_defined_filter_conditions != None else ""}
            {"GROUP BY " + bar_chart_group_by if bar_chart_group_by else ""} {stack_group_by}
            {having_conditions if having_conditions else ""}
            {"ORDER BY " + order_data_by_field + (" DESC" if order_data_by_desc else " ASC") if order_data_by_field else ""}
            {f"LIMIT {limit_data}" if limit_data else ""}
            ;
            """
    else:
        query = f"SELECT * FROM {sql_view_name};"

    return query


def generate_heatmap_query(query_request: QueryRequests) -> Tuple[str, str]:
    sql_view_name = query_request.sql_view_name
    where = query_request.pre_defined_filter_conditions
    data_field_name = query_request.data_field_name
    x_axis_field_name = query_request.x_axis_field_name
    agg_func = query_request.arithmetic_value_type
    show_drilldown = query_request.show_drilldown
    drill_down_field = query_request.drill_down_field
    having_conditions = query_request.having_conditions

    # --- SQL injection prevention ---
    _validate_identifier(sql_view_name, "sql_view_name")
    _validate_identifier(data_field_name, "data_field_name")
    _validate_identifier(x_axis_field_name, "x_axis_field_name")
    _validate_identifier(drill_down_field, "drill_down_field")
    _validate_allowlist(agg_func, _ALLOWED_AGGREGATIONS, "arithmetic_value_type")
    _validate_sql_fragment(where, "pre_defined_filter_conditions")
    _validate_sql_fragment(having_conditions, "having_conditions")

    drilldown_exp = (
        f", ARRAY_AGG({drill_down_field}) AS id"
        if show_drilldown and drill_down_field
        else ", ARRAY_AGG(id) AS id" if show_drilldown else ""
    )
    aggregate_expression = _build_aggregate_expression(data_field_name, agg_func)

    fetch_data_query = f"""
            SELECT 
                TO_CHAR({x_axis_field_name}, 'YYYY-MM-DD') as day,
                {aggregate_expression} as total_amount
                {drilldown_exp}
            FROM {sql_view_name}
            {where if where else ""}
            GROUP BY TO_CHAR({x_axis_field_name}, 'YYYY-MM-DD')
            {having_conditions if having_conditions else ""}
            ORDER BY day ASC
        """

    min_max_query = f"""
                SELECT
                    ROUND(MIN(total_amount)) min,
                    ROUND(MAX(total_amount)) max
                FROM ({fetch_data_query}) as sub_query;
        """

    return (fetch_data_query, min_max_query)


def generate_map_query(query_request: QueryRequests) -> str:
    sql_view_name = query_request.sql_view_name
    where = query_request.pre_defined_filter_conditions
    data_field_name = query_request.data_field_name
    x_axis_field_name = query_request.x_axis_field_name
    agg_func = query_request.arithmetic_value_type
    having_conditions = query_request.having_conditions

    # --- SQL injection prevention ---
    _validate_identifier(sql_view_name, "sql_view_name")
    _validate_identifier(data_field_name, "data_field_name")
    _validate_identifier(x_axis_field_name, "x_axis_field_name")
    _validate_allowlist(agg_func, _ALLOWED_AGGREGATIONS, "arithmetic_value_type")
    _validate_sql_fragment(where, "pre_defined_filter_conditions")
    _validate_sql_fragment(having_conditions, "having_conditions")

    aggregate_expression = _build_aggregate_expression(data_field_name, agg_func)

    query = f"""
        SELECT 
            lat::FLOAT as lat,
            lng::FLOAT as lng,
            {x_axis_field_name} as x,
            {aggregate_expression} as y
        FROM {sql_view_name}
        {where if where != None else ""}
        GROUP BY x, lat, lng
        {having_conditions if having_conditions else ""}
        ;
        """

    return query


# ---------------------------------------------------------------------------
# Target plans
# ---------------------------------------------------------------------------

# Odoo names this table, but pin it anyway so a bad payload can't read elsewhere.
_ALLOWED_TARGET_TABLES = frozenset(["dashboard_target_line_saas"])

# Marks which branch of the union a row came from, so filters can spare the
# plans that have nothing to answer them with.
_ROW_KIND_COLUMN = "qtfy_row_kind"

# Postgres types we'll cast a NULL to. Anything odd is rejected rather than
# interpolated, since this lands in the middle of a SELECT.
_PG_TYPE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_ ]*(\(\d+(,\s*\d+)?\))?(\[\])?$")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# How long a period lasts, per plan grain. Only used to walk the days of a period,
# so anything not listed here can't be pro-rated.
_GRAIN_INTERVALS = {"month": "1 month", "quarter": "3 months", "year": "1 year"}

_PRORATION_MODES = frozenset(["none", "calendar_days"])


def _quote_literal(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _validate_pg_type(value: str) -> str:
    if not isinstance(value, str) or not _PG_TYPE_RE.match(value.strip()):
        raise ValueError(f"Invalid column type: {value!r}")
    return value.strip()


def _split_top_level_and(condition: str) -> List[str]:
    """Split a WHERE clause into the predicates its ANDs join.

    Skips anything nested in parens or quotes, and leaves ``BETWEEN x AND y``
    alone — that AND belongs to the operator, not to us.
    """
    parts: List[str] = []
    depth = 0
    in_string = False
    pending_between = 0
    token_start = index = 0

    def word_at(i: int, word: str) -> bool:
        if condition[i : i + len(word)].upper() != word:
            return False
        before = condition[i - 1] if i else " "
        after = condition[i + len(word) : i + len(word) + 1] or " "
        return not (before.isalnum() or before == "_") and not (
            after.isalnum() or after == "_"
        )

    while index < len(condition):
        char = condition[index]
        if in_string:
            if char == "'":
                if condition[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                in_string = False
        elif char == "'":
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and word_at(index, "BETWEEN"):
            pending_between += 1
            index += 7
            continue
        elif depth == 0 and word_at(index, "AND"):
            if pending_between:
                pending_between -= 1
            else:
                parts.append(condition[token_start:index].strip())
                index += 3
                token_start = index
                continue
            index += 3
            continue
        index += 1

    parts.append(condition[token_start:].strip())
    return [part for part in parts if part]


def _target_plans(targets: Optional[dict]) -> List[dict]:
    """Validate the targets block and hand back its plans, or [] if there are none."""
    if not targets or not isinstance(targets, dict):
        return []
    plans = targets.get("plans") or []
    if not plans:
        return []

    table = targets.get("line_table")
    if table not in _ALLOWED_TARGET_TABLES:
        raise ValueError(f"Invalid target table: {table!r}")

    for plan in plans:
        try:
            int(plan["id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("targets.plans[].id must be an integer")
        _validate_identifier(plan.get("column"), "targets.plans[].column")
        _validate_identifier(plan.get("date_column"), "targets.plans[].date_column")
        for dimension in plan.get("dimension_columns") or []:
            _validate_identifier(dimension, "targets.plans[].dimension_columns")
        for column, values in (plan.get("dim_filters") or {}).items():
            _validate_identifier(column, "targets.plans[].dim_filters")
            if not isinstance(values, list):
                raise ValueError("targets.plans[].dim_filters values must be a list")
        for key in ("period_from", "period_to"):
            bound = plan.get(key)
            if bound and not _ISO_DATE_RE.match(str(bound)):
                raise ValueError(f"targets.{key} must be an ISO date (YYYY-MM-DD)")
        _validate_proration(plan)
    return plans


def _validate_proration(plan: dict) -> None:
    """Check the pro-rating settings, and only those that will be used.

    An older Odoo sends neither key. Rather than defaulting a grain we would then
    be guessing at, treat anything incomplete as "don't pro-rate" — guessing wrong
    would quietly drop a yearly plan's whole total into January.
    """
    mode = plan.get("proration")
    if mode is None:
        return
    if mode not in _PRORATION_MODES:
        raise ValueError(f"Invalid targets.proration: {mode!r}")
    if mode == "none":
        return

    grain = plan.get("period_grain")
    if grain not in _GRAIN_INTERVALS:
        raise ValueError(
            f"targets.period_grain must be one of {sorted(_GRAIN_INTERVALS)} "
            f"to pro-rate, got {grain!r}"
        )


def _prorates(plan: dict) -> bool:
    """Does this plan want its periods split across days?"""
    return (
        plan.get("proration") == "calendar_days"
        and plan.get("period_grain") in _GRAIN_INTERVALS
    )


def _row_kind(plan: dict) -> str:
    return "target:%d" % int(plan["id"])


def _target_branch(plan: dict, view_columns: List[dict], target_columns: List[str],
                   table: str) -> str:
    """One plan's arm of the union.

    Emits an expression per view column in order, so it lines up with the facts
    arm positionally: period_start goes in the plan's date column, each dims
    entry in its own column, NULL everywhere else.
    """
    dimensions = set(plan.get("dimension_columns") or [])
    prorated = _prorates(plan)
    # Pro-rated plans carry one row per day of the period, each holding its share,
    # so an ordinary date filter does the arithmetic for us.
    date_value = "d.day" if prorated else "t.period_start"
    # Left in float8. The branch groups by day so this SUM only ever sees one
    # row; it is the caller's own SUM over the days that loses ~1e-13, and
    # switching the column to numeric to chase that would hand back a Decimal,
    # which format_accounting skips because it tests int/float.
    share = "SUM(t.value / d.n)" if prorated else "SUM(t.value)"

    selects: List[str] = []
    groups: List[str] = []
    for column in view_columns:
        name = column["name"]
        pg_type = _validate_pg_type(column["type"])
        if name == plan.get("date_column"):
            expression = f"{date_value}::{pg_type}"
        elif name in dimensions:
            expression = f"(t.dims->>{_quote_literal(name)})::{pg_type}"
        else:
            selects.append(f"NULL::{pg_type}")
            continue
        selects.append(expression)
        groups.append(expression)

    own = plan["column"]
    for target_column in target_columns:
        selects.append(share if target_column == own else "NULL::numeric")
    selects.append(f"{_quote_literal(_row_kind(plan))}::text")

    conditions = ["t.plan_id = %d" % int(plan["id"])]
    for column, values in (plan.get("dim_filters") or {}).items():
        if column not in dimensions or not values:
            continue
        rendered = ", ".join(_quote_literal(v) for v in values)
        conditions.append(f"t.dims->>{_quote_literal(column)} IN ({rendered})")
    for key, operator in (("period_from", ">="), ("period_to", "<=")):
        if plan.get(key):
            conditions.append(f"t.period_start {operator} DATE {_quote_literal(plan[key])}")

    # No grouping expression means the plan shares no column with the view, and
    # collapsing to one row would staple the whole plan onto every group.
    group_by = f" GROUP BY {', '.join(groups)}" if groups else ""
    return (
        "SELECT " + ", ".join(selects) + f" FROM {table} t"
        + (_day_expansion(plan) if prorated else "")
        + f" WHERE {' AND '.join(conditions)}{group_by}"
    )


def _day_expansion(plan: dict) -> str:
    """Walk a period's days, carrying how many of them count.

    COUNT(*) OVER () is the number of qualifying days in this row's own period,
    which is what each day's share is divided by. Kept inside the lateral so the
    divisor follows the period rather than the query's date range.

    Left as float8: value is a double, and casting to numeric would hand psycopg2
    a Decimal, which format_accounting silently skips (it tests int/float). The
    recombination error either way is under 1e-12.
    """
    interval = _GRAIN_INTERVALS[plan["period_grain"]]
    return (
        " CROSS JOIN LATERAL (SELECT g.day::date AS day, "
        "COUNT(*) OVER ()::numeric AS n FROM generate_series("
        "t.period_start::timestamp, "
        f"(t.period_start + INTERVAL '{interval}' - INTERVAL '1 day')::timestamp, "
        f"INTERVAL '1 day') AS g(day)) AS d"
    )


def _build_target_union(sql_view_name: str, targets: Optional[dict]) -> Optional[str]:
    """``(facts UNION ALL targets) AS <view>``, or None when there is nothing to add.

    Wraps the view for this one query. The dataset is left alone, because it is
    usually shared with other dashboard items.
    """
    plans = _target_plans(targets)
    if not plans:
        return None
    view_columns = targets.get("view_columns") or []
    if not view_columns:
        return None
    for column in view_columns:
        _validate_identifier(column.get("name"), "targets.view_columns[].name")

    table = targets["line_table"]
    target_columns = [plan["column"] for plan in plans]
    fact_nulls = ", ".join(f"NULL::numeric AS {c}" for c in target_columns)
    branches = [
        f"SELECT src.*, {fact_nulls}, 'actual'::text AS {_ROW_KIND_COLUMN} "
        f"FROM {sql_view_name} src"
    ]
    branches += [
        _target_branch(plan, view_columns, target_columns, table) for plan in plans
    ]
    return "(" + "\nUNION ALL\n".join(branches) + f") AS {sql_view_name}"


def _spare_target_rows(where_clause: str, targets: Optional[dict]) -> str:
    """Widen the WHERE so target rows survive predicates they cannot answer.

    A target row holds real values in its plan's date and breakdown columns and
    NULL in the rest. Waving all target rows past the whole WHERE would hand back
    a plan's entire horizon against one month of actuals, so each predicate is
    only widened for the plans it reaches past.
    """
    plans = _target_plans(targets)
    if not plans or not where_clause:
        return where_clause

    column_names = [c["name"] for c in (targets.get("view_columns") or [])]
    carried = {
        _row_kind(plan): {plan.get("date_column")}
        | set(plan.get("dimension_columns") or [])
        for plan in plans
    }

    stripped = where_clause.strip()
    prefix = ""
    if stripped[:5].upper() == "WHERE":
        prefix, stripped = stripped[:5], stripped[5:].strip()
    if not stripped:
        return where_clause

    rewritten = []
    for predicate in _split_top_level_and(stripped):
        referenced = {
            name
            for name in column_names
            if re.search(r"\b%s\b" % re.escape(name), predicate, re.IGNORECASE)
        }
        spared = sorted(kind for kind, columns in carried.items() if referenced - columns)
        if spared:
            kinds = ", ".join(_quote_literal(kind) for kind in spared)
            predicate = f"({predicate} OR {_ROW_KIND_COLUMN} IN ({kinds}))"
        rewritten.append(predicate)
    return (prefix + " " if prefix else "") + " AND ".join(rewritten)


_FILTER_DATE_TOKEN_RE = re.compile(r"\{\{date_(from|to):([A-Za-z0-9_]+)\}\}")


def _apply_filter_date_tokens(expression: str | None, filter_dates: dict | None) -> str | None:
    """Swap {{date_from:<line_id>}} / {{date_to:<line_id>}} for that date filter's
    picked bound. An unpicked bound becomes -infinity/infinity so opening stays 0
    and all history counts as period."""
    if not expression or "{{date_" not in expression:
        return expression
    filter_dates = filter_dates or {}

    def _replace(match):
        bound, line_id = match.group(1), match.group(2)
        value = (filter_dates.get(line_id) or {}).get(bound)
        if not value or not isinstance(value, str) or value.strip().lower() in ("none", "null"):
            return "'-infinity'::date" if bound == "from" else "'infinity'::date"
        # Parse the whole value (no slicing) so trailing junk is rejected, not dropped.
        try:
            picked = datetime.datetime.fromisoformat(value.strip()).date()
        except ValueError:
            raise ValueError(f"Invalid date for filter {line_id} ({bound}): {value!r}")
        return f"'{picked.isoformat()}'::date"

    return _FILTER_DATE_TOKEN_RE.sub(_replace, expression)


def _apply_filter_date_tokens_to_columns(columns, filter_dates: dict | None):
    """Cumulative columns carry their own copy of the expression, so swap there too."""
    if not isinstance(columns, list):
        return columns
    return [
        {**col, "field_name": _apply_filter_date_tokens(col.get("field_name"), filter_dates)}
        if isinstance(col, dict)
        else col
        for col in columns
    ]


def _split_column_alias(column: str) -> Tuple[str, str | None]:
    """Split a SELECT column into ``(expression, raw alias)``; alias is None if absent."""
    column = column.strip()
    match = _COLUMN_ALIAS_RE.search(column)
    if not match:
        return column, None
    return column[: match.start()].strip(), match.group(1)


def _resolve_order_aliases(order_by: str, columns: List[Tuple[str, str]]) -> str:
    """Swap ORDER BY parts that name an output alias for that column's expression.

    A table may sort by an alias (e.g. ``"Total"``), which a plain ORDER BY accepts
    but a window's ORDER BY does not.
    """
    by_alias = {_normalize_alias(alias): expr for expr, alias in columns}
    resolved = []
    for part in _split_select_columns(order_by):
        part = part.strip()
        if re.fullmatch(r'"[^"]+"|[a-zA-Z_][\w$]*', part):
            part = by_alias.get(_normalize_alias(part), part)
        resolved.append(part)
    return ", ".join(resolved)


def _limit_rows_per_group(
    select_columns: str,
    body: str,
    partition_by: str,
    per_group: int,
    order_data_by_field: str | None,
    order_data_by_desc: bool,
    lim: str,
) -> str:
    """Wrap a table query so each ``partition_by`` group keeps its first rows.

    Rows are numbered per group in the table's own order and cut at ``per_group``;
    a second numbering over the whole result keeps that order in the outer query.
    """
    columns = [_split_column_alias(c) for c in _split_select_columns(select_columns)]
    if not columns or any(alias is None for _expr, alias in columns):
        raise ValueError("'rows_per_group_by' needs every table column to have an alias")

    window_order = ""
    if order_data_by_field:
        window_order = "ORDER BY " + _resolve_order_aliases(order_data_by_field, columns)
        if order_data_by_desc:
            window_order += " DESC"

    inner_columns = ",\n".join(f"{expr} AS {alias}" for expr, alias in columns)
    outer_columns = ", ".join(alias for _expr, alias in columns)
    return f"""
            SELECT {outer_columns}
            FROM (
                SELECT
                {inner_columns},
                ROW_NUMBER() OVER (PARTITION BY {partition_by} {window_order}) AS __group_row,
                ROW_NUMBER() OVER ({window_order}) AS __table_row
                {body}
            ) AS per_group
            WHERE __group_row <= {per_group}
            ORDER BY __table_row
            {lim}
            ;
            """


def generate_table_query(query_request: QueryRequests) -> str:
    sql_view_name = query_request.sql_view_name
    # Opening-balance fold cut-off: the period-start value and the date column it is
    # compared against; see ``_opening_boundary_literal`` / ``_opening_cutoff_clause``.
    opening_boundary = query_request.opening_boundary
    boundary_column = query_request.opening_boundary_column
    filtered_transactions = query_request.filtered_transactions
    data_field_name = query_request.data_field_name
    is_running = query_request.is_running
    pre_defined_filter_conditions = query_request.pre_defined_filter_conditions
    bar_chart_group_by = query_request.bar_chart_group_by
    order_data_by_field = query_request.order_data_by_field
    order_data_by_desc = query_request.order_data_by_desc
    limit_data = query_request.limit_data
    having_conditions = query_request.having_conditions
    cumulative_columns = query_request.cumulative_columns
    opening_filter = query_request.opening_filter_conditions
    rows_per_group_by = query_request.rows_per_group_by
    rows_per_group_limit = query_request.rows_per_group_limit

    # --- SQL injection prevention ---
    _validate_identifier(sql_view_name, "sql_view_name")
    _validate_identifier(order_data_by_field, "order_data_by_field")
    _validate_identifier(boundary_column, "opening_boundary_column")
    _validate_comma_separated_identifiers(bar_chart_group_by, "bar_chart_group_by")
    _validate_comma_separated_identifiers(rows_per_group_by, "rows_per_group_by")
    if rows_per_group_limit is not None and rows_per_group_limit < 0:
        raise ValueError("'rows_per_group_limit' must be a non-negative integer")
    _validate_sql_fragment(data_field_name, "data_field_name")
    _validate_sql_fragment(
        pre_defined_filter_conditions, "pre_defined_filter_conditions"
    )
    _validate_sql_fragment(having_conditions, "having_conditions")
    _validate_sql_fragment(opening_filter, "opening_filter_conditions")
    if limit_data is not None and limit_data < 0:
        raise ValueError("'limit_data' must be a non-negative integer")

    data_field_name = _apply_filter_date_tokens(data_field_name, query_request.filter_dates)
    cumulative_columns = _apply_filter_date_tokens_to_columns(
        cumulative_columns, query_request.filter_dates
    )
    # Picking a computed column as Group By / Order By sends its whole expression,
    # tokens included.
    bar_chart_group_by = _apply_filter_date_tokens(
        bar_chart_group_by, query_request.filter_dates
    )
    order_data_by_field = _apply_filter_date_tokens(
        order_data_by_field, query_request.filter_dates
    )
    # A HAVING filter on a period column (hide rows empty in both periods) names it too.
    having_conditions = _apply_filter_date_tokens(
        having_conditions, query_request.filter_dates
    )

    group_by = ""
    order_by = ""
    lim = ""

    group_by = "GROUP BY " + bar_chart_group_by if bar_chart_group_by else ""

    order_by = (
        "ORDER BY " + order_data_by_field + " DESC"
        if (order_data_by_field and order_data_by_desc)
        else "ORDER BY " + order_data_by_field if order_data_by_field else ""
    )

    if limit_data and limit_data > 0:
        lim = "LIMIT " + str(limit_data)

    select_columns = _apply_cumulative_columns(
        data_field_name,
        cumulative_columns,
        order_data_by_field,
        order_data_by_desc,
        sql_view_name,
        pre_defined_filter_conditions,
        opening_filter,
        opening_boundary,
        boundary_column,
    )

    # Target plans widen the FROM into a union and loosen the WHERE for the rows
    # that union adds. Both are no-ops when no plans were sent.
    target_union = _build_target_union(sql_view_name, query_request.targets)
    from_clause = target_union or sql_view_name
    where_clause = pre_defined_filter_conditions or ""
    if target_union:
        where_clause = _spare_target_rows(where_clause, query_request.targets)

    if rows_per_group_by and rows_per_group_limit:
        body = f"""
                FROM {from_clause}
                {where_clause}
                {group_by}
                {having_conditions if having_conditions else ""}"""
        return _limit_rows_per_group(
            select_columns,
            body,
            rows_per_group_by,
            rows_per_group_limit,
            order_data_by_field,
            order_data_by_desc,
            lim,
        )

    query = f"""
            SELECT
            {select_columns}
            {f"FROM {from_clause}"}
            {where_clause}
            {group_by}
            {having_conditions if having_conditions else ""}
            {order_by}
            {lim}
            ;
            """

    return query


# ---------------------------------------------------------------------------
# Matrix (server-side pivot) query
# ---------------------------------------------------------------------------

# Aggregations accepted for matrix measures
_ALLOWED_MATRIX_AGGREGATIONS = frozenset(
    ["sum", "avg", "count", "count_distinct", "min", "max"]
)

# Column-dimension grain -> SQL expression template
_MATRIX_GRAIN_EXPRS = {
    "day": "date_trunc('day', {f})::date",
    "week": "date_trunc('week', {f})::date",
    "month": "date_trunc('month', {f})::date",
    "quarter": "date_trunc('quarter', {f})::date",
    "year": "date_trunc('year', {f})::date",
    "value": "{f}",
}

# Reserved alias for the column-dimension bucket in the long-format result
MATRIX_COL_ALIAS = "__matrix_col"


def _matrix_measure_expr(field_name: str, aggregation: str) -> str:
    agg = (aggregation or "").strip().lower()
    if agg not in _ALLOWED_MATRIX_AGGREGATIONS:
        raise ValueError(
            f"Invalid matrix aggregation {aggregation!r}. "
            f"Allowed: {sorted(_ALLOWED_MATRIX_AGGREGATIONS)}"
        )
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {field_name})"
    return f"{agg.upper()}({field_name})"


def generate_matrix_query(query_request: QueryRequests) -> str:
    """Long-format aggregate query for the matrix type.

    One row per (row_dims x column bucket); the Odoo side reshapes to wide
    against a dense period spine, so no crosstab/GROUPING SETS is needed here.

    Row order is the row-dim ordinals unless ``matrix_row_sort`` names a column
    to sort by first — that one is aggregated, never selected, so it orders rows
    without becoming a visible column or a grouping key.
    """
    sql_view_name = query_request.sql_view_name
    row_dims = query_request.matrix_row_dims or []
    col_dim = query_request.matrix_col_dim or {}
    measures = query_request.matrix_measures or []
    row_sort = query_request.matrix_row_sort or {}
    pre_defined_filter_conditions = query_request.pre_defined_filter_conditions
    having_conditions = query_request.having_conditions

    _validate_identifier(sql_view_name, "sql_view_name")
    _validate_sql_fragment(pre_defined_filter_conditions, "pre_defined_filter_conditions")
    _validate_sql_fragment(having_conditions, "having_conditions")

    col_field = (col_dim.get("field_name") or "").strip()
    grain = (col_dim.get("grain") or "value").strip().lower()
    if not col_field:
        raise ValueError("'matrix_col_dim.field_name' is required")
    if grain not in _MATRIX_GRAIN_EXPRS:
        raise ValueError(
            f"Invalid matrix grain {grain!r}. Allowed: {sorted(_MATRIX_GRAIN_EXPRS)}"
        )
    if not measures:
        raise ValueError("At least one matrix measure is required")
    _validate_identifier(col_field, "matrix_col_dim.field_name")

    select_parts = []
    for dim in row_dims:
        field_name = (dim.get("field_name") or "").strip()
        alias = (dim.get("field_name_as") or field_name).strip()
        if not field_name:
            raise ValueError("matrix_row_dims[].field_name is required")
        _validate_identifier(field_name, "matrix_row_dims[].field_name")
        _validate_identifier(alias, "matrix_row_dims[].field_name_as")
        if alias == MATRIX_COL_ALIAS:
            raise ValueError(f"Alias {MATRIX_COL_ALIAS!r} is reserved")
        select_parts.append(f"{field_name} AS {alias}")

    col_expr = _MATRIX_GRAIN_EXPRS[grain].format(f=col_field)
    select_parts.append(f"{col_expr} AS {MATRIX_COL_ALIAS}")

    # Everything selected so far is a grouping column; group/order by ordinal to
    # dodge alias-vs-input-column ambiguity.
    group_ordinals = ", ".join(str(i + 1) for i in range(len(select_parts)))

    for measure in measures:
        field_name = (measure.get("field_name") or "").strip()
        alias = (measure.get("alias") or field_name).strip()
        if not field_name:
            raise ValueError("matrix_measures[].field_name is required")
        _validate_identifier(field_name, "matrix_measures[].field_name")
        # Aliases are interpolated unquoted as `AS {alias}`; a lenient fragment
        # fallback would let an expression through and break the SQL, so
        # require a plain identifier.
        if not _IDENTIFIER_RE.match(alias):
            raise ValueError(
                f"Invalid matrix measure alias {alias!r}: must be a plain identifier"
            )
        if alias == MATRIX_COL_ALIAS:
            raise ValueError(f"Alias {MATRIX_COL_ALIAS!r} is reserved")
        select_parts.append(
            f"{_matrix_measure_expr(field_name, measure.get('aggregation'))} AS {alias}"
        )

    # Aggregated rather than added to SELECT/GROUP BY: grouping by it would split
    # row groups when it is not functionally dependent on the row dims, and
    # selecting it would render an extra column.
    sort_field = row_sort.get("field_name")
    sort_field = sort_field.strip() if isinstance(sort_field, str) else ""
    if sort_field.lower() in ("none", "null"):
        sort_field = ""
    order_by = group_ordinals
    if sort_field:
        _validate_identifier(sort_field, "matrix_row_sort.field_name")
        direction = " DESC" if row_sort.get("desc") else ""
        order_by = f"MIN({sort_field}){direction}, {group_ordinals}"

    # No LIMIT: it would truncate the long-format result mid row-group; the
    # caller caps matrix rows after reshaping.
    query = f"""
            SELECT
            {", ".join(select_parts)}
            FROM {sql_view_name}
            {pre_defined_filter_conditions if pre_defined_filter_conditions else ""}
            GROUP BY {group_ordinals}
            {having_conditions if having_conditions else ""}
            ORDER BY {order_by}
            ;
            """

    return query


def generate_filter_values_query(query_request: QueryRequests) -> str:
    """Distinct values of one filter column, narrowed by the element's other
    filters (the "contextual" value list). The label column is optional."""
    view = query_request.sql_view_name
    column = query_request.data_field_name
    label = query_request.x_axis_field_name
    where = query_request.pre_defined_filter_conditions or ""
    _validate_identifier(view, "sql_view_name")
    _validate_identifier(column, "data_field_name")
    _validate_identifier(label, "x_axis_field_name")
    _validate_sql_fragment(where, "pre_defined_filter_conditions")
    if not view or not column:
        raise ValueError("filter_values needs sql_view_name and data_field_name")

    narrowed = ""
    if where.strip().upper().startswith("WHERE "):
        narrowed = f"AND ({where.strip()[6:]})"
    select = f"{column}, {label}" if label else column
    return (
        f"SELECT DISTINCT {select} FROM {view} "
        f"WHERE {column} IS NOT NULL {narrowed} "
        f"ORDER BY {label or column} ASC"
    )


def generate_rows_query(query_request: QueryRequests) -> str:
    """Every column of the source, filtered: a table snapshot with no columns."""
    view = query_request.sql_view_name
    where = query_request.pre_defined_filter_conditions or ""
    _validate_identifier(view, "sql_view_name")
    _validate_sql_fragment(where, "pre_defined_filter_conditions")
    if not view:
        raise ValueError("rows needs sql_view_name")
    return f"SELECT * FROM {view} {where}".strip()
