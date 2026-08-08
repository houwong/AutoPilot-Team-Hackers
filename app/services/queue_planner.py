# app/services/queue_planner.py
"""
Versioned queue-planning integration. The v2 path calls the new read-only Queue
Planner workflow; the existing Operator 1 path is retained only for explicit
legacy rollback.

The queue used to order candidates by the `Priority` column — Highest, High,
Medium, Low. That is the wrong question. A `Low` ticket whose SLA has already
breached for a VIP matters more than a `Highest` ticket sitting comfortably
within target, and sorting by the stored field puts them the wrong way round.

Operator 1 already computes the right order. Its `step_3_prioritize_and_report`
ranks on (SLA status, VIP):

    (Breached, VIP)    1        (At risk, VIP)    3        (Within SLA, VIP)    5
    (Breached, other)  2        (At risk, other)  4        (Within SLA, other)  6

which is exactly the order the queue planning note asks for. So the planner does
not reimplement ranking — it asks the operator that owns triage, which also
means the preview order is traceable to operator evidence rather than to a
constant in our code.

The v2 workflow is read-only and stops after ranking. The existing Operator 1 path is
safe only as an explicit legacy rollback and is never selected automatically.
Operator 1 is safe to call for planning: all three of its steps are read-only,
it writes nothing to Supabase and sends no notification, and calling the
workflow directly stops after ranking rather than continuing into diagnosis and
remediation the way the orchestrator does. No new Auto workflow is needed for
this legacy path; the new Queue Planner workflow is the v2 planning boundary.

The cost is latency — it fetches and enriches the whole backlog, taking a minute
or more — so the ranking is cached briefly. A preview is a planning action taken
every few minutes at most, not a page load.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models.command_center import Policy
from .auto_client import AutoClient, AutoError

log = logging.getLogger(__name__)

OPERATOR_1_ID = os.getenv("AUTO_WF_OP1", "019fd826-9991-7003-84a1-9bac5f1eda3d")

# Long enough that clicking Preview twice does not pay for two backlog scans,
# short enough that a ticket resolved in the meantime is not ranked for long.
# Staleness is not a correctness risk here: every candidate is filtered against
# live Supabase and our own tables afterwards, and re-checked again before it
# runs.
RANKING_TTL_SECONDS = int(os.getenv("QUEUE_RANKING_TTL", "600"))

_cache: dict[str, Any] = {"ranked_at": 0.0, "tickets": []}
_v2_cache: dict[str, Any] = {}

_PLANNER_POLICY_DEFAULTS: dict[str, str] = {
    "sla_targets": "VIP: 4h response / 24h resolution; Non-VIP: 8h response / 48h resolution",
    "at_risk_window_minutes": "120",
    "default_region": "Global",
    "as_of": "2026-07-25T00:00:00Z",
    "flood_threshold_count": "5",
    "flood_window_minutes": "120",
    "correlation_confidence_threshold": "0.7",
    "include_relationship_types": "is caused by, relates to",
    "recurring_error_min_count": "20",
    "priority_ranking_order": (
        "Breached VIP, Breached Non-VIP, At risk VIP, At risk Non-VIP, "
        "Within SLA VIP, Within SLA Non-VIP"
    ),
    "max_candidates": "1000",
}


class PlannerUnavailable(RuntimeError):
    """Raised when a queue plan is absent, malformed, or too stale to use."""


@dataclass(frozen=True)
class PlannerResult:
    """Validated ranking plus the evidence needed by the Command Center."""

    tickets: list[dict[str, Any]]
    mode: str
    stale: bool = False
    run_id: Optional[str] = None
    generated_at: Optional[str] = None
    effective_as_of: Optional[str] = None
    policy_snapshot: dict[str, str] = field(default_factory=dict)


def _workflow_run_id(run: dict[str, Any]) -> Optional[str]:
    nested = run.get("workflowRun") or {}
    value = (
        nested.get("id")
        or nested.get("workflowRunId")
        or run.get("workflowRunId")
        or run.get("id")
    )
    return str(value) if value else None


def _final_subworkflow_run_id(run: dict[str, Any]) -> Optional[str]:
    """Return the last audited child-run ID linked from an orchestrator step."""
    for activity in reversed(_activity_runs(run)):
        outputs = activity.get("outputs") or {}
        display_data = outputs.get("displayData") or {}
        html = display_data.get("html") if isinstance(display_data, dict) else None
        if not isinstance(html, str):
            continue
        match = re.search(r"/runs/([0-9A-Za-z-]+)", html)
        if match:
            return match.group(1)
    return None


def _json_from_workflow_run(run: dict[str, Any]) -> dict[str, Any]:
    """Extract the final planner JSON from Auto's several response shapes."""
    if isinstance(run.get("prioritized_tickets"), list):
        return run

    for activity in reversed(_activity_runs(run)):
        outputs = activity.get("outputs") or {}
        raw = outputs.get("output")
        if isinstance(raw, dict) and isinstance(raw.get("prioritized_tickets"), list):
            return raw
        if not isinstance(raw, str) or "prioritized_tickets" not in raw:
            continue
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            continue
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("prioritized_tickets"), list):
            return parsed
    raise PlannerUnavailable("Queue Planner returned no prioritized_tickets output")


