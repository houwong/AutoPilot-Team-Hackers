"""Run one explicit orchestrator test case through the Supervity API.

The target issue is always passed as ``target_issue_key`` so a test cannot
silently fall back to processing the highest-priority ticket in the queue.
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")

from app.services.auto_client import AutoClient, AutoError, EV_ACTIVITY, EV_ERROR, EV_RESULT


ROUTING_MAPPING = {
    "Request access": "Ops_Access",
    "Hardware Access": "Ops_Hardware",
    "SaaS Access": "Ops_SaaS",
    "Network Connectivity": "Ops_Net",
    "System Performance": "Ops_Core",
    "Security Incident": "Ops_Sec",
    "Other": "Ops_General",
}


def inputs_for(issue_key: str) -> dict[str, object]:
    return {
        "supabase_table_name": "issues",
        "priority_ranking_order": "Breached VIP, Breached Non-VIP, At risk VIP, At risk Non-VIP, Within SLA VIP, Within SLA Non-VIP",
        "sla_thresholds": "VIP: 4h response / 24h resolution, Non-VIP: 8h response / 48h resolution, 'At risk' = 2h before breach",
        "stalled_days_threshold": 3,
        "kb_confidence_threshold": 0.85,
        "routing_mapping_json": json.dumps(ROUTING_MAPPING),
        "support_slack_channel": "#ticket-escalations",
        "sla_targets": "VIP: 4h response / 24h resolution; Non-VIP: 8h response / 48h resolution",
        "at_risk_window_minutes": 120,
        "default_region": "Global",
        "as_of": "2026-07-25T00:00:00Z",
        "flood_threshold_count": 5,
        "flood_window_minutes": 120,
        "correlation_confidence_threshold": 0.7,
        "include_relationship_types": "is caused by, relates to",
        "recurring_error_min_count": 20,
        "blocking_statuses": "Rejected, Rolled Back",
        "escalating_statuses": "Pending CAB Approval",
        "require_cab_for_risk": "High, Medium",
        "auto_approve_risk_levels": "Low",
        "require_change_record_for_production": False,
        "target_issue_key": issue_key,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("issue_key")
    args = parser.parse_args()

    workflow_id = os.environ["AUTO_WF_ORCHESTRATOR"]
    client = AutoClient()
    run_id = None
    print(f"workflow_id={workflow_id}")
    print(f"target_issue_key={args.issue_key}")
    print("routing_mapping=enabled")

    try:
        async for event in client.stream(workflow_id, inputs_for(args.issue_key)):
            run_id = run_id or event.auto_run_id
            if event.event == EV_ACTIVITY:
                outputs = event.outputs or {}
                condition = (
                    f" condition_met={outputs.get('conditionMet')}"
                    if event.is_condition
                    else ""
                )
                print(f"activity step={event.step_id} status={event.status}{condition}")
            elif event.event == EV_RESULT:
                print(f"result status={event.status} steps={event.step_names}")
            elif event.event == EV_ERROR:
                print(f"error {event.raw[:1000]}")
    except AutoError as exc:
        print(f"auto_error={exc}")
        return 1

    print(f"workflow_run_id={run_id}")
    return 0 if run_id else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
