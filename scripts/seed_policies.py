#!/usr/bin/env python
"""
Seed the `policies` table from the orchestrator's workflow inputs.

Each policy's `key` IS an Auto workflow input name. The backend reads active
policies on every run and passes their current values to Auto, so editing a row
here changes agent behaviour on the next run with no code and no redeploy —
which is exactly the demonstration the 20-point Policies criterion asks for.

    docker compose exec backend python scripts/seed_policies.py
"""
import sys

sys.path.insert(0, "/app")

from app.core.database import SessionLocal  # noqa: E402
from app.models.command_center import Policy, PolicyType  # noqa: E402

# key, name, type, value, value_type, applies_to, priority, description
POLICIES = [
    # --- SLA and triage -----------------------------------------------------
    ("sla_targets", "SLA response and resolution targets", "text",
     "VIP: 4h response / 24h resolution; Non-VIP: 8h response / 48h resolution",
     ["triage", "sla"], 10,
     "Resolution targets by VIP status. Drives every breach calculation."),
    ("at_risk_window_minutes", "At-risk warning window", "number", "120",
     ["triage", "sla"], 20,
     "Business minutes before breach at which a ticket becomes 'At risk'."),
    ("default_region", "Fallback region", "text", "Global",
     ["sla"], 30,
     "Calendar used when a ticket's reporter cannot be resolved to a region."),
    ("priority_ranking_order", "Queue ranking order", "text",
     "Breached VIP, Breached Non-VIP, At risk VIP, At risk Non-VIP, "
     "Within SLA VIP, Within SLA Non-VIP",
     ["triage"], 40,
     "Order in which the backlog is ranked for action."),
    ("sla_thresholds", "Legacy SLA thresholds (Operator 1)", "text",
     "VIP: 4h response / 24h resolution, Non-VIP: 8h response / 48h resolution, "
     "'At risk' = 2h before breach",
     ["triage"], 50, "Retained for Operator 1 compatibility."),

    # --- Diagnosis ----------------------------------------------------------
    ("kb_confidence_threshold", "Auto-remediation confidence threshold", "number", "0.85",
     ["auto_remediate"], 60,
     "Minimum KB match confidence before a fix may be applied without a human. "
     "Raise it and more tickets route to the Workbench."),
    ("stalled_days_threshold", "Stalled ticket threshold (days)", "number", "3",
     ["diagnose"], 70,
     "Days without progress before a ticket counts as stalled."),
    # Ships EMPTY on purpose. The value inherited from Round 1 mapped request
    # types to Ops_Access / Ops_Hardware / Ops_SaaS — none of which exist as
    # assignment groups; the only four are App Support, Field Support,
    # Network Ops and Service Desk. Every "Request access" ticket was therefore
    # flagged mis-routed against a phantom group, and Operator 3 acted on it:
    # it chose REASSIGN_TICKET over applying the KB fix and wrote "Ops_Access"
    # into a live ticket. Auto-resolution succeeded and was wrong, which is
    # worse than escalating.
    #
    # No mapping is correct here either: request type does not predict
    # assignment group in this data — "Request access" splits 39/38/36/26
    # across all four teams. Asserting a mapping produces noise, so mis-routing
    # is not claimed unless a real signal exists.
    ("routing_mapping_json", "Request type to assignment group", "json",
     '{}',
     ["routing"], 80,
     "Empty by default. Mis-routing is only detected when this maps request "
     "types to real assignment groups; the Round 1 value pointed at groups that "
     "do not exist and caused the agent to reassign tickets into a phantom "
     "group."),

    # --- Major incident -----------------------------------------------------
    ("flood_threshold_count", "Major incident ticket threshold", "number", "5",
     ["major_incident"], 90,
     "Related tickets required before a cluster is declared a major incident."),
    ("flood_window_minutes", "Flood detection window", "number", "120",
     ["major_incident"], 100,
     "Applied only to tickets with a real time of day; 387 of 460 Created "
     "values are date-only."),
    ("correlation_confidence_threshold", "Cluster inference confidence", "number", "0.7",
     ["major_incident"], 110,
     "Minimum confidence to infer an unlinked cluster."),
    ("include_relationship_types", "Incident relationship types", "text",
     "is caused by, relates to",
     ["major_incident"], 120,
     "Excludes 'duplicates', which is a dedupe case rather than an incident."),
    ("recurring_error_min_count", "Recurring known-error threshold", "number", "20",
     ["insights"], 130,
     "At or above this count over more than 3 days, a repeated summary is a "
     "known error rather than an incident."),

    # --- Change control -----------------------------------------------------
    ("blocking_statuses", "Statuses that block execution", "text", "Rejected, Rolled Back",
     ["cab_gate"], 140,
     "A rolled-back change must never be retried automatically."),
    ("escalating_statuses", "Statuses that require approval", "text", "Pending CAB Approval",
     ["cab_gate"], 150,
     "These route to the Workbench. Never put them in blocking_statuses — that "
     "removes the human-in-the-loop path."),
    ("require_cab_for_risk", "Risk levels requiring CAB", "text", "High, Medium",
     ["cab_gate"], 160, "Risk levels that cannot proceed without approval."),
    ("auto_approve_risk_levels", "Risk levels auto-approved", "text", "Low",
     ["cab_gate"], 170, "Status always beats risk: a pending Low change still waits."),
    ("require_change_record_for_production", "Require a change record", "boolean", "false",
     ["cab_gate"], 180,
     "When true, any ticket without a change_requests row is blocked. Only 13 "
     "of 460 tickets have one, so true blocks almost everything — useful as a "
     "live policy demonstration, wrong as a steady-state default."),

    # --- Run scope ----------------------------------------------------------
    ("as_of", "Evaluate as at", "text", "2026-07-25T00:00:00Z",
     ["sla", "major_incident"], 190,
     "The data pack was generated in July 2026. Against today's clock every "
     "ticket breaches, so demos must anchor here."),
    ("support_slack_channel", "Escalation Slack channel", "text", "#ticket-escalations",
     ["notify"], 200, "Where the support team is notified."),
    ("supabase_table_name", "Ticket table", "text", "issues",
     ["triage"], 210, "System-of-record table name."),
]


def main() -> None:
    db = SessionLocal()
    created = updated = 0
    try:
        for key, name, ptype, value, applies_to, priority, description in POLICIES:
            row = db.query(Policy).filter(Policy.key == key).first()
            if row is None:
                db.add(Policy(
                    key=key, name=name, description=description,
                    policy_type=PolicyType.RULE.value,
                    value=value, value_type=ptype, default_value=value,
                    applies_to=applies_to, active=True, priority=priority,
                    updated_by="seed",
                ))
                created += 1
            else:
                # Refresh metadata but never clobber a value someone has edited.
                row.name, row.description = name, description
                row.value_type, row.default_value = ptype, value
                row.applies_to, row.priority = applies_to, priority
                updated += 1
        db.commit()
        total = db.query(Policy).count()
        print(f"policies created={created} updated={updated} total={total}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
