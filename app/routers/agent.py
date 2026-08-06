# app/routers/agent.py
"""
Agent run endpoints — trigger the Auto orchestrator and persist what happens.

Auto has no webhooks, and a Workflow API key cannot read run history back, so
the SSE stream is the ONLY opportunity to capture a run. Everything the
dashboard, Insights and audit trail will ever show is written here as events
arrive. If persistence drops an event, that information is gone for good.

Policy values are read from the `policies` table and passed as workflow inputs
on every run, which is what lets a business user change a threshold in the
Command Center and see the agent behave differently on the next run with no
code change and no redeploy.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.database import SessionLocal, get_db
from ..models.command_center import (
    AgentRun,
    ExceptionItem,
    ExceptionStatus,
    OperatorExecution,
    Policy,
    PolicyEvaluation,
    RunPhase,
    RunStatus,
    Severity,
)
from ..services.auto_client import (
    EV_ACTIVITY,
    EV_ERROR,
    EV_RESULT,
    EV_WORKFLOW,
    AutoClient,
    AutoError,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["Agent"])

ORCHESTRATOR_ID = os.getenv("AUTO_WF_ORCHESTRATOR", "019fd290-bdf0-7000-88b9-2b00a7dbb7fc")

# Statuses Auto reports for a finished step.
_DONE = {"completed", "succeeded", "success", "ok"}
_FAILED = {"failed", "error", "cancelled"}

# The orchestrator step that opens Auto's human form. When it starts, the run is
# waiting on a person: we detach, park the run, and take the decision into our
# own Workbench instead of leaving it in Auto's console.
HUMAN_STEP_ID = os.getenv("AUTO_HUMAN_STEP_ID", "step_4_rev")
GATE_STEP_ID = os.getenv("AUTO_GATE_STEP_ID", "step_4_gate")
INCIDENT_STEP_ID = os.getenv("AUTO_INCIDENT_STEP_ID", "step_0_incidents")
REMEDIATION_STEP_ID = os.getenv("AUTO_REMEDIATION_STEP_ID", "step_3_rem")


# =============================================================================
# SCHEMAS
# =============================================================================


class TriggerRequest(BaseModel):
    workflow_id: Optional[str] = Field(None, description="Defaults to the orchestrator")
    trigger: str = Field("manual", description="manual | ticket.created | schedule | workbench")
    target_issue_key: Optional[str] = Field(None, description="Run one specific ticket")
    inputs: dict[str, Any] = Field(default_factory=dict, description="Overrides policy values")
    parent_run_id: Optional[str] = None
    phase: str = RunPhase.ANALYSIS.value


class RunSummary(BaseModel):
    run_id: str
    auto_run_id: Optional[str]
    workflow_id: Optional[str]
    trigger: Optional[str]
    phase: Optional[str]
    status: Optional[str]
    issue_keys: Optional[list]
    error: Optional[str]
    duration_ms: Optional[float]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    operator_count: int = 0

    class Config:
        from_attributes = True


# =============================================================================
# POLICY -> WORKFLOW INPUTS
# =============================================================================


def resolve_inputs(db: Session, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Build the workflow inputs from active policies, then apply overrides.

    A policy's `key` is the Auto workflow input name, so the Policies page edits
    these rows directly and the next run picks them up. Overrides exist for
    ad-hoc runs and for the Workbench triggering a follow-up execution.
    """
    values: dict[str, Any] = {}
    for p in db.query(Policy).filter(Policy.active.is_(True)).order_by(Policy.priority).all():
        if p.value is None:
            continue
        if p.value_type == "number":
            try:
                values[p.key] = float(p.value) if "." in p.value else int(p.value)
            except ValueError:
                values[p.key] = p.value
        elif p.value_type == "boolean":
            values[p.key] = str(p.value).strip().lower() in ("true", "1", "yes")
        elif p.value_type == "json":
            try:
                values[p.key] = json.loads(p.value)
            except json.JSONDecodeError:
                values[p.key] = p.value
        else:
            values[p.key] = p.value
    values.update(overrides or {})
    return values


# =============================================================================
# THE RUN LOOP
# =============================================================================


