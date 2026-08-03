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
> - `as_of` (text, optional). ISO-8601 instant to evaluate against; empty means now.
>   **Required for demos.** The data pack was generated in July 2026, so against today's clock
>   every ticket breaches and the dashboard reads 100% breach. Anchor runs to
>   `2026-07-20T04:00:00Z`.
> - `issue_keys` (textarea, optional). Comma-separated. When empty, evaluate all unresolved tickets.
>
> **Steps.**
> 1. Fetch from Supabase `public`, project `stalled-ticket-resolver`, in parallel:
>    `issues`, `sla_calendar`, `team_roster`, `users_directory`.
> 2. **Resolve each ticket's region from the REPORTER, not the assignment group:**
>    `issues."Reporter"` → `users_directory.display_name` → `users_directory.location`
>    → `sla_calendar.region`. This join is exact — all 97 users map 1:1 onto the five
>    calendar regions.
>    **Do not resolve region via `customfield_10101 (Assignment group)`.** That mapping is
>    one-to-many and therefore ambiguous: App Support spans KL-HQ, Remote *and* Singapore,
>    because `team_roster` holds members of the same group in different regions. Using it
>    picks an arbitrary calendar and produces confidently wrong SLA numbers.
>    `display_name` is not unique in `users_directory`, so when more than one user matches,
>    take the first deterministically and set `reporter_ambiguous = true`.
>    If no match at all, use `default_region` and set `region_resolved = false`.
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
>     "issue_key": "", "row_id": "", "region": "", "timezone": "",
>     "region_resolved": true, "reporter_ambiguous": false,
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

### Known failure modes — five builds, five defects, all silent

> **Operator 5 passed on build 5** (3 Aug): all eight fixture tickets exact on elapsed
> minutes, SLA state and `breach_at`.

The single most important lesson for Operators 6 and 7: **not one of these five defects threw
an error.** Every build produced confident, well-formatted, plausible output. Four of the five
would have shipped unnoticed without a fixture of known-correct values to compare against.

| # | Defect | How it looked | How it was caught |
|---|---|---|---|
| 1 | `as_of` ignored, used wall-clock now | 8 Breached, 0 At risk — a plausible backlog | Every value exactly 281 min above reference |
| 2 | `03/07/2026` parsed month-first | One large number among large numbers | 215081 vs 45161 on ITSM-2091 |
| 3 | Stated-SLA column never fetched | "0 discrepancies" — looked like agreement | Empty column + fixture said 6 |
| 4 | 24×7 rule lost, fell back to 8h days | Totals still read 6 discrepancies | Three Remote values changed |
| 5 | Always-open day = 1439 min | Off by 8–17 min, invisible at a glance | Deficit equalled the day count exactly |

Defects 3 and 4 are the instructive pair. **#3 passed by comparing empty to empty**, so
"0 discrepancies" read as success. **#4 left the summary totals correct while three underlying
values were wrong**, because a ticket that flipped Breached → Within SLA still disagreed with
its stated status. *Never validate on summary counts — check every row.*

Build fixtures for Operators 6 and 7 **before** wiring them.

---

### The five defects in detail — observed 3 Aug

The first generated Operator 5 matched the reference engine **to the minute on 7 of 8 tickets**
across four regions, two timezones, the Penang holiday and 24×7 cover. Business-hours
arithmetic, region-from-reporter resolution and VIP classification were all correct. Three
things still broke. Add these as explicit instructions:

**1. `as_of` must actually be used.** The run ignored it and used wall-clock now — every
elapsed value came out exactly 281 minutes above the reference, a constant offset across all
regions. Result: 8 Breached, 0 At risk, 0 Within SLA. Instruct explicitly:

> *"Evaluate every ticket against the `as_of` instant. When `as_of` is provided you MUST use it
> as 'now' for all elapsed and remaining calculations. Never substitute the current system
> time. Echo the effective `as_of` back in the output so the caller can confirm it was applied."*

**2. Dates are day-first.** `ITSM-2091` has `Created = 03/07/2026`. The run read it as
**7 March** instead of 3 July and returned 215,081 business minutes against a true 45,161 — a
118-day error on a single ticket. Instruct explicitly:

