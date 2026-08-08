"""Offline contract checks for the Section 4.1 Auto imports."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts.check_queue_planner import (
    NEW_OPERATOR_ID,
    QUEUE_PLANNER_ID,
    validate_bundle,
)


ROOT = Path(__file__).parents[1]
OPERATOR_PATH = ROOT / "supervity" / "queue-planner" / "operator-1-queue-planning.import.json"
SLA_PATH = ROOT / "supervity" / "queue-planner" / "sla-evidence.import.json"
PLANNER_PATH = ROOT / "supervity" / "queue-planner" / "queue-planner.import.json"
OLD_OPERATOR_ID = "019fd826-9991-7003-84a1-9bac5f1eda3d"
SLA_EVIDENCE_ID = "019fe08a-71b9-7000-9fe4-ad091249e01d"
OP6_ID = "019fd826-9991-7002-921c-fa8b545f3373"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_new_artifacts_pass_offline_contract_validation():
    operator = load(OPERATOR_PATH)
    planner = load(PLANNER_PATH)

    assert validate_bundle(operator, kind="operator") == []
    assert validate_bundle(planner, kind="planner") == []


def test_new_planning_operator_name_does_not_duplicate_operator_1_label():
    definition = load(OPERATOR_PATH)["workflows"][0]["versions"][0]["definition"]

    assert definition["name"] == "Queue Planning Triage"


def test_builder_emits_single_step_sla_evidence_operator():
    sla = load(SLA_PATH)
    definition = sla["workflows"][0]["versions"][0]["definition"]

    assert definition["name"] == "Queue Planner SLA Evidence"
    assert definition["start_at"] == ["step_sla_evidence"]
    assert [step["id"] for step in definition["steps"]] == ["step_sla_evidence"]
    assert definition["steps"][0]["next_steps"] == []


def test_planner_references_only_the_verified_read_only_dependencies():
    operator = load(OPERATOR_PATH)
    planner = load(PLANNER_PATH)
    assert operator["rootWorkflowId"] == NEW_OPERATOR_ID
    assert planner["rootWorkflowId"] == QUEUE_PLANNER_ID
    assert NEW_OPERATOR_ID != OLD_OPERATOR_ID

    calls = {
        step["subworkflow_call"]["workflow_id"]
        for step in planner["workflows"][0]["versions"][0]["definition"]["steps"]
        if step.get("subworkflow_call")
    }
    assert calls == {SLA_EVIDENCE_ID, OP6_ID, NEW_OPERATOR_ID}
    assert OLD_OPERATOR_ID not in calls


def test_planner_artifact_matches_the_live_three_step_graph():
    definition = load(PLANNER_PATH)["workflows"][0]["versions"][0]["definition"]
    steps = {step["id"]: step for step in definition["steps"]}

    assert definition["start_at"] == ["step_sla_evidence", "step_incident_detector"]
    assert list(steps) == ["step_sla_evidence", "step_incident_detector", "step_triage"]
    assert steps["step_sla_evidence"]["depends_on"] == []
    assert steps["step_incident_detector"]["depends_on"] == []
    assert steps["step_triage"]["depends_on"] == [
        "step_sla_evidence",
        "step_incident_detector",
    ]


def test_validator_rejects_old_operator_and_llm_or_write_operations():
    operator = load(OPERATOR_PATH)
    mutated = copy.deepcopy(operator)
    step = mutated["workflows"][0]["versions"][0]["definition"]["steps"][0]
    step["code_cell"] += "\ncall_ai_llm('do not use')\nsupabase.table('issues').update({})\n"
    step["depends_on"] = [OLD_OPERATOR_ID]

    errors = validate_bundle(mutated, kind="operator")
    assert any("old Operator 1" in error for error in errors)
    assert any("LLM" in error for error in errors)
    assert any("write" in error.lower() for error in errors)


def test_validator_rejects_missing_required_output_fields():
    operator = load(OPERATOR_PATH)
    mutated = copy.deepcopy(operator)
    code = mutated["workflows"][0]["versions"][0]["definition"]["steps"][0]["code_cell"]
    mutated["workflows"][0]["versions"][0]["definition"]["steps"][0]["code_cell"] = code.replace(
        '"sla_evaluated_count":', '"removed_sla_evaluated_count":', 1
    )
    errors = validate_bundle(mutated, kind="operator")
    assert any("sla_evaluated_count" in error for error in errors)


def _operator_namespace():
    code = load(OPERATOR_PATH)["workflows"][0]["versions"][0]["definition"]["steps"][0]["code_cell"]
    code = code.rsplit("await step_queue_planning_operator()", 1)[0]
    namespace: dict = {}
    exec(compile(code, str(OPERATOR_PATH), "exec"), namespace)
    return namespace


def _sla_namespace():
    code = load(SLA_PATH)["workflows"][0]["versions"][0]["definition"]["steps"][0]["code_cell"]
    code = code.rsplit("await step_sla_evidence_operator()", 1)[0]
    namespace: dict = {}
    exec(compile(code, str(SLA_PATH), "exec"), namespace)
    return namespace


def test_planning_operator_unwraps_supervity_subworkflow_audit_output():
    namespace = _operator_namespace()
    expected = {
        "effective_as_of": "2026-08-08T08:45:00Z",
        "tickets": [
            {"issue_key": "ITSM-1", "computed_sla_state": "Breached", "is_vip": False}
        ],
    }
    wrapped = {
        "outputs": [
            {
                "stepId": "step_sla_evidence",
                "outputs": {"output": json.dumps(expected), "error": ""},
            }
        ]
    }

    assert namespace["_json_value"](wrapped, "sla_states_json") == expected


@pytest.mark.asyncio
async def test_single_step_sla_operator_evaluates_every_active_ticket_from_live_shaped_rows():
    namespace = _sla_namespace()
    namespace["user_inputs"] = {
        "sla_targets": "VIP: 4h response / 24h resolution; Non-VIP: 8h response / 48h resolution",
        "at_risk_window_minutes": "120",
        "default_region": "Global",
        "as_of": "2026-07-25T00:00:00Z",
    }

    async def fake_fetch():
        return {
            "issues": [
                {"Issue key": "ITSM-VIP", "Status": "Open", "Reporter": "Ada", "Created": "2026-07-24T01:00:00", "Summary": "VIP issue"},
                {"Issue key": "ITSM-STANDARD", "Status": "Waiting for support", "Reporter": "Ben", "Created": "2026-07-22T22:00:00", "Summary": "Standard issue"},
                {"Issue key": "ITSM-CLOSED", "Status": "Closed", "Reporter": "Ada", "Created": "2026-07-01T00:00:00", "Summary": "Closed issue"},
            ],
            "users": [
                {"display_name": "Ada", "x_vip": True, "location": "Remote"},
                {"display_name": "Ben", "x_vip": False, "location": "Remote"},
            ],
            "calendars": [
                {"region": "Remote", "timezone": "UTC", "business_hours": "24x7 follow-the-sun", "holiday_dates": ""},
                {"region": "Global", "timezone": "UTC", "business_hours": "24x7 follow-the-sun", "holiday_dates": ""},
            ],
        }

    namespace["_fetch_source_tables"] = fake_fetch
    await namespace["step_sla_evidence_operator"]()
    result = namespace["sla_evidence"]

    assert result["evaluated_count"] == 2
    assert result["effective_as_of"] == "2026-07-25T00:00:00+00:00"
    assert result["tickets"] == [
        {
            "issue_key": "ITSM-VIP",
            "computed_sla_state": "At risk",
            "is_vip": True,
            "elapsed_business_minutes": 1380,
            "sla_target_minutes": 1440,
        },
        {
            "issue_key": "ITSM-STANDARD",
            "computed_sla_state": "Breached",
            "is_vip": False,
            "elapsed_business_minutes": 3000,
            "sla_target_minutes": 2880,
        },
    ]


@pytest.mark.asyncio
async def test_operator_5_sla_overrides_stored_priority_and_incident_boost_stays_in_tier():
    namespace = _operator_namespace()
    namespace["user_inputs"] = {
        "sla_states_json": json.dumps(
            {
                "effective_as_of": "2026-07-25T00:00:00Z",
                "tickets": [
                    {"issue_key": "ITSM-CALM", "computed_sla_state": "Within SLA", "is_vip": False},
                    {"issue_key": "ITSM-BREACHED", "computed_sla_state": "Breached", "is_vip": True},
                    {"issue_key": "ITSM-INCIDENT", "computed_sla_state": "Breached", "is_vip": False},
                    {"issue_key": "ITSM-NORMAL", "computed_sla_state": "Breached", "is_vip": False},
                ],
            }
        ),
        "incident_clusters_json": json.dumps(
            {
                "clusters": [
                    {
                        "member_keys": ["ITSM-INCIDENT"],
                        "member_count": 10,
                        "assignment_groups": ["Service Desk"],
                        "recommended_action": "attach_to_existing",
                        "linked_incident_label": "INC-10",
                    },
                    {
                        "member_keys": ["ITSM-NORMAL"],
                        "member_count": 99,
                        "recommended_action": "monitor",
                    },
                ]
            }
        ),
        "priority_ranking_order": "Breached VIP, Breached Non-VIP, At risk VIP, At risk Non-VIP, Within SLA VIP, Within SLA Non-VIP",
        "max_candidates": "1000",
    }
    namespace["_fetch_active_issues"] = lambda: None

    async def fake_fetch():
        return [
            {"row_id": 1, "Issue key": "ITSM-CALM", "Status": "Open", "Priority": "Highest", "Updated": "2026-07-01"},
            {"row_id": 2, "Issue key": "ITSM-BREACHED", "Status": "Open", "Priority": "Low", "Updated": "2026-07-02"},
            {"row_id": 3, "Issue key": "ITSM-INCIDENT", "Status": "Open", "Priority": "Low", "Updated": "2026-07-03"},
            {"row_id": 4, "Issue key": "ITSM-NORMAL", "Status": "Open", "Priority": "Highest", "Updated": "2026-07-04"},
        ]

    namespace["_fetch_active_issues"] = fake_fetch
    await namespace["step_queue_planning_operator"]()
    result = namespace["prioritized_tickets"]

    assert [ticket["issue_key"] for ticket in result["prioritized_tickets"]] == [
        "ITSM-BREACHED",
        "ITSM-INCIDENT",
        "ITSM-NORMAL",
        "ITSM-CALM",
    ]
    assert result["prioritized_tickets"][0]["priority_rank"] == 1
    assert result["prioritized_tickets"][1]["major_incident_key"] == "INC-10"


@pytest.mark.asyncio
async def test_operator_fails_closed_when_operator_5_evidence_is_incomplete():
    namespace = _operator_namespace()
    namespace["user_inputs"] = {
        "sla_states_json": json.dumps(
            {"effective_as_of": "2026-07-25T00:00:00Z", "tickets": []}
        ),
        "incident_clusters_json": json.dumps({"clusters": []}),
        "priority_ranking_order": "Breached VIP, Breached Non-VIP, At risk VIP, At risk Non-VIP, Within SLA VIP, Within SLA Non-VIP",
        "max_candidates": "1000",
    }

    async def fake_fetch():
        return [{"Issue key": "ITSM-MISSING", "Status": "Open", "Priority": "Low"}]

    namespace["_fetch_active_issues"] = fake_fetch
    with pytest.raises(ValueError, match="INCOMPLETE_SLA_EVIDENCE"):
        await namespace["step_queue_planning_operator"]()


@pytest.mark.asyncio
async def test_planning_operator_resolves_platform_management_token_without_supabase_url(monkeypatch):
    namespace = _operator_namespace()
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append(url)
            if url.startswith("https://api.supabase.com/"):
                return FakeResponse([{"name": "service_role", "api_key": "service-key"}])
            return FakeResponse([{"Issue key": "ITSM-1", "Status": "Open"}])

    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_TOKEN", "sbp_management_token")
    monkeypatch.setattr(namespace["httpx"], "AsyncClient", FakeClient)

    rows = await namespace["_fetch_active_issues"]()

    assert rows == [{"Issue key": "ITSM-1", "Status": "Open"}]
    assert calls == [
        "https://api.supabase.com/v1/projects/pkwvlnvxmawvgqzqvkcg/api-keys",
        "https://pkwvlnvxmawvgqzqvkcg.supabase.co/rest/v1/issues",
    ]
