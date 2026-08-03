# Operator 6 test fixture — Major-Incident Detector

Expected values computed from the live Supabase data on 3 Aug 2026.
If Operator 6 disagrees with this document, the operator is wrong.

> ## ✅ PASSED — 3 Aug 2026, build 3
> 2 clusters (`ITSM-2180` 23 members, `ITSM-2199` 7 members, 0 VIPs each) · 2
> `declare_major_incident`, both `source=linked` · **0 inferred major incidents** ·
> 3 duplicates · 11 recurring known errors.
>
> **What made the difference:** builds 1 and 2 let the LLM do the counting and returned a
> cluster of 11 that matched no filter in the data. Build 3 split the step — a deterministic
> pandas cell computes clusters, duplicates and recurring errors with assertions, and the LLM
> only proposes emergent clusters and writes rationales. Op5 took five builds with arithmetic
> inside a model step; Op6 took three.
>
> **Rule for Operator 7: LLM steps for judgement, code cells for counting.** Anything a judge
> could check — member counts, blast radius, SLA minutes, discrepancy totals — belongs in code
> with assertions.
>
> **Emergent counts now verified too.** Build 3 reported 10 for `VPN drops after update`; the
> final build reports **17**, the true unlinked count. Emergent cluster sizes are safe to use in
> Insights.
>
> **Regression seen between builds 3 and 5 — worth knowing about.** An edit to the fetch step
> broke relationship-type filtering so that only `relates to` matched. ITSM-2180 collapsed from
> 23 members to 4 (its `relates to` count) and ITSM-2199 vanished entirely (it has only
> `is caused by` links). Duplicates and recurring errors kept reporting 3 and 11 throughout, so
> the summary still looked healthy. Cause: `"is caused by, relates to"` splits to
> `["is caused by", " relates to"]` and the leading space broke the comparison. Always
> `.strip().lower()` both sides, and assert the member counts.

---

## ⚠️ Read first: time-window clustering does not work on this data

**387 of 460 `Created` values are exactly `00:00`** — the column is date-only for most tickets.
Only 73 carry a real time-of-day, and 19 of those are incident members.

A "N tickets within W minutes" rule therefore treats every routine ticket raised on the same
date as simultaneous. Measured minimum window to gather 5 tickets sharing a summary:

| summary | tickets | in `incident_problem_links` | min window for 5 |
|---|---:|---:|---:|
| Shared drive access | 44 | 0 | **0 min** |
| Mailbox full | 40 | 0 | **0 min** |
| Guest wifi issue | 40 | 0 | **0 min** |
| Software install request | 40 | 0 | **0 min** |
| Laptop running slow | 38 | 0 | **0 min** |
| Keyboard replacement | 34 | 0 | **0 min** |
| Monitor flickering | 28 | 0 | **0 min** |
| VPN drops after update | 17 | 0 | **0 min** |
| **SSO loop on dashboard login** | 7 | **7 / 7** | 24 min |
| **Cannot access payroll portal** | 7 | **7 / 7** | 563 min |
| Printer offline | 43 | 0 | 990 min |

The two genuine incidents score **worse** than the noise. A flood window of 20 minutes would
declare eight false major incidents and rank the real ones last.

**Consequences for the design:**

1. **Explicit linkage is the primary path, and it is complete.** Both real clusters are fully
   represented in `incident_problem_links` *and* in `issues.linked_incident`. Nothing has to be
   inferred to pass.
2. **Never cluster on time alone.** Only consider a time window when the tickets involved carry
   a non-midnight `Created`. Skip the window test entirely for date-only tickets.
3. **A high-volume recurring summary is a known error, not an incident.** 44 "Shared drive
   access" tickets over 23 days is the seeded *recurring known-error across users* case. It must
   be reported as `recurring_known_error` with an action path of "author a KB article / create a
   policy", **not** `declare_major_incident`.

---

## Workflow inputs

```
workflowId = <Operator 6 workflow id>
inputs[flood_threshold_count]          = 5
inputs[flood_window_minutes]           = 120
inputs[correlation_confidence_threshold] = 0.7
inputs[include_relationship_types]     = is caused by, relates to
inputs[as_of]                          = 2026-07-25T00:00:00Z
```

`as_of` is after both incidents (20 and 22 July) so the full clusters are in scope.

---

## Expected output

### Cluster 1 — `ITSM-2180`

| field | value |
|---|---|
| `parent_issue_key` | `ITSM-2180` |
| parent summary | `MAJOR INCIDENT: Payroll portal outage` |
| `linked_incident_label` | `INC-9001` |
| `child_issue_keys` | 22 — `ITSM-2181`…`ITSM-2198` (18) + `ITSM-2223`…`ITSM-2226` (4) |
| member count | **23** |
| relationships | `is caused by` × 18, `relates to` × 4 |
| distinct reporters | 21 |
| VIP count | **0** |
| assignment groups | App Support, Field Support, Network Ops, Service Desk (all four) |
| components | `Network` × 23 |
| first_seen / last_seen | 2026-07-20 00:00 → 2026-07-20 09:27 |
| span | 567 min |
| `linkage_conflict` | **false** |
| `recommended_action` | `declare_major_incident` |

