"""add ticket queue and processed history

Revision ID: e6f7g8h9i0j1
Revises: d4e5f6g7h8i9
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e6f7g8h9i0j1"
down_revision: Union[str, None] = "d4e5f6g7h8i9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("selected_issue_key", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_agent_runs_selected_issue_key",
        "agent_runs",
        ["selected_issue_key"],
    )

    op.create_table(
        "queue_campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("batch_limit", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_queue_campaigns_id", "queue_campaigns", ["id"])
    op.create_index("ix_queue_campaigns_status", "queue_campaigns", ["status"])
    op.create_index("ix_queue_campaigns_created_at", "queue_campaigns", ["created_at"])

    op.create_table(
        "queue_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("issue_key", sa.String(length=64), nullable=False),
        sa.Column("source_status", sa.String(length=64), nullable=True),
        sa.Column("source_priority", sa.String(length=32), nullable=True),
        sa.Column("source_updated_at", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=True),
        sa.Column("latest_run_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("requeued_from_id", sa.Integer(), nullable=True),
        sa.Column("requeue_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["queue_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requeued_from_id"], ["queue_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "issue_key", name="uq_queue_items_campaign_issue"),
    )
    op.create_index("ix_queue_items_id", "queue_items", ["id"])
    op.create_index("ix_queue_items_campaign_id", "queue_items", ["campaign_id"])
    op.create_index("ix_queue_items_issue_key", "queue_items", ["issue_key"])
    op.create_index("ix_queue_items_state", "queue_items", ["state"])
    op.create_index("ix_queue_items_outcome", "queue_items", ["outcome"])
    op.create_index("ix_queue_items_latest_run_id", "queue_items", ["latest_run_id"])
    op.create_index("ix_queue_items_created_at", "queue_items", ["created_at"])


def downgrade() -> None:
    op.drop_table("queue_items")
    op.drop_table("queue_campaigns")
    op.drop_index("ix_agent_runs_selected_issue_key", table_name="agent_runs")
    op.drop_column("agent_runs", "selected_issue_key")
