# Build prompts — Operators 5, 6, 7 (Supervity Auto)

Build each as **its own workflow** and get it passing standalone before adding it to the
orchestrator as a `subworkflow_call` step. One giant workflow fails the gate.

Paste each block into Auto's workflow builder as the description, then verify the generated
steps match the "Steps" section before publishing.

**Shared conventions** (all three follow the Round 1 operators' house style):
- Use the platform-managed **native Supabase integration**. Never invent or mock rows.
- Echo `issue_key` and `row_id` through every step; raise `DATA_INTEGRITY_ERROR` on mismatch.
- Return **strict JSON** matching the stated output contract — the backend binds to it.
- Every numeric threshold is a **workflow input**, never a constant. Those inputs are what the
  Command Center's Policies page edits at runtime.

---

## Operator 5 — SLA & Business-Hours Engine

> **Purpose.** Compute the true SLA state of IT service desk tickets from business hours,
> regional timezones and public holidays — not from raw elapsed time, and not by trusting a
> precomputed field.
>
> **Inputs (all editable at runtime):**
> - `sla_targets` (textarea, required). Default: `VIP: 4h response / 24h resolution; Non-VIP: 8h response / 48h resolution`
> - `at_risk_window_minutes` (number, required). Default: `120`. A ticket is "At risk" when this many business minutes or fewer remain before breach.
> - `default_region` (text, required). Default: `Global`. Used when a ticket's region cannot be resolved.
> - `issue_keys` (textarea, optional). Comma-separated. When empty, evaluate all unresolved tickets.
>
> **Steps.**
> 1. Fetch from Supabase `public`, project `stalled-ticket-resolver`, in parallel:
>    `issues`, `sla_calendar`, `team_roster`, `users_directory`.
> 2. **Resolve each ticket's region** by joining `issues."customfield_10101 (Assignment group)"`
>    → `team_roster.assignment_group` → `team_roster.region` → `sla_calendar.region`.
>    If no match, use `default_region` and set `region_resolved = false`.
> 3. **Parse the calendar row.** `business_hours` is either `HH:MM-HH:MM Mon-Fri` or the literal
>    `24x7 follow-the-sun`. `holiday_dates` is a semicolon-separated list of `YYYY-MM-DD`.
>    `timezone` is an IANA name.
> 4. **Compute elapsed business minutes** from `issues."Created"` to now (or to `Resolution` when
>    resolved), counting only minutes that fall inside the business window, on a working weekday,
>    and not on a holiday. For `24x7 follow-the-sun`, count every minute with no exclusions.
>    All arithmetic in the region's timezone, then converted to UTC for output.
>    **`issues."Created"` is stored as `text`, not a timestamp — parse it explicitly.**
>    (`Updated` and `Due date` are `timestamptz`; only `Created` needs the cast.)
> 5. **Determine VIP** from `users_directory.x_vip` matched on `issues.Reporter`. Select the
>    matching target from `sla_targets`.
> 6. **Classify.** `Breached` when elapsed ≥ target; `At risk` when remaining ≤
>    `at_risk_window_minutes`; otherwise `Within SLA`. Also compute `breach_at_utc`, the instant
>    the ticket will breach if untouched.
> 7. **Cross-check.** Read `issues."customfield_10030 (Time to resolution)"` as
>    `stated_sla_status` and compare against the computed value. Do not let it override the
>    computation — report disagreement instead.
>
> **Output (strict JSON):**
> ```json
> { "evaluated_count": 0,
>   "tickets": [{
>     "issue_key": "", "row_id": "", "region": "", "timezone": "", "region_resolved": true,
>     "is_vip": false, "business_hours_rule": "", "target_minutes": 0,
>     "business_minutes_elapsed": 0, "minutes_to_breach": 0, "breach_at_utc": "",
>     "computed_sla_state": "Breached|At risk|Within SLA",
>     "stated_sla_status": "", "discrepancy": false
>   }] }
> ```
>
> **Guardrails.** Never treat `customfield_10030` as the source of truth — it is a Round 1
> precomputed field and this operator exists to replace it. Never invent holidays or business
> hours not present in `sla_calendar`. Never count minutes outside the business window unless the
> rule is `24x7 follow-the-sun`. If a timezone is unparseable, fail loudly rather than defaulting
> to UTC silently.

**Why the cross-check matters:** every `discrepancy: true` row is free material for the AI
Insights layer — "N tickets carry an SLA status that disagrees with business-hours reality."

---

## Operator 6 — Major-Incident Detector

> **Purpose.** Find the major incident hiding inside a flood of individual tickets: correlate
> related tickets, identify or confirm the parent incident, size the blast radius, and recommend
> whether to declare a major incident.
>
> **Inputs (all editable at runtime):**
> - `flood_threshold_count` (number, required). Default: `5`. Minimum related tickets before a cluster is treated as a candidate major incident.
> - `flood_window_minutes` (number, required). Default: `20`. Time window for detecting an emergent flood.
> - `correlation_confidence_threshold` (number, required). Default: `0.7`. Minimum confidence to infer an unlinked cluster.
> - `include_relationship_types` (textarea, required). Default: `is caused by, relates to`
>
> **Steps.**
> 1. Fetch `issues`, `incident_problem_links`, `users_directory`, `team_roster` from Supabase in parallel.
> 2. **Explicit clusters — two independent sources, use both.**
>    a. Group `incident_problem_links` by `parent_incident_key`, keeping rows whose
>       `relationship` appears in `include_relationship_types`. Parents present in the data are
>       `ITSM-2180` (22 children) and `ITSM-2199` (6).
>    b. Group `issues` by the non-null `linked_incident` label. Values present are `INC-9001`
>       (24 tickets) and `INC-9002` (7).
>    These describe the same clusters under different names. **Reconcile them** and report
>    `linked_incident_label` alongside `parent_issue_key`. Where the two disagree — a ticket
>    carrying an `INC-` label but no link row, or vice versa — set `linkage_conflict = true`
>    rather than silently preferring one. Those conflicts are real Insights material.
>    `source = "linked"`.
> 3. **Emergent clusters.** Among tickets *not* present in `incident_problem_links`, use the LLM
>    to group by semantic similarity of `Summary` + `Description` + `Components`, restricted to
>    tickets created within `flood_window_minutes` of each other. Assign each group a confidence
>    score; discard those below `correlation_confidence_threshold`. `source = "inferred"`.
> 4. **Size the blast radius** for every cluster: ticket count, distinct reporters, VIP count
>    (via `users_directory.x_vip`), affected assignment groups, earliest and latest ticket time.
> 5. **Recommend an action** per cluster: `declare_major_incident` when count ≥
>    `flood_threshold_count`; `attach_to_existing` when a parent already exists;
>    `monitor` when below threshold.
> 6. **Handle duplicates separately.** Rows with `relationship = "duplicates"` are not a major
>    incident — return them as a `duplicates` list recommending which key to keep and which to close.
>
> **Output (strict JSON):**
> ```json
> { "clusters": [{
>     "parent_issue_key": "", "linked_incident_label": null, "linkage_conflict": false,
>     "child_issue_keys": [], "source": "linked|inferred",
>     "ticket_count": 0, "distinct_reporters": 0, "vip_count": 0,
>     "affected_assignment_groups": [], "first_seen": "", "last_seen": "",
>     "confidence": 0.0, "recommended_action": "declare_major_incident|attach_to_existing|monitor",
>     "rationale": ""
>   }],
>   "duplicates": [{ "keep_issue_key": "", "close_issue_key": "", "reason": "" }] }
> ```
>
> **Guardrails.** Discover parents from the data — never hardcode an incident key. Never create a
> parent for a cluster below `flood_threshold_count`. Never merge clusters that share no reporter,
> component or assignment group merely because their text is similar. `duplicates` never become a
> major incident.

**Note on the data:** one large cluster sits under `ITSM-2180` (18 `is caused by` + 4
`relates to`) and a second under `ITSM-2199` (6), plus 3 `duplicates` pairs. Separately,
`issues.linked_incident` labels 24 tickets `INC-9001` and 7 `INC-9002`. Use these to verify —
but discover them from the data. The logic must hold for clusters it has never seen, because a
judge may ask for a different case.

---

## Operator 7 — Change / CAB Approval Gate

> **Purpose.** Decide whether a proposed remediation is allowed to touch production, based on
> change-management records. This operator **decides only** — it never modifies a ticket or
> executes a change.
>
> **Inputs (all editable at runtime):**
> - `require_cab_for_risk` (textarea, required). Default: `High, Medium`. Risk levels that require CAB approval before execution.
> - `auto_approve_risk_levels` (textarea, required). Default: `Low`. Risk levels allowed to proceed without CAB.
> - `blocking_statuses` (textarea, required). Default: `Pending CAB Approval, Rejected, Rolled Back`
> - `require_change_record_for_production` (checkbox, required). Default: `true`. When true, a production-affecting fix with no change record is blocked.
> - `issue_key` (text, required)
>
> **Steps.**
> 1. Fetch `change_requests` and `issues` from Supabase for the given `issue_key`.
> 2. **Locate the change record** by `change_requests.issue_key`. Records carry `change_id`,
>    `risk`, `status`, `cab_approval_required`, `approver`.
> 3. **No record found.** If `require_change_record_for_production` is true, decide `block` with
>    `requires_new_change_request = true`. Otherwise `allow`.
> 4. **Record found — evaluate in this order:**
>    - `status` in `blocking_statuses` → `block`. For `Rejected`, mark `terminal = true`.
>    - `status = "Rolled Back"` → `block` and set `prior_rollback = true`; a previously rolled-back
>      change is a strong signal the same fix should not be retried automatically.
>    - `cab_approval_required = true` and `status` is not `Implemented` → `escalate` for CAB approval.
>    - `risk` in `require_cab_for_risk` and no approver recorded → `escalate`.
>    - `risk` in `auto_approve_risk_levels` and `cab_approval_required = false` → `allow`.
>    - `status = "Implemented"` with an approver → `allow`.
> 5. **Check rollback history** across all `change_requests` rows for the same `issue_key` and
>    report how many previously reached `Rolled Back`.
>
> **Output (strict JSON):**
> ```json
> { "issue_key": "", "change_id": null, "risk": null, "status": null,
>   "cab_approval_required": false, "approver": null,
>   "decision": "allow|block|escalate",
>   "reason": "", "terminal": false, "prior_rollback": false,
>   "rollback_count": 0, "requires_new_change_request": false }
> ```
>
> **Guardrails.** This operator never writes to `change_requests` or `issues`. Never invent an
> approver name — an empty `approver` means not yet approved. Never infer approval from `risk`
> alone. When the decision is `escalate`, the orchestrator must route to the Command Center
> Workbench, not proceed.

**Where it goes in the orchestrator:** immediately *before* `step_5_exec`. Operator 3 decides
whether a fix is technically safe; Operator 7 decides whether it is *permitted*. Both must pass.

---

## After building

1. Test each standalone with the seeded cases:
   - Op5 — a `Remote` (24x7) ticket vs a `Penang` ticket spanning `2026-08-31`
   - Op6 — should discover `ITSM-2180` and `ITSM-2199` unaided
   - Op7 — `CHG-0001` (Pending CAB), `CHG-0005` (Rolled Back), `CHG-0011` (Low, no CAB)
2. Publish a stable version of each.
3. Add to the orchestrator as `subworkflow_call` steps.
4. **Write down each operator's frozen input/output contract** — the backend binds to it on Thursday.