async def _consume(run_pk: int, workflow_id: str, inputs: dict[str, Any]) -> None:
    """
    Drive one Auto run and persist every event.

    Uses its own session because it outlives the request.

    Rows are keyed by `stepId`, not `activityRunId`. Auto emits a separate
    activityRunId for each condition it evaluates on a branching step, so
    step_4_gate alone produced four ids for one execution. Keying by stepId
    collapses those, and also folds Auto's retries (attempt > 1) into the same
    row rather than inventing a new operator.
    """
    client = AutoClient()
    db: Session = SessionLocal()
    seq = 0
    by_step: dict[str, OperatorExecution] = {}

    try:
        run = db.query(AgentRun).get(run_pk)
        run.status = RunStatus.RUNNING.value
        db.commit()

        async for ev in client.stream(workflow_id, inputs):
            now = datetime.now(timezone.utc)

            if ev.auto_run_id and not run.auto_run_id:
                run.auto_run_id = ev.auto_run_id
                db.commit()

            if ev.event == EV_ACTIVITY and ev.step_id:
                row = by_step.get(ev.step_id)
                if row is None:
                    seq += 1
                    row = OperatorExecution(
                        agent_run_id=run.id,
                        operator_name=ev.step_id,  # backfilled with the real name at result
                        step_id=ev.step_id,
                        sequence=seq,
                        status=ev.status,
                        started_at=now,
                    )
                    db.add(row)
                    by_step[ev.step_id] = row
                else:
                    row.status = ev.status
                if ev.status in _DONE or ev.status in _FAILED:
                    row.ended_at = now
                    if row.started_at:
                        row.duration_ms = (now - row.started_at).total_seconds() * 1000
                    if ev.outputs:
                        if ev.is_condition:
                            # A branch evaluation, not the step's own result.
                            # Record which branch was taken, but never let it
                            # overwrite the operator's actual output.
                            conds = list((row.input or {}).get("conditions") or [])
                            conds.append(
                                {
                                    "met": ev.outputs.get("conditionMet"),
                                    "activity_run_id": ev.activity_run_id,
                                }
                            )
                            row.input = {**(row.input or {}), "conditions": conds}
                        else:
                            row.output = ev.outputs
                    if ev.status in _FAILED:
                        row.error = json.dumps(ev.payload)[:4000]
                if (ev.attempt or 1) > 1:
                    row.input = {**(row.input or {}), "attempt": ev.attempt}
                db.commit()

                # --- the pause ---------------------------------------------
                # Auto's human form has opened. Leaving the stream attached
                # would hold a connection and a DB session for as long as the
                # reviewer takes, and would leave the decision in Auto's
                # console rather than our Workbench. Detach and park the run.
                if ev.step_id == HUMAN_STEP_ID and ev.status not in _DONE:
                    await _park_for_human(client, db, run, by_step)
                    return

            elif ev.event == EV_WORKFLOW and ev.status:
                run.status = (
                    RunStatus.SUCCEEDED.value
                    if ev.status in _DONE
                    else RunStatus.FAILED.value
                    if ev.status in _FAILED
                    else RunStatus.RUNNING.value
                )
                db.commit()

            elif ev.event == EV_RESULT:
                # Step names only appear here; backfill them onto the rows.
                names = ev.step_names
                for row in by_step.values():
                    if row.step_id in names:
                        row.operator_name = names[row.step_id]
                run.result = ev.data if isinstance(ev.data, dict) else {"raw": ev.raw}
                run.status = RunStatus.SUCCEEDED.value
                # Finalise here as well as after the loop. The stream does not
                # always terminate cleanly once `result` has arrived, and a run
                # left without ended_at reports no duration on the dashboard.
                _finalise(run)
                db.commit()

            elif ev.event == EV_ERROR:
                run.status = RunStatus.FAILED.value
                run.error = ev.raw[:4000]
                _finalise(run)
                db.commit()

        if run.status == RunStatus.RUNNING.value:
            run.status = RunStatus.SUCCEEDED.value
        _finalise(run)
        db.commit()
        log.info("agent run %s finished: %s (%d steps)", run.run_id, run.status, seq)

    except AutoError as exc:
        log.error("auto run failed: %s", exc)
        run = db.query(AgentRun).get(run_pk)
        run.status = RunStatus.FAILED.value
        run.error = str(exc)[:4000]
        _finalise(run)
        db.commit()
    except Exception:  # noqa: BLE001 - never let a background task die silently
        log.exception("unexpected failure while consuming the Auto stream")
        run = db.query(AgentRun).get(run_pk)
        if run and run.status not in (RunStatus.SUCCEEDED.value, RunStatus.FAILED.value):
            run.status = RunStatus.FAILED.value
            run.error = "internal error while consuming the run stream"
            db.commit()
    finally:
        db.close()


# =============================================================================
# ENDPOINTS
# =============================================================================


