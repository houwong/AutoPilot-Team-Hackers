#!/usr/bin/env python
"""
Thin-wire spike: prove the backend can drive an Auto workflow end to end.

Runs a workflow through AutoClient.stream() and prints every SSE event, so we
know multipart encoding, auth headers and SSE parsing all work BEFORE the
Command Center depends on them.

    docker compose exec backend python scripts/auto_smoke.py op7 --issue ITSM-2180
    docker compose exec backend python scripts/auto_smoke.py orchestrator

Operator 7 is the fastest workflow (~2s) so it is the default target for a
quick connectivity check. The orchestrator takes minutes.
"""
import argparse
import asyncio
import os
import sys
from collections import Counter

sys.path.insert(0, "/app")

from app.services.auto_client import (  # noqa: E402
    EV_ACTIVITY,
    EV_ERROR,
    EV_RESULT,
    AutoClient,
    AutoError,
)

WORKFLOWS = {
    "orchestrator": os.getenv("AUTO_WF_ORCHESTRATOR", "019fd290-bdf0-7000-88b9-2b00a7dbb7fc"),
    "op1": os.getenv("AUTO_WF_OP1", "019fd290-bdf0-7003-9e19-5d53ec0cdbfb"),
    "op2": os.getenv("AUTO_WF_OP2", "019fd290-bdf0-7004-a60c-6d54283bad6c"),
    "op3": os.getenv("AUTO_WF_OP3", "019fd290-bdf0-7006-b54c-d791ae3df769"),
    "op4": os.getenv("AUTO_WF_OP4", "019fd290-bdf0-7007-8472-91df0ddeab17"),
    "op5": os.getenv("AUTO_WF_OP5", "019fd290-bdf0-7001-b84c-b24f8a66c031"),
    "op6": os.getenv("AUTO_WF_OP6", "019fd290-bdf0-7002-992a-58afae950865"),
    "op7": os.getenv("AUTO_WF_OP7", "019fd290-bdf0-7005-89da-5f9b78ece866"),
}

# Inputs that make each target do something interesting, from the fixtures.
PRESETS = {
    "op7": {
        "issue_key": "ITSM-2180",
        "blocking_statuses": "Rejected, Rolled Back",
        "escalating_statuses": "Pending CAB Approval",
        "require_cab_for_risk": "High, Medium",
        "auto_approve_risk_levels": "Low",
        "require_change_record_for_production": True,
    },
    "op6": {
        "flood_threshold_count": 5,
        "flood_window_minutes": 120,
        "correlation_confidence_threshold": 0.7,
        "include_relationship_types": "is caused by, relates to",
        "recurring_error_min_count": 20,
        "as_of": "2026-07-25T00:00:00Z",
    },
    "orchestrator": {
        "as_of": "2026-07-25T00:00:00Z",
        "require_change_record_for_production": False,
        "support_slack_channel": "#ticket-escalations",
    },
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="op7", choices=sorted(WORKFLOWS))
    ap.add_argument("--issue", help="override issue_key")
    args = ap.parse_args()

    workflow_id = WORKFLOWS[args.target]
    inputs = dict(PRESETS.get(args.target, {}))
    if args.issue:
        inputs["issue_key"] = args.issue

    print(f"target     : {args.target}")
    print(f"workflowId : {workflow_id}")
    print(f"inputs     : {inputs}\n")

    client = AutoClient()
    counts: Counter[str] = Counter()
    operators: list[str] = []

    auto_run_id = None
    retried: list[str] = []

    try:
        async for ev in client.stream(workflow_id, inputs):
            counts[ev.event] += 1
            auto_run_id = auto_run_id or ev.auto_run_id

            if ev.event == EV_ACTIVITY:
                attempt = f" attempt={ev.attempt}" if (ev.attempt or 1) > 1 else ""
                print(f"  [activity] {ev.step_id:<38} {ev.status}{attempt}")
                if (ev.attempt or 1) > 1:
                    retried.append(ev.step_id or "?")
                if ev.status == "completed" and ev.step_id:
                    operators.append(ev.step_id)
            elif ev.event == EV_RESULT:
                names = ev.step_names
                print(f"\n  [result] steps: {names}")
            elif ev.event == EV_ERROR:
                print(f"\n  [ERROR] {ev.raw[:600]}")
    except AutoError as exc:
        print(f"\nFAILED: {exc}")
        return 1

    print("\n--- summary ---")
    for name, n in sorted(counts.items()):
        print(f"  {name:<16} {n}")
    print(f"  auto_run_id       : {auto_run_id}")
    print(f"  steps completed   : {len(operators)}  {operators}")
    if retried:
        print(f"  steps retried     : {retried}")
    if not counts:
        print("  NO EVENTS RECEIVED — check headers and multipart encoding")
        return 1
    if not auto_run_id:
        print("  WARNING: no workflowRunId captured — agent_run cannot be traced back")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
