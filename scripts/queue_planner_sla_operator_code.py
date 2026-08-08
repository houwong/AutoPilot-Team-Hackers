"""Source code embedded into the single-step Queue Planner SLA operator."""

SLA_OPERATOR_CODE = r'''import asyncio
import json
import os
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx


ACTIVE_STATUSES = {"Open", "In Progress", "Waiting for support", "Waiting for customer"}


def _parse_datetime(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("MISSING_TICKET_CREATED")
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
        for pattern in ("%b %d %Y", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError("INVALID_TICKET_CREATED: " + text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolution_targets(raw):
    result = {"VIP": 1440, "Non-VIP": 2880}
    for group in str(raw or "").split(";"):
        if ":" not in group:
            continue
        label, rules = group.split(":", 1)
        category = "Non-VIP" if "non-vip" in label.casefold() else "VIP"
        match = re.search(r"(\d+)\s*([hm])\s*resolution", rules, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            result[category] = value * 60 if match.group(2).casefold() == "h" else value
    return result


def _business_minutes(start_utc, end_utc, calendar):
    if end_utc < start_utc:
        raise ValueError("AS_OF_PRECEDES_TICKET_CREATED")
    hours = str(calendar.get("business_hours") or "09:00-17:00 Mon-Fri")
    zone = ZoneInfo(str(calendar.get("timezone") or "UTC"))
    holidays = {
        value.strip()
        for value in str(calendar.get("holiday_dates") or "").split(";")
        if value.strip()
    }
    start_local = start_utc.astimezone(zone)
    end_local = end_utc.astimezone(zone)
    always_open = "24x7" in hours.casefold() or "24/7" in hours.casefold()
    match = re.search(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", hours)
    if not always_open and not match:
        raise ValueError("INVALID_BUSINESS_HOURS: " + hours)
    open_time = time(int(match.group(1)), int(match.group(2))) if match else time(0, 0)
    close_time = time(int(match.group(3)), int(match.group(4))) if match else time(0, 0)
    elapsed = 0
    day = start_local.date()
    while day <= end_local.date():
        is_holiday = day.isoformat() in holidays
        if not is_holiday and (always_open or day.weekday() < 5):
            window_start = datetime.combine(day, open_time, tzinfo=zone)
            window_end = (
                datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone)
                if always_open
                else datetime.combine(day, close_time, tzinfo=zone)
            )
            overlap_start = max(start_local, window_start)
            overlap_end = min(end_local, window_end)
            if overlap_start < overlap_end:
                elapsed += int((overlap_end - overlap_start).total_seconds() // 60)
        day += timedelta(days=1)
    return elapsed


async def _service_key(base_url, token):
    if not token:
        raise ValueError("SUPABASE_READ_CONFIGURATION_MISSING")
    if not token.startswith("sbp_"):
        return token
    project_ref = base_url.split("//", 1)[-1].split(".", 1)[0]
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            "https://api.supabase.com/v1/projects/" + project_ref + "/api-keys",
            headers={"Authorization": "Bearer " + token},
        )
    response.raise_for_status()
    key = next(
        (item.get("api_key") for item in response.json() if item.get("name") == "service_role"),
        None,
    )
    if not key:
        raise ValueError("SUPABASE_SERVICE_ROLE_KEY_MISSING")
    return key


async def _fetch_source_tables():
    base_url = str(os.environ.get("SUPABASE_URL") or "https://pkwvlnvxmawvgqzqvkcg.supabase.co").rstrip("/")
    key = await _service_key(base_url, os.environ.get("SUPABASE_TOKEN"))
    headers = {"apikey": key, "Authorization": "Bearer " + key}

    async def fetch(table):
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.get(
                base_url + "/rest/v1/" + table,
                params={"select": "*", "limit": "1000"},
                headers=headers,
            )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("INVALID_SUPABASE_" + table.upper())
        return rows

    issues, users, calendars = await asyncio.gather(
        fetch("issues"), fetch("users_directory"), fetch("sla_calendar")
    )
    return {"issues": issues, "users": users, "calendars": calendars}


async def step_sla_evidence_operator():
    source = await _fetch_source_tables()
    as_of = _parse_datetime(user_inputs.get("as_of") or datetime.now(timezone.utc).isoformat())
    targets = _resolution_targets(user_inputs.get("sla_targets"))
    try:
        at_risk = int(user_inputs.get("at_risk_window_minutes") or 120)
    except (TypeError, ValueError) as exc:
        raise ValueError("INVALID_AT_RISK_WINDOW") from exc
    default_region = str(user_inputs.get("default_region") or "Global")
    users = {}
    for row in source["users"]:
        name = str(row.get("display_name") or "").strip()
        if name:
            users.setdefault(name, row)
    calendars = {
        str(row.get("region")): row
        for row in source["calendars"]
        if row.get("region")
    }
    fallback_calendar = calendars.get(default_region)
    if not fallback_calendar:
        raise ValueError("MISSING_DEFAULT_SLA_CALENDAR")

    tickets = []
    for issue in source["issues"]:
        if str(issue.get("Status") or "").strip() not in ACTIVE_STATUSES:
            continue
        issue_key = str(issue.get("Issue key") or "").strip()
        if not issue_key:
            raise ValueError("MISSING_ACTIVE_ISSUE_KEY")
        user = users.get(str(issue.get("Reporter") or "").strip()) or {}
        raw_vip = user.get("x_vip", False)
        is_vip = raw_vip if isinstance(raw_vip, bool) else str(raw_vip).casefold() == "true"
        region = str(user.get("location") or default_region)
        calendar = calendars.get(region, fallback_calendar)
        created = _parse_datetime(issue.get("Created"))
        target = targets["VIP" if is_vip else "Non-VIP"]
        elapsed = _business_minutes(created, as_of, calendar)
        remaining = target - elapsed
        state = "Breached" if remaining < 0 else ("At risk" if remaining <= at_risk else "Within SLA")
        tickets.append({
            "issue_key": issue_key,
            "computed_sla_state": state,
            "is_vip": is_vip,
            "elapsed_business_minutes": elapsed,
            "sla_target_minutes": target,
        })

    result = {
        "evaluated_count": len(tickets),
        "effective_as_of": as_of.isoformat(),
        "tickets": tickets,
    }
    globals()["sla_evidence"] = result
    print(json.dumps(result, indent=2, default=str))


await step_sla_evidence_operator()
'''
