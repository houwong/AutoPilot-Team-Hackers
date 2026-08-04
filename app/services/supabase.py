# app/services/supabase.py
"""
Minimal PostgREST client for the service-desk system of record.

The operators reach Supabase through Auto's native integration. The Command
Center needs its own narrow path for one thing only: recording a human decision
that the agent is not allowed to make for itself — a CAB approval.

Deliberately small. Anything the agent should do belongs in an operator.

⚠️ PostgREST parses an unquoted `name (…)` in a select list as embedded-resource
syntax and fails with PGRST200. Columns such as
`customfield_10030 (Time to resolution)` must be double-quoted, and are best
aliased:  select=stated_sla:"customfield_10030 (Time to resolution)"
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")


class SupabaseError(RuntimeError):
    pass


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SupabaseError("SUPABASE_URL / SUPABASE_KEY are not configured")
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


async def select(
    table: str, params: dict[str, str], timeout: float = 30.0
) -> list[dict[str, Any]]:
    """Read rows. `params` are PostgREST query parameters."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers=_headers(), params=params)
    if r.status_code >= 400:
        raise SupabaseError(f"select {table} failed {r.status_code}: {r.text[:400]}")
    return r.json()


async def patch(
    table: str, match: dict[str, str], values: dict[str, Any], timeout: float = 30.0
) -> list[dict[str, Any]]:
    """
    Update rows matching `match` (PostgREST filter syntax, e.g. {"change_id": "eq.CHG-0001"}).

    Returns the updated rows so the caller can record exactly what changed.
    """
    qs = "&".join(f"{quote(k)}={quote(v)}" for k, v in match.items())
    url = f"{SUPABASE_URL}/rest/v1/{table}?{qs}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.patch(
            url, headers=_headers({"Prefer": "return=representation"}), json=values
        )
    if r.status_code >= 400:
        raise SupabaseError(f"patch {table} failed {r.status_code}: {r.text[:400]}")
    return r.json()


async def record_cab_approval(
    change_id: str, approver: str, approved: bool
) -> Optional[dict[str, Any]]:
    """
    Write a human CAB decision back to `change_requests`.

    This is what makes a Workbench approval real: Operator 7 reads
    `change_requests`, so once the status moves off `Pending CAB Approval` the
    gate returns a different decision on the next run. The human's action
    changes the system of record, not just our own audit trail.
    """
    values = {
        "status": "Implemented" if approved else "Rejected",
        "approver": approver or "Command Center",
    }
    rows = await patch("change_requests", {"change_id": f"eq.{change_id}"}, values)
    if not rows:
        log.warning("no change_requests row updated for %s", change_id)
        return None
    log.info("change %s -> %s by %s", change_id, values["status"], values["approver"])
    return rows[0]