**This cluster also contains the multilingual trap.** Four members describe the same outage in
different languages and must not be treated as unrelated:
`No puedo acceder al portal de nomina` (ES) · `无法访问薪资门户` (ZH) ·
`Impossible d'acceder au portail de paie` (FR) · plus the English variants
(`Cannot access payroll portal` ×7, `Payroll portal down` ×4,
`Timesheet submission failing` ×4, `Cannot submit timesheet` ×3, `Payroll site not loading` ×1).

### Cluster 2 — `ITSM-2199`

| field | value |
|---|---|
| `parent_issue_key` | `ITSM-2199` |
| parent summary | `SSO loop on dashboard login` |
| `linked_incident_label` | `INC-9002` |
| `child_issue_keys` | 6 — `ITSM-2200`…`ITSM-2205` |
| member count | **7** |
| relationships | `is caused by` × 6 |
| distinct reporters | 7 |
| VIP count | **0** |
| assignment groups | App Support only |
| components | `Access` × 7 |
| first_seen / last_seen | 2026-07-22 10:00 → 2026-07-22 10:40 |
| span | 40 min |
| `linkage_conflict` | **false** |
| `recommended_action` | `declare_major_incident` |

### Reconciliation — expect zero conflicts

The two linkage mechanisms agree exactly:

| | via `incident_problem_links` | via `issues.linked_incident` |
|---|---|---|
| ITSM-2180 cluster | 23 members | INC-9001 → 23 tickets |
| ITSM-2199 cluster | 7 members | INC-9002 → 7 tickets |

`linked_only` and `label_only` are both empty for both clusters. **Expected
`linkage_conflict_count = 0`.** Keep the check — it costs nothing and guards future data — but
a run reporting conflicts here has a bug.

### Duplicates — exactly 3 pairs

| close | keep | reporter | summary |
|---|---|---|---|
| `ITSM-2218` | `ITSM-2217` | Mei Lee | Password reset needed |
| `ITSM-2220` | `ITSM-2219` | Wan Teo | Password reset needed |
| `ITSM-2222` | `ITSM-2221` | James Teo | MFA device lost |

Same reporter, identical summary in each pair. These must appear under `duplicates`, never as a
major incident.

### Negative cases — must NOT be declared incidents

**Exactly 11 groups** at `recurring_error_min_count = 20`. Counts are *unlinked* tickets only
(members of `incident_problem_links` are excluded), grouped by exact `Summary`, span > 3 days.

| summary | unlinked tickets | span (days) |
|---|---:|---:|
| Shared drive access | 44 | 23 |
| Printer offline | 43 | 23 |
| Mailbox full | 40 | 23 |
| Guest wifi issue | 40 | 23 |
| Software install request | 40 | 22 |
| Laptop running slow | 38 | 23 |
| Keyboard replacement | 34 | 23 |
| Password reset needed | 33 | 22 |
| MFA device lost | 31 | 23 |
| Monitor flickering | 28 | 22 |
| Network issue | 22 | 21 |

`VPN drops after update` (17) falls below the threshold — it should appear as an emergent
cluster with action `monitor`, not as a recurring known error.

Sensitivity, in case the threshold is tuned: `>=20 → 11` · `>=25 → 10` · `>=30 → 9` · `>=34 → 7`.

> **Correction (3 Aug).** An earlier version of this file listed 8 groups. That was wrong — it
> was built from a truncated listing and omitted `Password reset needed`, `MFA device lost` and
> `Network issue`. The correct count at threshold 20 is **11**.
>
> Note that `Password reset needed` and `MFA device lost` each contain a few tickets that *are*
> in `incident_problem_links` (as `duplicates` pairs). Those tickets are excluded from the
> counts above, but the summary group still qualifies on its remaining unlinked tickets — do
> not drop an entire summary group merely because some of its tickets are linked.

**A run that declares any of these a major incident has failed**, regardless of how well it
handles the two real clusters. This is the primary thing this fixture tests.

---

## Summary totals

```
clusters                 : 2
  declare_major_incident : 2   (both source='linked')
members                  : 23 + 7
linkage_conflict_count   : 0
duplicates               : 3
recurring_known_errors   : 11  (at recurring_error_min_count = 20)
inferred major incidents : 0   <-- the one that matters
```

---

## Fastest check

1. Does it find `ITSM-2180` **and** `ITSM-2199` without being told they exist?
2. Does it declare **exactly 2** major incidents — not 10?
3. Are the 4 non-English payroll tickets inside cluster 1 rather than orphaned?

If (2) fails, emergent detection is clustering on date-only timestamps. That is the single most
likely failure mode for this operator on this data.
