"""onto_node: one node per key per release (plan 1.2)

Revision ID: a4e1c7d9b305
Revises: c0de0a1f6e01
"""
from alembic import op

revision = "a4e1c7d9b305"
down_revision = "c0de0a1f6e01"
branch_labels = None
depends_on = None

CONSTRAINT = "uq_onto_node_release_key"


def upgrade() -> None:
    # Draft nodes have release_id NULL, which Postgres never counts as equal.
    op.create_unique_constraint(CONSTRAINT, "onto_node", ["tenant_id", "release_id", "key"])


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "onto_node", type_="unique")
