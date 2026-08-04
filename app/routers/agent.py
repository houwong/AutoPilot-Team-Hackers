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
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.database import SessionLocal, get_db
from ..models.command_center import (
    AgentRun,
    OperatorExecution,
    Policy,
    RunPhase,
    RunStatus,
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

ORCHESTRATOR_ID = os.getenv("AUTO_WF_ORCHESTRATOR", "019f7943-03f8-7000-8313-d9ae873d1197")

# Statuses Auto reports for a finished step.
_DONE = {"completed", "succeeded", "success", "ok"}
_FAILED = {"failed", "error", "cancelled"}


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

    Uses its own session because it outlives the request. Each `activity-run`
    event is upserted by `activityRunId`: the `running` event creates the row,
    the `completed` event fills in status, output and duration.
    """
    client = AutoClient()
    db: Session = SessionLocal()
    seq = 0
    by_activity: dict[str, OperatorExecution] = {}

    try:
        run = db.query(AgentRun).get(run_pk)
        run.status = RunStatus.RUNNING.value
        db.commit()

        async for ev in client.stream(workflow_id, inputs):
            now = datetime.now(timezone.utc)

            if ev.auto_run_id and not run.auto_run_id:
                run.auto_run_id = ev.auto_run_id
                db.commit()

            if ev.event == EV_ACTIVITY and ev.activity_run_id:
                row = by_activity.get(ev.activity_run_id)
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
                    by_activity[ev.activity_run_id] = row
                else:
                    row.status = ev.status
                if ev.status in _DONE or ev.status in _FAILED:
                    row.ended_at = now
                    if row.started_at:
                        row.duration_ms = (now - row.started_at).total_seconds() * 1000
                    if ev.outputs:
                        row.output = ev.outputs
                    if ev.status in _FAILED:
                        row.error = json.dumps(ev.payload)[:4000]
                if (ev.attempt or 1) > 1:
                    row.input = {**(row.input or {}), "attempt": ev.attempt}
                db.commit()

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
                for row in by_activity.values():
                    if row.step_id in names:
                        row.operator_name = names[row.step_id]
                run.result = ev.data if isinstance(ev.data, dict) else {"raw": ev.raw}
                run.status = RunStatus.SUCCEEDED.value
                db.commit()

            elif ev.event == EV_ERROR:
                run.status = RunStatus.FAILED.value
                run.error = ev.raw[:4000]
                db.commit()

        run.ended_at = datetime.now(timezone.utc)
        if run.started_at:
            started = run.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            run.duration_ms = (run.ended_at - started).total_seconds() * 1000
        if run.status == RunStatus.RUNNING.value:
            run.status = RunStatus.SUCCEEDED.value
        db.commit()
        log.info("agent run %s finished: %s (%d steps)", run.run_id, run.status, seq)

    except AutoError as exc:
        log.error("auto run failed: %s", exc)
        run = db.query(AgentRun).get(run_pk)
        run.status = RunStatus.FAILED.value
        run.error = str(exc)[:4000]
        run.ended_at = datetime.now(timezone.utc)
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