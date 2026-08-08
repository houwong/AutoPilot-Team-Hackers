#!/usr/bin/env python
"""Offline checks for the Section 4.1 Queue Planner import artifacts.

This intentionally does not call Auto. It is a cheap gate before an import: the
new operator must be a distinct workflow, the planner must call only Operator 5,
Operator 6 and that new operator, and the code must be deterministic/read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

NEW_OPERATOR_ID = os.getenv("AUTO_WF_OP1_PLANNER", "").strip() or "019fe092-c299-7000-8ac4-42d685892cbf"
QUEUE_PLANNER_ID = os.getenv("AUTO_WF_QUEUE_PLANNER", "").strip() or "019fe08c-0dee-7000-9743-1f7e64f2c195"
OLD_OPERATOR_ID = "019fd826-9991-7003-84a1-9bac5f1eda3d"
OP5_ID = os.getenv("AUTO_WF_SLA_PLANNER", "").strip() or "019fe08a-71b9-7000-9fe4-ad091249e01d"
OP6_ID = os.getenv("AUTO_WF_OP6", "").strip() or "019fd826-9991-7002-921c-fa8b545f3373"

OPERATOR_INPUTS = {
    "sla_states_json",
    "incident_clusters_json",
    "priority_ranking_order",
    "max_candidates",
}
PLANNER_INPUTS = {
    "sla_targets",
    "at_risk_window_minutes",
    "default_region",
    "as_of",
    "flood_threshold_count",
    "flood_window_minutes",
    "correlation_confidence_threshold",
    "include_relationship_types",
    "recurring_error_min_count",
    "priority_ranking_order",
    "max_candidates",
}
REQUIRED_OUTPUT_TOKENS = {
    "schema_version",
    "generated_at",
    "effective_as_of",
    "tickets_scanned",
    "tickets_eligible",
    "sla_evaluated_count",
    "prioritized_tickets",
    "rank_position",
    "priority_rank",
    "major_incident_action",
}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for key, child in value.items():
            result.extend(_strings(key))
            result.extend(_strings(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_strings(child))
        return result
    return []


def _definition(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    workflows = doc.get("workflows")
    if not isinstance(workflows, list) or len(workflows) != 1:
        errors.append("artifact must contain exactly one workflow")
        return None, errors
    versions = workflows[0].get("versions")
    if not isinstance(versions, list) or len(versions) != 1:
        errors.append("artifact must contain exactly one workflow version")
        return None, errors
    definition = versions[0].get("definition")
    if not isinstance(definition, dict):
        errors.append("workflow version is missing definition")
        return None, errors
    return definition, errors


def _inputs(definition: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name"))
        for item in definition.get("inputs", [])
        if isinstance(item, dict) and item.get("name")
    }


def validate_bundle(doc: dict[str, Any], *, kind: str) -> list[str]:
    """Return human-readable contract failures; an empty list means valid."""
    errors: list[str] = []
    definition, shape_errors = _definition(doc)
    errors.extend(shape_errors)
    if definition is None:
        return errors

    root_id = str(doc.get("rootWorkflowId") or "")
    expected_id = NEW_OPERATOR_ID if kind == "operator" else QUEUE_PLANNER_ID
    if root_id != expected_id:
        errors.append(f"rootWorkflowId must be {expected_id}")
    expected_inputs = OPERATOR_INPUTS if kind == "operator" else PLANNER_INPUTS
    missing_inputs = sorted(expected_inputs - _inputs(definition))
    if missing_inputs:
        errors.append(f"missing required inputs: {', '.join(missing_inputs)}")

    text = "\n".join(_strings(doc))
    lowered = text.casefold()
    if OLD_OPERATOR_ID in text:
        errors.append("artifact references the old Operator 1 ID")
    if "call_ai_llm" in lowered or re.search(r"\bllm\b", lowered):
        errors.append("artifact uses an LLM; queue planning must be deterministic")
    if "customfield_10030" in lowered or "time to resolution" in lowered:
        errors.append("artifact reads stored SLA/time-to-resolution data")
    if re.search(r"\.\s*(insert|update|upsert|delete)\s*\(", lowered):
        errors.append("artifact contains a Supabase write operation")
    if re.search(r"\b(send|post_message|sendmail|notify|notification)\b", lowered):
        errors.append("artifact contains a write/notification operation")

    services = {str(service).casefold() for service in definition.get("business_functions", [])}
    if kind == "operator":
        steps = definition.get("steps") or []
        if len(steps) != 1:
            errors.append("new planning operator must stop after one deterministic step")
        code = "\n".join(_strings(steps))
        for token in sorted(REQUIRED_OUTPUT_TOKENS):
            if not re.search(rf'"{re.escape(token)}"\s*:', code):
                errors.append(f"operator code does not define required output field {token}")
        for token in ("sorted(", "INCOMPLETE_SLA_EVIDENCE", "INVALID_PRIORITY_RANKING_ORDER"):
            if token not in code:
                errors.append(f"operator code is missing deterministic guard {token}")
    elif kind == "planner":
        steps = definition.get("steps") or []
        calls = {
            str(step.get("subworkflow_call", {}).get("workflow_id"))
            for step in steps
            if isinstance(step, dict) and step.get("subworkflow_call")
        }
        expected_calls = {OP5_ID, OP6_ID, NEW_OPERATOR_ID}
        if calls != expected_calls:
            errors.append(f"planner workflow calls {sorted(calls)}, expected {sorted(expected_calls)}")
        if set(definition.get("start_at") or []) != {
            "step_sla_evidence",
            "step_incident_detector",
        }:
            errors.append("SLA evidence and incident detection must start in parallel")
        planner_step = next((s for s in steps if s.get("id") == "step_triage"), None)
        if not planner_step or set(planner_step.get("depends_on") or []) != {
            "step_sla_evidence",
            "step_incident_detector",
        }:
            errors.append("Queue Planning Triage must depend on both evidence steps")
        if any(s.get("id") in {"step_operator_2", "step_operator_3", "step_operator_4", "step_operator_7"} for s in steps):
            errors.append("planner workflow invokes an execution operator")
    return errors


def _check(path: Path, kind: str) -> int:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {path}: {exc}")
        return 1
    errors = validate_bundle(document, kind=kind)
    if errors:
        print(f"FAIL {path}")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"PASS {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator", type=Path)
    parser.add_argument("--planner", type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).parents[1]
    operator = args.operator or root / "supervity" / "queue-planner" / "operator-1-queue-planning.import.json"
    planner = args.planner or root / "supervity" / "queue-planner" / "queue-planner.import.json"
    return max(_check(operator, "operator"), _check(planner, "planner"))


if __name__ == "__main__":
    sys.exit(main())
