"""add supabase verification to queue items

A completed workflow path proves the agent ran, not that the ticket changed.
These columns record whether the service-desk row actually matches what
Operator 3 reported writing, so a write-path outcome can be reported as
verified rather than assumed.

Revision ID: f7g8h9i0j1k2
Revises: e6f7g8h9i0j1
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f7g8h9i0j1k2"
down_revision: Union[str, None] = "e6f7g8h9i0j1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "queue_items",
        sa.Column("verification", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "queue_items",
        sa.Column("verification_detail", sa.JSON(), nullable=True),
    )
    op.add_column(
        "queue_items",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_queue_items_verification", "queue_items", ["verification"])


def downgrade() -> None:
    op.drop_index("ix_queue_items_verification", table_name="queue_items")
    op.drop_column("queue_items", "verified_at")
    op.drop_column("queue_items", "verification_detail")
    op.drop_column("queue_items", "verification")
