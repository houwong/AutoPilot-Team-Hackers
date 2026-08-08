"""add ranking evidence to queue items

Records why a ticket sits where it does in a batch — the SLA class, VIP flag and
Operator 1 rank it was ordered on. Frozen with the item, like the batch itself,
so the order stays explainable after the live SLA state has moved on: a reviewer
who approved a batch should be able to see what it was ranked on at the time
they approved it, not what it would rank on today.

Revision ID: g8h9i0j1k2l3
Revises: f7g8h9i0j1k2
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "g8h9i0j1k2l3"
down_revision: Union[str, None] = "f7g8h9i0j1k2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("queue_items", sa.Column("sla_status", sa.String(length=32), nullable=True))
    op.add_column("queue_items", sa.Column("vip", sa.Boolean(), nullable=True))
    op.add_column("queue_items", sa.Column("priority_rank", sa.Integer(), nullable=True))
    op.add_column("queue_items", sa.Column("ranked_by", sa.String(length=32), nullable=True))
    op.add_column("queue_items", sa.Column("ranking_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("queue_items", "ranking_reason")
    op.drop_column("queue_items", "ranked_by")
    op.drop_column("queue_items", "priority_rank")
    op.drop_column("queue_items", "vip")
    op.drop_column("queue_items", "sla_status")
