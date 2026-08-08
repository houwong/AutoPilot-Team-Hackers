# app/services/queue_planner.py
"""
Rank the queue with Operator 1 instead of the ticket's stored Priority.

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

Operator 1 is safe to call for planning: all three of its steps are read-only,
it writes nothing to Supabase and sends no notification, and calling the
workflow directly stops after ranking rather than continuing into diagnosis and
remediation the way the orchestrator does. No new Auto workflow is needed for
this, despite the plan assuming one would be.

The cost is latency — it fetches and enriches the whole backlog, taking a minute
or more — so the ranking is cached briefly. A preview is a planning action taken
every few minutes at most, not a page load.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
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


def _planner_inputs(db: Session) -> dict[str, str]:
    """Operator 1 requires both of these, and both are editable policies."""
    values = {
        p.key: p.value
        for p in db.query(Policy)
        .filter(Policy.key.in_(["priority_ranking_order", "sla_thresholds"]))
        .all()
        if p.value
    }
    return {
        "priority_ranking_order": values.get(
            "priority_ranking_order",
            "Breached VIP, Breached Non-VIP, At risk VIP, At risk Non-VIP, "
            "Within SLA VIP, Within SLA Non-VIP",
        ),
        "sla_thresholds": values.get(
            "sla_thresholds",
            "VIP: 4h response / 24h resolution, Non-VIP: 8h response / 48h resolution",
        ),
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
        run = await client.execute(OPERATOR_1_ID, _planner_inputs(db))
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


def rank_index(tickets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """issue_key -> the ranking evidence, so a preview can explain its order."""
    index: dict[str, dict[str, Any]] = {}
    for position, ticket in enumerate(tickets):
        key = str(ticket.get("issue_key") or "").strip()
        if not key or key in index:
            continue
        index[key] = {
            "position": position,
            "priority_rank": ticket.get("priority_rank"),
            "sla_status": ticket.get("sla_status"),
            "vip": ticket.get("vip"),
            "assignment_group": ticket.get("assignment_group"),
        }
    return index


def ranking_reason(evidence: Optional[dict[str, Any]]) -> str:
    """One line a reviewer can read on the preview, explaining the position."""
    if not evidence:
        return "Ranked by stored Priority — Operator 1 ranking was unavailable."
    sla = evidence.get("sla_status") or "unknown SLA"
    who = "VIP" if evidence.get("vip") else "non-VIP"
    rank = evidence.get("priority_rank")
    return f"{sla}, {who} (Operator 1 rank {rank})"
