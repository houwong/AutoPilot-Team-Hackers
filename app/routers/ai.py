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

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
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
    if not re.search(
        r"workbench|review|exception|waiting|approv|queue|need.*(attention|decision|me)|"
        r"what should i|blocked|stuck|outstanding|pending",
        msg,
        re.I,
    ):
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
    # `how.*doing` used to live here and swallowed "how is Slack doing right
    # now?", answering with run statistics — a confidently wrong answer, which
    # is worse than a refusal. General "how are things" phrasings are matched
    # here only after the system-specific routes have declined.
    if not re.search(
        r"autonom|how many run|performance|stats|statistic|rate|resolve|"
        r"how (are|is) (it|things|we|the agent)|everything ok|summar|"
        r"what happened|recently|today|fail|error|went wrong|broke",
        msg,
        re.I,
    ):
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


async def _integrations(msg: str, db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    """
    Report what each connected system is doing right now.

    This used to return a fixed paragraph explaining how verification works,
    which meant "why is Outlook degraded?" got a lecture on methodology instead
    of the answer. Read the live registry instead, and name the specific system
    when the question names one.
    """
    if not re.search(
        r"slack|outlook|e-?mail|integration|supabase|superv|health|connect|"
        r"system|notif|channel",
        msg,
        re.I,
    ):
        return None

    from .integrations import list_integrations

    payload = await list_integrations(db)
    rows = payload.get("integrations") or []
    summary = payload.get("summary") or {}

    # If the question names one system, answer about that system.
    #
    # Match ANY word of the name, not just the first: "Microsoft Outlook" is
    # asked about as "outlook", and matching only the leading word meant the
    # question fell through to the general summary. Ignore short filler words so
    # "Command Center Workbench" is not matched by an unrelated "center".
    lowered = msg.lower()
    named = next(
        (
            r
            for r in rows
            if any(w for w in r["name"].lower().split() if len(w) > 4 and w in lowered)
        ),
        None,
    )
    if named is None and re.search(r"e-?mail", msg, re.I):
        named = next((r for r in rows if "outlook" in r["name"].lower()), None)

    if named:
        detail = named.get("detail") or {}
        lines = [f"**{named['name']}** is **{named['status']}**."]
        if detail.get("delivery_status"):
            lines.append(
                f"The operator's last send reported `{detail['delivery_status']}`."
            )
        if detail.get("note"):
            lines.append(f"_{detail['note']}_")
        if detail.get("last_run_at"):
            lines.append(f"Last exercised {detail['last_run_at']}.")
        lines.append("")
        lines.append(
            f"Verified by **{named.get('verification')}** — "
            + {
                "probed": "we called it just now and timed the response.",
                "observed": "we hold no credentials for it, so health comes from "
                "the last operator run that used it, including that operator's "
                "own per-channel delivery status.",
                "internal": "it is our own record-keeping.",
            }.get(named.get("verification"), "see the Data Manager.")
        )
        return "\n".join(lines), [
            _tool("read_integrations", {"name": named["name"]},
                  {"status": named["status"], "detail": detail})
        ]

    lines = [
        f"**{summary.get('healthy', 0)} of {summary.get('total', 0)}** connected "
        f"systems are healthy.",
        "",
    ]
    for r in rows:
        d = r.get("detail") or {}
        extra = d.get("delivery_status") or d.get("note") or ""
        lines.append(f"- **{r['name']}** — {r['status']}{f' ({extra})' if extra else ''}")
    if not summary.get("meets_round2_floor", True):
        lines.append("")
        lines.append("Not every category has a verified connection yet.")
    return "\n".join(lines), [
        _tool("read_integrations", {}, summary)
    ]


async def _backlog(msg: str, db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    """
    Answer questions about the ticket backlog itself.

    "How many tickets are breached?" is one of the most obvious things to ask a
    service-desk agent, and it used to fall through to the refusal because every
    route was about the agent rather than the work.
    """
    if not re.search(
        r"backlog|breach|at.?risk|sla|how many ticket|ticket.*(total|open|count)|"
        r"open ticket|incident",
        msg,
        re.I,
    ):
        return None

    from .dashboard import kpis as dashboard_kpis

    data = await dashboard_kpis(db)
    sd = data.get("service_desk") or {}
    if not sd.get("available"):
        return (
            "I could not read the service desk just now, so I have no backlog "
            "figures to give you.",
            [_tool("read_service_desk", {}, {"available": False})],
        )

    sla = sd.get("sla_stated") or {}
    lines = [
        f"**{sd.get('tickets_open', 0)}** open of **{sd.get('tickets_total', 0)}** tickets.",
        "",
        f"- **{sla.get('breached', 0)}** breached",
        f"- **{sla.get('at_risk', 0)}** at risk",
        f"- **{sla.get('within_sla', 0)}** within SLA",
    ]
    if sd.get("open_incidents"):
        lines += ["", f"Open major incidents: {', '.join(sd['open_incidents'])}."]
    lines += [
        "",
        "Those are the labels recorded on the ticket. Operator 5 recomputes SLA "
        "from business hours and regional holidays, and disagrees with the "
        "recorded label on most tickets — the queue is ranked on its answer, not "
        "on this one.",
    ]
    return "\n".join(lines), [
        _tool("read_service_desk", {}, {"open": sd.get("tickets_open"), "sla": sla})
    ]


def _greeting(msg: str, db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    """
    A greeting gets a short offer, not the full capability list.

    Two things were wrong here. `fullmatch` meant "hi there" and "hiya" were
    refused while "hi" was welcomed, which is the kind of brittleness that makes
    a chat feel broken. And answering a one-word hello with the entire
    capabilities dump is a wall of text: the right reply to "hi" is a sentence
    and a couple of concrete things to try.

    Matched at the start of the message rather than across the whole of it, so a
    greeting that carries a real question ("hi, why did ITSM-2180 escalate")
    falls through to the route that can answer it.
    """
    if not re.match(
        r"\s*(hi|hii+|hiya|hey+|hello|yo|sup|howdy|greetings|"
        r"good (morning|afternoon|evening))\b"
        # "hi there" and "hey team" are still just a greeting. Allowing the
        # trailing address matters more than it looks: refusing "hi there" while
        # welcoming "hi" is exactly the brittleness that makes a chat feel
        # broken on the first message anyone types.
        r"(\s+(there|team|folks|all|again))?[\s!.,]*$",
        msg,
        re.I,
    ):
        return None

    open_items = (
        db.query(ExceptionItem)
        .filter(ExceptionItem.status == ExceptionStatus.OPEN.value)
        .count()
    )
    opener = (
        f"There {'is' if open_items == 1 else 'are'} **{open_items}** "
        f"item{'' if open_items == 1 else 's'} waiting for a decision."
        if open_items
        else "Nothing is waiting for a human right now."
    )
    return (
        f"{opener} I can answer from the agent's own records — runs, operator "
        f"steps, Workbench items, policies.\n\n"
        f"Try:\n"
        f"- *what needs review?*\n"
        f"- *why did ITSM-2180 escalate?*\n"
        f"- *run ITSM-2005*",
        [_tool("read_workbench", {"status": "open"}, {"count": open_items})],
    )


def _pleasantry(msg: str, _db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    """
    Acknowledge a closer instead of refusing it.

    "thanks" and "ok" are not questions, and answering them with "I could not
    match that to anything I can read" makes the panel feel like it is scolding
    the user for being polite.
    """
    if not re.match(
        r"\s*(thanks?|thank you|ty|ok|okay|k|cool|nice|got it|great|perfect|"
        r"cheers|bye)\b[\s!.,]*$",
        msg,
        re.I,
    ):
        return None
    return "Any time. Ask whenever you need the records checked.", []


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


async def _trigger_run(msg: str, db: Session) -> Optional[tuple[str, list[ToolCall]]]:
    """
    Start a run from the chat.

    The guide requires this of the AI Manager: answer from real records *and*
    trigger or re-trigger an Operator from the same place. Answering alone is
    half the surface.

    Deliberately narrow. It starts the orchestrator, optionally on a named
    ticket, and reports what it did. It does not approve anything, resolve a
    Workbench item, or edit a policy — those are decisions with consequences,
    and they belong on the surfaces built to show their context, not behind a
    sentence typed into a chat box.

    The same guard the dashboard uses applies: a closed ticket is refused and
    the refusal is explained, because the agent must not reopen finished work.
    """
    if not re.search(r"\b(run|trigger|re-?run|start|execute)\b", msg, re.I):
        return None
    # "why did ITSM-2180 escalate" also contains a ticket key, so require an
    # imperative reading rather than a question.
    if re.search(r"^\s*(why|what|how|when|who|is|are|does|did|can)\b", msg.strip(), re.I):
        return None

    m = ISSUE_KEY_RE.search(msg)
    key = m.group(1).upper() if m else None

    from .agent import ORCHESTRATOR_ID, _consume, _refuse_closed_ticket, resolve_inputs

    if key:
        try:
            await _refuse_closed_ticket(key)
        except HTTPException as exc:
            return (
                f"I did not start a run. {exc.detail}",
                [_tool("trigger_run", {"issue_key": key},
                       {"started": False, "reason": exc.detail})],
            )

    inputs = resolve_inputs(db, {"target_issue_key": key} if key else {})
    run = AgentRun(
        run_id=str(uuid.uuid4()),
        workflow_id=ORCHESTRATOR_ID,
        trigger="ai_manager",
        status=RunStatus.PENDING.value,
        issue_keys=[key] if key else None,
        inputs=inputs,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    asyncio.create_task(_consume(run.id, ORCHESTRATOR_ID, inputs))

    target = f"**{key}**" if key else "the top of the queue"
    return (
        f"Started a run on {target} — run `{run.run_id[:8]}`.\n\n"
        f"It takes about two minutes. The orchestrator will fan out to Operators 5 "
        f"and 6, triage, diagnose, then decide at the change gate whether to "
        f"remediate, escalate to a human, or block. Ask me about {key or 'the ticket'} "
        f"once it finishes, or watch it on the dashboard.\n\n"
        f"It ran with the policy values currently active — including a confidence "
        f"threshold of "
        f"{next((p.value for p in db.query(Policy).filter(Policy.key == 'kb_confidence_threshold').all()), 'unset')}."
        , [_tool("trigger_run", {"issue_key": key, "workflow": "orchestrator"},
                 {"started": True, "run_id": run.run_id})],
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


# Order is the routing logic, so it is deliberate rather than incidental.
#
# _trigger_run first: "run ITSM-2180" also contains a ticket key, and
# _explain_ticket would otherwise answer with history instead of doing what was
# asked. _trigger_run declines anything phrased as a question, so "why did
# ITSM-2180 escalate" still reaches the explainer.
#
# The specific routes come before the general ones. _integrations sits above
# _performance because "how is Slack doing right now?" is a question about
# Slack, and _performance's looser phrasing used to swallow it and answer with
# run statistics instead.
ROUTES = (
    _greeting,
    _pleasantry,
    _trigger_run,
    _explain_ticket,
    _asking_what_i_do,
    _integrations,
    _backlog,
    _workbench,
    _policies,
    _performance,
)


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
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
            if asyncio.iscoroutine(hit):
                hit = await hit
        except Exception:  # noqa: BLE001 — one broken answerer must not kill the panel
            log.exception("AI Manager answerer %s failed", getattr(route, "__name__", route))
            continue
        if hit:
            answer, calls = hit
            return ChatResponse(response=answer, tool_calls=calls)

    # Nothing matched. Say what was understood before saying what was not: a
    # bare refusal plus a capability list reads as a wall, and the most common
    # near-miss is a real ticket key in a question phrased in a way no route
    # recognised.
    key = ISSUE_KEY_RE.search(msg)
    if key:
        return ChatResponse(
            response=(
                f"I did not follow the question, but I do have records for "
                f"**{key.group(1).upper()}**. Ask me *why {key.group(1).upper()} "
                f"escalated* for its path and decision, or say *run "
                f"{key.group(1).upper()}* to start a fresh run on it."
            ),
            tool_calls=[_tool("near_miss", {"issue_key": key.group(1).upper()}, {})],
        )

    answer, calls = _capabilities(msg, db)
    return ChatResponse(
        response=(
            "I could not match that to anything I can read from the records, so "
            "I would rather say so than guess.\n\n" + answer
        ),
        tool_calls=calls,
    )
