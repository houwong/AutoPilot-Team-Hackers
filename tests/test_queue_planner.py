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
from app.models.command_center import Policy
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


def test_normalize_planner_result_preserves_sla_and_incident_evidence():
    raw = {
        "schema_version": "1",
        "generated_at": "2026-08-08T04:00:00Z",
        "effective_as_of": "2026-07-25T00:00:00Z",
        "tickets_scanned": 460,
        "tickets_eligible": 372,
        "sla_evaluated_count": 460,
        "prioritized_tickets": [
            {
                "issue_key": "ITSM-2180",
                "row_id": 2180,
                "source_status": "Open",
                "source_priority": "Highest",
                "source_updated_at": "2026-07-20T00:00:00Z",
                "assignment_group": "Service Desk",
                "sla_status": "Breached",
                "vip": True,
                "priority_rank": 1,
                "rank_position": 1,
                "major_incident_key": "INC-9001",
                "major_incident_action": "attach_to_existing",
                "incident_ticket_count": 24,
                "incident_vip_count": 3,
                "ranking_reason": "Breached VIP; INC-9001",
            }
        ],
    }

    result = queue_planner.normalize_planner_result(raw, run_id="planner-run")

    assert result.mode == "queue_planner"
    assert result.run_id == "planner-run"
    assert result.effective_as_of == "2026-07-25T00:00:00Z"
    assert result.tickets[0]["sla_status"] == "Breached"
    assert result.tickets[0]["major_incident_key"] == "INC-9001"
    assert result.tickets[0]["incident_ticket_count"] == 24


def test_normalize_planner_result_rejects_missing_sla_evidence():
    raw = {
        "schema_version": "1",
        "generated_at": "2026-08-08T04:00:00Z",
        "effective_as_of": "2026-07-25T00:00:00Z",
        "tickets_scanned": 1,
        "tickets_eligible": 1,
        "sla_evaluated_count": 0,
        "prioritized_tickets": [
            {"issue_key": "ITSM-1", "rank_position": 1, "priority_rank": 1}
        ],
    }

    with pytest.raises(queue_planner.PlannerUnavailable, match="SLA evidence"):
        queue_planner.normalize_planner_result(raw)


def test_normalize_planner_result_rejects_inconsistent_coverage_counts():
    raw = {
        "schema_version": "1",
        "tickets_scanned": 1,
        "tickets_eligible": 2,
        "sla_evaluated_count": 2,
        "prioritized_tickets": [],
    }

    with pytest.raises(queue_planner.PlannerUnavailable, match="coverage"):
        queue_planner.normalize_planner_result(raw)


def test_normalize_planner_result_requires_evidence_timestamps():
    raw = {
        "schema_version": "1",
        "tickets_scanned": 0,
        "tickets_eligible": 0,
        "sla_evaluated_count": 0,
        "prioritized_tickets": [],
    }

    with pytest.raises(queue_planner.PlannerUnavailable, match="timestamp"):
        queue_planner.normalize_planner_result(raw)


def test_planner_inputs_include_all_runtime_policy_fields(db):
    inputs = queue_planner._planner_inputs(db)

    assert inputs["sla_targets"].startswith("VIP:")
    assert inputs["at_risk_window_minutes"] == "120"
    assert inputs["default_region"] == "Global"
    assert inputs["as_of"] == "2026-07-25T00:00:00Z"
    assert inputs["flood_threshold_count"] == "5"
    assert inputs["include_relationship_types"] == "is caused by, relates to"
    assert inputs["priority_ranking_order"].startswith("Breached VIP")
    assert inputs["max_candidates"] == "1000"


def test_planner_policy_fingerprint_changes_when_policy_changes(db):
    first = queue_planner._planner_policy_snapshot(db)
    db.add(Policy(key="as_of", name="As of", value="2026-07-26T00:00:00Z"))
    db.commit()
    second = queue_planner._planner_policy_snapshot(db)

    assert queue_planner._policy_fingerprint(first) != queue_planner._policy_fingerprint(second)