@router.post("/runs", response_model=RunSummary, status_code=202)
async def trigger_run(
    body: TriggerRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger the orchestrator and return immediately.

    Returns 202 with a run_id; poll GET /agent/runs/{run_id} for progress. The
    orchestrator takes minutes once Operators 5 and 6 scan the backlog, so this
    must not block the request.
    """
    workflow_id = body.workflow_id or ORCHESTRATOR_ID
    inputs = resolve_inputs(db, body.inputs)
    if body.target_issue_key:
        inputs["target_issue_key"] = body.target_issue_key

    run = AgentRun(
        run_id=str(uuid.uuid4()),
        workflow_id=workflow_id,
        trigger=body.trigger,
        phase=body.phase,
        status=RunStatus.PENDING.value,
        issue_keys=[body.target_issue_key] if body.target_issue_key else None,
        inputs=inputs,
        parent_run_id=body.parent_run_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    background.add_task(asyncio.run, _consume(run.id, workflow_id, inputs))

    return RunSummary(**_summarise(run, 0))


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    limit: int = Query(25, le=200),
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Recent runs, newest first — the dashboard's activity feed."""
    q = db.query(AgentRun)
    if status:
        q = q.filter(AgentRun.status == status)
    runs = q.order_by(AgentRun.id.desc()).limit(limit).all()
    counts = {
        r.id: db.query(OperatorExecution).filter(OperatorExecution.agent_run_id == r.id).count()
        for r in runs
    }
    return [RunSummary(**_summarise(r, counts.get(r.id, 0))) for r in runs]


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)):
    """One run with its operator steps — the trace a judge will want to see."""
    run = db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    steps = (
        db.query(OperatorExecution)
        .filter(OperatorExecution.agent_run_id == run.id)
        .order_by(OperatorExecution.sequence)
        .all()
    )
    return {
        "run": _summarise(run, len(steps)),
        "inputs": run.inputs,
        "result": run.result,
        "operators": [
            {
                "sequence": s.sequence,
                "operator_name": s.operator_name,
                "step_id": s.step_id,
                "status": s.status,
                "duration_ms": s.duration_ms,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "error": s.error,
            }
            for s in steps
        ],
    }


_SUB_RUN_RE = re.compile(r"/runs/([0-9a-f-]{20,})", re.I)


# Which policy a gate decision is attributable to, so the log points at the rule
# a reviewer would actually change.
_GATE_POLICY_FOR: dict[str, str] = {
    "requires_new_change_request": "require_change_record_for_production",
    "prior_rollback": "blocking_statuses",
    "terminal": "blocking_statuses",
    "policy_conflict": "auto_approve_risk_levels",
}


def _log_gate_evaluation(db: Session, run: AgentRun, gate: dict) -> None:
    """
    Record the change gate's decision against the policy that drove it.

    `policy_value_at_eval` is stored deliberately: when a judge changes a
    threshold and re-runs, both evaluations must remain readable, each showing
    the value that was in force when it was made. Without it, editing a rule
    silently rewrites the history of every decision taken under the old one.
    """
    decision = (gate or {}).get("decision")
    if not decision:
        return

    # Pick the most specific rule that explains this outcome.
    key = next(
        (k for flag, k in _GATE_POLICY_FOR.items() if gate.get(flag)),
        "escalating_statuses" if decision == "escalate" else "blocking_statuses",
    )
    policy = db.query(Policy).filter(Policy.key == key).first()

    db.add(
        PolicyEvaluation(
            agent_run_id=run.id,
            policy_id=policy.id if policy else None,
            action="cab_gate",
            issue_key=gate.get("issue_key") or (run.issue_keys or [None])[0],
            context=gate,
            decision=decision,
            reason=gate.get("reason"),
            policy_value_at_eval=str((run.inputs or {}).get(key, policy.value if policy else "")),
        )
    )
    db.commit()
    log.info("policy evaluation logged: %s -> %s (%s)", key, decision, gate.get("issue_key"))


def _finalise(run: AgentRun) -> None:
    """Stamp `ended_at` and `duration_ms`. Idempotent — safe to call more than once."""
    if run.ended_at is None:
        run.ended_at = datetime.now(timezone.utc)
    if run.duration_ms is None and run.started_at:
        started = run.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        ended = run.ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        run.duration_ms = (ended - started).total_seconds() * 1000


async def _step_result(
    client: AutoClient, by_step: dict[str, OperatorExecution], step_id: str
) -> dict:
    """
    Get an operator's structured result for one orchestrator step.

    A delegating step's own output contains only a link to the sub-workflow run,
    so the decision JSON must be fetched from that run. Falls back to any inline
    output for steps that are not delegations.
    """
    row = by_step.get(step_id)
    out = row.output if row is not None else None
    if not isinstance(out, dict):
        return {}

    inner = out.get("output")
    if isinstance(inner, dict):
        return inner
    if isinstance(inner, str) and inner.strip():
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass

    html = ((out.get("displayData") or {}).get("html")) or ""
    m = _SUB_RUN_RE.search(html)
    if not m:
        return {}
    try:
        return await client.get_step_result(m.group(1))
    except AutoError as exc:
        log.warning("could not read sub-workflow result for %s: %s", step_id, exc)
        return {}


