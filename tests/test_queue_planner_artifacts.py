"""Offline contract checks for the two Section 4.1 Auto imports."""

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
PLANNER_PATH = ROOT / "supervity" / "queue-planner" / "queue-planner.import.json"
OLD_OPERATOR_ID = "019fd826-9991-7003-84a1-9bac5f1eda3d"
OP5_ID = "019fd290-bdf0-7001-b84c-b24f8a66c031"
OP6_ID = "019fd290-bdf0-7002-992a-58afae950865"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_new_artifacts_pass_offline_contract_validation():
    operator = load(OPERATOR_PATH)
    planner = load(PLANNER_PATH)

    assert validate_bundle(operator, kind="operator") == []
    assert validate_bundle(planner, kind="planner") == []


def test_planner_references_the_new_operator_and_existing_read_only_operators_only():
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
    assert calls == {OP5_ID, OP6_ID, NEW_OPERATOR_ID}
    assert OLD_OPERATOR_ID not in calls


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
