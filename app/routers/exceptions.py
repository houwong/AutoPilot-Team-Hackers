# app/routers/exceptions.py
"""
The Workbench — the human queue.

Every item arrives with full context and the agent's recommendation. A person
approves, modifies or rejects, the decision is recorded, and the workflow
continues from there.

HOW THE LOOP ACTUALLY CLOSES
Auto cannot be resumed mid-run from outside, and we must not re-implement the
orchestration in the backend — that would move delegation off Auto. So an
approval works by changing the fact the agent reasons about:

    approve  ->  write the decision into `change_requests`  (system of record)
             ->  trigger a fresh orchestrator run for the same ticket
             ->  Operator 7 now reads status 'Implemented' and returns `allow`
             ->  remediation proceeds and Operator 4 notifies

That is what a CAB approval *is* — a change to the change record. The human's
action alters the world, not just our audit trail, and the agent responds to it
on its own terms. Orchestration stays entirely on Auto.

A rejection records the decision and stops. Nothing is remediated.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models.command_center import (
    AgentRun,
    ExceptionItem,
    ExceptionStatus,
    OperatorExecution,
    Resolution,
    RunPhase,
    RunStatus,
)
from ..services import supabase
from ..services.auto_client import AutoClient, AutoError
from .agent import ORCHESTRATOR_ID, _backfill_parked_steps, _consume, _finalise, resolve_inputs

log = logging.getLogger(__name__)

router = APIRouter(prefix="/exceptions", tags=["Workbench"])


# =============================================================================
# SCHEMAS
# =============================================================================


class ResolveRequest(BaseModel):
    resolution: str = Field(..., description="approved | modified | rejected")
    notes: Optional[str] = Field(None, description="Why — recorded for the audit trail")
    resolved_by: str = Field("Dev User", description="Who decided")
    rerun: bool = Field(True, description="Trigger the follow-up run on approval")


class ExceptionOut(BaseModel):
    id: int
    exception_type: Optional[str]
    severity: Optional[str]
    primary_issue_key: Optional[str]
    issue_keys: Optional[list]
    title: Optional[str]
    recommendation: Optional[str]
    confidence: Optional[float]
    status: Optional[str]
    resolution: Optional[str]
    resolution_notes: Optional[str]
    resolved_by: Optional[str]
    resolved_at: Optional[datetime]
    follow_up_run_id: Optional[str]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


# =============================================================================
# QUEUE
# =============================================================================


@router.get("", response_model=list[ExceptionOut])
def list_exceptions(
    status: Optional[str] = Query(None, description="open | in_review | resolved"),
    exception_type: Optional[str] = None,
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    """The queue, most severe and newest first."""
    q = db.query(ExceptionItem)
    if status:
        q = q.filter(ExceptionItem.status == status)
    if exception_type:
        q = q.filter(ExceptionItem.exception_type == exception_type)
    # critical before warning before info, then newest.
    order = {"critical": 0, "warning": 1, "info": 2}
    rows = q.order_by(ExceptionItem.id.desc()).limit(limit).all()
    return sorted(rows, key=lambda r: (order.get(r.severity or "info", 3), -r.id))


@router.get("/{exception_id}")
def get_exception(exception_id: int, db: Session = Depends(get_db)):
    """
    One item with everything the reviewer needs: the gate's reasoning, the
    change record, the incident blast radius, and the policy values that were
    in force when the decision was made.
    """
    item = db.query(ExceptionItem).get(exception_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Exception not found")
    run = db.query(AgentRun).get(item.agent_run_id) if item.agent_run_id else None
    return {
        "exception": ExceptionOut.model_validate(item).model_dump(),
        "context": item.context,
        "run": {"run_id": run.run_id, "status": run.status} if run else None,
    }


# =============================================================================
# RESOLUTION
# =============================================================================


@router.post("/{exception_id}/resolve")
async def resolve_exception(
    exception_id: int,
    body: ResolveRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Record a human decision and, on approval, let the agent continue.

    Approval writes the CAB decision to `change_requests` so Operator 7 returns
    a different answer, then triggers a fresh orchestrator run for the same
    ticket. Nothing about the orchestration moves into this backend.
    """
    item = db.query(ExceptionItem).get(exception_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Exception not found")
    if item.status == ExceptionStatus.RESOLVED.value:
        raise HTTPException(status_code=409, detail="Already resolved")

    resolution = body.resolution.strip().lower()
    if resolution not in {r.value for r in Resolution}:
        raise HTTPException(
            status_code=422,
            detail=f"resolution must be one of {sorted(r.value for r in Resolution)}",
        )

    approved = resolution in (Resolution.APPROVED.value, Resolution.MODIFIED.value)
    gate = (item.context or {}).get("gate") or {}
    change_id = gate.get("change_id")
    cab_result: dict[str, Any] | None = None

    # Operator 7 currently returns change_id as null even when it has clearly read
    # the row (it reports that row's risk, status and approver). Without an id the
    # approval cannot be written back, the gate escalates again on the follow-up
    # run, and the reviewer is stuck in a loop. Look it up by issue_key instead —
    # the mapping is 1:1 in this data — so the Workbench does not depend on an
    # operator field that may be missing.
    if not change_id and item.primary_issue_key:
        try:
            rows = await supabase.select(
                "change_requests",
                {"issue_key": f"eq.{item.primary_issue_key}", "select": "change_id,status"},
            )
            if rows:
                change_id = rows[0].get("change_id")
                log.info(
                    "resolved change_id %s for %s from Supabase (gate omitted it)",
                    change_id,
                    item.primary_issue_key,
                )
        except supabase.SupabaseError as exc:
            log.warning("change lookup failed for %s: %s", item.primary_issue_key, exc)

    # 1. Write the decision to the system of record, when there is a change to decide on.
    if change_id:
        try:
            cab_result = await supabase.record_cab_approval(
                change_id=change_id, approver=body.resolved_by, approved=approved
            )
        except supabase.SupabaseError as exc:
            log.error("could not record CAB decision for %s: %s", change_id, exc)
            raise HTTPException(
                status_code=502, detail=f"Could not update change record: {exc}"
            ) from exc

    # 2. Complete the Auto run that is waiting on this decision.
    #
    # A run parked at a human step waits for exactly one thing: that form. Auto
    # renders it with buttons posting to
    # /api/v1/user-forms/{activityRunId}/approve|reject and a single
    # `review[notes]` field, so the Command Center can submit it on the
    # reviewer's behalf and the original run continues into step_5_exec and its
    # notification.
    #
    # This used to be missing, and the comment at the top of this file asserted
    # it was impossible. The consequence was that approving recorded a decision
    # and started a SEPARATE run while the original stayed parked forever — so
    # the reviewer's decision never completed the workflow it belonged to, which
    # is precisely what the human-in-the-loop gate asks for.
    #
    # Failure here is reported, not swallowed: if the form cannot be submitted
    # the reviewer must know their decision did not reach the agent.
    form_result: dict[str, Any] | None = None
    form_client: AutoClient | None = None
    parent_run = db.query(AgentRun).get(item.agent_run_id) if item.agent_run_id else None
    if parent_run and parent_run.auto_run_id:
        client = AutoClient()
        form_client = client
        try:
            activity_id = await client.find_waiting_form(parent_run.auto_run_id)
            if activity_id:
                form_result = await client.submit_human_form(
                    activity_id, approved=approved, notes=body.notes or ""
                )
                log.info(
                    "submitted Auto review form for run %s (%s)",
                    parent_run.run_id,
                    "approved" if approved else "rejected",
                )
        except AutoError as exc:
            log.error("could not submit the Auto review form: %s", exc)
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Your decision was not sent to the agent: {exc}. "
                    "Nothing has been recorded — try again."
                ),
            ) from exc

    # 3. Record the human decision.
    item.status = ExceptionStatus.RESOLVED.value
    item.resolution = resolution
    item.resolution_notes = body.notes
    item.resolved_by = body.resolved_by
    item.resolved_at = datetime.now(timezone.utc)
    db.commit()

    # The stream was deliberately detached while the form was waiting. Once a
    # decision is submitted, reconcile that same Auto run so the parked
    # step_4_rev changes from ``running`` to ``completed`` and the terminal
    # notification is recorded. Without this pass the Workbench says the
    # decision was saved but the queue remains awaiting_human forever.
    if form_result is not None and form_client is not None and parent_run is not None:
        by_step = {
            step.step_id: step
            for step in db.query(OperatorExecution)
            .filter(OperatorExecution.agent_run_id == parent_run.id)
            .order_by(OperatorExecution.sequence)
            .all()
            if step.step_id
        }
        await _backfill_parked_steps(
            form_client,
            db,
            parent_run,
            by_step,
            reconcile_waiting_step=True,
        )
        done = {"completed", "succeeded", "success", "ok"}
        failed = {"failed", "error", "cancelled"}
        terminal_ids = {
            "step_6_notif_auto",
            "step_6_notif_escalated",
            "step_6_notif_rejected",
            "step_6_notif_manual",
        }
        terminal_steps = [
            step for step in by_step.values() if step.step_id in terminal_ids
        ]
        if any(step.status in done for step in terminal_steps):
            parent_run.status = RunStatus.SUCCEEDED.value
            _finalise(parent_run)
        elif any(step.status in failed for step in terminal_steps):
            parent_run.status = RunStatus.FAILED.value
            _finalise(parent_run)
        db.commit()

    # 4. Only start a NEW run when there was no parked run to continue.
    #
    # Submitting the form above resumes the original run, which is the right
    # outcome: the decision completes the workflow it belonged to. A second run
    # is a fallback for the case where nothing was waiting — the run had already
    # been abandoned, or the item predates form submission.
    #
    # A re-run only helps when something the agent reads has actually changed.
    #
    # CAB case: the change record was just updated, so Operator 7 returns a
    # different answer. Low-confidence WITH a matched article: waiving the
    # threshold can tip it into acting. Low-confidence with NO article: nothing
    # changes. Operator 3 needs an article to apply, and a threshold of 0 does
    # not conjure one.
    #
    # ITSM-2325 ("Network issue") has no knowledge-base article. Approving it
    # sent kb_confidence_threshold 0, Operator 3 returned HUMAN_REVIEW_REQUIRED
    # with kb_article_id "" exactly as before, and the run filed a fresh
    # identical exception. The reviewer approves, the item vanishes, a new one
    # appears, forever — and from the outside it looks like the button does
    # nothing.
    #
    # So: only re-run when the outcome can differ. Otherwise record the decision
    # and say plainly that the agent has no fix to apply, which is the truth the
    # reviewer needs in order to handle it themselves.
    context = item.context or {}
    diagnosis = context.get("diagnosis") or {}
    remediation = context.get("remediation") or {}
    has_article = bool(
        (remediation.get("kb_article_id") or "").strip()
        or (diagnosis.get("kb_article_id") or "").strip()
        or diagnosis.get("kb_match_found")
    )
    rerun_would_help = bool(change_id) or has_article

    # The form was submitted, so the original run is already continuing. Adding
    # a second run here would duplicate the work and file a duplicate item.
    if form_result is not None:
        rerun_would_help = False

    follow_up: Optional[str] = None
    no_rerun_reason: Optional[str] = None
    if (
        approved
        and body.rerun
        and item.primary_issue_key
        and form_result is None
        and not rerun_would_help
    ):
        no_rerun_reason = (
            f"Approval recorded, but no follow-up run was triggered: diagnosis "
            f"found no knowledge-base article for {item.primary_issue_key}, and "
            f"there is no change record to approve. The agent has no fix to "
            f"apply, so a re-run would reach the same conclusion and re-open "
            f"this item. Handle this ticket manually."
        )
        log.info("no re-run for %s — %s", item.primary_issue_key, no_rerun_reason)

    if (
        approved
        and body.rerun
        and item.primary_issue_key
        and form_result is None
        and rerun_would_help
    ):
        overrides: dict[str, Any] = {"target_issue_key": item.primary_issue_key}
        if not change_id:
            overrides["kb_confidence_threshold"] = 0
            log.info(
                "remediation approved by %s for %s — waiving the confidence bar for this run",
                body.resolved_by,
                item.primary_issue_key,
            )
        inputs = resolve_inputs(db, overrides)
        parent = db.query(AgentRun).get(item.agent_run_id) if item.agent_run_id else None
        run = AgentRun(
            run_id=str(uuid.uuid4()),
            workflow_id=ORCHESTRATOR_ID,
            selected_issue_key=item.primary_issue_key,
            trigger="workbench",
            phase=RunPhase.EXECUTION.value,
            status=RunStatus.PENDING.value,
            issue_keys=[item.primary_issue_key],
            inputs=inputs,
            parent_run_id=parent.run_id if parent else None,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        item.follow_up_run_id = run.run_id
        db.commit()
        follow_up = run.run_id

        background.add_task(asyncio.run, _consume(run.id, ORCHESTRATOR_ID, inputs))

    return {
        "exception_id": item.id,
        "resolution": resolution,
        "resolved_by": item.resolved_by,
        "change_record_updated": cab_result,
        "review_form_submitted": form_result is not None,
        "follow_up_run_id": follow_up,
        "message": (
            (
                f"{'Approved' if approved else 'Rejected'}. The paused run has been "
                f"resumed with your decision and is continuing now."
            )
            if form_result is not None
            else "Approved. A follow-up run has been triggered; the change gate "
            "will now allow it."
            if follow_up
            else no_rerun_reason
            or "Recorded. No follow-up run was triggered."
        ),
    }


@router.get("/stats/summary")
def exception_stats(db: Session = Depends(get_db)):
    """Counts for the dashboard's exception queue tile."""
    rows = db.query(ExceptionItem).all()
    return {
        "total": len(rows),
        "open": sum(1 for r in rows if r.status == ExceptionStatus.OPEN.value),
        "resolved": sum(1 for r in rows if r.status == ExceptionStatus.RESOLVED.value),
        "by_type": {
            t: sum(1 for r in rows if r.exception_type == t)
            for t in {r.exception_type for r in rows if r.exception_type}
        },
        "by_severity": {
            s: sum(1 for r in rows if r.severity == s)
            for s in {r.severity for r in rows if r.severity}
        },
    }
