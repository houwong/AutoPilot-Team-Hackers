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
from ..services import supabase
from ..services.auto_client import (
    EV_ACTIVITY,
    EV_ERROR,
    EV_RESULT,
    EV_WORKFLOW,
    AutoClient,
    AutoError,
)
from ..services.supabase import SupabaseError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["Agent"])

# The rebuilt orchestrator (6 Aug). The Round 1/2 original,
# 019fd290-bdf0-7000-88b9-2b00a7dbb7fc, still exists and still runs — set
# AUTO_WF_ORCHESTRATOR to it to roll back. Keep it: it is the only fallback if
# the rebuild turns out to have a fault, and existing Workbench items link to
# sub-workflow runs that live under it.
ORCHESTRATOR_ID = os.getenv("AUTO_WF_ORCHESTRATOR", "019fd826-9991-7000-873c-ea6fca4c660b")

# Statuses Auto reports for a finished step.
_DONE = {"completed", "succeeded", "success", "ok"}
_FAILED = {"failed", "error", "cancelled"}
_ACTIVE_RUN_STATUSES = {
    RunStatus.PENDING.value,
    RunStatus.RUNNING.value,
    RunStatus.AWAITING_HUMAN.value,
}

# The orchestrator step that opens Auto's human form. When it starts, the run is
# waiting on a person: we detach, park the run, and take the decision into our
# own Workbench instead of leaving it in Auto's console.
HUMAN_STEP_ID = os.getenv("AUTO_HUMAN_STEP_ID", "step_4_rev")
GATE_STEP_ID = os.getenv("AUTO_GATE_STEP_ID", "step_4_gate")
INCIDENT_STEP_ID = os.getenv("AUTO_INCIDENT_STEP_ID", "step_0_incidents")
REMEDIATION_STEP_ID = os.getenv("AUTO_REMEDIATION_STEP_ID", "step_3_rem")
DIAGNOSIS_STEP_ID = os.getenv("AUTO_DIAGNOSIS_STEP_ID", "step_2_diag")


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
    selected_issue_key: Optional[str]
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
                if ev.step_id in NOTIFICATION_STEPS and ev.status in _DONE:
                    await _persist_notification_result(client, by_step, ev.step_id)
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
                _backfill_selected_issue_key(run, ev.data)
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
        _backfill_selected_issue_key(run, run.result)
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


# Statuses meaning the ticket is finished. The agent must not act on these.
CLOSED_STATUSES = {"resolved", "closed", "done", "cancelled", "canceled"}


async def _refuse_closed_ticket(issue_key: str) -> None:
    """
    Refuse to target a ticket that is already finished.

    Operator 1 excludes closed tickets from its eligible list — 88 of the 460 in
    this dataset are Resolved, and it ranks the other 372. But a targeted run
    bypasses that ranking entirely: step_1_sweep looks the key up, fails to find
    it among the eligible, and fetches it straight from Supabase. The filter is
    skipped precisely because the ticket was excluded by it.

    The consequence is not theoretical. Targeting ITSM-2248 — "Keyboard
    replacement", Resolved, "Approved remediation applied." — reopened it to In
    Progress and overwrote its resolution with "Applying automated KB workaround
    from article KB-100", an article about VPN. The run reported success and
    parked for review, so nothing looked wrong from the outside.

    Fixed here rather than in Auto because this is the boundary a request
    crosses: refusing the run costs one Supabase read, while an operator edit
    risks the orchestrator wiring, which has regressed on five of five recent
    saves. Auto keeping its own guard would be better still — this is a floor,
    not a ceiling.

    A Supabase failure is NOT treated as a refusal. This guard exists to stop a
    confident wrong action, and turning an outage into "no runs at all" trades
    one failure for a worse one.
    """
    try:
        rows = await supabase.select(
            "issues",
            {'"Issue key"': f"eq.{issue_key}", "select": 'status:"Status"'},
        )
    except SupabaseError as exc:
        log.warning("could not verify %s before running: %s", issue_key, exc)
        return

    if not rows:
        raise HTTPException(status_code=404, detail=f"{issue_key} does not exist")

    status = str(rows[0].get("status") or "").strip()
    if status.lower() in CLOSED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{issue_key} is {status}. The agent does not act on closed "
                "tickets — remediation would reopen it and overwrite its "
                "resolution."
            ),
        )


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
    run = await create_agent_run(db, background, body)

    return RunSummary(**_summarise(run, 0))