async def _park_for_human(
    client: AutoClient, db: Session, run: AgentRun, by_step: dict[str, OperatorExecution]
) -> None:
    """
    Mark the run as awaiting a human and create the Workbench item.

    The item carries everything the reviewer needs to decide without leaving the
    page: the gate's reasoning, the change record, and — when the ticket belongs
    to a major incident — the blast radius Operator 6 measured.
    """
    gate = await _step_result(client, by_step, GATE_STEP_ID)
    remediation = await _step_result(client, by_step, REMEDIATION_STEP_ID)
    incidents = await _step_result(client, by_step, INCIDENT_STEP_ID)

    issue_key = (
        gate.get("issue_key")
        or remediation.get("issue_key")
        or (run.issue_keys or [None])[0]
    )

    # Which route reached the human decides the exception type and wording.
    decision = (gate.get("decision") or "").lower()
    if decision == "escalate":
        if gate.get("policy_conflict"):
            etype, title = "policy_conflict", f"Policy conflict on {issue_key}"
        else:
            etype, title = "cab_required", f"CAB approval required for {issue_key}"
        recommendation = gate.get("reason") or "Awaiting CAB review"
        severity = Severity.WARNING.value
    else:
        etype = "low_confidence"
        title = f"Remediation needs review for {issue_key}"
        recommendation = remediation.get("reason") or remediation.get("outcome") or ""
        severity = Severity.WARNING.value

    # Attach the incident context if this ticket is part of a cluster.
    #
    # Operator 6's field names have changed across rebuilds — `member_keys` now,
    # `child_issue_keys` before — so read both and normalise to one shape. The
    # Workbench page renders from this, and a reviewer seeing a lone CAB
    # approval instead of "head of a 23-ticket incident" is missing the single
    # most important piece of context on the page.
    cluster = None
    for c in incidents.get("clusters") or []:
        members = list(c.get("member_keys") or [])
        if not members:
            members = [c.get("parent_issue_key")] + list(c.get("child_issue_keys") or [])
        if issue_key and issue_key in members:
            cluster = {
                "parent_issue_key": c.get("parent_issue_key"),
                "linked_incident_label": c.get("linked_incident_label") or c.get("name"),
                "child_issue_keys": [k for k in members if k != c.get("parent_issue_key")],
                "ticket_count": c.get("member_count") or c.get("ticket_count") or len(members),
                "distinct_reporters": c.get("distinct_reporters"),
                "vip_count": c.get("vip_count"),
                "affected_assignment_groups": (
                    c.get("assignment_groups") or c.get("affected_assignment_groups") or []
                ),
                "first_seen": c.get("first_seen"),
                "last_seen": c.get("last_seen"),
                "recommended_action": c.get("recommended_action"),
                "rationale": c.get("rationale"),
            }
            break
    if cluster:
        severity = Severity.CRITICAL.value
        label = cluster["linked_incident_label"] or cluster["parent_issue_key"]
        title = f"{title} — part of {label} ({cluster['ticket_count']} tickets)"

    item = ExceptionItem(
        agent_run_id=run.id,
        exception_type=etype,
        severity=severity,
        primary_issue_key=issue_key,
        issue_keys=(
            [cluster.get("parent_issue_key")] + list(cluster.get("child_issue_keys") or [])
            if cluster
            else ([issue_key] if issue_key else None)
        ),
        title=title,
        context={
            "gate": gate,
            "remediation": remediation,
            "incident": cluster,
            "policies_at_run": run.inputs,
        },
        recommendation=recommendation,
        confidence=remediation.get("confidence") or gate.get("confidence"),
        status=ExceptionStatus.OPEN.value,
    )
    _log_gate_evaluation(db, run, gate)
    db.add(item)

    run.status = RunStatus.AWAITING_HUMAN.value
    _finalise(run)
    db.commit()
    log.info("run %s parked for human review: %s (%s)", run.run_id, title, etype)


def _summarise(run: AgentRun, operator_count: int) -> dict:
    return {
        "run_id": run.run_id,
        "auto_run_id": run.auto_run_id,
        "workflow_id": run.workflow_id,
        "trigger": run.trigger,
        "phase": run.phase,
        "status": run.status,
        "issue_keys": run.issue_keys,
        "error": run.error,
        "duration_ms": run.duration_ms,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "operator_count": operator_count,
    }