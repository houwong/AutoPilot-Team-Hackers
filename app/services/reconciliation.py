# app/services/reconciliation.py
"""
Check that a ticket the agent claims to have changed actually changed.

A completed workflow path proves the agent ran, not that anything happened. The
queue derives `auto_remediated` from `step_6_notif_auto` completing, and
`human_approved` from `step_6_notif_manual` completing — both are evidence about
the orchestrator, not about the service desk.

The two can disagree, and they have. On 7 Aug a queue campaign reported
ITSM-2003 as `human_approved` while Supabase still showed it `Waiting for
support` with no resolution. Nothing had failed; the ticket simply had no
knowledge-base article, so Operator 3 correctly declined to act — and the queue
called that a completed human-approved outcome anyway.

Operator 3 returns an `expected_state` describing what it intended the row to
look like. It also returns its own `observed_state`, but that is read inside the
same run moments after its own write, so it cannot catch a write that never
happened or a value changed afterwards. This module re-reads Supabase at
reconciliation time instead, which is the check that actually distinguishes
"reported success" from "the ticket changed".

Results:

    verified            every expected field matches what Supabase holds now
    verification_failed the workflow claimed a write; the row disagrees
    not_applicable      the path deliberately changed nothing (block, reject,
                        or a review that ended without remediation)
    unknown             Operator 3 returned no expected_state to check against
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import supabase

log = logging.getLogger(__name__)

VERIFIED = "verified"
VERIFICATION_FAILED = "verification_failed"
NOT_APPLICABLE = "not_applicable"
UNKNOWN = "unknown"

# Operator 3 spells the expected row two ways. `evaluate_safety_and_route`
# returns the lowercase shape it reasons with ({"status", "comments"}), and
# `execute_and_verify_update` returns the Supabase column names it actually
# writes ({"Status", "Resolution"}). Compare on the column names, and translate
# the reasoning shape onto them rather than silently skipping those fields.
_FIELD_ALIASES = {
    "status": "Status",
    "comments": "Resolution",
    "resolution": "Resolution",
    "assignment_group": "customfield_10101 (Assignment group)",
    "assignee": "Assignee",
}


def _canonical_field(name: str) -> str:
    return _FIELD_ALIASES.get(str(name).strip().lower(), str(name).strip())


def _normalise(value: Any) -> str:
    """Compare as trimmed, case-folded text: Supabase returns everything as str."""
    if value is None:
        return ""
    return str(value).strip().casefold()


def expected_state_from(remediation: dict[str, Any] | None) -> dict[str, str]:
    """
    Pull the expected row out of an Operator 3 result, keyed by Supabase column.

    Takes whichever spelling the operator used and maps it onto the column names
    the comparison reads.
    """
    expected = (remediation or {}).get("expected_state")
    if not isinstance(expected, dict):
        return {}
    return {
        _canonical_field(k): v
        for k, v in expected.items()
        if v is not None and str(v).strip() != ""
    }


async def verify_ticket(
    issue_key: str, remediation: dict[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    """
    Compare Operator 3's intent against the live row.

    Returns (result, detail). `detail` always carries enough to explain the
    verdict on screen without a second lookup — a reviewer seeing
    `verification_failed` needs the mismatched fields, not just the word.

    A Supabase failure yields UNKNOWN rather than VERIFICATION_FAILED. Reporting
    a write as broken because our own read failed would be a worse error than
    the one this exists to catch.
    """
    expected = expected_state_from(remediation)
    if not expected:
        decision = str((remediation or {}).get("decision") or "").strip().upper()
        # A path that intentionally writes nothing is not a failure to write.
        if decision in {"HUMAN_REVIEW_REQUIRED", "BLOCKED", "ESCALATED", "REJECTED"}:
            return NOT_APPLICABLE, {"reason": f"decision {decision} makes no ticket change"}
        return UNKNOWN, {"reason": "Operator 3 returned no expected_state to check"}

    try:
        rows = await supabase.select(
            "issues", {'"Issue key"': f"eq.{issue_key}", "select": "*"}
        )
    except supabase.SupabaseError as exc:
        log.warning("could not re-read %s for verification: %s", issue_key, exc)
        return UNKNOWN, {"reason": f"could not read the ticket back: {exc}"}

    if not rows:
        return VERIFICATION_FAILED, {
            "reason": f"{issue_key} does not exist in Supabase",
            "expected": expected,
        }

    observed = rows[0]
    mismatches = [
        {
            "field": field,
            "expected": value,
            "observed": observed.get(field),
        }
        for field, value in expected.items()
        if _normalise(observed.get(field)) != _normalise(value)
    ]

    if mismatches:
        return VERIFICATION_FAILED, {
            "reason": (
                f"{len(mismatches)} field(s) do not match what the agent reported "
                f"writing to {issue_key}"
            ),
            "mismatches": mismatches,
            "expected": expected,
        }

    return VERIFIED, {
        "reason": f"all {len(expected)} expected field(s) match the live ticket",
        "expected": expected,
        "checked_fields": sorted(expected),
    }


def is_write_path(state: Optional[str]) -> bool:
    """
    States that assert the agent changed the ticket, and so must be verified.

    Deliberately narrow. `blocked` and `human_rejected` are outcomes where not
    writing is the correct result, and verifying them would invert the meaning
    of a pass.
    """
    return state in {"auto_remediated", "human_approved"}