async def create_agent_run(
    db: Session, background: BackgroundTasks, body: TriggerRequest
) -> AgentRun:
    """Create one explicit or legacy run for both the UI and queue worker."""
    workflow_id = body.workflow_id or ORCHESTRATOR_ID
    inputs = resolve_inputs(db, body.inputs)
    target = (body.target_issue_key or "").strip() or None
    if target:
        await _refuse_closed_ticket(target)
        inputs["target_issue_key"] = target

        # A direct/manual trigger must not start a second run for a ticket that
        # is already being processed. Queue ticks have their own row lock, but
        # the dashboard and AI Manager call this endpoint directly; without a
        # guard, two clicks (or a retried request) can remediate/notify twice.
        # Workbench follow-ups are intentional continuations of the parked run
        # and are therefore exempt from this check.
        if body.trigger != "workbench" and not body.parent_run_id:
            active = (
                db.query(AgentRun)
                .filter(
                    AgentRun.selected_issue_key == target,
                    AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                )
                .order_by(AgentRun.id.desc())
                .first()
            )
            if active:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{target} already has an active agent run "
                        f"({active.run_id}, status={active.status})"
                    ),
                )
            open_exception = (
                db.query(ExceptionItem)
                .filter(
                    ExceptionItem.primary_issue_key == target,
                    ExceptionItem.status.in_(
                        [ExceptionStatus.OPEN.value, ExceptionStatus.IN_REVIEW.value]
                    ),
                )
                .order_by(ExceptionItem.id.desc())
                .first()
            )
            if open_exception:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{target} has an unresolved Workbench exception "
                        f"({open_exception.id}); resolve or requeue it before starting another run"
                    ),
                )

    run = AgentRun(
        run_id=str(uuid.uuid4()),
        workflow_id=workflow_id,
        selected_issue_key=target,
        trigger=body.trigger,
        phase=body.phase,
        status=RunStatus.PENDING.value,
        issue_keys=[target] if target else None,
        inputs=inputs,
        parent_run_id=body.parent_run_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    background.add_task(asyncio.run, _consume(run.id, workflow_id, inputs))
    return run


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


def _backfill_selected_issue_key(run: AgentRun, payload: Any) -> None:
    """Recover the actual ticket for legacy blank-target runs."""
    if run.selected_issue_key:
        return
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend(
            payload.get(key)
            for key in ("issue_key", "Issue key", "target_issue_key")
        )
        for nested_key in ("output", "result", "ticket_data", "ticket"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                candidates.extend(
                    nested.get(key)
                    for key in ("issue_key", "Issue key", "target_issue_key")
                )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            run.selected_issue_key = value
            run.issue_keys = [value]
            return


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


# The orchestrator steps that delegate to Operator 4.
NOTIFICATION_STEPS = (
    "step_6_notif_auto",
    "step_6_notif_escalated",
    "step_6_notif_rejected",
    "step_6_notif_manual",
)


async def _persist_notification_result(
    client: AutoClient, by_step: dict[str, OperatorExecution], step_id: str
) -> None:
    """
    Store Operator 4's per-channel delivery result on the notification row.

    A delegating step's own output is only a link to the sub-workflow run, so
    the orchestrator step reads `completed` whether or not the message actually
    went out. Operator 4 knows better — it returns slack_delivery_status and
    email_delivery_status separately — and the Data Manager derives a channel's
    health from exactly this.

    Without it Outlook reported healthy while every send was failing on an
    ErrorExceededMessageLimit quota, because nobody looked past the step status.
    A false green on an integration panel is worse than an honest unknown.
    """
    row = by_step.get(step_id)
    if row is None:
        return
    result = await _step_result(client, by_step, step_id)
    if result:
        row.output = {**(row.output or {}), "operator_result": result}


def reclaim_orphaned_runs() -> int:
    """
    Mark runs abandoned by a dead process as failed. Call once at startup.

    _consume drives a run from an in-process background task, so any restart —
    deploy, crash, `docker compose restart` — abandons whatever it was
    streaming. Nothing revisits that row afterwards, so it counts as in-flight
    on the dashboard forever: one run sat at 'running' from 4 to 6 Aug across
    dozens of restarts, inflating the in-progress count and dragging the
    autonomy rate.

    Such a run genuinely cannot be recovered. Auto has no webhook to call us
    back and a Workflow API key cannot list run history, so the SSE stream that
    carried its events is the only record and it died with the process.
    Recording that honestly is the only correct outcome.

    Runs parked at a human form are untouched — they carry awaiting_human, not
    running, and are waiting on a person rather than on us.
    """
    db: Session = SessionLocal()
    try:
        orphaned = db.query(AgentRun).filter(AgentRun.status == RunStatus.RUNNING.value).all()
        for run in orphaned:
            run.status = RunStatus.FAILED.value
            run.error = (
                "Abandoned: the backend restarted while this run was streaming. "
                "Auto cannot replay a run, so its events are unrecoverable."
            )
            # Stamp an end time but deliberately leave duration_ms null rather
            # than calling _finalise. When the run actually died is unknown, and
            # dating it to this restart invents the elapsed time in between: run
            # 6 was reclaimed two days after it stalled and reported a 47-hour
            # duration, which pushed the dashboard's average run time from 106
            # seconds to 97 minutes.
            if run.ended_at is None:
                run.ended_at = datetime.now(timezone.utc)
            log.warning("reclaimed orphaned run %s (started %s)", run.run_id, run.started_at)
        db.commit()
        return len(orphaned)
    finally:
        db.close()


async def _backfill_parked_steps(
    client: AutoClient,
    db: Session,
    run: AgentRun,
    by_step: dict[str, OperatorExecution],
    *,
    reconcile_waiting_step: bool = False,
) -> None:
    """
    Record the steps that finish after we detach from the stream.

    Parking closes the stream the moment Auto's human form opens, but the
    escalate branch fans out: step_4_gate routes to step_4_rev AND to
    step_6_notif_escalated, and the notification completes a few seconds later.
    Without this the run timeline stops at the form, so the Workbench shows a
    CAB approval waiting with no evidence anyone was told — the run looks like
    it silently dropped the escalation, which is exactly what the dead-branch
    bug used to do.

    Polls rather than reading once: the notification took 8-20s to finish after
    step_4_rev started waiting. This holds the session for under a minute, not
    for the reviewer's thinking time, which is what detaching was protecting
    against.

    ``reconcile_waiting_step`` is used after the Command Center submits the
    human form. During the initial park the streamed ``step_4_rev`` row is
    intentionally left as ``running`` so the Workbench visibly shows a paused
    decision. Once the form is submitted, Auto changes that same activity to
    ``completed``; without this explicit reconciliation the Command Center
    would keep the old status forever and a queue item could never leave
    ``awaiting_human``.
    """
    if not run.auto_run_id:
        return
    seq = max((r.sequence or 0) for r in by_step.values()) if by_step else 0
    added: set[str] = set()
    for _ in range(4):
        await asyncio.sleep(8)
        try:
            detail = await client.get_run(run.auto_run_id)
        except AutoError as exc:  # a missed notification must not fail the run
            log.warning("backfill failed for run %s: %s", run.run_id, exc)
            return
        for activity in detail.get("activityRuns") or []:
            step_id = activity.get("stepId")
            outputs = activity.get("outputs") or {}
            # `conditionMet` marks a branch evaluation, not a step's own work.
            if not step_id or "conditionMet" in outputs:
                continue
            row = by_step.get(step_id)
            if row is None:
                seq += 1
                row = OperatorExecution(
                    agent_run_id=run.id,
                    operator_name=step_id,
                    step_id=step_id,
                    sequence=seq,
                    started_at=datetime.now(timezone.utc),
                )
                db.add(row)
                by_step[step_id] = row
                added.add(step_id)
            elif step_id not in added and not (
                reconcile_waiting_step and step_id == HUMAN_STEP_ID
            ):
                # Streamed rows are authoritative; never overwrite them. This
                # also leaves step_4_rev showing that it is still waiting.
                continue
            elif step_id == HUMAN_STEP_ID and reconcile_waiting_step:
                # The activity is the same human form that was streamed before
                # parking. Auto now has the authoritative post-decision status.
                added.add(step_id)
            # Keep polling our own rows: the first read usually catches the
            # notification mid-flight, and a row frozen at 'running' reads as a
            # hung step rather than a delivered message.
            row.status = activity.get("status")
            if outputs:
                row.output = outputs
            if row.status in _DONE or row.status in _FAILED:
                row.ended_at = datetime.now(timezone.utc)
                if row.started_at:
                    started = row.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    row.duration_ms = (
                        row.ended_at.replace(tzinfo=timezone.utc) - started
                    ).total_seconds() * 1000
                if step_id in NOTIFICATION_STEPS:
                    await _persist_notification_result(client, by_step, step_id)
        db.commit()
        if all(
            (by_step[s].status in _DONE or by_step[s].status in _FAILED) for s in added
        ) and added:
            break
    for step_id in added:
        log.info("run %s backfilled %s (%s)", run.run_id, step_id,
                 by_step[step_id].status)


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
    diagnosis = await _step_result(client, by_step, DIAGNOSIS_STEP_ID)

    issue_key = (
        gate.get("issue_key")
        or remediation.get("issue_key")
        or (run.issue_keys or [None])[0]
    )
    if issue_key and not run.selected_issue_key:
        run.selected_issue_key = issue_key
        run.issue_keys = [issue_key]

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

    # Warn when the proposed fix has no diagnostic basis.
    #
    # Operator 3 returns kb_article_id and workaround_for_requester even when
    # Operator 2 reported no match at all. ITSM-2020 ("Monitor flickering") was
    # diagnosed kb_match_found=false, kb_article_id=null, kb_confidence=0.0 —
    # and the review item still offered "KB-100 / Roll back NIC driver / apply
    # hotfix". KB-100 is "VPN drops after Windows security update", and no
    # article covers monitor flickering. Approving it wrote a VPN driver
    # rollback onto a monitor ticket.
    #
    # The gate did its job and the run did stop for a person. The failure is
    # that the person was handed a specific, plausible, fabricated fix with no
    # way to tell — a human-in-the-loop failure of information rather than of
    # control, and the harder kind to catch. Surfaced here rather than fixed in
    # Operator 3 because this is the surface the reviewer actually reads.
    proposed_kb = remediation.get("kb_article_id")
    if proposed_kb and diagnosis and not diagnosis.get("kb_match_found"):
        severity = Severity.CRITICAL.value
        unsupported = (
            f"UNVERIFIED FIX: diagnosis found no knowledge-base match "
            f"(confidence {diagnosis.get('kb_confidence', 0)}), yet remediation "
            f"proposes {proposed_kb}. Confirm the article actually applies to "
            f"'{diagnosis.get('summary') or issue_key}' before approving."
        )
        recommendation = f"{unsupported} {recommendation}".strip()

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
            "diagnosis": diagnosis,
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

    # The Workbench item exists and the reviewer can act; anything below is
    # timeline completeness, so it runs after the commit above.
    await _backfill_parked_steps(client, db, run, by_step)


def _summarise(run: AgentRun, operator_count: int) -> dict:
    return {
        "run_id": run.run_id,
        "auto_run_id": run.auto_run_id,
        "workflow_id": run.workflow_id,
        "trigger": run.trigger,
        "phase": run.phase,
        "status": run.status,
        "issue_keys": run.issue_keys,
        "selected_issue_key": run.selected_issue_key,
        "error": run.error,
        "duration_ms": run.duration_ms,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "operator_count": operator_count,
    }
