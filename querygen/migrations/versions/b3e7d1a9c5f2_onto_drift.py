"""onto_drift: one row per broken binding of a tenant's release, open until Odoo has the field again (plan 2.5, contract 9)

Metadata only: field names and a plain-English problem. A row is opened by a drift comparison and resolved by the next
one that finds the binding fine again. The pending proposal for a row lives in onto_proposal (source drift).

Revision ID: b3e7d1a9c5f2
Revises: 5f2c9a7d1e3b
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "b3e7d1a9c5f2"
down_revision = "5f2c9a7d1e3b"
branch_labels = None
depends_on = None

TABLE = "onto_drift"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False, index=True),
        sa.Column("release_hash", sa.String, nullable=False),
        sa.Column("node_key", sa.String, nullable=False),
        sa.Column("binding", sa.String, nullable=False),
        sa.Column("problem", sa.String, nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
