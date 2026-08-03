"""add command center tables

Round 2: the Command Center's own record of what the Auto agent did.

The Auto Workflow API key is execute-only and its run-history endpoints are
not usable for us, so Auto cannot be queried after the fact. Everything the
dashboard, Insights and audit trail need is persisted here as the SSE stream
arrives.

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6g7h8
Create Date: 2026-08-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6g7h8i9"
down_revision: Union[str, None] = "c3d4e5f6g7h8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------- agent_runs
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("auto_run_id", sa.String(length=128), nullable=True),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("trigger", sa.String(length=64), nullable=True),
        sa.Column("phase", sa.String(length=4), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("issue_keys", sa.JSON(), nullable=True),
        sa.Column("inputs", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("parent_run_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_id", "agent_runs", ["id"])
    op.create_index("ix_agent_runs_run_id", "agent_runs", ["run_id"], unique=True)
    op.create_index("ix_agent_runs_auto_run_id", "agent_runs", ["auto_run_id"])
    op.create_index("ix_agent_runs_workflow_id", "agent_runs", ["workflow_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_parent_run_id", "agent_runs", ["parent_run_id"])

    # ------------------------------------------------------ operator_executions
    op.create_table(
        "operator_executions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_run_id", sa.Integer(), nullable=True),
        sa.Column("operator_name", sa.String(length=128), nullable=True),
        sa.Column("step_id", sa.String(length=128), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("input", sa.JSON(), nullable=True),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_operator_executions_id", "operator_executions", ["id"])
    op.create_index("ix_operator_executions_agent_run_id", "operator_executions", ["agent_run_id"])
    op.create_index("ix_operator_executions_operator_name", "operator_executions", ["operator_name"])
    op.create_index("ix_operator_executions_status", "operator_executions", ["status"])

    # ---------------------------------------------------------------- policies
    op.create_table(
        "policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("policy_type", sa.String(length=16), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value_type", sa.String(length=16), nullable=True),
        sa.Column("default_value", sa.Text(), nullable=True),
        sa.Column("applies_to", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_policies_id", "policies", ["id"])
    op.create_index("ix_policies_key", "policies", ["key"], unique=True)
    op.create_index("ix_policies_active", "policies", ["active"])

    # ------------------------------------------------------- policy_evaluations
    op.create_table(
        "policy_evaluations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_run_id", sa.Integer(), nullable=True),
        sa.Column("policy_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=True),
        sa.Column("issue_key", sa.String(length=64), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("policy_value_at_eval", sa.Text(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["policies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_policy_evaluations_id", "policy_evaluations", ["id"])
    op.create_index("ix_policy_evaluations_agent_run_id", "policy_evaluations", ["agent_run_id"])
    op.create_index("ix_policy_evaluations_policy_id", "policy_evaluations", ["policy_id"])
    op.create_index("ix_policy_evaluations_action", "policy_evaluations", ["action"])
    op.create_index("ix_policy_evaluations_issue_key", "policy_evaluations", ["issue_key"])
    op.create_index("ix_policy_evaluations_decision", "policy_evaluations", ["decision"])
    op.create_index("ix_policy_evaluations_evaluated_at", "policy_evaluations", ["evaluated_at"])

    # ---------------------------------------------------------- exception_items
    op.create_table(
        "exception_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_run_id", sa.Integer(), nullable=True),
        sa.Column("exception_type", sa.String(length=64), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("primary_issue_key", sa.String(length=64), nullable=True),
        sa.Column("issue_keys", sa.JSON(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("resolution", sa.String(length=16), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_run_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_exception_items_id", "exception_items", ["id"])
    op.create_index("ix_exception_items_agent_run_id", "exception_items", ["agent_run_id"])
    op.create_index("ix_exception_items_exception_type", "exception_items", ["exception_type"])
    op.create_index("ix_exception_items_severity", "exception_items", ["severity"])
    op.create_index("ix_exception_items_primary_issue_key", "exception_items", ["primary_issue_key"])
    op.create_index("ix_exception_items_status", "exception_items", ["status"])
    op.create_index("ix_exception_items_created_at", "exception_items", ["created_at"])

    # ---------------------------------------------------------------- insights
    op.create_table(
        "insights",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("insight_type", sa.String(length=16), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("action_path", sa.Text(), nullable=True),
        sa.Column("action_type", sa.String(length=64), nullable=True),
        sa.Column("source_run_ids", sa.JSON(), nullable=True),
        sa.Column("dismissed", sa.Boolean(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_insights_id", "insights", ["id"])
    op.create_index("ix_insights_insight_type", "insights", ["insight_type"])
    op.create_index("ix_insights_severity", "insights", ["severity"])
    op.create_index("ix_insights_dismissed", "insights", ["dismissed"])
    op.create_index("ix_insights_generated_at", "insights", ["generated_at"])

    # ------------------------------------------------------------ integrations
    op.create_table(
        "integrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_integrations_id", "integrations", ["id"])
    op.create_index("ix_integrations_name", "integrations", ["name"], unique=True)
    op.create_index("ix_integrations_category", "integrations", ["category"])
    op.create_index("ix_integrations_status", "integrations", ["status"])


def downgrade() -> None:
    op.drop_table("integrations")
    op.drop_table("insights")
    op.drop_table("exception_items")
    op.drop_table("policy_evaluations")
    op.drop_table("policies")
    op.drop_table("operator_executions")
    op.drop_table("agent_runs")
