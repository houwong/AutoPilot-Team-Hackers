"""
Tests for ranking the queue on SLA state rather than the stored Priority column.

The bug being prevented: a `Low` ticket already breaching SLA for a VIP sorts
below a `Highest` ticket sitting comfortably within target, because the stored
Priority field answers a different question than "what should be worked next".
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.services import queue as queue_service
from app.services import queue_planner


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.mark.asyncio
async def test_operator_1_ranking_beats_stored_priority(db, monkeypatch):
    """A breached VIP Low must outrank a within-SLA Highest."""

    async def fake_select(_table, _params):
        return [
            {"Issue key": "ITSM-CALM", "Status": "Open", "Priority": "Highest",
             "Updated": "2026-07-10"},
            {"Issue key": "ITSM-BREACHED", "Status": "Open", "Priority": "Low",
             "Updated": "2026-07-01"},
        ]

    async def fake_ranking(_db, force_refresh: bool = False):
        # The order Operator 1 returns: breached VIP first, whatever the
        # stored Priority says.
        return [
            {"issue_key": "ITSM-BREACHED", "priority_rank": 1,
             "sla_status": "Breached", "vip": True},
            {"issue_key": "ITSM-CALM", "priority_rank": 6,
             "sla_status": "Within SLA", "vip": False},
        ]

    monkeypatch.setattr(queue_service.supabase, "select", fake_select)
    monkeypatch.setattr(queue_service, "ranked_backlog", fake_ranking)

    result = await queue_service.eligible_snapshots(db, 10)

    assert [i["issue_key"] for i in result] == ["ITSM-BREACHED", "ITSM-CALM"]
    assert result[0]["ranked_by"] == "operator_1"
    assert result[0]["sla_status"] == "Breached"
    assert result[0]["vip"] is True
    assert "Operator 1 rank 1" in result[0]["ranking_reason"]


@pytest.mark.asyncio
async def test_falls_back_to_stored_priority_when_operator_1_is_unavailable(
    db, monkeypatch
):
    """
    An unreachable Operator 1 must degrade, not break.

    A preview ordered by stored Priority is worse than one ordered by SLA and far
    better than no preview, so the fallback is deliberate — and it labels itself
    so nobody has to guess which ordering they are looking at.
    """

    async def fake_select(_table, _params):
        return [
            {"Issue key": "ITSM-LOW", "Status": "Open", "Priority": "Low",
             "Updated": "2026-07-01"},
            {"Issue key": "ITSM-HIGH", "Status": "Open", "Priority": "High",
             "Updated": "2026-07-10"},
        ]

    async def no_ranking(_db, force_refresh: bool = False):
        return []

    monkeypatch.setattr(queue_service.supabase, "select", fake_select)
    monkeypatch.setattr(queue_service, "ranked_backlog", no_ranking)

    result = await queue_service.eligible_snapshots(db, 10)

    assert [i["issue_key"] for i in result] == ["ITSM-HIGH", "ITSM-LOW"]
    assert all(i["ranked_by"] == "source_priority" for i in result)


@pytest.mark.asyncio
async def test_tickets_operator_1_did_not_rank_are_deferred_not_dropped(
    db, monkeypatch
):
    """
    Operator 1 filters records it considers ineligible.

    Those must sort after the ranked ones rather than disappearing: silently
    losing a ticket hides work, where deferring it is visible.
    """

    async def fake_select(_table, _params):
        return [
            {"Issue key": "ITSM-UNRANKED", "Status": "Open", "Priority": "Highest",
             "Updated": "2026-07-01"},
            {"Issue key": "ITSM-RANKED", "Status": "Open", "Priority": "Low",
             "Updated": "2026-07-02"},
        ]

    async def fake_ranking(_db, force_refresh: bool = False):
        return [{"issue_key": "ITSM-RANKED", "priority_rank": 2,
                 "sla_status": "Breached", "vip": False}]

    monkeypatch.setattr(queue_service.supabase, "select", fake_select)
    monkeypatch.setattr(queue_service, "ranked_backlog", fake_ranking)

    result = await queue_service.eligible_snapshots(db, 10)

    assert [i["issue_key"] for i in result] == ["ITSM-RANKED", "ITSM-UNRANKED"]


def test_rank_index_keeps_the_first_position_for_a_repeated_key():
    index = queue_planner.rank_index(
        [
            {"issue_key": "ITSM-1", "priority_rank": 1},
            {"issue_key": "ITSM-1", "priority_rank": 9},
        ]
    )
    assert index["ITSM-1"]["position"] == 0
    assert index["ITSM-1"]["priority_rank"] == 1
