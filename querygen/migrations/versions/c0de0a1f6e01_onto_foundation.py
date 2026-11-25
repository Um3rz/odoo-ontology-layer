"""onto foundation: tenant registry + contract 3.4 ontology/operations tables

Plan 0.6. Every table except `tenant` has tenant_id (FK to tenant), created_at,
updated_at. No table holds raw Odoo rows or question/answer text.

Revision ID: c0de0a1f6e01
Revises: f2c8a6d1b9e4
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "c0de0a1f6e01"
down_revision = None  # first migration of this repo (was f2c8a6d1b9e4 in querygen-api)
branch_labels = None
depends_on = None


def _ts():
    return sa.DateTime(timezone=True)


def _common():
    return [
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False, index=True),
    ]


def _stamps():
    return [
        sa.Column("created_at", _ts(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _ts(), nullable=False, server_default=sa.func.now()),
    ]


def _release_fk():
    return sa.Column("release_id", sa.Integer, sa.ForeignKey("onto_release.id"), nullable=True, index=True)


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("odoo_base_url", sa.String, nullable=False),
        sa.Column("db_uuid", sa.String, nullable=False),
        sa.Column("web_origins", pg.JSONB, nullable=False, server_default="[]"),
        *_stamps(),
        sa.UniqueConstraint("odoo_base_url"),
        sa.UniqueConstraint("db_uuid"),
    )

    op.create_table(
        "onto_release", *_common(),
        sa.Column("hash", sa.String, nullable=False),
        sa.Column("created_by_uid", sa.Integer),
        sa.Column("published_at", _ts()),
        *_stamps(),
        sa.UniqueConstraint("tenant_id", "hash"),
    )
    op.create_table(
        "onto_node", *_common(), _release_fk(),
        sa.Column("key", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("payload", pg.JSONB),
        sa.Column("status", sa.String, nullable=False, server_default="proposed"),
        sa.Column("provenance", pg.JSONB),
        *_stamps(),
    )
    op.create_table(
        "onto_edge", *_common(), _release_fk(),
        sa.Column("src_key", sa.String, nullable=False),
        sa.Column("dst_key", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("payload", pg.JSONB),
        *_stamps(),
    )
    op.create_table(
        "onto_proposal", *_common(),
        sa.Column("node_key", sa.String, nullable=False),
        sa.Column("payload", pg.JSONB),
        sa.Column("confidence", sa.Float),
        sa.Column("validation", pg.JSONB),
        sa.Column("state", sa.String, nullable=False, server_default="proposed"),
        sa.Column("approver_uid", sa.Integer),
        sa.Column("approved_at", _ts()),
        *_stamps(),
    )
    op.create_table(
        "job_run", *_common(),
        sa.Column("job", sa.String, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("heartbeat_at", _ts()),
        sa.Column("result", pg.JSONB),
        *_stamps(),
    )
    op.create_table(
        "odoo_tick", *_common(),
        sa.Column("at", _ts(), nullable=False),
        sa.Column("module_version", sa.String),
        sa.Column("release", sa.String),
        sa.Column("steps", pg.JSONB),
        sa.Column("lag_s", sa.Float),
        *_stamps(),
    )
    op.create_table(
        "decision_log", *_common(),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("decision_type", sa.String, nullable=False),
        sa.Column("option_ids", pg.JSONB),
        sa.Column("chosen", sa.String),
        sa.Column("confidence", sa.Float),
        sa.Column("provider", sa.String),
        sa.Column("ms", sa.Integer),
        sa.Column("agreed_with_claude", sa.Boolean),
        *_stamps(),
    )
    op.create_table(
        "llm_usage", *_common(),
        sa.Column("day", sa.String, nullable=False),
        sa.Column("provider", sa.String, nullable=False),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost", sa.Float, nullable=False, server_default="0"),
        *_stamps(),
    )
    op.create_table(
        "eval_run", *_common(),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("state", sa.String, nullable=False, server_default="pending"),
        sa.Column("result", pg.JSONB),
        *_stamps(),
    )


def downgrade() -> None:
    for t in ("eval_run", "llm_usage", "decision_log", "odoo_tick", "job_run",
              "onto_proposal", "onto_edge", "onto_node", "onto_release", "tenant"):
        op.drop_table(t)
