"""Tests for Command Center agent-run lifecycle guards."""

import pytest
from fastapi import HTTPException
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.command_center import (
    AgentRun,
    ExceptionItem,
    ExceptionStatus,
    RunStatus,
)
from app.routers import agent as agent_router


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.mark.asyncio
async def test_manual_target_rejects_duplicate_active_run(db, monkeypatch):
    existing = AgentRun(
        run_id="existing-run",
        selected_issue_key="ITSM-2065",
        status=RunStatus.RUNNING.value,
    )
    db.add(existing)
    db.commit()

    async def fake_refuse_closed(_issue_key):
        return None

    monkeypatch.setattr(agent_router, "_refuse_closed_ticket", fake_refuse_closed)

    with pytest.raises(HTTPException) as error:
        await agent_router.create_agent_run(
            db,
            BackgroundTasks(),
            agent_router.TriggerRequest(
                trigger="manual",
                target_issue_key="ITSM-2065",
            ),
        )

    assert error.value.status_code == 409
    assert "existing-run" in str(error.value.detail)


@pytest.mark.asyncio
async def test_workbench_follow_up_can_continue_parked_target(db, monkeypatch):
    existing = AgentRun(
        run_id="parked-run",
        selected_issue_key="ITSM-2180",
        status=RunStatus.AWAITING_HUMAN.value,
    )
    db.add(existing)
    db.commit()

    async def fake_refuse_closed(_issue_key):
        return None

    def fake_consume(*_args, **_kwargs):
        return None

    def fake_run(coro):
        coro.close()

    monkeypatch.setattr(agent_router, "_refuse_closed_ticket", fake_refuse_closed)
    monkeypatch.setattr(agent_router, "_consume", fake_consume)
    monkeypatch.setattr(agent_router.asyncio, "run", fake_run)

    follow_up = await agent_router.create_agent_run(
        db,
        BackgroundTasks(),
        agent_router.TriggerRequest(
            trigger="workbench",
            parent_run_id="parked-run",
            target_issue_key="ITSM-2180",
        ),
    )

    assert follow_up.selected_issue_key == "ITSM-2180"
    assert follow_up.parent_run_id == "parked-run"


@pytest.mark.asyncio
async def test_manual_target_rejects_open_workbench_exception(db, monkeypatch):
    db.add(
        ExceptionItem(
            primary_issue_key="ITSM-2179",
            status=ExceptionStatus.OPEN.value,
            title="Needs review",
        )
    )
    db.commit()

    async def fake_refuse_closed(_issue_key):
        return None

    monkeypatch.setattr(agent_router, "_refuse_closed_ticket", fake_refuse_closed)

    with pytest.raises(HTTPException) as error:
        await agent_router.create_agent_run(
            db,
            BackgroundTasks(),
            agent_router.TriggerRequest(
                trigger="manual",
                target_issue_key="ITSM-2179",
            ),
        )

    assert error.value.status_code == 409
    assert "unresolved Workbench exception" in str(error.value.detail)