def normalize_planner_result(
    raw: dict[str, Any],
    *,
    run_id: Optional[str] = None,
    mode: str = "queue_planner",
    stale: bool = False,
    policy_snapshot: Optional[dict[str, str]] = None,
) -> PlannerResult:
    """Validate and normalize the strict JSON contract from the new planner."""
    payload = _json_from_workflow_run(raw)
    if str(payload.get("schema_version")) != "1":
        raise PlannerUnavailable("Queue Planner returned an unsupported schema version")

    tickets = payload.get("prioritized_tickets")
    scanned = payload.get("tickets_scanned")
    eligible = payload.get("tickets_eligible")
    evaluated = payload.get("sla_evaluated_count")
    if not isinstance(tickets, list):
        raise PlannerUnavailable("Queue Planner returned no ticket list")
    if not all(isinstance(value, int) and value >= 0 for value in (scanned, eligible, evaluated)):
        raise PlannerUnavailable("Queue Planner returned invalid coverage counts")
    if scanned < eligible or eligible < len(tickets):
        raise PlannerUnavailable("Queue Planner returned invalid coverage counts")
    if not payload.get("generated_at") or not payload.get("effective_as_of"):
        raise PlannerUnavailable("Queue Planner returned missing evidence timestamps")
    for timestamp_name in ("generated_at", "effective_as_of"):
        try:
            datetime.fromisoformat(str(payload[timestamp_name]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise PlannerUnavailable(
                f"Queue Planner returned invalid {timestamp_name} timestamp"
            ) from exc
    if evaluated < eligible:
        raise PlannerUnavailable("Queue Planner returned incomplete SLA evidence")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_positions: set[int] = set()
    valid_sla = {"breached", "at risk", "within sla"}
    for ticket in tickets:
        if not isinstance(ticket, dict):
            raise PlannerUnavailable("Queue Planner returned a non-object ticket")
        key = str(ticket.get("issue_key") or "").strip()
        if not key or key in seen:
            raise PlannerUnavailable("Queue Planner returned duplicate or missing issue keys")
        status = str(ticket.get("sla_status") or "").strip()
        if status.casefold() not in valid_sla:
            raise PlannerUnavailable(f"Queue Planner returned invalid SLA evidence for {key}")
        if not isinstance(ticket.get("vip"), bool):
            raise PlannerUnavailable(f"Queue Planner returned invalid VIP evidence for {key}")
        if not isinstance(ticket.get("priority_rank"), int) or not 1 <= ticket["priority_rank"] <= 6:
            raise PlannerUnavailable(f"Queue Planner returned invalid priority rank for {key}")
        if (
            not isinstance(ticket.get("rank_position"), int)
            or ticket["rank_position"] < 1
            or ticket["rank_position"] > eligible
        ):
            raise PlannerUnavailable(f"Queue Planner returned invalid rank position for {key}")
        if ticket["rank_position"] in seen_positions:
            raise PlannerUnavailable("Queue Planner returned duplicate rank positions")
        seen.add(key)
        seen_positions.add(ticket["rank_position"])
        item = dict(ticket)
        item["issue_key"] = key
        item["sla_status"] = status
        normalized.append(item)

    return PlannerResult(
        tickets=normalized,
        mode=mode,
        stale=stale,
        run_id=run_id,
        generated_at=(str(payload["generated_at"]) if payload.get("generated_at") else None),
        effective_as_of=(str(payload["effective_as_of"]) if payload.get("effective_as_of") else None),
        policy_snapshot=dict(policy_snapshot or {}),
    )


async def v2_ranked_backlog(
    db: Session,
    *,
    policy_snapshot: Optional[dict[str, str]] = None,
) -> PlannerResult:
    """Call only the new read-only Queue Planner workflow."""
    workflow_id = os.getenv("AUTO_WF_QUEUE_PLANNER", "").strip()
    if not workflow_id:
        raise PlannerUnavailable("AUTO_WF_QUEUE_PLANNER is not configured")
    client = AutoClient()
    try:
        run = await client.execute(
            workflow_id,
            _planner_inputs(db, policy_snapshot=policy_snapshot),
        )
    except AutoError as exc:
        raise PlannerUnavailable(f"Queue Planner unavailable: {exc}") from exc
    run_id = _workflow_run_id(run)
    try:
        return normalize_planner_result(
            run,
            run_id=run_id,
            policy_snapshot=policy_snapshot,
        )
    except PlannerUnavailable as root_error:
        child_run_id = _final_subworkflow_run_id(run)
        if not child_run_id:
            raise root_error
        try:
            child_result = await client.get_step_result(child_run_id)
        except AutoError as exc:
            raise PlannerUnavailable(
                f"Queue Planner final triage run unavailable: {exc}"
            ) from exc
        return normalize_planner_result(
            child_result,
            run_id=run_id,
            policy_snapshot=policy_snapshot,
        )


async def planner_result(db: Session, force_refresh: bool = False) -> PlannerResult:
    """Select legacy or v2 planning explicitly; never switch automatically."""
    mode = os.getenv("QUEUE_PLANNER_MODE", "legacy").strip().lower()
    if mode == "legacy":
        return await legacy_ranked_backlog(db, force_refresh=force_refresh)
    if mode == "supervity_v2":
        policy_snapshot = _planner_policy_snapshot(db)
        fingerprint = _policy_fingerprint(policy_snapshot)
        now = time.time()
        cached = _v2_cache.get("result")
        cached_age = now - float(_v2_cache.get("cached_at") or 0)
        same_policy = _v2_cache.get("fingerprint") == fingerprint
        fresh_ttl = int(os.getenv("QUEUE_PLANNER_FRESH_TTL", "60"))
        stale_ttl = int(os.getenv("QUEUE_PLANNER_STALE_TTL", "600"))
        if (
            not force_refresh
            and isinstance(cached, PlannerResult)
            and same_policy
            and cached_age < fresh_ttl
        ):
            return cached
        try:
            result = await v2_ranked_backlog(db, policy_snapshot=policy_snapshot)
        except PlannerUnavailable:
            if isinstance(cached, PlannerResult) and same_policy and cached_age <= stale_ttl:
                return replace(cached, mode="queue_planner_stale", stale=True)
            raise
        _v2_cache.clear()
        _v2_cache.update(
            {"cached_at": now, "fingerprint": fingerprint, "result": result}
        )
        return result
    raise PlannerUnavailable(f"Unsupported QUEUE_PLANNER_MODE: {mode}")


def _planner_policy_snapshot(db: Session) -> dict[str, str]:
    """Read the ten editable inputs that define a queue-planning result."""
    keys = [key for key in _PLANNER_POLICY_DEFAULTS if key != "max_candidates"]
    values = {
        p.key: str(p.value or p.default_value)
        for p in db.query(Policy).filter(Policy.key.in_(keys)).all()
        if p.value or p.default_value
    }
    return {
        key: values.get(key, default)
        for key, default in _PLANNER_POLICY_DEFAULTS.items()
    }


def _policy_fingerprint(snapshot: dict[str, str]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _planner_inputs(
    db: Session,
    *,
    policy_snapshot: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Inputs for the new Queue Planner workflow."""
    return dict(policy_snapshot or _planner_policy_snapshot(db))


def _legacy_inputs(db: Session) -> dict[str, str]:
    """Keep the existing Operator 1 contract available for manual rollback."""
    inputs = _planner_policy_snapshot(db)
    inputs["sla_thresholds"] = (
        "VIP: 4h response / 24h resolution, Non-VIP: 8h response / 48h resolution, "
        "'At risk' = 2h before breach"
    )
    return {
        key: inputs[key]
        for key in ("priority_ranking_order", "sla_thresholds")
    }


def _activity_runs(run: dict[str, Any]) -> list[dict[str, Any]]:
    """
    The step records, from either response shape.

    `POST /execute` nests them under `workflowRun`; `GET /workflow-runs/{id}`
    returns them at the top level. Reading only one shape yields an empty list
    and looks exactly like a workflow that produced no output — which is how
    this first failed.
    """
    nested = (run.get("workflowRun") or {}).get("activityRuns")
    if isinstance(nested, list) and nested:
        return nested
    top = run.get("activityRuns")
    return top if isinstance(top, list) else []


def _extract_tickets(run: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Pull `prioritized_tickets` out of Operator 1's run.

    Its last step prints a JSON blob preceded by a plain-text line about the
    uploaded CSV, so the payload has to be located inside the output rather than
    parsed from the whole string.
    """
    for activity in reversed(_activity_runs(run)):
        outputs = activity.get("outputs") or {}
        if "conditionMet" in outputs:
            continue
        raw = outputs.get("output")
        if not isinstance(raw, str) or "prioritized_tickets" not in raw:
            continue
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            continue
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        tickets = payload.get("prioritized_tickets")
        if isinstance(tickets, list):
            return [t for t in tickets if isinstance(t, dict) and t.get("issue_key")]
    return []


async def ranked_backlog(
    db: Session, force_refresh: bool = False
) -> list[dict[str, Any]]:
    """
    Operator 1's ranked backlog, cached.

    Returns [] when Operator 1 cannot be reached. Callers fall back to their own
    ordering rather than failing: a preview ordered by stored Priority is worse
    than one ordered by SLA, but far better than no preview at all, and the
    caller reports which ordering it used so nobody has to guess.
    """
    age = time.time() - float(_cache.get("ranked_at") or 0)
    if not force_refresh and _cache.get("tickets") and age < RANKING_TTL_SECONDS:
        return _cache["tickets"]

    client = AutoClient()
    try:
        run = await client.execute(OPERATOR_1_ID, _legacy_inputs(db))
    except AutoError as exc:
        log.warning("queue planner could not reach Operator 1: %s", exc)
        return []

    tickets = _extract_tickets(run)
    if not tickets:
        log.warning("Operator 1 returned no prioritized_tickets for planning")
        return []

    _cache["tickets"] = tickets
    _cache["ranked_at"] = time.time()
    log.info("queue planner ranked %d tickets via Operator 1", len(tickets))
    return tickets


async def legacy_ranked_backlog(
    db: Session, force_refresh: bool = False
) -> PlannerResult:
    """Return the legacy list behind a typed result for explicit rollback."""
    return PlannerResult(
        tickets=await ranked_backlog(db, force_refresh=force_refresh),
        mode="legacy",
    )


def rank_index(
    tickets: list[dict[str, Any]],
    *,
    ranked_by: str = "operator_1",
) -> dict[str, dict[str, Any]]:
    """issue_key -> the ranking evidence, so a preview can explain its order."""
    index: dict[str, dict[str, Any]] = {}
    for position, ticket in enumerate(tickets):
        key = str(ticket.get("issue_key") or "").strip()
        if not key or key in index:
            continue
        supplied_position = ticket.get("rank_position")
        if isinstance(supplied_position, int) and supplied_position >= 1:
            position = supplied_position - 1
        index[key] = {
            "position": position,
            "ranked_by": ranked_by,
            "priority_rank": ticket.get("priority_rank"),
            "sla_status": ticket.get("sla_status"),
            "vip": ticket.get("vip"),
            "assignment_group": ticket.get("assignment_group"),
            "major_incident_key": ticket.get("major_incident_key"),
            "major_incident_action": ticket.get("major_incident_action"),
            "incident_ticket_count": ticket.get("incident_ticket_count"),
            "incident_vip_count": ticket.get("incident_vip_count"),
            "ranking_reason": ticket.get("ranking_reason"),
            "source_priority": ticket.get("source_priority"),
            "source_status": ticket.get("source_status"),
            "source_updated_at": ticket.get("source_updated_at"),
        }
    return index


def ranking_reason(evidence: Optional[dict[str, Any]]) -> str:
    """One line a reviewer can read on the preview, explaining the position."""
    if not evidence:
        return "Ranked by stored Priority — Operator 1 ranking was unavailable."
    if evidence.get("ranking_reason"):
        return str(evidence["ranking_reason"])
    sla = evidence.get("sla_status") or "unknown SLA"
    who = "VIP" if evidence.get("vip") else "non-VIP"
    rank = evidence.get("priority_rank")
    planner_name = (
        "Queue Planner"
        if str(evidence.get("ranked_by", "")).startswith("queue_planner")
        else "Operator 1"
    )
    return f"{sla}, {who} ({planner_name} rank {rank})"
