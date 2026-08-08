"""Persistent ticket queue and processed-history helpers.

The queue deliberately sits outside Supervity Auto. Auto's workflow is a
single-ticket decision graph, so the Command Center snapshots a bounded batch
and starts one explicit-target run at a time.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.command_center import (
    AgentRun,
    ExceptionItem,
    ExceptionStatus,
    OperatorExecution,
    QueueCampaign,
    QueueCampaignStatus,
    QueueItem,
    QueueItemState,
    RunStatus,
)
from . import supabase
from .auto_client import AutoClient, AutoError
from .operator_steps import DONE_STEP_STATUSES, canonical_step_map
from .queue_planner import rank_index, ranked_backlog, ranking_reason
from .reconciliation import VERIFICATION_FAILED, verify_ticket

# A delegating step's stored output holds only a link to the operator's own run;
# the structured result has to be fetched from there.
_SUB_RUN_RE = re.compile(r"/runs/([0-9a-f-]{20,})", re.I)

log = logging.getLogger(__name__)

ACTIVE_TICKET_STATUSES = {
    "open",
    "in progress",
    "waiting for support",
    "waiting for customer",
}

NON_RETRYABLE_STATES = {
    QueueItemState.AUTO_REMEDIATED.value,
    QueueItemState.HUMAN_APPROVED.value,
    QueueItemState.BLOCKED.value,
    QueueItemState.HUMAN_REJECTED.value,
    QueueItemState.FAILED.value,
    QueueItemState.SKIPPED_CLOSED.value,
    QueueItemState.COMPLETED_UNKNOWN.value,
}

PRIORITY_ORDER = {
    "highest": 0,
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _field(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _snapshot(row: dict[str, Any]) -> dict[str, Any] | None:
    issue_key = str(_field(row, "issue_key", "Issue key", "issueKey") or "").strip()
    status = str(_field(row, "status", "Status") or "").strip()
    if not issue_key or status.lower() not in ACTIVE_TICKET_STATUSES:
        return None
    return {
        "issue_key": issue_key,
        "source_status": status,
        "source_priority": str(_field(row, "priority", "Priority") or "unknown"),
        "source_updated_at": str(_field(row, "updated", "Updated") or ""),
    }


async def eligible_snapshots(db: Session, limit: int = 10) -> list[dict[str, Any]]:
    """Read and rank eligible tickets without changing Supabase."""
    limit = max(1, min(int(limit), 100))

    # Fetch enough of the backlog to rank meaningfully.
    #
    # This used to read `limit * 5` rows — fifteen for a batch of three — and
    # sort those. Ranking an arbitrary page is not ranking: whichever rows
    # PostgREST happened to return first became the candidates, so the top of
    # the queue was decided by storage order rather than by SLA. Read the whole
    # open backlog instead; it is a few hundred rows of five columns, and the
    # ordering is the entire point of the step.
    rows = await supabase.select(
        "issues",
        {
            "select": '"Issue key","Status","Priority","Updated",row_id',
            '"Status"': "in.(Open,In Progress,Waiting for support,Waiting for customer)",
            "limit": "1000",
        },
    )
    candidates = [s for row in rows if (s := _snapshot(row))]

    historical = {
        key
        for (key,) in db.query(QueueItem.issue_key)
        .filter(QueueItem.state.in_(NON_RETRYABLE_STATES))
        .distinct()
        .all()
    }
    active_queue = {
        key
        for (key,) in db.query(QueueItem.issue_key)
        .filter(
            QueueItem.state.in_(
                [
                    QueueItemState.PREVIEW.value,
                    QueueItemState.PENDING.value,
                    QueueItemState.RUNNING.value,
                    QueueItemState.AWAITING_HUMAN.value,
                ]
            )
        )
        .distinct()
        .all()
    }
    active_runs = db.query(AgentRun).filter(
        AgentRun.status.in_([
            RunStatus.PENDING.value,
            RunStatus.RUNNING.value,
            RunStatus.AWAITING_HUMAN.value,
        ])
    ).all()
    active_run_keys = {
        run.selected_issue_key.strip()
        for run in active_runs
        if run.selected_issue_key and run.selected_issue_key.strip()
    }
    terminal_run_keys: set[str] = set()
    for run in db.query(AgentRun).filter(
        AgentRun.status.in_([
            RunStatus.SUCCEEDED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        ])
    ).all():
        keys = [run.selected_issue_key] if run.selected_issue_key else (
            run.issue_keys if isinstance(run.issue_keys, list) else []
        )
        terminal_run_keys.update(str(key).strip() for key in keys if str(key).strip())
    open_exceptions = {
        item.primary_issue_key
        for item in db.query(ExceptionItem)
        .filter(ExceptionItem.status.in_([ExceptionStatus.OPEN.value, ExceptionStatus.IN_REVIEW.value]))
        .all()
        if item.primary_issue_key
    }
    excluded = historical | active_queue | active_run_keys | terminal_run_keys | open_exceptions

    # Order by Operator 1's SLA-aware ranking, not the stored Priority column.
    #
    # Sorting on Priority answers the wrong question: a Low ticket whose SLA has
    # already breached for a VIP outranks a Highest ticket sitting comfortably
    # within target, and the stored field puts them the wrong way round.
    # Operator 1 ranks on (SLA status, VIP), which is the order the queue
    # planning note asks for, and asking it keeps the preview traceable to
    # operator evidence rather than to a constant in this file.
    ranking = rank_index(await ranked_backlog(db))

    def sort_key(item: dict[str, Any]) -> tuple[int, int, str, str]:
        evidence = ranking.get(item["issue_key"])
        # Unranked tickets sort after ranked ones rather than being dropped:
        # Operator 1 filters out records it considers ineligible, and silently
        # losing them here would hide work rather than defer it.
        if evidence is None:
            return (
                1,
                PRIORITY_ORDER.get(item["source_priority"].strip().lower(), 9),
                item["source_updated_at"] or "9999-12-31",
                item["issue_key"],
            )
        return (0, int(evidence["position"]), "", item["issue_key"])

    result: list[dict[str, Any]] = []
    for item in sorted(candidates, key=sort_key):
        if item["issue_key"] in excluded:
            continue
        evidence = ranking.get(item["issue_key"])
        item["sla_status"] = (evidence or {}).get("sla_status")
        item["vip"] = (evidence or {}).get("vip")
        item["priority_rank"] = (evidence or {}).get("priority_rank")
        item["ranked_by"] = "operator_1" if evidence else "source_priority"
        item["ranking_reason"] = ranking_reason(evidence)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def campaign_counts(db: Session, campaign: QueueCampaign) -> dict[str, int]:
    counts: dict[str, int] = {}
    for (state,) in db.query(QueueItem.state).filter(QueueItem.campaign_id == campaign.id).all():
        counts[state] = counts.get(state, 0) + 1
    counts["total"] = sum(counts.values())
    counts["processed"] = sum(
        counts.get(state, 0) for state in NON_RETRYABLE_STATES
    )
    return counts


def campaign_payload(db: Session, campaign: QueueCampaign) -> dict[str, Any]:
    return {
        "id": campaign.id,
        "name": campaign.name,
        "source": campaign.source,
        "status": campaign.status,
        "batch_limit": campaign.batch_limit,
        "created_by": campaign.created_by,
        "created_at": campaign.created_at,
        "confirmed_at": campaign.confirmed_at,
        "started_at": campaign.started_at,
        "completed_at": campaign.completed_at,
        "last_tick_at": campaign.last_tick_at,
        "counts": campaign_counts(db, campaign),
    }


def item_payload(item: QueueItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "campaign_id": item.campaign_id,
        "issue_key": item.issue_key,
        "source_status": item.source_status,
        "source_priority": item.source_priority,
        "source_updated_at": item.source_updated_at,
        "state": item.state,
        "outcome": item.outcome,
        "latest_run_id": item.latest_run_id,
        "attempt_count": item.attempt_count,
        "last_error": item.last_error,
        "requeued_from_id": item.requeued_from_id,
        "requeue_reason": item.requeue_reason,
        "created_at": item.created_at,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
        # `outcome` says which path the agent took; `verification` says whether
        # the ticket actually changed. Kept separate on purpose — reporting one
        # as the other is what let a campaign call ITSM-2003 human_approved
        # while Supabase still had it untouched.
        "verification": item.verification,
        "verification_detail": item.verification_detail,
        "verified_at": item.verified_at,
        "sla_status": item.sla_status,
        "vip": item.vip,
        "priority_rank": item.priority_rank,
        "ranked_by": item.ranked_by,
        "ranking_reason": item.ranking_reason,
    }


def active_campaign(db: Session) -> QueueCampaign | None:
    return (
        db.query(QueueCampaign)
        .filter(QueueCampaign.status.in_([QueueCampaignStatus.PREVIEW.value, QueueCampaignStatus.RUNNING.value, QueueCampaignStatus.PAUSED.value]))
        .order_by(QueueCampaign.id.desc())
        .first()
    )


def _operator_steps(db: Session, run: AgentRun) -> list[OperatorExecution]:
    return (
        db.query(OperatorExecution)
        .filter(OperatorExecution.agent_run_id == run.id)
        .all()
    )


def classify_run(db: Session, run: AgentRun, exception: ExceptionItem | None = None) -> str | None:
    """Return a terminal queue state only when the persisted path proves it."""
    if run.status == RunStatus.AWAITING_HUMAN.value:
        return QueueItemState.AWAITING_HUMAN.value
    if run.status in {RunStatus.PENDING.value, RunStatus.RUNNING.value}:
        return QueueItemState.RUNNING.value
    if run.status in {RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
        return QueueItemState.FAILED.value

    steps = _operator_steps(db, run)
    by_step = canonical_step_map(steps)
    if (
        exception
        and exception.status == ExceptionStatus.RESOLVED.value
        and exception.resolution == "rejected"
        and by_step.get("step_6_notif_rejected")
        and by_step["step_6_notif_rejected"].status in DONE_STEP_STATUSES
    ):
        return QueueItemState.HUMAN_REJECTED.value
    if by_step.get("step_6_notif_auto") and by_step["step_6_notif_auto"].status in DONE_STEP_STATUSES:
        return QueueItemState.AUTO_REMEDIATED.value
    if by_step.get("step_6_notif_rejected") and by_step["step_6_notif_rejected"].status in DONE_STEP_STATUSES:
        return QueueItemState.BLOCKED.value
    if by_step.get("step_6_notif_manual") and by_step["step_6_notif_manual"].status in DONE_STEP_STATUSES:
        if exception and exception.resolution == "rejected":
            return QueueItemState.HUMAN_REJECTED.value
        return QueueItemState.HUMAN_APPROVED.value
    return QueueItemState.COMPLETED_UNKNOWN.value


def synchronize_campaign(db: Session, campaign: QueueCampaign) -> None:
    """Synchronize queue items from persisted run and Workbench records."""
    items = (
        db.query(QueueItem)
        .filter(
            QueueItem.campaign_id == campaign.id,
            QueueItem.state.in_([
                QueueItemState.RUNNING.value,
                QueueItemState.AWAITING_HUMAN.value,
            ]),
        )
        .all()
    )
    for item in items:
        if not item.latest_run_id:
            continue
        run = db.query(AgentRun).filter(AgentRun.run_id == item.latest_run_id).first()
        if not run:
            continue
        exception = (
            db.query(ExceptionItem)
            .filter(ExceptionItem.agent_run_id == run.id)
            .order_by(ExceptionItem.id.desc())
            .first()
        )
        run_for_state = run
        if exception and exception.status == ExceptionStatus.RESOLVED.value:
            # Some Workbench approvals resume the parked Auto run; others use
            # the documented fallback follow-up run. Track whichever run is
            # actually carrying the post-decision execution.
            if exception.follow_up_run_id:
                follow_up = db.query(AgentRun).filter(
                    AgentRun.run_id == exception.follow_up_run_id
                ).first()
                if follow_up:
                    run_for_state = follow_up
                    item.latest_run_id = follow_up.run_id
            else:
                # The resolver marks the parent succeeded immediately after
                # submitting Auto's form. Wait for the manual notification
                # step before declaring a terminal result.
                terminal_notification = (
                    "step_6_notif_manual"
                    if exception.resolution in {"approved", "modified"}
                    else "step_6_notif_rejected"
                )
                manual_step = canonical_step_map(
                    db.query(OperatorExecution).filter(
                        OperatorExecution.agent_run_id == run.id,
                        OperatorExecution.step_id == terminal_notification,
                    ).all()
                ).get(terminal_notification)
                if not manual_step or manual_step.status not in DONE_STEP_STATUSES:
                    item.state = QueueItemState.AWAITING_HUMAN.value
                    continue
        state = classify_run(db, run_for_state, exception)
        if (
            exception
            and exception.status == ExceptionStatus.RESOLVED.value
            and exception.resolution in {"approved", "modified"}
            and state == QueueItemState.AUTO_REMEDIATED.value
        ):
            state = QueueItemState.HUMAN_APPROVED.value
        if state is None:
            continue
        if state in {QueueItemState.RUNNING.value, QueueItemState.AWAITING_HUMAN.value}:
            item.state = state
            continue
        if state == QueueItemState.FAILED.value and (item.attempt_count or 0) < 2:
            # A transient Auto/API failure gets one retry on the next tick.
            # After the second failed attempt the item remains terminal until
            # an administrator explicitly requeues it.
            item.state = QueueItemState.PENDING.value
            item.outcome = None
            item.last_error = run.error
            continue
        item.state = state
        item.outcome = state
        item.last_error = run_for_state.error
        item.completed_at = item.completed_at or _now()

    pending = db.query(QueueItem).filter(
        QueueItem.campaign_id == campaign.id,
        QueueItem.state.in_([
            QueueItemState.PENDING.value,
            QueueItemState.RUNNING.value,
            QueueItemState.AWAITING_HUMAN.value,
        ]),
    ).count()
    if campaign.status == QueueCampaignStatus.RUNNING.value and pending == 0:
        campaign.status = QueueCampaignStatus.COMPLETED.value
        campaign.completed_at = campaign.completed_at or _now()
    db.commit()


async def reconcile_campaign(db: Session, campaign: QueueCampaign) -> int:
    """
    Check that tickets the queue calls remediated actually changed.

    `synchronize_campaign` decides an outcome from the workflow path: it sees
    step_6_notif_auto complete and records `auto_remediated`. That is evidence
    the orchestrator finished a branch, not that the service desk moved. The two
    have disagreed — a campaign reported ITSM-2003 as `human_approved` while
    Supabase still held it at "Waiting for support" with no resolution, because
    the ticket had no knowledge-base article and Operator 3 had correctly
    declined to act.

    So a write-path outcome is re-read from Supabase and compared against the
    `expected_state` Operator 3 reported. The result is recorded beside the
    outcome rather than replacing it: `outcome` stays the answer to "which path
    did the agent take", and `verification` answers "did the ticket change".
    Collapsing those into one word is what produced the discrepancy.

    Runs separately from synchronize_campaign because it needs network I/O, and
    that function is called from synchronous request paths. Returns the number
    of items verified so callers can log it.

    Deliberately does not retry or repair. Its whole job is to notice, and a
    mismatch surfaces as `verification_failed` for a person to look at.
    """
    items = (
        db.query(QueueItem)
        .filter(
            QueueItem.campaign_id == campaign.id,
            QueueItem.verification.is_(None),
            QueueItem.state.in_([
                QueueItemState.AUTO_REMEDIATED.value,
                QueueItemState.HUMAN_APPROVED.value,
            ]),
        )
        .all()
    )
    if not items:
        return 0

    client = AutoClient()
    checked = 0
    for item in items:
        remediation: dict = {}
        run = (
            db.query(AgentRun).filter(AgentRun.run_id == item.latest_run_id).first()
            if item.latest_run_id
            else None
        )
        if run:
            by_step = canonical_step_map(
                db.query(OperatorExecution)
                .filter(OperatorExecution.agent_run_id == run.id)
                .all()
            )
            # Prefer the executing step: step_5_exec runs after an approval and
            # carries the state that was actually written, where step_3_rem may
            # only hold the pre-approval recommendation.
            for step_id in ("step_5_exec", "step_3_rem"):
                row = by_step.get(step_id)
                if row is None:
                    continue
                try:
                    result = await _remediation_result(client, row)
                except AutoError as exc:
                    log.warning("could not read %s for verification: %s", step_id, exc)
                    continue
                if result:
                    remediation = result
                    break

        result, detail = await verify_ticket(item.issue_key, remediation)
        item.verification = result
        item.verification_detail = detail
        item.verified_at = _now()
        checked += 1
        if result == VERIFICATION_FAILED:
            log.error(
                "queue item %s (%s) reported %s but Supabase disagrees: %s",
                item.id,
                item.issue_key,
                item.state,
                detail.get("reason"),
            )
    db.commit()
    return checked


async def _remediation_result(client: AutoClient, row: OperatorExecution) -> dict:
    """Operator 3's structured result for one recorded step, via its sub-run."""
    output = row.output if isinstance(row.output, dict) else {}
    inner = output.get("operator_result")
    if isinstance(inner, dict) and inner:
        return inner
    html = ((output.get("displayData") or {}).get("html")) or ""
    match = _SUB_RUN_RE.search(html)
    if not match:
        return {}
    return await client.get_step_result(match.group(1))


def claim_next(db: Session, campaign_id: int) -> QueueItem | None:
    """Claim one pending item; row locking protects multi-worker ticks."""
    item = (
        db.query(QueueItem)
        .filter(
            QueueItem.campaign_id == campaign_id,
            QueueItem.state == QueueItemState.PENDING.value,
        )
        .order_by(QueueItem.id)
        .with_for_update(skip_locked=True)
        .first()
    )
    if item is None:
        return None
    item.state = QueueItemState.RUNNING.value
    item.attempt_count = (item.attempt_count or 0) + 1
    item.started_at = item.started_at or _now()
    db.commit()
    db.refresh(item)
    return item
