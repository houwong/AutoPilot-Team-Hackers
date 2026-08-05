# app/routers/insights.py
"""
AI Insights — read and regenerate.

Insights are computed on demand rather than on a schedule: the evidence counts
change every time the agent runs, and a stale insight about a pattern that has
since been resolved is worse than none.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models.command_center import Insight
from ..services import insights as engine
from ..services import supabase

log = logging.getLogger(__name__)

router = APIRouter(prefix="/insights", tags=["Insights"])

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


class InsightOut(BaseModel):
    id: int
    insight_type: Optional[str]
    severity: Optional[str]
    title: str
    body: Optional[str]
    evidence: Optional[dict]
    confidence: Optional[float]
    action_path: Optional[str]
    action_type: Optional[str]
    dismissed: Optional[bool]
    generated_at: Optional[datetime]

    class Config:
        from_attributes = True


@router.get("", response_model=list[InsightOut])
def list_insights(
    severity: Optional[str] = None,
    insight_type: Optional[str] = None,
    include_dismissed: bool = False,
    db: Session = Depends(get_db),
):
    """Most severe first — that is the order a reader wants them in."""
    q = db.query(Insight)
    if not include_dismissed:
        q = q.filter(Insight.dismissed.is_(False))
    if severity:
        q = q.filter(Insight.severity == severity)
    if insight_type:
        q = q.filter(Insight.insight_type == insight_type)
    rows = q.all()
    rows.sort(key=lambda i: (_SEVERITY_ORDER.get(i.severity or "info", 3), -(i.id or 0)))
    return rows


@router.post("/generate", response_model=list[InsightOut])
async def generate_insights(
    as_of: Optional[str] = Query(
        None,
        description="ISO instant to evaluate SLA against. The data pack is from July 2026, "
        "so demos should pass 2026-07-25T00:00:00Z.",
    ),
    db: Session = Depends(get_db),
):
    """
    Recompute every insight from current data.

    Replaces the stored set rather than appending: evidence counts move with
    each run, and duplicated observations would bury the real signal.
    """
    try:
        found = await engine.generate(db, as_of=as_of)
    except supabase.SupabaseError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach the system of record, so insights cannot be computed: {exc}",
        ) from exc
    found.sort(key=lambda i: (_SEVERITY_ORDER.get(i.severity or "info", 3), -(i.id or 0)))
    return found


@router.post("/{insight_id}/dismiss", response_model=InsightOut)
def dismiss(insight_id: int, db: Session = Depends(get_db)):
    """Hide an insight that has been acted on or judged not useful."""
    row = db.query(Insight).get(insight_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Insight not found")
    row.dismissed = True
    db.commit()
    db.refresh(row)
    return row


@router.get("/stats/summary")
def stats(db: Session = Depends(get_db)):
    """Counts for the Insights page header and the dashboard tile."""
    rows = db.query(Insight).filter(Insight.dismissed.is_(False)).all()
    by_sev: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for r in rows:
        by_sev[r.severity or "info"] = by_sev.get(r.severity or "info", 0) + 1
        by_type[r.insight_type or "pattern"] = by_type.get(r.insight_type or "pattern", 0) + 1
        if r.action_type and r.action_type != "none":
            by_action[r.action_type] = by_action.get(r.action_type, 0) + 1
    return {
        "total": len(rows),
        "by_severity": by_sev,
        "by_type": by_type,
        "actionable": sum(by_action.values()),
        "by_action": by_action,
        "last_generated_at": max((r.generated_at for r in rows if r.generated_at), default=None),
    }
