"""Unit tests for queue selection and outcome classification."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.command_center import (
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
from app.routers import queue as queue_router
from app.services import queue as queue_service
from app.services.queue_planner import PlannerResult, PlannerUnavailable


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.mark.asyncio
async def test_preview_orders_priority_and_excludes_active_or_terminal(db, monkeypatch):
    campaign = QueueCampaign(
        name="existing",
        source="manual",
        status=QueueCampaignStatus.RUNNING.value,
        batch_limit=10,
    )
    db.add(campaign)
    db.flush()
    db.add_all(
        [
            QueueItem(
                campaign_id=campaign.id,
                issue_key="ITSM-DONE",
                state=QueueItemState.AUTO_REMEDIATED.value,
                attempt_count=1,
            ),
            QueueItem(
                campaign_id=campaign.id,
                issue_key="ITSM-ACTIVE",
                state=QueueItemState.PENDING.value,
                attempt_count=0,
            ),
            AgentRun(
                run_id="already-processed",
                selected_issue_key="ITSM-MANUAL",
                status=RunStatus.SUCCEEDED.value,
            ),
        ]
    )
    db.commit()

    async def fake_select(_table, _params):
        return [
            {"Issue key": "ITSM-LOW", "Status": "Open", "Priority": "Low", "Updated": "2026-07-01"},
            {"Issue key": "ITSM-HIGH", "Status": "Open", "Priority": "High", "Updated": "2026-07-10"},
            {"Issue key": "ITSM-DONE", "Status": "Open", "Priority": "Highest", "Updated": "2026-07-01"},
            {"Issue key": "ITSM-ACTIVE", "Status": "Open", "Priority": "Highest", "Updated": "2026-07-01"},
            {"Issue key": "ITSM-MANUAL", "Status": "Open", "Priority": "Highest", "Updated": "2026-07-01"},
        ]

    monkeypatch.setattr(queue_service.supabase, "select", fake_select)
    result = await queue_service.eligible_snapshots(db, 10)

    assert [item["issue_key"] for item in result] == ["ITSM-HIGH", "ITSM-LOW"]


@pytest.mark.asyncio
async def test_planned_snapshots_returns_v2_evidence_metadata(db, monkeypatch):
    monkeypatch.setenv("QUEUE_PLANNER_MODE", "supervity_v2")

    async def fake_select(_table, _params):
        return [
            {"Issue key": "ITSM-PLANNED", "Status": "Open", "Priority": "Low", "Updated": "2026-07-01"}
        ]

    async def fake_planner(_db, force_refresh: bool = False):
        return PlannerResult(
            tickets=[
                {
                    "issue_key": "ITSM-PLANNED",
                    "priority_rank": 1,
                    "rank_position": 1,
                    "source_priority": "Highest",
                    "sla_status": "Breached",
                    "vip": True,
                    "major_incident_key": "INC-9001",
                    "incident_ticket_count": 24,
                    "incident_vip_count": 3,
                }
            ],
            mode="queue_planner",
            run_id="planner-run",
            effective_as_of="2026-07-25T00:00:00Z",
        )

    monkeypatch.setattr(queue_service.supabase, "select", fake_select)
    monkeypatch.setattr(queue_service, "planner_result", fake_planner)

    snapshots, result = await queue_service.planned_snapshots(db, 10)

    assert snapshots[0]["ranked_by"] == "queue_planner"
    assert snapshots[0]["major_incident_key"] == "INC-9001"
    assert snapshots[0]["source_priority"] == "Low"
    assert result.run_id == "planner-run"


@pytest.mark.asyncio
async def test_v2_preview_persists_planner_evidence(db, monkeypatch):
    monkeypatch.setenv("QUEUE_PLANNER_MODE", "supervity_v2")

    async def fake_planned(_db, limit, force_refresh=False):
        return (
            [
                {
                    "issue_key": "ITSM-PLANNED",
                    "source_status": "Open",
                    "source_priority": "Low",
                    "source_updated_at": "2026-07-01",
                    "sla_status": "Breached",
                    "vip": True,
                    "priority_rank": 1,
                    "rank_position": 1,
                    "ranked_by": "queue_planner",
                    "ranking_reason": "Breached VIP; INC-9001",
                    "major_incident_key": "INC-9001",
                    "major_incident_action": "attach_to_existing",
                    "incident_ticket_count": 24,
                    "incident_vip_count": 3,
                }
            ],
            PlannerResult(
                tickets=[],
                mode="queue_planner",
                run_id="planner-run",
                generated_at="2026-08-08T04:00:00Z",
                effective_as_of="2026-07-25T00:00:00Z",
                policy_snapshot={"as_of": "2026-07-25T00:00:00Z"},
            ),
        )

    monkeypatch.setattr(queue_router, "planned_snapshots", fake_planned)

    response = await queue_router.preview_campaign(
        queue_router.PreviewRequest(limit=1, name="v2 preview"), db
    )

    campaign = response["campaign"]
    item = response["items"][0]
    assert campaign["planner_mode"] == "queue_planner"
    assert campaign["planner_run_id"] == "planner-run"
    assert campaign["planner_policy_snapshot"] == {"as_of": "2026-07-25T00:00:00Z"}
    assert item["rank_position"] == 1
    assert item["ranking_evidence"]["major_incident_key"] == "INC-9001"


@pytest.mark.asyncio
async def test_v2_planner_failure_preserves_existing_preview(db, monkeypatch):
    monkeypatch.setenv("QUEUE_PLANNER_MODE", "supervity_v2")
    current = QueueCampaign(
        name="keep-me",
        source="manual",
        status=QueueCampaignStatus.PREVIEW.value,
        batch_limit=1,
    )
    db.add(current)
    db.commit()

    async def fail(_db, limit, force_refresh=False):
        raise PlannerUnavailable("Queue Planner unavailable")

    monkeypatch.setattr(queue_router, "planned_snapshots", fail)

    with pytest.raises(queue_router.HTTPException) as error:
        await queue_router.preview_campaign(
            queue_router.PreviewRequest(limit=1), db
        )

    assert error.value.status_code == 503
    db.refresh(current)
    assert current.status == QueueCampaignStatus.PREVIEW.value


def test_classify_run_requires_a_recognized_terminal_step(db):
    run = AgentRun(
        run_id="run-unknown",
        status=RunStatus.SUCCEEDED.value,
        issue_keys=["ITSM-1"],
    )
    db.add(run)
    db.commit()
    assert queue_service.classify_run(db, run) == QueueItemState.COMPLETED_UNKNOWN.value


def test_parked_human_review_does_not_block_next_queue_tick(db):
    campaign = QueueCampaign(
        name="continue-after-review",
        source="manual",
        status=QueueCampaignStatus.RUNNING.value,
        batch_limit=2,
    )
    db.add(campaign)
    db.flush()
    db.add_all(
        [
            QueueItem(
                campaign_id=campaign.id,
                issue_key="ITSM-REVIEW",
                state=QueueItemState.AWAITING_HUMAN.value,
                attempt_count=1,
            ),
            QueueItem(
                campaign_id=campaign.id,
                issue_key="ITSM-NEXT",
                state=QueueItemState.PENDING.value,
                attempt_count=0,
            ),
        ]
    )
    db.commit()

    assert queue_router._active_run_item(db, campaign.id) is None


def test_running_item_still_blocks_next_queue_tick(db):
    campaign = QueueCampaign(
        name="one-active-run",
        source="manual",
        status=QueueCampaignStatus.RUNNING.value,
        batch_limit=2,
    )
    db.add(campaign)
    db.flush()
    running = QueueItem(
        campaign_id=campaign.id,
        issue_key="ITSM-RUNNING",
        state=QueueItemState.RUNNING.value,
        attempt_count=1,
    )
    db.add(running)
    db.commit()

    assert queue_router._active_run_item(db, campaign.id).id == running.id


def test_classify_run_distinguishes_auto_block_and_human_outcomes(db):
    auto = AgentRun(run_id="run-auto", status=RunStatus.SUCCEEDED.value)
    blocked = AgentRun(run_id="run-block", status=RunStatus.SUCCEEDED.value)
    human = AgentRun(run_id="run-human", status=RunStatus.SUCCEEDED.value)
    db.add_all([auto, blocked, human])
    db.flush()
    db.add_all(
        [
            OperatorExecution(agent_run_id=auto.id, step_id="step_6_notif_auto", status="completed"),
            OperatorExecution(agent_run_id=blocked.id, step_id="step_6_notif_rejected", status="completed"),
            OperatorExecution(agent_run_id=human.id, step_id="step_6_notif_manual", status="completed"),
        ]
    )
    db.commit()
    assert queue_service.classify_run(db, auto) == QueueItemState.AUTO_REMEDIATED.value
    assert queue_service.classify_run(db, blocked) == QueueItemState.BLOCKED.value
    assert queue_service.classify_run(db, human) == QueueItemState.HUMAN_APPROVED.value


def test_classify_run_distinguishes_human_rejection_from_gate_block(db):
    run = AgentRun(run_id="run-human-rejected", status=RunStatus.SUCCEEDED.value)
    db.add(run)
    db.flush()
    db.add(
        OperatorExecution(
            agent_run_id=run.id,
            step_id="step_6_notif_rejected",
            status="completed",
        )
    )
    exception = ExceptionItem(
        agent_run_id=run.id,
        primary_issue_key="ITSM-REVIEW",
        status=ExceptionStatus.RESOLVED.value,
        resolution="rejected",
    )
    db.add(exception)
    db.commit()

    assert queue_service.classify_run(db, run, exception) == QueueItemState.HUMAN_REJECTED.value


def test_classify_run_prefers_terminal_duplicate_activity(db):
    """A stale running duplicate must not hide a completed notification."""
    run = AgentRun(run_id="run-duplicate-notification", status=RunStatus.SUCCEEDED.value)
    db.add(run)
    db.flush()
    db.add_all(
        [
            OperatorExecution(
                agent_run_id=run.id,
                step_id="step_6_notif_rejected",
                status="completed",
                output={"delivered": True},
            ),
            OperatorExecution(
                agent_run_id=run.id,
                step_id="step_6_notif_rejected",
                status="running",
            ),
        ]
    )
    exception = ExceptionItem(
        agent_run_id=run.id,
        primary_issue_key="ITSM-REVIEW",
        status=ExceptionStatus.RESOLVED.value,
        resolution="rejected",
    )
    db.add(exception)
    db.commit()

    assert queue_service.classify_run(db, run, exception) == QueueItemState.HUMAN_REJECTED.value


def test_campaign_counts_do_not_call_cancelled_preview_processed(db):
    campaign = QueueCampaign(
        name="preview",
        source="manual",
        status=QueueCampaignStatus.CANCELLED.value,
        batch_limit=10,
    )
    db.add(campaign)
    db.flush()
    db.add(
        QueueItem(
            campaign_id=campaign.id,
            issue_key="ITSM-1",
            state=QueueItemState.CANCELLED.value,
            attempt_count=0,
        )
    )
    db.commit()
    counts = queue_service.campaign_counts(db, campaign)
    assert counts["cancelled"] == 1
    assert counts["processed"] == 0


def test_campaign_payload_orders_items_by_frozen_rank_position(db):
    campaign = QueueCampaign(
        name="ranked",
        source="manual",
        status=QueueCampaignStatus.PREVIEW.value,
        batch_limit=2,
    )
    db.add(campaign)
    db.flush()
    db.add_all(
        [
            QueueItem(campaign_id=campaign.id, issue_key="ITSM-SECOND", state="preview", rank_position=2),
            QueueItem(campaign_id=campaign.id, issue_key="ITSM-FIRST", state="preview", rank_position=1),
        ]
    )
    db.commit()

    response = queue_router.get_campaign(campaign.id, db)

    assert [item["issue_key"] for item in response["items"]] == ["ITSM-FIRST", "ITSM-SECOND"]


def test_history_orders_queue_items_by_frozen_rank_position(db):
    campaign = QueueCampaign(
        name="ranked-history",
        source="manual",
        status=QueueCampaignStatus.COMPLETED.value,
        batch_limit=2,
    )
    db.add(campaign)
    db.flush()
    db.add_all(
        [
            QueueItem(campaign_id=campaign.id, issue_key="ITSM-SECOND", state="completed_unknown", rank_position=2),
            QueueItem(campaign_id=campaign.id, issue_key="ITSM-FIRST", state="completed_unknown", rank_position=1),
        ]
    )
    db.commit()

    response = queue_router.list_items(campaign_id=campaign.id, page=1, page_size=100, db=db)

    assert [item["issue_key"] for item in response["items"]] == ["ITSM-FIRST", "ITSM-SECOND"]


def test_resolved_human_run_waits_for_manual_notification(db):
    campaign = QueueCampaign(
        name="human",
        source="manual",
        status=QueueCampaignStatus.RUNNING.value,
        batch_limit=1,
    )
    db.add(campaign)
    db.flush()
    run = AgentRun(run_id="run-human-wait", status=RunStatus.SUCCEEDED.value)
    db.add(run)
    db.flush()
    item = QueueItem(
        campaign_id=campaign.id,
        issue_key="ITSM-2180",
        state=QueueItemState.RUNNING.value,
        latest_run_id=run.run_id,
        attempt_count=1,
    )
    exception = ExceptionItem(
        agent_run_id=run.id,
        primary_issue_key="ITSM-2180",
        status=ExceptionStatus.RESOLVED.value,
        resolution="approved",
    )
    db.add_all([item, exception])
    db.commit()

    queue_service.synchronize_campaign(db, campaign)
    assert item.state == QueueItemState.AWAITING_HUMAN.value

    db.add(OperatorExecution(agent_run_id=run.id, step_id="step_6_notif_manual", status="completed"))
    db.commit()
    queue_service.synchronize_campaign(db, campaign)
    assert item.state == QueueItemState.HUMAN_APPROVED.value


def test_resolved_rejected_run_waits_for_rejected_notification(db):
    campaign = QueueCampaign(
        name="human-rejected",
        source="manual",
        status=QueueCampaignStatus.RUNNING.value,
        batch_limit=1,
    )
    db.add(campaign)
    db.flush()
    run = AgentRun(run_id="run-human-rejected-sync", status=RunStatus.SUCCEEDED.value)
    db.add(run)
    db.flush()
    item = QueueItem(
        campaign_id=campaign.id,
        issue_key="ITSM-REVIEW",
        state=QueueItemState.AWAITING_HUMAN.value,
        latest_run_id=run.run_id,
        attempt_count=1,
    )
    exception = ExceptionItem(
        agent_run_id=run.id,
        primary_issue_key="ITSM-REVIEW",
        status=ExceptionStatus.RESOLVED.value,
        resolution="rejected",
    )
    db.add_all([item, exception])
    db.commit()

    queue_service.synchronize_campaign(db, campaign)
    assert item.state == QueueItemState.AWAITING_HUMAN.value

    db.add(
        OperatorExecution(
            agent_run_id=run.id,
            step_id="step_6_notif_rejected",
            status="completed",
        )
    )
    db.commit()
    queue_service.synchronize_campaign(db, campaign)
    assert item.state == QueueItemState.HUMAN_REJECTED.value
    assert campaign.status == QueueCampaignStatus.COMPLETED.value


def test_list_items_synchronizes_before_serializing_resolved_review(db):
    """History reads must not race the active-campaign synchronization call."""
    campaign = QueueCampaign(
        name="history-sync",
        source="manual",
        status=QueueCampaignStatus.RUNNING.value,
        batch_limit=1,
    )
    db.add(campaign)
    db.flush()
    run = AgentRun(run_id="run-list-sync", status=RunStatus.SUCCEEDED.value)
    db.add(run)
    db.flush()
    item = QueueItem(
        campaign_id=campaign.id,
        issue_key="ITSM-LIST-SYNC",
        state=QueueItemState.AWAITING_HUMAN.value,
        latest_run_id=run.run_id,
        attempt_count=1,
    )
    exception = ExceptionItem(
        agent_run_id=run.id,
        primary_issue_key="ITSM-LIST-SYNC",
        status=ExceptionStatus.RESOLVED.value,
        resolution="rejected",
    )
    db.add_all(
        [
            item,
            exception,
            OperatorExecution(
                agent_run_id=run.id,
                step_id="step_6_notif_rejected",
                status="completed",
            ),
        ]
    )
    db.commit()

    result = queue_router.list_items(
        campaign_id=campaign.id,
        page=1,
        page_size=100,
        db=db,
    )

    assert result["items"][0]["state"] == QueueItemState.HUMAN_REJECTED.value
