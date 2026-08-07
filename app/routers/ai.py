# app/routers/ai.py
"""
The AI Manager chat — answers questions about the agent from the agent's own records.

There is no language model behind this, and that is the point. Everything it says
is read from `agent_runs`, `operator_executions`, `exception_items`, `policies`
and the live integration probes, and every answer names the rows it came from. A
model asked "why did ITSM-2180 escalate?" can produce a fluent, plausible reason
it inferred rather than read. This cannot: if the run is not in the database, it
says so.

That matters more here than fluency. The whole product is a case for trusting an
agent's decisions, demonstrated on a dataset where several bugs this project
shipped produced confident, well-formed, wrong output. A chat panel that invents
an explanation for a decision would be the same failure wearing a friendlier
face.

Each answer carries `tool_calls` describing what was consulted, which the panel
renders — so a reader can see the grounding rather than take the text on faith.

The trade is coverage: it answers the questions it has patterns for and refuses
the rest, listing what it does know instead of guessing.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import String, func
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

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI Manager"])

ISSUE_KEY_RE = re.compile(r"\b(ITSM-\d{3,})\b", re.I)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., description="What the user typed")
    history: list[ChatMessage] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[ToolCall] = Field(default_factory=list)


def _tool(name: str, args: dict[str, Any], result: Any) -> ToolCall:
    return ToolCall(id=f"{name}-{abs(hash(str(args))) % 100000}", name=name,
                    args=args, result=result)


# --- answerers ---------------------------------------------------------------
#
# Each returns (answer, tool_calls) or None when it does not apply. Order in
# ROUTES decides precedence, so put the specific ones first: "why did ITSM-2180
# escalate" mentions a ticket AND the word "escalate", and the ticket-specific
# answer is the better one.


def _explain_ticket(msg: str, db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    """Why a specific ticket went the way it did."""
    m = ISSUE_KEY_RE.search(msg)
    if not m:
        return None
    key = m.group(1).upper()

    run = (
        db.query(AgentRun)
        .filter(AgentRun.issue_keys.isnot(None))
        .filter(func.cast(AgentRun.issue_keys, String).like(f"%{key}%"))
        .order_by(AgentRun.id.desc())
        .first()
    )
    if run is None:
        return (
            f"I have no run recorded for {key}. Either the agent has not been "
            f"pointed at it, or the run predates this database. You can start one "
            f"from the dashboard by entering {key} and clicking Run.",
            [_tool("find_runs", {"issue_key": key}, {"found": 0})],
        )

    steps = (
        db.query(OperatorExecution)
        .filter(OperatorExecution.agent_run_id == run.id)
        .order_by(OperatorExecution.sequence)
        .all()
    )
    item = (
        db.query(ExceptionItem)
        .filter(ExceptionItem.agent_run_id == run.id)
        .order_by(ExceptionItem.id.desc())
        .first()
    )

    path = " -> ".join(s.step_id for s in steps) or "no steps recorded"
    lines = [
        f"**{key}** — last run `{run.run_id[:8]}` finished as **{run.status}**"
        f"{f' in {round((run.duration_ms or 0)/1000)}s' if run.duration_ms else ''}.",
        "",
        f"Path taken: `{path}`",
    ]

    gate = ((item.context or {}).get("gate") if item else None) or {}
    if gate.get("decision"):
        lines += ["", f"The change gate decided **{gate['decision']}**."]
        if gate.get("reason"):
            lines.append(f"Reason given: _{gate['reason']}_")
        if gate.get("change_id"):
            lines.append(
                f"Change record `{gate['change_id']}` — risk {gate.get('risk') or 'unknown'}, "
                f"status {gate.get('status') or 'unknown'}."
            )

    if item:
        lines += ["", f"It raised a **{item.exception_type}** item ({item.severity}) "
                      f"in the Workbench: {item.title}"]
        if item.status == ExceptionStatus.RESOLVED.value:
            lines.append(f"Resolved as **{item.resolution}** by {item.resolved_by}.")
        else:
            lines.append("It is still open and waiting for a decision.")
        if item.recommendation:
            lines.append(f"\nRecommendation on the item: _{item.recommendation[:400]}_")

    if run.status == RunStatus.SUCCEEDED.value and not item:
        lines += ["", "No human review was needed — the agent completed this one on its own."]

    return "\n".join(lines), [
        _tool("find_runs", {"issue_key": key}, {"run_id": run.run_id, "status": run.status}),
        _tool("read_steps", {"run_id": run.run_id}, {"steps": [s.step_id for s in steps]}),
        _tool("read_workbench", {"issue_key": key},
              {"exception_id": item.id if item else None}),
    ]


def _workbench(msg: str, db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    if not re.search(r"workbench|review|exception|waiting|approv|queue", msg, re.I):
        return None
    items = (
        db.query(ExceptionItem)
        .filter(ExceptionItem.status == ExceptionStatus.OPEN.value)
        .order_by(ExceptionItem.id.desc())
        .all()
    )
    if not items:
        return (
            "Nothing is waiting for a human right now — the Workbench is empty.",
            [_tool("read_workbench", {"status": "open"}, {"count": 0})],
        )
    lines = [f"**{len(items)}** item(s) need a decision:", ""]
    for it in items:
        lines.append(
            f"- **{it.primary_issue_key}** — {it.exception_type} ({it.severity}). {it.title}"
        )
        if it.recommendation:
            lines.append(f"  _{it.recommendation[:220]}_")
    return "\n".join(lines), [
        _tool("read_workbench", {"status": "open"},
              {"count": len(items), "keys": [i.primary_issue_key for i in items]})
    ]


def _performance(msg: str, db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    if not re.search(r"autonom|how many run|performance|stats|rate|how.*doing|resolve", msg, re.I):
        return None
    total = db.query(AgentRun).count()
    succeeded = db.query(AgentRun).filter(AgentRun.status == RunStatus.SUCCEEDED.value).count()
    waiting = db.query(AgentRun).filter(
        AgentRun.status == RunStatus.AWAITING_HUMAN.value).count()
    failed = db.query(AgentRun).filter(AgentRun.status == RunStatus.FAILED.value).count()
    invocations = db.query(OperatorExecution).count()
    rate = round(succeeded / total * 100, 1) if total else 0.0
    avg = db.query(func.avg(AgentRun.duration_ms)).scalar()

    return (
        f"Across **{total}** runs: **{succeeded}** completed without a human, "
        f"**{waiting}** are waiting for a decision, **{failed}** failed.\n\n"
        f"That is an autonomy rate of **{rate}%**"
        f"{f', averaging {round(avg/1000)}s per run' if avg else ''}, "
        f"over {invocations} operator invocations.\n\n"
        f"The rate counts a run as autonomous only when it finished without "
        f"parking for review. Runs deliberately aimed at escalation tickets pull "
        f"it down, so read it alongside the Workbench rather than on its own.",
        [_tool("read_runs", {}, {"total": total, "succeeded": succeeded,
                                 "awaiting_human": waiting, "failed": failed})],
    )


def _policies(msg: str, db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    if not re.search(r"polic|threshold|rule|setting|config", msg, re.I):
        return None
    rows = db.query(Policy).filter(Policy.active.is_(True)).order_by(Policy.priority).all()

    # A named policy, if the question mentions one.
    for p in rows:
        token = p.key.replace("_", " ")
        if p.key.lower() in msg.lower() or token.lower() in msg.lower():
            return (
                f"**{p.name}** (`{p.key}`) is currently **{p.value}**.\n\n"
                f"{p.description or ''}\n\n"
                f"Applies to: {', '.join(p.applies_to or []) or 'unscoped'}. "
                f"Editing it on the Policies page changes the agent's behaviour on "
                f"the next run — the value is passed to the workflow as an input, "
                f"so there is no code change or redeploy.",
                [_tool("read_policies", {"key": p.key}, {"value": p.value})],
            )

    lines = [f"**{len(rows)}** active policies. The ones that most change behaviour:", ""]
    for key in ("kb_confidence_threshold", "flood_threshold_count", "escalating_statuses",
                "blocking_statuses", "require_change_record_for_production", "as_of"):
        p = next((r for r in rows if r.key == key), None)
        if p:
            lines.append(f"- **{p.name}** (`{p.key}`) = `{p.value}`")
    lines += ["", "Ask about any one by name for what it does. All of them are "
                  "editable on the Policies page and take effect on the next run."]
    return "\n".join(lines), [
        _tool("read_policies", {}, {"active": len(rows)})
    ]


def _integrations(msg: str, _db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    if not re.search(r"slack|outlook|email|integration|supabase|health|connect", msg, re.I):
        return None
    return (
        "Integration health is probed live on the Data Manager page, and each one "
        "reports **how** it was verified rather than just green or red:\n\n"
        "- **probed** — we called it just now and timed it (Supabase, Supervity Auto)\n"
        "- **observed** — we hold no credentials, so health comes from the last "
        "operator run that used it, including the operator's own per-channel "
        "delivery status (Slack, Outlook)\n"
        "- **internal** — our own records (Workbench)\n\n"
        "That distinction is deliberate: a channel whose step completed has not "
        "necessarily delivered anything. Outlook reported healthy for a whole day "
        "while every send was failing on a mailbox quota, because only the "
        "orchestrator step was being checked. Open the Data Manager for current "
        "status.",
        [_tool("describe_integrations", {}, {"source": "app/routers/integrations.py"})],
    )


def _capabilities(_msg: str, _db: Session) -> tuple[str, list[ToolCall]]:
    return (
        "I answer from this Command Center's own records — runs, operator steps, "
        "Workbench items, policies — so everything I say is read, not inferred. "
        "There is no language model here, which means I cannot invent an "
        "explanation for a decision the agent made.\n\n"
        "Things I can answer:\n\n"
        "- **Why did ITSM-2180 escalate?** — the path a ticket took, the gate's "
        "decision and reason, and what landed in the Workbench\n"
        "- **What needs review?** — everything open and why\n"
        "- **What is our autonomy rate?** — run outcomes and timings\n"
        "- **What is the confidence threshold?** — any policy value and what it does\n"
        "- **Is Slack working?** — how integration health is established\n\n"
        "Ask about a ticket by key for the most detail.",
        [],
    )


def _asking_what_i_do(msg: str, db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    """
    Answer "what can you do" directly.

    Without this the question falls through to the same fallback as "what is the
    weather in Paris", so a reasonable opening question gets prefixed with "I
    can't answer that" before the list of things it can answer.
    """
    if not re.search(r"what can you|what do you do|help|capabilit|who are you|how do you work",
                     msg, re.I):
        return None
    return _capabilities(msg, db)


ROUTES = (_explain_ticket, _asking_what_i_do, _workbench, _performance,
          _policies, _integrations)


@router.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    """
    Answer a question about the agent from the agent's own records.

    Returns 200 with an honest refusal rather than an error when nothing matches:
    the panel's own error path shows "I encountered an error", which reads as
    broken software when the truth is simply that the question was outside what
    can be answered from the data.
    """
    msg = (body.message or "").strip()
    if not msg:
        answer, calls = _capabilities(msg, db)
        return ChatResponse(response=answer, tool_calls=calls)

    for route in ROUTES:
        try:
            hit = route(msg, db)
        except Exception:  # noqa: BLE001 — one broken answerer must not kill the panel
            log.exception("AI Manager answerer %s failed", getattr(route, "__name__", route))
            continue
        if hit:
            answer, calls = hit
            return ChatResponse(response=answer, tool_calls=calls)

    answer, calls = _capabilities(msg, db)
    return ChatResponse(
        response="I can't answer that from what the Command Center records.\n\n" + answer,
        tool_calls=calls,
    )
