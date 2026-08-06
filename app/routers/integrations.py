# app/routers/integrations.py
"""
Data Manager — a live registry of every system this build is connected to.

Round 2 requires at least three live integrations across two categories,
including one channel and one system of record, visible and healthy here.

Health is established honestly, and the method is reported alongside the result:

  probed    we called the system just now and timed it
  observed  we cannot call it directly without credentials the operators hold,
            so status is derived from the last operator run that used it

Outlook and Slack are reached by Operator 4 through Auto's managed connections.
The Command Center has no credentials for them, and inventing a green light for
something we have not checked would be worse than saying how we know.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models.command_center import (
    AgentRun,
    HealthStatus,
    Integration,
    IntegrationCategory,
    OperatorExecution,
)
from ..services import supabase
from ..services.auto_client import AUTO_BASE_URL, AutoClient, AutoError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["Data Manager"])

# An observed integration is stale if no operator has used it recently.
OBSERVED_STALE_AFTER = timedelta(hours=24)

# name, category, purpose, how we verify it, which orchestrator steps prove it,
# and which field of Operator 4's result reports that channel's delivery.
#
# `steps` is a list because one channel is reached by several branches: Slack
# goes out on an escalation AND on a block, and binding it to only
# step_6_notif_rejected reported Slack as "never exercised" while it was
# succeeding on every escalate run — a block decision has never occurred in
# this dataset.
#
# `delivery_key` is what makes the status honest. The orchestrator step
# completes whether or not the message left the building; Operator 4 reports
# per channel, and only it knows that Outlook has been refusing every send on a
# mailbox quota while the step around it reported success.
REGISTRY: list[dict[str, Any]] = [
    {
        "name": "Supabase",
        "category": IntegrationCategory.SYSTEM_OF_RECORD.value,
        "purpose": "Ticket backlog, users, knowledge base, change records, SLA calendar",
        "method": "probed",
        "steps": [],
        "delivery_key": None,
    },
    {
        "name": "Supervity Auto",
        "category": IntegrationCategory.SYSTEM_OF_RECORD.value,
        "purpose": "Orchestrator and 7 operator agents",
        "method": "probed",
        "steps": [],
        "delivery_key": None,
    },
    {
        "name": "Microsoft Outlook",
        "category": IntegrationCategory.CHANNEL.value,
        "purpose": "Notifies the requester when a ticket is resolved, blocked or escalated",
        "method": "observed",
        "steps": ["step_6_notif_auto", "step_6_notif_escalated", "step_6_notif_manual"],
        "delivery_key": "email_delivery_status",
    },
    {
        "name": "Slack",
        "category": IntegrationCategory.CHANNEL.value,
        "purpose": "Escalations to #ticket-escalations for the support team",
        "method": "observed",
        "steps": ["step_6_notif_escalated", "step_6_notif_rejected"],
        "delivery_key": "slack_delivery_status",
    },
    {
        "name": "Command Center Workbench",
        "category": IntegrationCategory.HUMAN_LOOP.value,
        "purpose": "Human review of exceptions the agent must not decide alone",
        "method": "internal",
        "steps": [],
        "delivery_key": None,
    },
]


async def _probe_supabase() -> tuple[str, float | None, dict]:
    started = time.perf_counter()
    try:
        rows = await supabase.select("issues", {"select": "Issue key", "limit": "1"})
        ms = (time.perf_counter() - started) * 1000
        return HealthStatus.HEALTHY.value, ms, {"reachable": True, "sample_rows": len(rows)}
    except supabase.SupabaseError as exc:
        return HealthStatus.DOWN.value, None, {"error": str(exc)[:300]}


async def _probe_auto() -> tuple[str, float | None, dict]:
    started = time.perf_counter()
    try:
        client = AutoClient()
        # Cheapest authenticated read that proves credentials and reachability.
        async with httpx.AsyncClient(timeout=30) as http:
            from ..services.auto_client import _headers  # local import: private helper

            r = await http.get(f"{AUTO_BASE_URL}/api/v1/workflows?page=1&limit=1", headers=_headers())
        ms = (time.perf_counter() - started) * 1000
        if r.status_code >= 400:
            return HealthStatus.DEGRADED.value, ms, {"status_code": r.status_code}
        total = (r.json().get("pagination") or {}).get("total")
        return HealthStatus.HEALTHY.value, ms, {"workflows": total, "host": AUTO_BASE_URL}
    except (AutoError, httpx.HTTPError) as exc:
        return HealthStatus.DOWN.value, None, {"error": str(exc)[:300]}


def _observe(
    db: Session, step_ids: list[str], delivery_key: Optional[str] = None
) -> tuple[str, Optional[datetime], dict]:
    """
    Derive a channel's health from the last operator run that used it.

    Reads the most recent execution across ALL the steps that reach this
    channel, then prefers Operator 4's own per-channel verdict over the
    orchestrator step's status. The step completing only means the operator ran;
    it says nothing about whether the message was delivered.
    """
    row = (
        db.query(OperatorExecution)
        .filter(OperatorExecution.step_id.in_(step_ids))
        .order_by(OperatorExecution.id.desc())
        .first()
    )
    if row is None:
        return (
            HealthStatus.UNKNOWN.value,
            None,
            {"note": f"no run has exercised {' or '.join(step_ids)} yet"},
        )
    when = row.ended_at or row.started_at
    if when and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    detail = {
        "via_step": row.step_id,
        "last_status": row.status,
        "last_run_at": when.isoformat() if when else None,
    }

    # Operator 4's verdict wins when we have it.
    result = (row.output or {}).get("operator_result") or {}
    delivered = str(result.get(delivery_key) or "").upper() if delivery_key else ""
    if delivered:
        detail["delivery_status"] = delivered
        if delivered != "SUCCESS":
            detail["note"] = result.get("failure_reason") or "operator reported a failed send"
            return HealthStatus.DEGRADED.value, when, detail

    if row.status not in ("completed", "succeeded", "ok"):
        return HealthStatus.DEGRADED.value, when, detail
    if when and datetime.now(timezone.utc) - when > OBSERVED_STALE_AFTER:
        detail["note"] = "last verified over 24h ago"
        return HealthStatus.UNKNOWN.value, when, detail
    if delivery_key and not delivered:
        # The step ran but predates delivery-status capture, or the operator
        # returned nothing usable. Say so rather than paint it green.
        detail["note"] = "step completed; no per-channel delivery status recorded"
        return HealthStatus.UNKNOWN.value, when, detail
    return HealthStatus.HEALTHY.value, when, detail


def _workbench_health(db: Session) -> tuple[str, dict]:
    runs = db.query(AgentRun).count()
    return (
        HealthStatus.HEALTHY.value if runs else HealthStatus.UNKNOWN.value,
        {"runs_recorded": runs},
    )


@router.get("")
async def list_integrations(db: Session = Depends(get_db)):
    """
    The registry with current health.

    Checks run on request rather than on a schedule so the page always reflects
    the moment it was opened — a stale green light is worse than none.
    """
    out = []
    for spec in REGISTRY:
        row = db.query(Integration).filter(Integration.name == spec["name"]).first()
        if row is None:
            row = Integration(
                name=spec["name"],
                category=spec["category"],
                purpose=spec["purpose"],
            )
            db.add(row)

        if spec["name"] == "Supabase":
            status, ms, detail = await _probe_supabase()
            row.latency_ms = ms
            last_seen = datetime.now(timezone.utc)
        elif spec["name"] == "Supervity Auto":
            status, ms, detail = await _probe_auto()
            row.latency_ms = ms
            last_seen = datetime.now(timezone.utc)
        elif spec["method"] == "observed":
            status, last_seen, detail = _observe(
                db, spec["steps"], spec.get("delivery_key")
            )
            row.latency_ms = None
        else:
            status, detail = _workbench_health(db)
            last_seen = datetime.now(timezone.utc)

        row.status = status
        row.last_check_at = datetime.now(timezone.utc)
        row.detail = {**detail, "verification": spec["method"]}
        db.commit()

        out.append(
            {
                "name": row.name,
                "category": row.category,
                "purpose": row.purpose,
                "status": row.status,
                "verification": spec["method"],
                "latency_ms": round(row.latency_ms) if row.latency_ms else None,
                "last_seen_at": last_seen.isoformat() if last_seen else None,
                "last_check_at": row.last_check_at.isoformat(),
                "detail": row.detail,
            }
        )

    return {
        "integrations": out,
        "summary": _summarise(out),
    }


def _summarise(rows: list[dict]) -> dict:
    healthy = [r for r in rows if r["status"] == HealthStatus.HEALTHY.value]
    categories = {r["category"] for r in healthy}
    return {
        "total": len(rows),
        "healthy": len(healthy),
        "degraded": sum(1 for r in rows if r["status"] == HealthStatus.DEGRADED.value),
        "down": sum(1 for r in rows if r["status"] == HealthStatus.DOWN.value),
        "unknown": sum(1 for r in rows if r["status"] == HealthStatus.UNKNOWN.value),
        "categories_live": sorted(categories),
        # Round 2 floor: 3+ live integrations across 2+ categories, including a
        # channel and a system of record.
        "meets_round2_floor": (
            len(healthy) >= 3
            and len(categories) >= 2
            and IntegrationCategory.CHANNEL.value in categories
            and IntegrationCategory.SYSTEM_OF_RECORD.value in categories
        ),
    }
