# app/routers/dashboard.py
"""
Dashboard KPIs — the live operational picture.

Every number here is computed from something that actually happened: runs the
Command Center triggered, operator steps it recorded, exceptions a human dealt
with, and the current state of the ticket backlog in the system of record.

Nothing is seeded. If the agent has not run, the agent panel reads zero — which
is the honest answer, and better than a number that never moves.

One deliberate distinction: `sla_stated` counts come from the ticket's
`customfield_10030` label, which is written at intake and never recalculated.
Operator 5 computes the real business-hours state and disagrees with that label
on most tickets. The dashboard shows the stated view because it is what the
service desk currently believes; the disagreement itself is an Insight.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models.command_center import (
    AgentRun,
    ExceptionItem,
    ExceptionStatus,
    OperatorExecution,
    Policy,
    RunStatus,
)
from ..services import supabase

log = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

_OPEN_TICKET_STATUSES = {"Open", "In Progress", "Waiting for support", "Waiting for customer"}


async def _service_desk_snapshot() -> dict[str, Any]:
    """Current backlog from the system of record. Degrades gracefully if unreachable."""
    try:
        rows = await supabase.select(
            "issues",
            {
                "select": 'Issue key,Status,Priority,'
                          'sla_stated:"customfield_10030 (Time to resolution)",'
                          'grp:"customfield_10101 (Assignment group)",linked_incident',
                "limit": "2000",
            },
        )
    except supabase.SupabaseError as exc:
        log.warning("service desk snapshot unavailable: %s", exc)
        return {"available": False, "error": str(exc)[:200]}

    open_rows = [r for r in rows if (r.get("Status") or "") in _OPEN_TICKET_STATUSES]
    sla = Counter((r.get("sla_stated") or "Unknown") for r in open_rows)
    incidents = {r["linked_incident"] for r in rows if (r.get("linked_incident") or "").strip()}

    return {
        "available": True,
        "tickets_total": len(rows),
        "tickets_open": len(open_rows),
        "by_status": dict(Counter((r.get("Status") or "Unknown") for r in rows)),
        "by_assignment_group": dict(
            Counter((r.get("grp") or "Unassigned") for r in open_rows)
        ),
        "sla_stated": {
            "breached": sla.get("Breached", 0),
            "at_risk": sla.get("At risk", 0),
            "within_sla": sla.get("Within SLA", 0),
        },
        "open_incidents": sorted(incidents),
    }


@router.get("/kpis")
async def kpis(db: Session = Depends(get_db)):
    """Everything the dashboard needs, in one call."""
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    runs: list[AgentRun] = db.query(AgentRun).all()
    steps_total = db.query(OperatorExecution).count()
    exceptions: list[ExceptionItem] = db.query(ExceptionItem).all()

    runs_today = [r for r in runs if r.started_at and _aware(r.started_at) >= day_ago]
    succeeded = [r for r in runs if r.status == RunStatus.SUCCEEDED.value]
    awaiting = [r for r in runs if r.status == RunStatus.AWAITING_HUMAN.value]
    failed = [r for r in runs if r.status == RunStatus.FAILED.value]

    durations = [r.duration_ms for r in runs if r.duration_ms]
    resolved_exceptions = [e for e in exceptions if e.status == ExceptionStatus.RESOLVED.value]

    # A run that finished without needing a person is one the agent handled alone.
    handled_alone = len(succeeded)
    needed_a_human = len(awaiting) + len(resolved_exceptions)
    decided = handled_alone + needed_a_human

    # How long a human took to clear an exception.
    review_times = [
        (_aware(e.resolved_at) - _aware(e.created_at)).total_seconds() * 1000
        for e in resolved_exceptions
        if e.resolved_at and e.created_at
    ]

    return {
        "generated_at": now.isoformat(),
        "agent": {
            "runs_total": len(runs),
            "runs_24h": len(runs_today),
            "succeeded": len(succeeded),
            "awaiting_human": len(awaiting),
            "failed": len(failed),
            "operator_invocations": steps_total,
            "avg_run_ms": round(sum(durations) / len(durations)) if durations else None,
            # Share of decisions the agent completed without a person. Reported as
            # null rather than 0 when nothing has run, so an idle system does not
            # look like a failing one.
            "autonomy_rate": round(handled_alone / decided * 100, 1) if decided else None,
        },
        "workbench": {
            "open": sum(1 for e in exceptions if e.status == ExceptionStatus.OPEN.value),
            "resolved": len(resolved_exceptions),
            "by_severity": dict(Counter(e.severity for e in exceptions if e.severity)),
            "by_type": dict(Counter(e.exception_type for e in exceptions if e.exception_type)),
            "avg_review_ms": round(sum(review_times) / len(review_times))
            if review_times
            else None,
        },
        "policies": {
            "total": db.query(Policy).count(),
            "active": db.query(Policy).filter(Policy.active.is_(True)).count(),
        },
        "service_desk": await _service_desk_snapshot(),
    }


@router.get("/activity")
def activity(hours: int = 24, db: Session = Depends(get_db)):
    """
    Runs and operator invocations bucketed by hour — the activity chart.

    Buckets are emitted even when empty so the chart has a continuous axis
    rather than collapsing to the few hours that happen to have data.
    """
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=hours - 1)

    runs = [r for r in db.query(AgentRun).all() if r.started_at and _aware(r.started_at) >= start]
    steps = db.query(OperatorExecution).all()

    buckets: dict[str, dict[str, int]] = {}
    for i in range(hours):
        label = (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00")
        buckets[label] = {"runs": 0, "operators": 0, "exceptions": 0}

    for r in runs:
        label = _aware(r.started_at).replace(minute=0, second=0, microsecond=0).strftime(
            "%Y-%m-%dT%H:00"
        )
        if label in buckets:
            buckets[label]["runs"] += 1

    run_ids = {r.id for r in runs}
    for s in steps:
        if s.agent_run_id in run_ids and s.started_at:
            label = _aware(s.started_at).replace(minute=0, second=0, microsecond=0).strftime(
                "%Y-%m-%dT%H:00"
            )
            if label in buckets:
                buckets[label]["operators"] += 1

    for e in db.query(ExceptionItem).all():
        if e.created_at and _aware(e.created_at) >= start:
            label = _aware(e.created_at).replace(minute=0, second=0, microsecond=0).strftime(
                "%Y-%m-%dT%H:00"
            )
            if label in buckets:
                buckets[label]["exceptions"] += 1

    return {
        "hours": hours,
        "series": [{"hour": k, **v} for k, v in sorted(buckets.items())],
    }


def _aware(dt: datetime) -> datetime:
    """Postgres can hand back naive datetimes; treat those as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
