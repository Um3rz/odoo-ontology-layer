from typing import Optional, List
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field, Relationship, Column, TEXT, JSON
from sqlalchemy import UniqueConstraint
from pydantic import model_validator
from uuid import uuid4

# String fields in QueryRequests that JS clients may send as `false` instead of `null`
_QUERYREQUESTS_STRING_FIELDS = frozenset(
    {
        "sql_view_name",
        "analytic_type",
        "arithmetic_value_type",
        "data_field_name",
        "date_field_name",
        "x_axis_field_name",
        "bar_chart_aggregation_type",
        "comparison_grouping_category",
        "bar_chart_group_by",
        "pre_defined_filter_conditions",
        "opening_filter_conditions",
        "opening_boundary",
        "opening_boundary_column",
        "order_data_by_field",
        "previous_balance",
        "filtered_transactions",
        "stacking_group_by",
        "drill_down_field",
        "having_conditions",
        "rows_per_group_by",
    }
)


class LicenseCache(SQLModel, table=True):
    """Cache table for license key validation results."""

    id: int | None = Field(default=None, primary_key=True, index=True)
    license_key: str = Field(unique=True, index=True)
    status: dict = Field(sa_column=Column(JSON))
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    refresh_in_progress: bool = False


class QueryRequests(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True, index=True)
    sql_view_name: str | None
    analytic_type: str | None
    arithmetic_value_type: str | None
    data_field_name: str | None
    date_field_name: str | None
    x_axis_field_name: str | None
    bar_chart_aggregation_type: str | None
    comparison_grouping_category: str | None
    bar_chart_group_by: str | None
    pre_defined_filter_conditions: str | None
    opening_filter_conditions: str | None = None
    # Opening-balance cut-off: the period-start value and the date column it is
    # compared against (which may differ from ``order_data_by_field``).
    opening_boundary: str | None = None
    opening_boundary_column: str | None = None
    order_data_by_field: str | None
    previous_balance: str | None
    filtered_transactions: str | None
    stacking_group_by: str | None
    drill_down_field: str | None
    order_data_by_desc: bool = False
    show_drilldown: bool = False
    show_metrics: bool = False
    is_running: bool = False
    limit_data: int | None
    having_conditions: str | None = None
    cumulative_columns: Optional[List[dict]] = Field(default=None, sa_column=Column(JSON))
    # Matrix (server-side pivot): row dims [{field_name, field_name_as}], column
    # dimension {field_name, grain} and measures [{field_name, aggregation, alias}].
    matrix_row_dims: Optional[List[dict]] = Field(default=None, sa_column=Column(JSON))
    matrix_col_dim: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    matrix_measures: Optional[List[dict]] = Field(default=None, sa_column=Column(JSON))
    # Hidden row ordering {field_name, desc}: sorts matrix rows by a column that
    # is never selected or grouped (see ``generate_matrix_query``).
    matrix_row_sort: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    # Multi-series charts: one aggregate column per measure, aliased y, y1, y2, …
    # [{field_name, aggregation, alias}]. When set, replaces the single `y`
    # column in ``generate_chart_query``. Empty/None keeps the single-series path.
    chart_measures: Optional[List[dict]] = Field(default=None, sa_column=Column(JSON))
    # Target plans unioned into the view: {line_table, view_columns, plans[]}.
    # Adds a target_<slug> column per plan. See ``_build_target_union``.
    targets: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    # Picked range per date filter line, {"<line_id>": {"from", "to"}}, for the
    # {{date_from:<id>}} / {{date_to:<id>}} tokens in table column expressions.
    filter_dates: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    # Table "top N per group": keep the first rows_per_group_limit rows (in the
    # table's order) of each rows_per_group_by partition. See ``_limit_rows_per_group``.
    rows_per_group_by: str | None = None
    rows_per_group_limit: int | None = None
    # Structured filters (see app.utils.filters). When present they replace the
    # WHERE/HAVING/opening strings above, which older modules still send.
    filters: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def _coerce_string_fields(cls, data):
        """Convert non-string, non-None values (e.g. JS `false`) to None for all
        string fields so downstream validation never receives unexpected types."""
        if isinstance(data, dict):
            for field in _QUERYREQUESTS_STRING_FIELDS:
                v = data.get(field)
                if not isinstance(v, (str, type(None))):
                    data[field] = None
                elif isinstance(v, str) and v.strip().lower() in ("none", "null"):
                    data[field] = None
            # cumulative_columns must be a list; coerce anything else (e.g. JS `false`) to None
            cc = data.get("cumulative_columns")
            if cc is not None and not isinstance(cc, list):
                data["cumulative_columns"] = None
            for list_field in ("matrix_row_dims", "matrix_measures", "chart_measures"):
                v = data.get(list_field)
                if v is not None and not isinstance(v, list):
                    data[list_field] = None
            for dict_field in ("matrix_col_dim", "matrix_row_sort", "targets", "filters"):
                v = data.get(dict_field)
                if v is not None and not isinstance(v, dict):
                    data[dict_field] = None
        return data