@pytest.mark.asyncio
async def test_v2_failure_uses_same_policy_cache_with_stale_marker(db, monkeypatch):
    monkeypatch.setenv("QUEUE_PLANNER_MODE", "supervity_v2")
    monkeypatch.setenv("AUTO_WF_QUEUE_PLANNER", "new-planner-id")
    monkeypatch.setattr(queue_planner, "_v2_cache", {})
    fresh = queue_planner.PlannerResult(
        tickets=[
            {
                "issue_key": "ITSM-1",
                "sla_status": "Breached",
                "vip": False,
                "priority_rank": 2,
                "rank_position": 1,
            }
        ],
        mode="queue_planner",
        run_id="old-run",
    )
    snapshot = queue_planner._planner_policy_snapshot(db)
    fingerprint = queue_planner._policy_fingerprint(snapshot)
    monkeypatch.setattr(
        queue_planner,
        "_v2_cache",
        {"cached_at": queue_planner.time.time(), "fingerprint": fingerprint, "result": fresh},
    )

    async def fail(_db, *, policy_snapshot=None):
        raise queue_planner.PlannerUnavailable("network")

    monkeypatch.setattr(queue_planner, "v2_ranked_backlog", fail)

    result = await queue_planner.planner_result(db, force_refresh=True)

    assert result.mode == "queue_planner_stale"
    assert result.stale is True
    assert result.run_id == "old-run"


@pytest.mark.asyncio
async def test_v2_failure_without_cache_blocks_preview_planning(db, monkeypatch):
    monkeypatch.setenv("QUEUE_PLANNER_MODE", "supervity_v2")
    monkeypatch.setattr(queue_planner, "_v2_cache", {})

    async def fail(_db, *, policy_snapshot=None):
        raise queue_planner.PlannerUnavailable("network")

    monkeypatch.setattr(queue_planner, "v2_ranked_backlog", fail)

    with pytest.raises(queue_planner.PlannerUnavailable, match="network"):
        await queue_planner.planner_result(db, force_refresh=True)


@pytest.mark.asyncio
async def test_v2_mode_calls_the_new_queue_planner_id(db, monkeypatch):
    monkeypatch.setenv("QUEUE_PLANNER_MODE", "supervity_v2")
    monkeypatch.setenv("AUTO_WF_QUEUE_PLANNER", "new-planner-id")
    monkeypatch.setattr(queue_planner, "_cache", {})
    called = {}

    async def fake_execute(_client, workflow_id, inputs):
        called["workflow_id"] = workflow_id
        called["inputs"] = inputs
        return {
            "workflowRun": {"id": "planner-run"},
            "activityRuns": [
                {
                    "outputs": {
                        "output": '{"schema_version":"1","generated_at":"2026-08-08T04:00:00Z",'
                        '"effective_as_of":"2026-07-25T00:00:00Z","tickets_scanned":1,'
                        '"tickets_eligible":1,"sla_evaluated_count":1,"prioritized_tickets":['
                        '{"issue_key":"ITSM-1","sla_status":"Breached","vip":false,'
                        '"priority_rank":2,"rank_position":1,"ranking_reason":"Breached"}]}'
                    }
                }
            ],
        }

    monkeypatch.setattr(queue_planner.AutoClient, "execute", fake_execute)

    result = await queue_planner.planner_result(db)

    assert called["workflow_id"] == "new-planner-id"
    assert result.tickets[0]["issue_key"] == "ITSM-1"


@pytest.mark.asyncio
async def test_legacy_mode_does_not_call_the_new_planner(db, monkeypatch):
    monkeypatch.setenv("QUEUE_PLANNER_MODE", "legacy")
    called = {"legacy": 0, "v2": 0}

    async def fake_legacy(_db, force_refresh=False):
        called["legacy"] += 1
        return [{"issue_key": "ITSM-LEGACY"}]

    async def fail_v2(_db, *, policy_snapshot=None):
        called["v2"] += 1
        raise AssertionError("v2 planner must not run in legacy mode")

    monkeypatch.setattr(queue_planner, "ranked_backlog", fake_legacy)
    monkeypatch.setattr(queue_planner, "v2_ranked_backlog", fail_v2)

    result = await queue_planner.planner_result(db)

    assert result.mode == "legacy"
    assert result.tickets[0]["issue_key"] == "ITSM-LEGACY"
    assert called == {"legacy": 1, "v2": 0}


@pytest.mark.asyncio
async def test_unknown_mode_is_a_configuration_error(db, monkeypatch):
    monkeypatch.setenv("QUEUE_PLANNER_MODE", "surprise")

    with pytest.raises(queue_planner.PlannerUnavailable, match="Unsupported"):
        await queue_planner.planner_result(db)
