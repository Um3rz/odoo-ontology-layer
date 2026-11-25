"""Onto foundation tables (plan contract 3.4). No table here may hold raw Odoo rows.

Every table except the registry (`tenant`) carries `tenant_id` (FK to the
registry), `created_at` and `updated_at`. Query them only through
`app.db.tenant.scoped`.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, DateTime, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(**kw) -> Any:
    return Field(default=None, sa_column=Column(JSONB, **kw))


def _created() -> Any:
    return Field(
        default_factory=_now,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


def _updated() -> Any:
    return Field(
        default_factory=_now,
        sa_column=Column(
            DateTime(timezone=True), nullable=False,
            server_default=func.now(), onupdate=func.now(),
        ),
    )


class Tenant(SQLModel, table=True):
    """Tenant registry: one row per licensed Odoo database (D30)."""

    __tablename__ = "tenant"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    odoo_base_url: str = Field(unique=True, index=True)  # normalised origin, no trailing slash
    db_uuid: str = Field(unique=True)  # Odoo ir.config_parameter database.uuid
    web_origins: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]"))
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class TenantRow(SQLModel):
    """Not a table: shared columns of every tenant-owned table."""

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: uuid.UUID = Field(foreign_key="tenant.id", index=True)


class OntoRelease(TenantRow, table=True):
    __tablename__ = "onto_release"
    __table_args__ = (UniqueConstraint("tenant_id", "hash"),)

    hash: str
    created_by_uid: Optional[int] = None
    published_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class OntoReleaseCurrent(SQLModel, table=True):
    """The release every read treats as current: one row per tenant (plan 1.9). Set by publish and activate.

    Not a TenantRow: tenant_id is unique here, so the mixin's plain index would be redundant.
    """

    __tablename__ = "onto_release_current"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_onto_release_current_tenant"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: uuid.UUID = Field(foreign_key="tenant.id")
    release_id: int = Field(foreign_key="onto_release.id")
    set_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    set_by_uid: Optional[int] = None
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class OntoNode(TenantRow, table=True):
    __tablename__ = "onto_node"
    __table_args__ = (UniqueConstraint("tenant_id", "release_id", "key", name="uq_onto_node_release_key"),)

    release_id: Optional[int] = Field(default=None, foreign_key="onto_release.id", index=True)
    key: str
    kind: str
    payload: Optional[dict] = _json()
    status: str = "proposed"
    provenance: Optional[dict] = _json()
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class OntoEdge(TenantRow, table=True):
    __tablename__ = "onto_edge"

    release_id: Optional[int] = Field(default=None, foreign_key="onto_release.id", index=True)
    src_key: str
    dst_key: str
    kind: str
    payload: Optional[dict] = _json()
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class OntoProposal(TenantRow, table=True):
    __tablename__ = "onto_proposal"

    node_key: str
    payload: Optional[dict] = _json()
    confidence: Optional[float] = None
    validation: Optional[dict] = _json()
    state: str = "proposed"
    approver_uid: Optional[int] = None
    approved_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class JobRun(TenantRow, table=True):
    __tablename__ = "job_run"

    job: str
    state: str
    heartbeat_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    result: Optional[dict] = _json()  # counts and timings only, never rows
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class OdooTick(TenantRow, table=True):
    __tablename__ = "odoo_tick"

    at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    module_version: Optional[str] = None
    release: Optional[str] = None
    steps: Optional[dict] = _json()
    lag_s: Optional[float] = None
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class DecisionLog(TenantRow, table=True):
    """Content-free: no question text."""

    __tablename__ = "decision_log"

    run_id: str
    decision_type: str
    option_ids: Optional[list] = _json()
    chosen: Optional[str] = None
    confidence: Optional[float] = None
    provider: Optional[str] = None
    ms: Optional[int] = None
    agreed_with_claude: Optional[bool] = None
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class LlmUsage(TenantRow, table=True):
    """Content-free: counts and cost only."""

    __tablename__ = "llm_usage"

    day: str  # ISO date
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0
    created_at: datetime = _created()
    updated_at: datetime = _updated()


class OntoDrift(TenantRow, table=True):
    """One broken binding of a tenant's release, open until Odoo has the field again (plan 2.5, contract 9).

    Metadata only: field names and a plain-English problem, never a record value. node_key is "<object>.<property>".
    """

    __tablename__ = "onto_drift"

    release_hash: str
    node_key: str
    binding: str  # the Odoo field path the property reads
    problem: str
    detected_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    resolved_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    created_at: datetime = _created()
    updated_at: datetime = _updated()


ONTO_TABLES = frozenset(
    ["tenant", "onto_release", "onto_release_current", "onto_node", "onto_edge", "onto_proposal",
     "job_run", "odoo_tick", "decision_log", "llm_usage", "eval_run", "onto_drift"]
)


class EvalRun(TenantRow, table=True):
    __tablename__ = "eval_run"

    name: str
    state: str = "pending"
    result: Optional[dict] = _json()
    created_at: datetime = _created()
    updated_at: datetime = _updated()
