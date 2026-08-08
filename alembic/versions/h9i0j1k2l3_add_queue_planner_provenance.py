"""add queue planner provenance

Revision ID: h9i0j1k2l3
Revises: g8h9i0j1k2l3
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "h9i0j1k2l3"
down_revision: Union[str, None] = "g8h9i0j1k2l3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("queue_campaigns", sa.Column("planner_mode", sa.String(length=32), nullable=True))
    op.add_column("queue_campaigns", sa.Column("planner_run_id", sa.String(length=128), nullable=True))
    op.add_column("queue_campaigns", sa.Column("planner_generated_at", sa.String(length=64), nullable=True))
    op.add_column("queue_campaigns", sa.Column("planner_effective_as_of", sa.String(length=64), nullable=True))
    op.add_column("queue_campaigns", sa.Column("planner_stale", sa.Boolean(), nullable=True, server_default=sa.false()))
    op.add_column("queue_campaigns", sa.Column("planner_policy_snapshot", sa.JSON(), nullable=True))
    op.add_column("queue_items", sa.Column("rank_position", sa.Integer(), nullable=True))
    op.add_column("queue_items", sa.Column("ranking_evidence", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("queue_items", "ranking_evidence")
    op.drop_column("queue_items", "rank_position")
    op.drop_column("queue_campaigns", "planner_policy_snapshot")
    op.drop_column("queue_campaigns", "planner_stale")
    op.drop_column("queue_campaigns", "planner_effective_as_of")
    op.drop_column("queue_campaigns", "planner_generated_at")
    op.drop_column("queue_campaigns", "planner_run_id")
    op.drop_column("queue_campaigns", "planner_mode")