> *"`Created` is TEXT in three formats: ISO (`2026-07-08 00:00:00`), `Mon DD YYYY`
> (`Jul 14 2026`), and **DD/MM/YYYY, day first** (`03/07/2026` is 3 July 2026, NOT 7 March).
> Never interpret a slash date as month-first. The day component in this data reaches 26, which
> is impossible for a month. If a value matches none of these three, raise an error rather than
> guessing — a misparsed date silently corrupts every downstream number."*

**3. The stated-SLA column is not being fetched.** Fixes 1 and 2 landed on the second build —
all eight elapsed values then matched the reference exactly. But the run still reported
**0 discrepancies with an empty "Stated Status" column on every row**. It is not comparing
wrongly; it is reading nothing and calling empty-vs-empty a match.

**The cause is PostgREST select syntax, not a typo.** The column is literally
`customfield_10030 (Time to resolution)` — with a space and parentheses. PostgREST reads
`name (…)` as *embedded-resource* syntax, i.e. a join, so it looks for a foreign-key
relationship instead of a column. Tested against the live project:

| `select=` | result |
|---|---|
| `customfield_10030` | `400` — `column issues.customfield_10030 does not exist` |
| `customfield_10030 (Time to resolution)` | `400 PGRST200` — *"Searched for a foreign key relationship between 'issues' and 'customfield_10030'"* |
| `*` | ✅ returns the column |
| `"customfield_10030 (Time to resolution)"` (double-quoted) | ✅ returns the column |
| `stated_sla:"customfield_10030 (Time to resolution)"` (aliased) | ✅ **best** — clean key name |

Use the alias form. It sidesteps the quoting trap *and* avoids spaces and parentheses in the
JSON keys downstream. Verified response:

```json
[ { "Issue key": "ITSM-2000", "stated_sla": "Within SLA", "grp": "Network Ops" } ]
```

Instruct explicitly:

> *"When selecting columns whose names contain spaces or parentheses, alias them with a
> double-quoted source name. PostgREST parses an unquoted `name (…)` as an embedded resource
> and fails with PGRST200. Use exactly:*
>
> ```
> select=Issue key,stated_sla:"customfield_10030 (Time to resolution)",grp:"customfield_10101 (Assignment group)"
> ```
>
> *and read the values as `stated_sla` and `grp`. `select=*` is an acceptable fallback. Never
> request bare `customfield_10030` — that column does not exist.*
>
> *After computing `computed_sla_state`, compare it with `stated_sla` case-insensitively after
> trimming, and set `discrepancy = true` whenever they differ. If `stated_sla` is null or empty,
> set `stated_sla_missing = true` rather than reporting a match — an empty stated value is
> never a match. Expect a high discrepancy rate: the stated field is a static label written at
> intake and never recalculated. Do NOT suppress discrepancies or reconcile them by preferring
> the stated value."*

The same trap applies to `customfield_10101 (Assignment group)`, which Operators 1 and 2
already read — check their select statements too.

**Expected after all three fixes**, on the fixture inputs:
**6 Breached · 2 Within SLA · 0 At risk · 6 discrepancies.** Only ITSM-2000 and ITSM-2004
should match. Verify against [`round2-op5-test-fixture.md`](round2-op5-test-fixture.md).

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

> **Reference implementation.** The Command Center already contains a tested engine for all
> of this: [`app/services/sla.py`](../app/services/sla.py), with 35 tests in
> [`tests/test_sla.py`](../tests/test_sla.py). Mirror its behaviour — the parsing rules,
> the opening-hour clamp and the region join are already verified against the real data.

1. Test each standalone with the seeded cases:
   - Op5 — **use [`round2-op5-test-fixture.md`](round2-op5-test-fixture.md)**: 8 real tickets
     with known-correct expected output, including the ITSM-2036 / ITSM-2013 control pair
     (same created date, same VIP status, 4× different elapsed purely from the calendar)
   - Op6 — should discover `ITSM-2180` and `ITSM-2199` unaided
   - Op7 — `CHG-0001` (Pending CAB), `CHG-0005` (Rolled Back), `CHG-0011` (Low, no CAB)
2. Publish a stable version of each.
3. Add to the orchestrator as `subworkflow_call` steps.
4. **Write down each operator's frozen input/output contract** — the backend binds to it on Thursday.
