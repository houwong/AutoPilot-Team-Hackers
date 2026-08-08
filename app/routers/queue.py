"""Ticket queue control and processed-ticket history APIs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models.command_center import (
    AgentRun,
    ExceptionItem,
    QueueCampaign,
    QueueCampaignStatus,
    QueueItem,
    QueueItemState,
)
from ..routers.agent import (
    TriggerRequest,
    create_agent_run,
)
from ..services.queue import (
    NON_RETRYABLE_STATES,
    active_campaign,
    campaign_payload,
    claim_next,
    classify_run,
    planned_snapshots,
    reconcile_campaign,
    item_payload,
    ordered_campaign_items,
    synchronize_campaign,
)
from ..services.queue_planner import PlannerUnavailable

router = APIRouter(prefix="/queue", tags=["Ticket Queue"])


class PreviewRequest(BaseModel):
    limit: int = Field(10, ge=1, le=100)
    name: str | None = None
    source: str = Field("manual", pattern="^(manual|schedule)$")


class RequeueRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


def _active_run_item(db: Session, campaign_id: int) -> QueueItem | None:
    # Human Review parks only its own ticket. A different pending ticket may
    # continue, while create_agent_run still prevents same-key duplicates.
    return (
        db.query(QueueItem)
        .filter(
            QueueItem.campaign_id == campaign_id,
            QueueItem.state == QueueItemState.RUNNING.value,
        )
        .order_by(QueueItem.id)
        .first()
    )


async def _tick_campaign(
    db: Session,
    background: BackgroundTasks,
    campaign: QueueCampaign,
) -> dict[str, Any]:
    """Synchronize and start at most one explicit-target run."""
    synchronize_campaign(db, campaign)
    db.refresh(campaign)
    campaign.last_tick_at = datetime.now(timezone.utc)
    db.commit()

    if campaign.status != QueueCampaignStatus.RUNNING.value:
        return {"started": False, "reason": f"campaign_{campaign.status}"}
    active = _active_run_item(db, campaign.id)
    if active:
        return {
            "started": False,
            "reason": "active_run",
            "item": item_payload(active),
        }

    item = claim_next(db, campaign.id)
    if item is None:
        campaign.status = QueueCampaignStatus.COMPLETED.value
        campaign.completed_at = campaign.completed_at or datetime.now(timezone.utc)
        db.commit()
        return {"started": False, "reason": "campaign_complete"}

    try:
        run = await create_agent_run(
            db,
            background,
            TriggerRequest(trigger="schedule", target_issue_key=item.issue_key),
        )
    except HTTPException as exc:
        item.state = (
            QueueItemState.SKIPPED_CLOSED.value
            if exc.status_code in {404, 409}
            else QueueItemState.FAILED.value
        )
        item.outcome = item.state
        item.last_error = str(exc.detail)
        item.completed_at = datetime.now(timezone.utc)
        db.commit()
        return {"started": False, "reason": "target_refused", "item": item_payload(item)}
    except Exception as exc:  # noqa: BLE001 - queue state must remain auditable
        item.state = QueueItemState.FAILED.value
        item.outcome = item.state
        item.last_error = str(exc)[:1000]
        item.completed_at = datetime.now(timezone.utc)
        db.commit()
        return {"started": False, "reason": "run_creation_failed", "item": item_payload(item)}

    item.latest_run_id = run.run_id
    db.commit()
    return {
        "started": True,
        "item": item_payload(item),
        "run": {
            "run_id": run.run_id,
            "issue_key": run.selected_issue_key,
            "status": run.status,
        },
    }


@router.post("/campaigns/preview")
async def preview_campaign(body: PreviewRequest, db: Session = Depends(get_db)):
    current = active_campaign(db)
    if current and current.status in {
        QueueCampaignStatus.RUNNING.value,
        QueueCampaignStatus.PAUSED.value,
    }:
        raise HTTPException(status_code=409, detail="A queue campaign is already active")
    # Plan before touching an existing preview. A failed v2 planner must be a
    # safe read-only error, not an accidental loss of the last reviewable batch.
    try:
        snapshots, planner = await planned_snapshots(db, body.limit)
    except PlannerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if current and current.status == QueueCampaignStatus.PREVIEW.value:
        current.status = QueueCampaignStatus.CANCELLED.value
        db.commit()

    campaign = QueueCampaign(
        name=body.name or f"Ticket batch {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        source=body.source,
        status=QueueCampaignStatus.PREVIEW.value,
        batch_limit=body.limit,
        planner_mode=planner.mode,
        planner_run_id=planner.run_id,
        planner_generated_at=planner.generated_at,
        planner_effective_as_of=planner.effective_as_of,
        planner_stale=planner.stale,
        planner_policy_snapshot=planner.policy_snapshot,
    )
    db.add(campaign)
    db.flush()
    for snapshot in snapshots:
        ranking_evidence = snapshot.get("ranking_evidence")
        if ranking_evidence is None:
            ranking_evidence = {
                key: snapshot[key]
                for key in (
                    "sla_status",
                    "vip",
                    "priority_rank",
                    "rank_position",
                    "ranked_by",
                    "ranking_reason",
                    "assignment_group",
                    "major_incident_key",
                    "major_incident_action",
                    "incident_ticket_count",
                    "incident_vip_count",
                )
                if snapshot.get(key) is not None
            }
        db.add(
            QueueItem(
                campaign_id=campaign.id,
                issue_key=snapshot["issue_key"],
                source_status=snapshot["source_status"],
                source_priority=snapshot["source_priority"],
                source_updated_at=snapshot["source_updated_at"],
                state=QueueItemState.PREVIEW.value,
                attempt_count=0,
                # Freeze why it was ranked here, alongside the frozen batch.
                sla_status=snapshot.get("sla_status"),
                vip=snapshot.get("vip"),
                priority_rank=snapshot.get("priority_rank"),
                ranked_by=snapshot.get("ranked_by"),
                ranking_reason=snapshot.get("ranking_reason"),
                rank_position=snapshot.get("rank_position"),
                ranking_evidence=ranking_evidence or None,
            )
        )
    db.commit()
    db.refresh(campaign)
    return {
        "campaign": campaign_payload(db, campaign),
        "items": [item_payload(item) for item in ordered_campaign_items(campaign)],
        "warning": "Preview only; no Supervity run or Supabase write has started.",
    }


@router.post("/campaigns/{campaign_id}/confirm")
async def confirm_campaign(
    campaign_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    campaign = db.query(QueueCampaign).filter(QueueCampaign.id == campaign_id).first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != QueueCampaignStatus.PREVIEW.value:
        raise HTTPException(status_code=409, detail="Only a preview can be confirmed")
    items = db.query(QueueItem).filter(QueueItem.campaign_id == campaign.id).all()
    if not items:
        raise HTTPException(status_code=409, detail="Preview contains no eligible tickets")
    for item in items:
        item.state = QueueItemState.PENDING.value
    campaign.status = QueueCampaignStatus.RUNNING.value
    campaign.confirmed_at = datetime.now(timezone.utc)
    campaign.started_at = campaign.started_at or datetime.now(timezone.utc)
    db.commit()
    tick = await _tick_campaign(db, background, campaign)
    db.refresh(campaign)
    return {"campaign": campaign_payload(db, campaign), "tick": tick}


@router.get("/campaigns/active")
async def get_active_campaign(db: Session = Depends(get_db)):
    campaign = active_campaign(db)
    if not campaign:
        return {"campaign": None}
    synchronize_campaign(db, campaign)
    # Also verify here, not only on tick: a demo may never tick, and a reviewer
    # opening the page should not be shown "auto_remediated" for a ticket whose
    # row never changed.
    await reconcile_campaign(db, campaign)
    db.refresh(campaign)
    return {"campaign": campaign_payload(db, campaign)}


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.query(QueueCampaign).filter(QueueCampaign.id == campaign_id).first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {
        "campaign": campaign_payload(db, campaign),
        "items": [item_payload(item) for item in ordered_campaign_items(campaign)],
    }


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.query(QueueCampaign).filter(QueueCampaign.id == campaign_id).first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != QueueCampaignStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail="Only a running campaign can be paused")
    campaign.status = QueueCampaignStatus.PAUSED.value
    db.commit()
    return {"campaign": campaign_payload(db, campaign)}


@router.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(
    campaign_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    campaign = db.query(QueueCampaign).filter(QueueCampaign.id == campaign_id).first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status != QueueCampaignStatus.PAUSED.value:
        raise HTTPException(status_code=409, detail="Only a paused campaign can resume")
    campaign.status = QueueCampaignStatus.RUNNING.value
    db.commit()
    tick = await _tick_campaign(db, background, campaign)
    return {"campaign": campaign_payload(db, campaign), "tick": tick}


@router.post("/campaigns/{campaign_id}/cancel")
def cancel_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.query(QueueCampaign).filter(QueueCampaign.id == campaign_id).first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status in {QueueCampaignStatus.COMPLETED.value, QueueCampaignStatus.CANCELLED.value}:
        raise HTTPException(status_code=409, detail="Campaign is already terminal")
    db.query(QueueItem).filter(
        QueueItem.campaign_id == campaign.id,
        QueueItem.state.in_([QueueItemState.PREVIEW.value, QueueItemState.PENDING.value]),
    ).update({"state": QueueItemState.CANCELLED.value}, synchronize_session=False)
    campaign.status = QueueCampaignStatus.CANCELLED.value
    campaign.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {"campaign": campaign_payload(db, campaign)}


@router.post("/tick")
async def tick(
    background: BackgroundTasks,
    campaign_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    campaign = (
        db.query(QueueCampaign).filter(QueueCampaign.id == campaign_id).first()
        if campaign_id
        else db.query(QueueCampaign)
        .filter(QueueCampaign.status == QueueCampaignStatus.RUNNING.value)
        .order_by(QueueCampaign.id.desc())
        .first()
    )
    if campaign is None:
        return {"started": False, "reason": "no_running_campaign"}
    result = await _tick_campaign(db, background, campaign)
    # Verify write-path outcomes here rather than inside synchronize_campaign:
    # this needs a Supabase read, and synchronize runs from synchronous request
    # paths. The tick is the natural place — it is the recurring heartbeat, so
    # every finished item gets checked without anyone having to open a page.
    await reconcile_campaign(db, campaign)
    return {"campaign": campaign_payload(db, campaign), "tick": result}


@router.get("/items")
def list_items(
    state: str | None = None,
    outcome: str | None = None,
    campaign_id: int | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    # Keep the history endpoint authoritative on its own.  The Processed
    # Tickets page loads the active campaign and the item history in parallel;
    # relying on the campaign request to synchronize first made the serialized
    # item state race the two requests and briefly left a resolved review shown
    # as ``awaiting_human``.  Synchronize the relevant campaign before reading
    # queue rows so every caller sees the persisted terminal outcome.
    campaign = (
        db.query(QueueCampaign).filter(QueueCampaign.id == campaign_id).first()
        if campaign_id
        else active_campaign(db)
    )
    if campaign and campaign.status in {
        QueueCampaignStatus.PREVIEW.value,
        QueueCampaignStatus.RUNNING.value,
        QueueCampaignStatus.PAUSED.value,
    }:
        synchronize_campaign(db, campaign)

    requested_state = state
    query = db.query(QueueItem)
    if requested_state:
        query = query.filter(QueueItem.state == requested_state)
    if outcome:
        query = query.filter(QueueItem.outcome == outcome)
    if campaign_id:
        query = query.filter(QueueItem.campaign_id == campaign_id)
    if search:
        query = query.filter(QueueItem.issue_key.ilike(f"%{search.strip()}%"))
    queue_items = (
        query.order_by(QueueItem.id.desc())
        .all()
    )
    linked_runs = {item.latest_run_id for item in db.query(QueueItem).all() if item.latest_run_id}
    manual_items: list[dict[str, Any]] = []
    for run in db.query(AgentRun).filter(AgentRun.run_id.notin_(linked_runs or {""})).all():
        if campaign_id:
            continue
        keys = []
        if run.selected_issue_key:
            keys = [run.selected_issue_key]
        elif isinstance(run.issue_keys, list):
            keys = [str(key) for key in run.issue_keys if key]
        if not keys:
            continue
        exception = (
            db.query(ExceptionItem)
            .filter(ExceptionItem.agent_run_id == run.id)
            .order_by(ExceptionItem.id.desc())
            .first()
        )
        run_state = classify_run(db, run, exception)
        key = keys[0]
        synthetic = {
            "id": -run.id,
            "campaign_id": 0,
            "issue_key": key,
            "source_status": "Manual run",
            "source_priority": None,
            "source_updated_at": None,
            "state": run_state or QueueItemState.COMPLETED_UNKNOWN.value,
            "outcome": run_state or QueueItemState.COMPLETED_UNKNOWN.value,
            "latest_run_id": run.run_id,
            "attempt_count": 1,
            "last_error": run.error,
            "requeued_from_id": None,
            "requeue_reason": None,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "completed_at": run.ended_at,
            "history_source": "manual",
        }
        if requested_state and synthetic["state"] != requested_state:
            continue
        manual_items.append(synthetic)

    records = [item_payload(item) for item in queue_items]
    for record in records:
        record["history_source"] = "queue"
    records.extend(manual_items)
    if outcome:
        records = [record for record in records if record.get("outcome") == outcome]
    if search:
        records = [record for record in records if search.strip().lower() in record["issue_key"].lower()]
    # Queue history is presented in the frozen planner order within the newest
    # campaign. Legacy/manual rows without a rank remain deterministic at the
    # end rather than being reordered by response timing.
    records.sort(
        key=lambda record: (
            0 if record.get("history_source") == "queue" else 1,
            -int(record.get("campaign_id") or 0),
            record.get("rank_position") is None,
            record.get("rank_position") if record.get("rank_position") is not None else 10**9,
            int(record.get("id") or 0),
        )
    )
    total = len(records)
    records = records[(page - 1) * page_size : page * page_size]
    return {
        "items": records,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/items/{item_id}/requeue")
async def requeue_item(
    item_id: int,
    body: RequeueRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if item_id <= 0:
        raise HTTPException(status_code=400, detail="Manual runs must be requeued from their original queue item")
    old = db.query(QueueItem).filter(QueueItem.id == item_id).first()
    if old is None:
        raise HTTPException(status_code=404, detail="Queue item not found")
    if old.state not in NON_RETRYABLE_STATES:
        raise HTTPException(status_code=409, detail="Only a terminal item can be requeued")

    campaign = (
        db.query(QueueCampaign)
        .filter(QueueCampaign.status == QueueCampaignStatus.RUNNING.value)
        .order_by(QueueCampaign.id.desc())
        .first()
    )
    if campaign is None:
        campaign = QueueCampaign(
            name=f"Requeue {old.issue_key}",
            source="manual",
            status=QueueCampaignStatus.RUNNING.value,
            batch_limit=1,
            confirmed_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
        db.add(campaign)
        db.flush()

    duplicate = db.query(QueueItem).filter(
        QueueItem.campaign_id == campaign.id,
        QueueItem.issue_key == old.issue_key,
        QueueItem.state.in_([
            QueueItemState.PREVIEW.value,
            QueueItemState.PENDING.value,
            QueueItemState.RUNNING.value,
            QueueItemState.AWAITING_HUMAN.value,
        ]),
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="Ticket is already active in a queue")
    new_item = QueueItem(
        campaign_id=campaign.id,
        issue_key=old.issue_key,
        source_status=old.source_status,
        source_priority=old.source_priority,
        source_updated_at=old.source_updated_at,
        state=QueueItemState.PENDING.value,
        attempt_count=0,
        requeued_from_id=old.id,
        requeue_reason=body.reason.strip(),
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    tick_result = await _tick_campaign(db, background, campaign)
    return {"item": item_payload(new_item), "tick": tick_result}
