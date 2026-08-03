# app/models/command_center.py
"""
Command Center models — Round 2.

These tables are the Command Center's own record of what the Auto agent did.
They are NOT a copy of the service desk data (that lives in Supabase and is
reached through the Auto operators' native integration).

Why they matter: the Auto Workflow API key is execute-only, and
`GET /workflow-runs` returns nothing usable for us, so Auto cannot be queried
for run history. Everything the dashboard, Insights and audit trail need has to
be persisted here as the SSE stream arrives.

    agent_run           one orchestrator invocation
      operator_execution  one operator step within a run
    policy              a business rule, editable with no code
      policy_evaluation   one decision made against a policy
    exception_item      the Workbench queue
    insight             generated observations
    integration         Data Manager registry
"""

from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from ..core.database import Base


# =============================================================================
# ENUMS — string-valued so they round-trip cleanly through JSON and the API
# =============================================================================


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"  # paused at the Workbench (Run A finished)
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunPhase(str, Enum):
    """The two-run split: analysis pauses for a human, execution resumes after."""

    ANALYSIS = "A"
    EXECUTION = "B"


class PolicyType(str, Enum):
    RULE = "rule"  # deterministic threshold / comparison
    NATURAL_LANGUAGE = "nl"  # evaluated by a model against context


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


class ExceptionStatus(str, Enum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"


class Resolution(str, Enum):
    APPROVED = "approved"
    MODIFIED = "modified"
    REJECTED = "rejected"


class InsightType(str, Enum):
    PATTERN = "pattern"
    ANOMALY = "anomaly"
    RECOMMENDATION = "recommendation"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class IntegrationCategory(str, Enum):
    CHANNEL = "channel"
    SYSTEM_OF_RECORD = "system_of_record"
    HUMAN_LOOP = "human_loop"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


# =============================================================================
# AGENT RUNS
# =============================================================================


class AgentRun(Base):
    """One invocation of the Auto orchestrator."""

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), unique=True, index=True, nullable=False)
    auto_run_id = Column(String(128), index=True)  # Auto's own id, when it returns one
    workflow_id = Column(String(64), index=True)

    trigger = Column(String(64))  # ticket.created | manual | schedule | workbench
    phase = Column(String(4), default=RunPhase.ANALYSIS.value)
    status = Column(String(32), default=RunStatus.PENDING.value, index=True)

    issue_keys = Column(JSON)  # tickets in scope for this run
    inputs = Column(JSON)  # policy values passed to Auto, as sent
    result = Column(JSON)
    error = Column(Text)

    # Set when a Workbench resolution spawns the follow-up execution run.
    parent_run_id = Column(String(64), index=True)

    started_at = Column(DateTime(timezone=True), server_default=func.now())
    ended_at = Column(DateTime(timezone=True))
    duration_ms = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    operators = relationship(
        "OperatorExecution", back_populates="run", cascade="all, delete-orphan"
    )


class OperatorExecution(Base):
    """One operator step inside a run — written per `activity-run` SSE event."""

    __tablename__ = "operator_executions"

    id = Column(Integer, primary_key=True, index=True)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id"), index=True)

    operator_name = Column(String(128), index=True)
    step_id = Column(String(128))
    sequence = Column(Integer)  # order within the run
    status = Column(String(32), index=True)

    input = Column(JSON)
    output = Column(JSON)
    error = Column(Text)

    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))
    duration_ms = Column(Float)

    run = relationship("AgentRun", back_populates="operators")


# =============================================================================
# POLICIES
# =============================================================================


class Policy(Base):
    """
    A business rule that constrains the agent.

    `key` maps to an Auto workflow input name (kb_confidence_threshold,
    at_risk_window_minutes, ...). The backend reads current values here and
    passes them on the next execute call, which is what lets a business user
    change behaviour with no code and no redeploy.
    """

    __tablename__ = "policies"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(128), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text)

    policy_type = Column(String(16), default=PolicyType.RULE.value)
    value = Column(Text)  # current value; Auto inputs are text
    value_type = Column(String(16), default="text")  # number | text | boolean | json
    default_value = Column(Text)

    applies_to = Column(JSON)  # actions this policy gates, e.g. ["auto_remediate"]
    active = Column(Boolean, default=True, index=True)
    priority = Column(Integer, default=100)  # lower evaluates first

    updated_by = Column(String(255))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    evaluations = relationship("PolicyEvaluation", back_populates="policy")


class PolicyEvaluation(Base):
    """
    One decision made against one policy, before the action executed.

    `policy_value_at_eval` is deliberately denormalised: when a judge changes a
    threshold and re-runs, the audit trail must still show what the value was
    at the moment each decision was made.
    """

    __tablename__ = "policy_evaluations"

    id = Column(Integer, primary_key=True, index=True)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id"), index=True)
    policy_id = Column(Integer, ForeignKey("policies.id"), index=True)

    action = Column(String(128), index=True)
    issue_key = Column(String(64), index=True)
    context = Column(JSON)

    decision = Column(String(16), index=True)
    reason = Column(Text)
    policy_value_at_eval = Column(Text)

    evaluated_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    policy = relationship("Policy", back_populates="evaluations")


# =============================================================================
# WORKBENCH
# =============================================================================


class ExceptionItem(Base):
    """The human queue. Every item carries enough context to decide."""

    __tablename__ = "exception_items"

    id = Column(Integer, primary_key=True, index=True)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id"), index=True)

    exception_type = Column(String(64), index=True)
    # vip_sla_breach | cab_required | low_confidence | policy_conflict |
    # missing_data | rollback_failed | major_incident | novel_scenario
    severity = Column(String(16), default=Severity.WARNING.value, index=True)

    primary_issue_key = Column(String(64), index=True)
    issue_keys = Column(JSON)  # correlated tickets, for major incidents

    title = Column(String(255))
    context = Column(JSON)  # everything the reviewer needs
    recommendation = Column(Text)  # what the agent would do
    confidence = Column(Float)

    status = Column(String(16), default=ExceptionStatus.OPEN.value, index=True)
    resolution = Column(String(16))
    resolution_notes = Column(Text)
    resolved_by = Column(String(255))
    resolved_at = Column(DateTime(timezone=True))

    # The Run B triggered by resolving this item.
    follow_up_run_id = Column(String(64))

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


# =============================================================================
# INSIGHTS
# =============================================================================


class Insight(Base):
    """Generated from data the agent actually processed — never from fixtures."""

    __tablename__ = "insights"

    id = Column(Integer, primary_key=True, index=True)

    insight_type = Column(String(16), index=True)
    severity = Column(String(16), default=Severity.INFO.value, index=True)

    title = Column(String(255), nullable=False)
    body = Column(Text)
    evidence = Column(JSON)  # the rows/counts this was computed from
    confidence = Column(Float)

    action_path = Column(Text)  # what a human should do about it
    action_type = Column(String(64))  # create_policy | open_incident | investigate | none

    source_run_ids = Column(JSON)
    dismissed = Column(Boolean, default=False, index=True)

    generated_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


# =============================================================================
# DATA MANAGER
# =============================================================================


class Integration(Base):
    """Live registry of connected systems — the Data Manager surface."""

    __tablename__ = "integrations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), unique=True, index=True, nullable=False)
    category = Column(String(32), index=True)
    purpose = Column(Text)

    status = Column(String(16), default=HealthStatus.UNKNOWN.value, index=True)
    last_check_at = Column(DateTime(timezone=True))
    latency_ms = Column(Float)
    detail = Column(JSON)  # last error, record counts, endpoint

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
