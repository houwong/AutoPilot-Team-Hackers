"""Helpers for reducing persisted Auto activity rows to one state per step."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from ..models.command_center import AgentRun, OperatorExecution


DONE_STEP_STATUSES = {"completed", "succeeded", "success", "ok"}
FAILED_STEP_STATUSES = {"failed", "error", "cancelled"}


def _terminal(row: OperatorExecution) -> bool:
    return row.status in DONE_STEP_STATUSES or row.status in FAILED_STEP_STATUSES


def _prefer(current: OperatorExecution | None, candidate: OperatorExecution) -> bool:
    """Return whether candidate is the best row for a step.

    Auto can expose the same activity more than once while a parked human form
    resumes. A terminal row is stronger evidence than a stale running row;
    otherwise the newest row wins, with an output-bearing row preferred when
    IDs are unavailable in a test double.
    """

    if current is None:
        return True
    current_terminal = _terminal(current)
    candidate_terminal = _terminal(candidate)
    if candidate_terminal != current_terminal:
        return candidate_terminal
    if bool(candidate.output) != bool(current.output):
        return bool(candidate.output)
    return (candidate.id or 0) >= (current.id or 0)


def canonical_step_map(rows: Iterable[OperatorExecution]) -> dict[str, OperatorExecution]:
    """Collapse duplicate activity rows to the strongest state per step ID."""

    result: dict[str, OperatorExecution] = {}
    for row in rows:
        if row.step_id and _prefer(result.get(row.step_id), row):
            result[row.step_id] = row
    return result


def load_canonical_step_map(db: Session, run: AgentRun) -> dict[str, OperatorExecution]:
    rows = (
        db.query(OperatorExecution)
        .filter(OperatorExecution.agent_run_id == run.id)
        .order_by(OperatorExecution.sequence, OperatorExecution.id)
        .all()
    )
    return canonical_step_map(rows)
