# app/routers/policies.py
"""
AI Policies — the rules a business owns.

Each policy's `key` is the name of an Auto workflow input. The backend reads
active policies on every run and passes their current values to the agent, so
editing a row here changes what the agent does on the next run with no code
change and no redeploy.

Evaluations are recorded with `policy_value_at_eval` — the threshold as it stood
at the moment of the decision. Without that, changing a policy would silently
rewrite the history of every decision made under the old value.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models.command_center import Policy, PolicyEvaluation

log = logging.getLogger(__name__)

router = APIRouter(prefix="/policies", tags=["Policies"])

# Which operator each policy actually steers. Shown as groups so a reviewer sees
# 21 rules organised by what they control, not one flat list of fields.
GROUPS: dict[str, tuple[str, str]] = {
    "sla_targets": ("SLA & triage", "Operator 5 · Operator 1"),
    "at_risk_window_minutes": ("SLA & triage", "Operator 5 · Operator 1"),
    "default_region": ("SLA & triage", "Operator 5 · Operator 1"),
    "priority_ranking_order": ("SLA & triage", "Operator 5 · Operator 1"),
    "sla_thresholds": ("SLA & triage", "Operator 5 · Operator 1"),
    "kb_confidence_threshold": ("Diagnosis & remediation", "Operator 2 · Operator 3"),
    "stalled_days_threshold": ("Diagnosis & remediation", "Operator 2 · Operator 3"),
    "routing_mapping_json": ("Diagnosis & remediation", "Operator 2 · Operator 3"),
    "flood_threshold_count": ("Major incidents", "Operator 6"),
    "flood_window_minutes": ("Major incidents", "Operator 6"),
    "correlation_confidence_threshold": ("Major incidents", "Operator 6"),
    "include_relationship_types": ("Major incidents", "Operator 6"),
    "recurring_error_min_count": ("Major incidents", "Operator 6"),
    "blocking_statuses": ("Change control", "Operator 7"),
    "escalating_statuses": ("Change control", "Operator 7"),
    "require_cab_for_risk": ("Change control", "Operator 7"),
    "auto_approve_risk_levels": ("Change control", "Operator 7"),
    "require_change_record_for_production": ("Change control", "Operator 7"),
    "as_of": ("Run scope", "All operators"),
    "support_slack_channel": ("Run scope", "Operator 4"),
    "supabase_table_name": ("Run scope", "Operator 1"),
}
GROUP_ORDER = [
    "SLA & triage",
    "Diagnosis & remediation",
    "Major incidents",
    "Change control",
    "Run scope",
]


# =============================================================================
# SCHEMAS
# =============================================================================


class PolicyOut(BaseModel):
    id: int
    key: str
    name: str
    description: Optional[str]
    policy_type: Optional[str]
    value: Optional[str]
    value_type: Optional[str]
    default_value: Optional[str]
    applies_to: Optional[list]
    active: Optional[bool]
    priority: Optional[int]
    updated_by: Optional[str]
    updated_at: Optional[datetime]
    group: Optional[str] = None
    steers: Optional[str] = None
    is_modified: bool = False

    class Config:
        from_attributes = True


class PolicyUpdate(BaseModel):
    value: Optional[str] = None
    active: Optional[bool] = None
    updated_by: str = Field("Command Center", description="Who changed it")


class EvaluationOut(BaseModel):
    id: int
    policy_key: Optional[str] = None
    policy_name: Optional[str] = None
    action: Optional[str]
    issue_key: Optional[str]
    decision: Optional[str]
    reason: Optional[str]
    policy_value_at_eval: Optional[str]
    evaluated_at: Optional[datetime]

    class Config:
        from_attributes = True


# =============================================================================
# ENDPOINTS
# =============================================================================


def _decorate(p: Policy) -> PolicyOut:
    group, steers = GROUPS.get(p.key, ("Other", ""))
    out = PolicyOut.model_validate(p)
    out.group = group
    out.steers = steers
    out.is_modified = (p.value or "") != (p.default_value or "")
    return out


@router.get("", response_model=list[PolicyOut])
def list_policies(
    active_only: bool = False,
    db: Session = Depends(get_db),
):
    """All policies, ordered so groups read in the order the agent applies them."""
    q = db.query(Policy)
    if active_only:
        q = q.filter(Policy.active.is_(True))
    rows = [_decorate(p) for p in q.order_by(Policy.priority).all()]
    rows.sort(key=lambda r: (GROUP_ORDER.index(r.group) if r.group in GROUP_ORDER else 99, r.priority or 0))
    return rows


@router.get("/grouped")
def grouped(db: Session = Depends(get_db)):
    """The same policies bucketed by what they steer — how the UI renders them."""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in db.query(Policy).order_by(Policy.priority).all():
        out = _decorate(p)
        buckets[out.group or "Other"].append(out.model_dump())
    return {
        "groups": [
            {"name": name, "policies": buckets[name]}
            for name in GROUP_ORDER
            if buckets.get(name)
        ]
        + [
            {"name": name, "policies": rows}
            for name, rows in buckets.items()
            if name not in GROUP_ORDER
        ]
    }


@router.patch("/{key}", response_model=PolicyOut)
def update_policy(key: str, body: PolicyUpdate, db: Session = Depends(get_db)):
    """
    Change a rule. Takes effect on the next run — no restart, no redeploy.

    The value is validated against `value_type` so a bad edit is rejected here
    rather than failing inside an operator halfway through a run.
    """
    p = db.query(Policy).filter(Policy.key == key).first()
    if p is None:
        raise HTTPException(status_code=404, detail=f"No policy named {key!r}")

    if body.value is not None:
        candidate = body.value.strip()
        if p.value_type == "number":
            try:
                float(candidate)
            except ValueError:
                raise HTTPException(
                    status_code=422, detail=f"{key} must be a number, got {candidate!r}"
                ) from None
        elif p.value_type == "boolean":
            if candidate.lower() not in ("true", "false", "1", "0", "yes", "no"):
                raise HTTPException(
                    status_code=422, detail=f"{key} must be true or false, got {candidate!r}"
                )
            candidate = "true" if candidate.lower() in ("true", "1", "yes") else "false"
        elif p.value_type == "json":
            try:
                json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422, detail=f"{key} must be valid JSON: {exc}"
                ) from None
        p.value = candidate

    if body.active is not None:
        p.active = body.active

    p.updated_by = body.updated_by
    db.commit()
    db.refresh(p)
    log.info("policy %s changed to %r by %s", key, p.value, body.updated_by)
    return _decorate(p)


@router.post("/{key}/reset", response_model=PolicyOut)
def reset_policy(key: str, db: Session = Depends(get_db)):
    """Put a rule back to the value it shipped with."""
    p = db.query(Policy).filter(Policy.key == key).first()
    if p is None:
        raise HTTPException(status_code=404, detail=f"No policy named {key!r}")
    p.value = p.default_value
    p.updated_by = "reset"
    db.commit()
    db.refresh(p)
    return _decorate(p)


@router.get("/evaluations", response_model=list[EvaluationOut])
def evaluations(
    limit: int = Query(50, le=500),
    issue_key: Optional[str] = None,
    decision: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    The decision log.

    Each row records what was decided, why, and the policy value in force at
    that moment — so a decision made last week still makes sense after the rule
    has been changed since.
    """
    q = db.query(PolicyEvaluation)
    if issue_key:
        q = q.filter(PolicyEvaluation.issue_key == issue_key)
    if decision:
        q = q.filter(PolicyEvaluation.decision == decision)
    rows = q.order_by(PolicyEvaluation.id.desc()).limit(limit).all()

    keys = {p.id: (p.key, p.name) for p in db.query(Policy).all()}
    out = []
    for r in rows:
        item = EvaluationOut.model_validate(r)
        if r.policy_id in keys:
            item.policy_key, item.policy_name = keys[r.policy_id]
        out.append(item)
    return out


@router.get("/evaluations/stats")
def evaluation_stats(db: Session = Depends(get_db)):
    """Counts for the Policies page header."""
    rows = db.query(PolicyEvaluation).all()
    by_decision: dict[str, int] = defaultdict(int)
    for r in rows:
        by_decision[r.decision or "unknown"] += 1
    return {
        "total": len(rows),
        "by_decision": dict(by_decision),
        "last_evaluated_at": max(
            (r.evaluated_at for r in rows if r.evaluated_at),
            default=None,
        ),
    }
