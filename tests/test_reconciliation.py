"""
Tests for the check that a ticket the agent claims to have changed did change.

The case that motivated this is real, not hypothetical: a queue campaign on
7 Aug reported ITSM-2003 as `human_approved` while Supabase still held it at
"Waiting for support" with no resolution. Nothing errored — Operator 3 had
correctly declined to act because the ticket had no knowledge-base article, and
the queue read a completed notification branch as a completed outcome.
"""

from __future__ import annotations

import pytest

from app.services import reconciliation


@pytest.mark.asyncio
async def test_matching_row_verifies(monkeypatch):
    async def fake_select(_table, _params):
        return [{"Issue key": "ITSM-1", "Status": "In Progress",
                 "Resolution": "Applying automated KB workaround from article KB-100."}]

    monkeypatch.setattr(reconciliation.supabase, "select", fake_select)
    result, detail = await reconciliation.verify_ticket(
        "ITSM-1",
        {
            "decision": "AUTO_REMEDIATE",
            "expected_state": {
                "Status": "In Progress",
                "Resolution": "Applying automated KB workaround from article KB-100.",
            },
        },
    )
    assert result == reconciliation.VERIFIED
    assert detail["checked_fields"] == ["Resolution", "Status"]


@pytest.mark.asyncio
async def test_unchanged_row_fails_verification(monkeypatch):
    """The exact ITSM-2003 shape: the agent reports a write, the row disagrees."""

    async def fake_select(_table, _params):
        return [{"Issue key": "ITSM-2003", "Status": "Waiting for support",
                 "Resolution": None}]

    monkeypatch.setattr(reconciliation.supabase, "select", fake_select)
    result, detail = await reconciliation.verify_ticket(
        "ITSM-2003",
        {
            "decision": "AUTO_REMEDIATE",
            "expected_state": {"Status": "In Progress", "Resolution": "Fix applied."},
        },
    )
    assert result == reconciliation.VERIFICATION_FAILED
    assert {m["field"] for m in detail["mismatches"]} == {"Status", "Resolution"}


@pytest.mark.asyncio
async def test_review_decision_is_not_applicable(monkeypatch):
    """Not writing is the correct outcome here, so it must not read as a failure."""

    async def fake_select(_table, _params):  # pragma: no cover - must not be called
        raise AssertionError("a no-write decision should not re-read the ticket")

    monkeypatch.setattr(reconciliation.supabase, "select", fake_select)
    result, _ = await reconciliation.verify_ticket(
        "ITSM-2020", {"decision": "HUMAN_REVIEW_REQUIRED", "expected_state": {}}
    )
    assert result == reconciliation.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_supabase_failure_is_unknown_not_failure(monkeypatch):
    """Our own read failing must not be reported as the agent's write failing."""

    async def fake_select(_table, _params):
        raise reconciliation.supabase.SupabaseError("connection refused")

    monkeypatch.setattr(reconciliation.supabase, "select", fake_select)
    result, _ = await reconciliation.verify_ticket(
        "ITSM-1",
        {"decision": "AUTO_REMEDIATE", "expected_state": {"Status": "Resolved"}},
    )
    assert result == reconciliation.UNKNOWN


def test_lowercase_expected_state_maps_onto_supabase_columns():
    """
    Operator 3 spells the expected row two ways.

    `evaluate_safety_and_route` returns {"status", "comments"}; the executing
    step returns the real column names. Comparing on the raw keys would skip
    every field from the first shape and report a vacuous pass.
    """
    expected = reconciliation.expected_state_from(
        {"expected_state": {"status": "In Progress", "comments": "Workaround applied."}}
    )
    assert expected == {"Status": "In Progress", "Resolution": "Workaround applied."}


def test_only_write_paths_are_verified():
    assert reconciliation.is_write_path("auto_remediated")
    assert reconciliation.is_write_path("human_approved")
    # Not writing is the point of these two; verifying them would invert a pass.
    assert not reconciliation.is_write_path("blocked")
    assert not reconciliation.is_write_path("human_rejected")
