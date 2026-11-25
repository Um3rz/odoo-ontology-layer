"""onto_release_current: the release every read treats as current, one row per tenant (plan 1.9)

onto_release rows are immutable, so "current" cannot be a flag on them. This pointer table is updated by
publish and by the steward activate route. It is the only write that changes which release is current.

Revision ID: 5f2c9a7d1e3b
Revises: a4e1c7d9b305
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "5f2c9a7d1e3b"
down_revision = "a4e1c7d9b305"
branch_labels = None
depends_on = None

TABLE = "onto_release_current"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("release_id", sa.Integer, sa.ForeignKey("onto_release.id"), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("set_by_uid", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", name="uq_onto_release_current_tenant"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