class QueryResponses(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True, index=True)
    sql_query: str | None = None
    sql_query_2: str | None = None
    # Set when the request's structured filters were applied, so a module that
    # sent them can tell an older querygen (which would drop them) apart.
    filters_compiled: bool | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    query_request_id: int | None = Field(default=None, foreign_key="queryrequests.id")


class Reseller(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True, index=True)
    verified: bool = False
    uid: str = Field(unique=True, index=True)
    code: str = Field(default_factory=lambda: str(uuid4()))
    email: str
    name: str
    role: str
    company: str
    link: str
    company_size: str | None
    revenue: str | None
    current_tool: str | None
    heard_about_us: str | None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContactUs(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True, index=True)
    name: str
    email: str
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResellerAnon(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str | None = Field(unique=True, index=True)
    verified: bool = False
    code: str = Field(default_factory=lambda: str(uuid4()))
    email: str
    name: str
    role: str
    company: str
    link: str
    company_size: str | None
    revenue: str | None
    current_tool: str | None
    heard_about_us: str | None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProcessedWebhookEvent(SQLModel, table=True):
    """Durable dedup key for processed Polar webhook events (by order id), so a
    retried at-least-once delivery can't re-fire emails / CRM writes across
    workers and restarts. Defined here (not in a route module) so Alembic
    autogenerate sees it."""

    __tablename__ = "processed_webhook_events"
    order_id: str = Field(primary_key=True)
    processed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AiCreditTally(SQLModel, table=True):
    """Tenths of a credit a customer has run up and not yet been billed for. The
    Polar meter counts whole events, so a 0.1-credit chat message waits here
    until ten of them make one event. See app/utils/credit_ledger.py."""

    __tablename__ = "ai_credit_tally"
    customer_key: str = Field(primary_key=True)
    tenths: int = 0


class AiRepairBudget(SQLModel, table=True):
    """Free regenerations a billed build may spend fixing an item that didn't
    load. Rows expire; see app/utils/credit_ledger.py."""

    __tablename__ = "ai_repair_budget"
    token: str = Field(primary_key=True)
    customer_key: str
    remaining: int
    expires_at: int  # unix seconds


def utcnow_naive() -> datetime:
    # The timestamp columns carry no zone: an aware value would be converted to
    # the session's zone on insert, so UTC is written naive and read back as is.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EmbeddedKey(SQLModel, table=True):
    """A key baked into a module release. Revoking a generation is how an
    install moves from early access to "activate your license"."""

    __tablename__ = "embedded_keys"
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)
    generation: str = Field(unique=True)
    note: str | None = None
    created_at: datetime = Field(default_factory=utcnow_naive)
    # Activation stays closed for this generation until this time; revoking opens it too.
    activation_opens_at: datetime | None = None
    revoked_at: datetime | None = None


class License(SQLModel, table=True):
    """One Odoo Apps purchase: a number of databases. Production and staging
    are recorded but not counted apart. Its included AI credits are claimed
    through a free Polar checkout, once."""

    __tablename__ = "licenses"
    __table_args__ = (UniqueConstraint("email", "order_ref"),)
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    source: str = "odoo_apps"
    order_ref: str
    odoo_version: str | None = None
    seats: int = 2
    purchased_at: datetime | None = None
    claimed_at: datetime | None = None
    claim_order_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow_naive)


class Install(SQLModel, table=True):
    """One Odoo database, identified by its database.uuid."""

    __tablename__ = "installs"
    id: int | None = Field(default=None, primary_key=True)
    uid: str = Field(unique=True, index=True)
    base_url: str | None = None
    kind: str | None = None  # production | staging
    module_version: str | None = None
    odoo_version: str | None = None
    first_seen: datetime = Field(default_factory=utcnow_naive)
    last_seen: datetime = Field(default_factory=utcnow_naive)
    # Minted at activation; replaces the embedded key in that database.
    token: str | None = Field(default=None, unique=True, index=True)
    license_id: int | None = Field(default=None, foreign_key="licenses.id")
    polar_customer_id: str | None = None
    polar_customer_email: str | None = None
    grace_until: datetime | None = None
    status_override: str | None = None  # restricted | allowed
    note: str | None = None


class ActivationCode(SQLModel, table=True):
    __tablename__ = "activation_codes"
    id: int | None = Field(default=None, primary_key=True)
    install_id: int = Field(foreign_key="installs.id", index=True)
    email: str
    code: str
    expires_at: datetime
    used_at: datetime | None = None
    attempts: int = 0
    created_at: datetime = Field(default_factory=utcnow_naive)


class CreditCoupon(SQLModel, table=True):
    """Issued when AI credits are bought before any database is tied to the
    buyer; redeeming it inside Odoo makes that binding."""

    __tablename__ = "credit_coupons"
    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(unique=True, index=True)
    polar_customer_id: str
    polar_customer_email: str | None = None
    order_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow_naive)
    redeemed_at: datetime | None = None
    redeemed_install_id: int | None = Field(default=None, foreign_key="installs.id")
