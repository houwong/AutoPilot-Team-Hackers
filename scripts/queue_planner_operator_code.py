"""Source code embedded into the importable Queue Planning operator."""

# This file is deliberately plain Python: the build script embeds it into the
# Auto export without asking a model to rewrite the ranking logic.

OPERATOR_CODE = r'''import json
import os
import sys
from datetime import datetime, timezone

import httpx


ACTIVE_STATUSES = {"Open", "In Progress", "Waiting for support", "Waiting for customer"}
VALID_SLA = {"Breached", "At risk", "Within SLA"}
PRIORITY_WEIGHT = {"highest": 0, "critical": 0, "high": 1, "medium": 2, "low": 3}
REQUIRED_OUTPUT_FIELDS = (
    "schema_version", "generated_at", "effective_as_of", "tickets_scanned",
    "tickets_eligible", "sla_evaluated_count", "prioritized_tickets",
)


def _json_value(value, label):
    if isinstance(value, dict) and isinstance(value.get("outputs"), list):
        for activity in reversed(value["outputs"]):
            if not isinstance(activity, dict):
                continue
            outputs = activity.get("outputs") or {}
            raw = outputs.get("output") if isinstance(outputs, dict) else None
            if isinstance(raw, (dict, list)):
                return raw
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, (dict, list)):
                    return parsed
        raise ValueError("INVALID_" + label.upper())
    if isinstance(value, (dict, list)):
        return value
    if value is None or str(value).strip() == "":
        raise ValueError("MISSING_" + label.upper())
    try:
        return json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError("INVALID_" + label.upper()) from exc


def _as_list(payload, key, label):
    if isinstance(payload, list):
        return payload
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, list):
        raise ValueError("INVALID_" + label.upper())
    return value


def _iso(value):
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _priority_order(raw):
    expected = (
        "Breached VIP", "Breached Non-VIP", "At risk VIP",
        "At risk Non-VIP", "Within SLA VIP", "Within SLA Non-VIP",
    )
    values = tuple(part.strip() for part in str(raw or "").split(",") if part.strip())
    if values != expected or len(set(values)) != 6:
        raise ValueError("INVALID_PRIORITY_RANKING_ORDER")
    return {value: position + 1 for position, value in enumerate(values)}


def _sla_evidence(payload):
    tickets = _as_list(payload, "tickets", "sla_evidence")
    effective_as_of = payload.get("effective_as_of") if isinstance(payload, dict) else None
    if not effective_as_of:
        raise ValueError("MISSING_SLA_EFFECTIVE_AS_OF")
    evidence = {}
    for ticket in tickets:
        if not isinstance(ticket, dict):
            raise ValueError("INVALID_SLA_EVIDENCE")
        issue_key = str(ticket.get("issue_key") or ticket.get("Issue key") or "").strip()
        status = str(ticket.get("computed_sla_state") or ticket.get("sla_status") or "").strip()
        vip = ticket.get("is_vip", ticket.get("vip"))
        if not issue_key or status not in VALID_SLA or not isinstance(vip, bool):
            raise ValueError("INVALID_SLA_EVIDENCE")
        evidence[issue_key] = {"sla_status": status, "vip": vip}
    return evidence, str(effective_as_of)


def _incident_members(cluster):
    if not isinstance(cluster, dict):
        return [], None, None, None, [], 0
    parent = str(cluster.get("parent_issue_key") or "").strip() or None
    children = [str(key).strip() for key in (cluster.get("child_issue_keys") or []) if str(key).strip()]
    members = [str(key).strip() for key in (cluster.get("member_keys") or []) if str(key).strip()]
    keys = list(dict.fromkeys(([parent] if parent else []) + children + members))
    count = cluster.get("ticket_count", cluster.get("member_count"))
    if not isinstance(count, int) or count < len(keys):
        count = len(keys)
    groups = cluster.get("affected_assignment_groups", cluster.get("assignment_groups")) or []
    groups = [str(group).strip() for group in groups if str(group).strip()]
    action = str(cluster.get("recommended_action") or cluster.get("major_incident_action") or "monitor").strip()
    incident_key = str(cluster.get("linked_incident_label") or parent or "").strip() or None
    vip_count = cluster.get("vip_count")
    if not isinstance(vip_count, int) or vip_count < 0:
        vip_count = 0
    return keys, incident_key, action, count, groups, vip_count


def _incident_evidence(payload):
    raw = payload.get("clusters") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise ValueError("INVALID_INCIDENT_EVIDENCE")
    result = {}
    for cluster in raw:
        keys, incident_key, action, count, groups, vip_count = _incident_members(cluster)
        if not keys:
            continue
        normalized = {
            "major_incident_key": incident_key,
            "major_incident_action": action,
            "incident_ticket_count": count,
            "incident_vip_count": vip_count,
            "assignment_groups": groups,
        }
        for key in keys:
            result.setdefault(key, normalized)
    return result


async def _fetch_active_issues():
    base_url = str(
        os.environ.get("SUPABASE_URL")
        or "https://pkwvlnvxmawvgqzqvkcg.supabase.co"
    ).rstrip("/")
    token = os.environ.get("SUPABASE_TOKEN")
    if not token:
        raise ValueError("SUPABASE_READ_CONFIGURATION_MISSING")
    api_key = token
    if token.startswith("sbp_"):
        project_ref = base_url.split("//", 1)[-1].split(".", 1)[0]
        async with httpx.AsyncClient(timeout=30.0) as client:
            key_response = await client.get(
                "https://api.supabase.com/v1/projects/" + project_ref + "/api-keys",
                headers={"Authorization": "Bearer " + token},
            )
        key_response.raise_for_status()
        api_key = next(
            (
                item.get("api_key")
                for item in key_response.json()
                if item.get("name") == "service_role"
            ),
            None,
        )
        if not api_key:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY_MISSING")
    params = {
        "select": 'row_id,"Issue key","Status","Priority","Updated","customfield_10101 (Assignment group)"',
        "Status": "in.(Open,In Progress,Waiting for support,Waiting for customer)",
        "limit": "1000",
    }
    headers = {"apikey": api_key, "Authorization": "Bearer " + api_key}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(base_url + "/rest/v1/issues", params=params, headers=headers)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("INVALID_SUPABASE_ISSUE_ROWS")
    return rows


async def step_queue_planning_operator():
    sla_payload = _json_value(user_inputs.get("sla_states_json"), "sla_states_json")
    incident_payload = _json_value(user_inputs.get("incident_clusters_json"), "incident_clusters_json")
    sla, effective_as_of = _sla_evidence(sla_payload)
    incidents = _incident_evidence(incident_payload)
    priority_ranks = _priority_order(user_inputs.get("priority_ranking_order"))
    max_candidates = min(int(user_inputs.get("max_candidates") or 1000), 1000)
    if max_candidates < 1:
        raise ValueError("INVALID_MAX_CANDIDATES")

    rows = await _fetch_active_issues()
    active = []
    for row in rows:
        status = str(row.get("Status") or "").strip()
        issue_key = str(row.get("Issue key") or "").strip()
        if status in ACTIVE_STATUSES and issue_key:
            active.append(row)
    missing = [str(row.get("Issue key") or "").strip() for row in active if str(row.get("Issue key") or "").strip() not in sla]
    if missing:
        raise ValueError("INCOMPLETE_SLA_EVIDENCE: " + ",".join(sorted(missing)[:20]))

    ranked = []
    for row in active:
        issue_key = str(row.get("Issue key")).strip()
        sla_row = sla[issue_key]
        tier = sla_row["sla_status"] + (" VIP" if sla_row["vip"] else " Non-VIP")
        incident = incidents.get(issue_key) or {}
        action = incident.get("major_incident_action")
        actionable = action in {"declare_major_incident", "attach_to_existing"}
        stored_priority = str(row.get("Priority") or "").strip()
        updated = _iso(row.get("Updated"))
        ranked.append({
            "issue_key": issue_key,
            "row_id": row.get("row_id"),
            "source_status": row.get("Status"),
            "source_priority": row.get("Priority"),
            "source_updated_at": row.get("Updated"),
            "assignment_group": row.get("customfield_10101 (Assignment group)"),
            "sla_status": sla_row["sla_status"],
            "vip": sla_row["vip"],
            "_tier_rank": priority_ranks[tier],
            "_incident_boost": 0 if actionable else 1,
            "_incident_count": int(incident.get("incident_ticket_count") or 0),
            "_incident_vip_count": int(incident.get("incident_vip_count") or 0),
            "_stored_priority_rank": PRIORITY_WEIGHT.get(stored_priority.casefold(), 9),
            "_updated": updated,
            "major_incident_key": incident.get("major_incident_key"),
            "major_incident_action": action,
            "incident_ticket_count": incident.get("incident_ticket_count"),
            "incident_vip_count": incident.get("incident_vip_count"),
        })

    ranked.sort(key=lambda item: (
        item["_tier_rank"],
        item["_incident_boost"],
        -item["_incident_count"],
        -item["_incident_vip_count"],
        item["_stored_priority_rank"],
        item["_updated"] is None,
        item["_updated"] or datetime.max.replace(tzinfo=timezone.utc),
        item["issue_key"],
    ))
    output = []
    for position, item in enumerate(ranked[:max_candidates], 1):
        tier = item["sla_status"] + (" VIP" if item["vip"] else " Non-VIP")
        incident_note = ""
        if item["major_incident_action"] in {"declare_major_incident", "attach_to_existing"}:
            incident_note = "; major incident " + str(item["major_incident_key"] or "detected") + " with " + str(item["incident_ticket_count"] or 0) + " tickets"
        output.append({
            key: value for key, value in item.items() if not key.startswith("_")
        } | {
            "priority_rank": priority_ranks[tier],
            "rank_position": position,
            "ranking_reason": tier + incident_note + "; stored " + str(item["source_priority"] or "unknown"),
        })
    result = {
        "schema_version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "effective_as_of": effective_as_of,
        "tickets_scanned": len(rows),
        "tickets_eligible": len(active),
        "sla_evaluated_count": len(sla),
        "prioritized_tickets": output,
    }
    globals()["prioritized_tickets"] = result
    print(json.dumps(result, indent=2, default=str))


await step_queue_planning_operator()
'''
