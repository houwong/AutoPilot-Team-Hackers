# app/services/insights.py
"""
AI Insights — patterns a person would not assemble by hand.

Every insight is computed from data the agent actually processed or from the
state of the system of record. Nothing is invented, and each one carries the
evidence it was derived from so a reviewer can check the claim rather than take
it on trust.

Two kinds, as the brief distinguishes them:

  operational      — what is happening in the work itself: recurring known
                     errors, knowledge gaps, incidents forming, SLA risk,
                     uneven load
  automation       — what is happening to the operation: a manual step that
                     keeps recurring and should become a policy or an operator

An insight that cannot be acted on is not worth showing, so each carries an
`action_path` naming the specific next step.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models.command_center import (
    AgentRun,
    ExceptionItem,
    Insight,
    InsightType,
    PolicyEvaluation,
    Severity,
)
from . import supabase
from .sla import BREACHED, build_calendar, evaluate, parse_created

log = logging.getLogger(__name__)

# A ticket summary seen this many times over more than this many days is a
# recurring known error rather than an incident. Mirrors Operator 6's default.
RECURRING_MIN_COUNT = 20
RECURRING_MIN_DAYS = 3

# Below this CSAT score a resolution is treated as unsatisfactory.
CSAT_POOR = 3


_STOPWORDS = {"the", "a", "an", "on", "in", "of", "for", "to", "after", "and", "is", "not"}

# Titles the knowledge base ships as stubs rather than real articles.
_PLACEHOLDER_TITLE = re.compile(r"^known issue \d+$", re.I)


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def _tokens(text: str | None) -> set[str]:
    return {w for w in _norm(text).split() if w and w not in _STOPWORDS}


def _covered_by(summary: str | None, kb_tokens: list[set[str]], threshold: float = 0.6) -> bool:
    """
    Does any knowledge base article plausibly cover this ticket?

    Exact title matching is far too strict: the article "VPN drops after Windows
    security update" genuinely covers tickets summarised "VPN drops after
    update", and an exact comparison reports those as uncovered. Containment —
    what share of the summary's words appear in the article's title — matches
    the way Operator 2 actually links them.
    """
    s = _tokens(summary)
    if not s:
        return False
    return any(len(s & k) / len(s) >= threshold for k in kb_tokens if k)


async def generate(db: Session, as_of: str | None = None) -> list[Insight]:
    """
    Recompute the insight set and replace what is stored.

    Insights are regenerated rather than accumulated: a stale insight about a
    pattern that has since been resolved is worse than none, and the evidence
    counts change every time the agent runs.
    """
    try:
        tickets = await supabase.select(
            "issues",
            {
                "select": 'Issue key,Summary,Status,Priority,Created,Reporter,Components,'
                          'linked_incident,'
                          'stated_sla:"customfield_10030 (Time to resolution)",'
                          'grp:"customfield_10101 (Assignment group)"',
                "limit": "2000",
            },
        )
        kb = await supabase.select("knowledge_base", {"select": "*", "limit": "500"})
        roster = await supabase.select("team_roster", {"select": "*", "limit": "200"})
        csat = await supabase.select("csat_surveys", {"select": "*", "limit": "500"})
        links = await supabase.select("incident_problem_links", {"select": "*", "limit": "500"})
        users = await supabase.select("users_directory", {"select": "*", "limit": "500"})
        calendars = await supabase.select("sla_calendar", {"select": "*", "limit": "50"})
    except supabase.SupabaseError as exc:
        log.error("cannot generate insights, system of record unreachable: %s", exc)
        raise

    found: list[Insight] = []
    found += _recurring_known_errors(tickets, kb)
    found += _knowledge_gaps(tickets, kb)
    found += _major_incidents(tickets, links, users)
    found += _sla_reality_gap(tickets, users, calendars, as_of)
    found += _uneven_load(tickets, roster)
    found += _csat_risk(tickets, csat)
    found += _automation_opportunities(db)

    # Replace wholesale — see the docstring.
    db.query(Insight).delete()
    for i in found:
        db.add(i)
    db.commit()
    log.info("generated %d insights", len(found))
    return found


# =============================================================================
# OPERATIONAL
# =============================================================================


def _recurring_known_errors(tickets: list[dict], kb: list[dict]) -> list[Insight]:
    """Same summary, many users, over weeks — a known error, not an incident."""
    by_summary: dict[str, list[dict]] = defaultdict(list)
    for t in tickets:
        by_summary[t.get("Summary") or ""].append(t)

    kb_tokens = [_tokens(a.get("title")) for a in kb if not _PLACEHOLDER_TITLE.match(a.get("title") or "")]
    out: list[Insight] = []

    for summary, rows in by_summary.items():
        if len(rows) < RECURRING_MIN_COUNT or not summary:
            continue
        dates = sorted(
            parse_created(r["Created"]) for r in rows if r.get("Created")
        )
        if not dates:
            continue
        span_days = (dates[-1] - dates[0]).days
        if span_days <= RECURRING_MIN_DAYS:
            continue

        reporters = len({r.get("Reporter") for r in rows})
        has_kb = _covered_by(summary, kb_tokens)
        out.append(
            Insight(
                insight_type=InsightType.PATTERN.value,
                severity=Severity.WARNING.value if not has_kb else Severity.INFO.value,
                title=f"{len(rows)} tickets for “{summary}” over {span_days} days",
                body=(
                    f"{reporters} different people have raised the same request. "
                    + (
                        "A knowledge base article exists, so these should be deflecting "
                        "themselves — they are not."
                        if has_kb
                        else "No knowledge base article covers it, so every one is handled "
                        "from scratch."
                    )
                ),
                evidence={
                    "summary": summary,
                    "ticket_count": len(rows),
                    "distinct_reporters": reporters,
                    "span_days": span_days,
                    "has_kb_article": has_kb,
                    "sample_keys": [r["Issue key"] for r in rows[:5]],
                },
                confidence=0.95,
                action_path=(
                    "Review why the existing article is not deflecting these — wrong "
                    "trigger, or not surfaced at intake."
                    if has_kb
                    else f"Author a knowledge base article for “{summary}” and mark it "
                    "auto-safe so the agent can resolve it without a human."
                ),
                action_type="author_kb_article" if not has_kb else "investigate",
            )
        )
    return sorted(out, key=lambda i: -(i.evidence or {}).get("ticket_count", 0))[:6]


def _knowledge_gaps(tickets: list[dict], kb: list[dict]) -> list[Insight]:
    """How much of the backlog has no article behind it, and how real the KB is."""
    if not tickets or not kb:
        return []

    real = [a for a in kb if not _PLACEHOLDER_TITLE.match(a.get("title") or "")]
    placeholders = len(kb) - len(real)
    kb_tokens = [_tokens(a.get("title")) for a in real]

    uncovered = [t for t in tickets if not _covered_by(t.get("Summary"), kb_tokens)]
    share = len(uncovered) / len(tickets) * 100
    top = Counter(t.get("Summary") for t in uncovered).most_common(5)

    out: list[Insight] = []

    if share >= 20:
        out.append(
            Insight(
                insight_type=InsightType.RECOMMENDATION.value,
                severity=Severity.WARNING.value,
                title=f"{share:.0f}% of tickets have no knowledge base article behind them",
                body=(
                    f"{len(uncovered)} of {len(tickets)} tickets match none of the "
                    f"{len(real)} usable articles. Each is diagnosed from first principles "
                    "and none can be auto-resolved, so every one costs a person's time."
                ),
                evidence={
                    "uncovered": len(uncovered),
                    "total": len(tickets),
                    "usable_articles": len(real),
                    "biggest_gaps": [{"summary": s, "tickets": n} for s, n in top],
                },
                confidence=0.85,
                action_path=(
                    "Write articles for the five summaries above first — they account for "
                    f"{sum(n for _, n in top)} tickets between them."
                ),
                action_type="author_kb_article",
            )
        )

    # A knowledge base that is mostly empty stubs looks healthy by article count
    # and is useless in practice. Worth saying out loud.
    if placeholders and placeholders / len(kb) > 0.5:
        out.append(
            Insight(
                insight_type=InsightType.ANOMALY.value,
                severity=Severity.WARNING.value,
                title=f"{placeholders} of {len(kb)} knowledge base articles are empty placeholders",
                body=(
                    f"Only {len(real)} articles have a real title; the rest are named "
                    "“Known issue N”. The knowledge base looks populated by count and "
                    "cannot deflect anything."
                ),
                evidence={
                    "total_articles": len(kb),
                    "usable": len(real),
                    "placeholders": placeholders,
                    "real_titles": [a.get("title") for a in real][:10],
                },
                confidence=1.0,
                action_path=(
                    "Have the agent author articles from resolved tickets — the recurring "
                    "summaries above are the obvious first candidates."
                ),
                action_type="author_kb_article",
            )
        )
    return out


def _major_incidents(tickets: list[dict], links: list[dict], users: list[dict]) -> list[Insight]:
    """Clusters of tickets sharing one root cause."""
    vip = {u.get("display_name") for u in users if str(u.get("x_vip")).lower() == "true"}
    by_key = {t["Issue key"]: t for t in tickets}

    clusters: dict[str, list[str]] = defaultdict(list)
    for link in links:
        if (link.get("relationship") or "") != "duplicates":
            clusters[link["parent_incident_key"]].append(link["child_issue_key"])

    out: list[Insight] = []
    for parent, children in clusters.items():
        members = [parent] + children
        rows = [by_key[k] for k in members if k in by_key]
        if len(rows) < 5:
            continue
        groups = sorted({r.get("grp") for r in rows if r.get("grp")})
        label = next(
            (r.get("linked_incident") for r in rows if (r.get("linked_incident") or "").strip()),
            None,
        )
        languages = _language_spread(rows)
        out.append(
            Insight(
                insight_type=InsightType.ANOMALY.value,
                severity=Severity.CRITICAL.value,
                title=f"{len(rows)} tickets trace to one root cause ({label or parent})",
                body=(
                    f"{len({r.get('Reporter') for r in rows})} people across "
                    f"{len(groups)} teams reported the same failure"
                    + (
                        f", in {len(languages)} languages — the same outage described in "
                        + ", ".join(languages)
                        + "."
                        if len(languages) > 1
                        else "."
                    )
                    + " Handled individually this is dozens of separate investigations."
                ),
                evidence={
                    "parent": parent,
                    "linked_incident": label,
                    "ticket_count": len(rows),
                    "distinct_reporters": len({r.get("Reporter") for r in rows}),
                    "vip_count": sum(1 for r in rows if r.get("Reporter") in vip),
                    "assignment_groups": groups,
                    "languages": languages,
                    "members": members[:25],
                },
                confidence=1.0,
                action_path=(
                    f"Declare {label or parent} a major incident, attach the {len(children)} "
                    "child tickets, and run one comms thread instead of many."
                ),
                action_type="open_incident",
            )
        )
    return out


def _language_spread(rows: list[dict]) -> list[str]:
    """Detect the multilingual duplicates hiding inside a cluster."""
    langs = set()
    for r in rows:
        s = r.get("Summary") or ""
        if re.search(r"[一-鿿]", s):
            langs.add("Chinese")
        elif re.search(r"\b(no puedo|acceder|nomina)\b", s, re.I):
            langs.add("Spanish")
        elif re.search(r"\b(impossible|acceder au|paie)\b", s, re.I):
            langs.add("French")
        else:
            langs.add("English")
    return sorted(langs)


def _sla_reality_gap(
    tickets: list[dict], users: list[dict], calendars: list[dict], as_of: str | None
) -> list[Insight]:
    """
    The stated SLA label versus business-hours reality.

    `customfield_10030` is written at intake and never recalculated, so the
    service desk is steering on a number that has drifted from the truth.
    """
    if not calendars:
        return []
    now = parse_created(as_of) if as_of else datetime.now(timezone.utc).replace(tzinfo=None)
    cal_by_region = {c["region"]: build_calendar(c) for c in calendars}
    loc_by_name: dict[str, str] = {}
    for u in users:
        loc_by_name.setdefault(u.get("display_name") or "", u.get("location") or "")
    vip = {u.get("display_name") for u in users if str(u.get("x_vip")).lower() == "true"}

    disagreements: list[dict] = []
    checked = 0
    for t in tickets:
        if (t.get("Status") or "") in ("Resolved", "Done", "Closed"):
            continue
        region = loc_by_name.get(t.get("Reporter") or "")
        cal = cal_by_region.get(region)
        if not cal or not t.get("Created"):
            continue
        try:
            res = evaluate(
                t["Created"],
                now,
                cal,
                1440 if t.get("Reporter") in vip else 2880,
                120,
            )
        except (ValueError, KeyError):
            continue
        checked += 1
        stated = (t.get("stated_sla") or "").strip()
        if stated and stated.lower() != res.state.lower():
            disagreements.append(
                {
                    "issue_key": t["Issue key"],
                    "stated": stated,
                    "computed": res.state,
                    "elapsed_business_minutes": res.business_minutes_elapsed,
                    "region": cal.region,
                }
            )

    if checked == 0 or not disagreements:
        return []

    share = len(disagreements) / checked * 100
    hidden = [d for d in disagreements if d["computed"] == BREACHED and d["stated"] != BREACHED]
    return [
        Insight(
            insight_type=InsightType.ANOMALY.value,
            severity=Severity.CRITICAL.value if hidden else Severity.WARNING.value,
            title=(
                f"{len(disagreements)} of {checked} open tickets carry an SLA status that "
                "disagrees with business-hours reality"
            ),
            body=(
                f"The status recorded on the ticket is written at intake and never "
                f"recalculated. Recomputed against each region's working hours and holidays, "
                f"{share:.0f}% disagree"
                + (
                    f", and {len(hidden)} are already breached while still labelled otherwise."
                    if hidden
                    else "."
                )
            ),
            evidence={
                "checked": checked,
                "disagreements": len(disagreements),
                "hidden_breaches": len(hidden),
                "examples": disagreements[:8],
            },
            confidence=0.95,
            action_path=(
                "Recalculate SLA at intake from `sla_calendar` rather than trusting the "
                "stored label, and re-rank the queue on the computed value."
            ),
            action_type="investigate",
        )
    ]


def _uneven_load(tickets: list[dict], roster: list[dict]) -> list[Insight]:
    """Open work per team, weighted by how many people that team has."""
    open_rows = [
        t for t in tickets if (t.get("Status") or "") not in ("Resolved", "Done", "Closed")
    ]
    per_group = Counter(t.get("grp") or "Unassigned" for t in open_rows)
    size = Counter(m.get("assignment_group") for m in roster)
    if not per_group or not size:
        return []

    loads = [
        (g, n, size.get(g, 0), n / size[g])
        for g, n in per_group.items()
        if size.get(g)
    ]
    if len(loads) < 2:
        return []
    loads.sort(key=lambda x: -x[3])
    worst, best = loads[0], loads[-1]
    if worst[3] < best[3] * 1.5:
        return []

    return [
        Insight(
            insight_type=InsightType.PATTERN.value,
            severity=Severity.WARNING.value,
            title=f"{worst[0]} carries {worst[3]:.0f} open tickets per person, {best[0]} carries {best[3]:.0f}",
            body=(
                f"{worst[0]} has {worst[1]} open tickets across {worst[2]} people. "
                f"{best[0]} has {best[1]} across {best[2]}. That is a "
                f"{worst[3] / max(best[3], 0.1):.1f}× difference in load."
            ),
            evidence={
                "per_team": [
                    {"team": g, "open": n, "people": s, "per_person": round(r, 1)}
                    for g, n, s, r in loads
                ]
            },
            confidence=0.85,
            action_path=(
                f"Rebalance routing away from {worst[0]}, or staff it from the on-call "
                "roster during peaks."
            ),
            action_type="investigate",
        )
    ]


def _csat_risk(tickets: list[dict], csat: list[dict]) -> list[Insight]:
    """Resolutions the requester was not happy with."""
    scored = [c for c in csat if str(c.get("score") or "").strip().isdigit()]
    if len(scored) < 5:
        return []
    poor = [c for c in scored if int(c["score"]) <= CSAT_POOR]
    if not poor:
        return []
    share = len(poor) / len(scored) * 100
    non_response = len(csat) - len(scored)
    return [
        Insight(
            insight_type=InsightType.PATTERN.value,
            severity=Severity.WARNING.value if share > 25 else Severity.INFO.value,
            title=f"{share:.0f}% of surveyed resolutions scored {CSAT_POOR} or below",
            body=(
                f"{len(poor)} of {len(scored)} returned surveys were unsatisfactory"
                + (
                    f", and {non_response} surveys were never answered at all."
                    if non_response
                    else "."
                )
                + " A closed ticket is not the same as a solved problem."
            ),
            evidence={
                "responses": len(scored),
                "poor": len(poor),
                "non_response": non_response,
                "examples": [
                    {"issue_key": c.get("issue_key"), "score": c.get("score"),
                     "comment": c.get("comment")}
                    for c in poor[:5]
                ],
            },
            confidence=0.9,
            action_path=(
                "Route any survey scoring at or below "
                f"{CSAT_POOR} back to the Workbench for a human follow-up."
            ),
            action_type="create_policy",
        )
    ]


# =============================================================================
# AUTOMATION OPPORTUNITIES — about the operation, not the tickets
# =============================================================================


def _automation_opportunities(db: Session) -> list[Insight]:
    """
    What the agent keeps handing to a human, and what that suggests.

    This is the insight class the brief calls out as the strongest to build:
    a manual step recurring often enough to deserve a policy or a new operator.
    """
    out: list[Insight] = []
    day_ago = datetime.now(timezone.utc) - timedelta(days=7)

    exceptions = db.query(ExceptionItem).all()
    if exceptions:
        by_type = Counter(e.exception_type for e in exceptions if e.exception_type)
        top_type, n = by_type.most_common(1)[0]
        resolved = [e for e in exceptions if e.resolution]
        approved = sum(1 for e in resolved if e.resolution in ("approved", "modified"))

        if n >= 2:
            approval_rate = (approved / len(resolved) * 100) if resolved else None
            out.append(
                Insight(
                    insight_type=InsightType.RECOMMENDATION.value,
                    severity=Severity.INFO.value,
                    title=f"“{(top_type or '').replace('_', ' ')}” is the most common reason a human is needed",
                    body=(
                        f"{n} of {len(exceptions)} exceptions were this type"
                        + (
                            f", and {approval_rate:.0f}% of reviewed items were approved. "
                            "A rule that is almost always approved is a candidate for "
                            "automation."
                            if approval_rate is not None and approval_rate >= 75
                            else "."
                        )
                    ),
                    evidence={
                        "by_type": dict(by_type),
                        "total": len(exceptions),
                        "resolved": len(resolved),
                        "approved": approved,
                    },
                    confidence=0.8,
                    action_path=(
                        "Relax the policy that produces this exception, or add an operator "
                        "that handles the common case so only the genuinely unusual ones "
                        "reach a person."
                    ),
                    action_type="create_policy",
                )
            )

    evaluations = db.query(PolicyEvaluation).all()
    if evaluations:
        by_decision = Counter(e.decision for e in evaluations if e.decision)
        blocked = by_decision.get("block", 0)
        if blocked and blocked / max(len(evaluations), 1) > 0.4:
            out.append(
                Insight(
                    insight_type=InsightType.RECOMMENDATION.value,
                    severity=Severity.WARNING.value,
                    title=f"{blocked} of {len(evaluations)} gate decisions were blocks",
                    body=(
                        "Most proposed actions are being stopped before they run. Either the "
                        "change records are out of date, or the gate is stricter than the "
                        "operation needs."
                    ),
                    evidence={"by_decision": dict(by_decision)},
                    confidence=0.75,
                    action_path=(
                        "Check `require_change_record_for_production` and "
                        "`require_cab_for_risk` on the Policies page against how this desk "
                        "actually works."
                    ),
                    action_type="create_policy",
                )
            )

    runs = [r for r in db.query(AgentRun).all() if r.started_at]
    recent = [r for r in runs if _aware(r.started_at) >= day_ago]
    if len(recent) >= 3:
        durations = [r.duration_ms for r in recent if r.duration_ms]
        if durations:
            avg = sum(durations) / len(durations) / 1000
            out.append(
                Insight(
                    insight_type=InsightType.PATTERN.value,
                    severity=Severity.INFO.value,
                    title=f"The agent averages {avg:.0f}s per ticket end to end",
                    body=(
                        f"{len(recent)} runs in the last 7 days. Each one covers triage, "
                        "diagnosis, incident correlation and the change gate — work that "
                        "would otherwise be done by hand."
                    ),
                    evidence={
                        "runs": len(recent),
                        "avg_seconds": round(avg, 1),
                        "fastest_seconds": round(min(durations) / 1000, 1),
                        "slowest_seconds": round(max(durations) / 1000, 1),
                    },
                    confidence=1.0,
                    action_path="Use this as the baseline when reporting time saved.",
                    action_type="none",
                )
            )
    return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
