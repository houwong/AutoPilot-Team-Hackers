"""
Shared test setup.

The queue planner and the reconciliation check both reach outside the process —
one calls Operator 1 on Auto, the other re-reads Supabase. Neither belongs in a
unit test: they make the suite slow, non-deterministic, and dependent on a live
workspace and a valid API key.

Stubbing them here rather than in each test means a new test cannot silently
acquire a network call by touching the queue. A test that wants ranking or
verification behaviour overrides these explicitly, which also makes that
intent visible in the test itself.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Neutralise the two outbound calls the queue makes, for every test."""
    from app.services import queue as queue_service
    from app.services import reconciliation

    async def no_ranking(_db, force_refresh: bool = False):
        # Empty means "Operator 1 unavailable", and eligible_snapshots falls
        # back to its stored-Priority ordering — the behaviour under test.
        return []

    async def no_verification(_issue_key, _remediation):
        return reconciliation.UNKNOWN, {"reason": "stubbed in tests"}

    monkeypatch.setattr(queue_service, "ranked_backlog", no_ranking)
    monkeypatch.setattr(queue_service, "verify_ticket", no_verification)
