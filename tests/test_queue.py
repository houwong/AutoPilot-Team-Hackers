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
from app.services import queue as queue_service


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


def test_classify_run_requires_a_recognized_terminal_step(db):
    run = AgentRun(
        run_id="run-unknown",
        status=RunStatus.SUCCEEDED.value,
        issue_keys=["ITSM-1"],
    )
    db.add(run)
    db.commit()
    assert queue_service.classify_run(db, run) == QueueItemState.COMPLETED_UNKNOWN.value


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
