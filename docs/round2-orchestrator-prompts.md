# Orchestrator build prompts — IT Ticker Orchestrator

Four prompts, in order. **Run one, verify, then run the next.** Do not batch them — the
orchestrator is the riskiest thing in the build and each prompt changes its shape.

Target end state: **one orchestrator coordinating 7 operators**, with parallel fan-out,
conditional branching, retries and a human loop.

```
                    ┌─ Op5  SLA & Business-Hours ─┐
   trigger ────────►┤                              ├──► Op1 Triage ──► Op2 Diagnose
                    └─ Op6  Major-Incident Detect ─┘      (batch)         (per ticket)
                                                                              │
                                                                              ▼
                                                                 Op3  Remediation ANALYSIS
                                                                              │
                                                                              ▼
                                                                 Op7  CAB / Change gate
                                                        ┌─────────┬───────────┴──────────┐
                                                     allow     escalate                block
                                                        │          │                     │
                                                Op3 execute   Workbench            Op4 notify
                                                        │      (human form / Run B)
                                                        ▼
                                                  Op4 notify
```

## Before you start

**Publish a stable version of all 8 workflows.** Everything is Ready and nothing is published.
Each prompt below is a structural edit; without a published version there is nothing to revert
to. Operator 6 already broke once after passing.

Operator workflow IDs, for reference:

| operator | id |
|---|---|
| Op1 Backlog Sweep & Triage | `019f7441-4a4b-7000-b133-e05f2baea0c7` |
| Op2 Diagnosis | `019f7486-e47c-7000-afa3-129bce27f4cd` |
| Op3 Safe Remediation | `019f7401-8d0e-7000-ba92-2325d75fe3fd` |
| Op4 Requester Notification | `019f7432-3c5b-7000-b62a-9ba69d7bcd1e` |
| Op5 / Op6 / Op7 | see the Auto UI |

---

## Prompt A — rewire the four inline steps

Cheapest gain in the build. Takes the orchestrator from **3 wired operators to 4**.

```
Four steps in this orchestrator re-implement work that existing operator agents
already do. Convert each to a delegation. Change nothing else — steps 1, 2, 3
and the human review form are correct and must keep working.

step_5_exec  (currently ~6,700 chars of inline code)
  Trigger the "Operator 3: Safe Remediation Operator" agent. Map the approved
  remediation from `rev_result` and `ticket_data` into its inputs, preserving
  issue_key and row_id exactly. Save its output to `exec_result`.
  Do NOT update Supabase from this step — the operator does that.

step_6_notif_auto
  Trigger the "Operator 4: Requester Notification" agent with outcome
  AUTO_RESOLVED. Map issue_key, requester_email, action_taken, workaround,
  priority and verification_status from `rem_result` and `ticket_data`.
  Do NOT send any email or Slack message from this step.

step_6_notif_manual
  Trigger the "Operator 4: Requester Notification" agent with the outcome from
  `exec_result`. Map issue_key, requester_email, action_taken, workaround,
  priority, escalation_reason and verification_status from `exec_result`,
  `rev_result` and `ticket_data`.
  Do NOT send any email or Slack message from this step.

step_6_notif_rejected
  Trigger the "Operator 4: Requester Notification" agent with the reviewer's
  decision from `rev_result`. Include the reviewer's reasoning as
  escalation_reason.
  Do NOT send any email or Slack message from this step.

Each of these four steps must become a subworkflow call with a workflow_id,
exactly like step_1_sweep, step_2_diag and step_3_rem. None may contain code
that talks to Outlook, Slack or Supabase directly.
```

**Verify:** the orchestrator must now reference **four** distinct operator workflow ids
including `019f7432` (Op4). It currently references three and never mentions Op4.
Run one ticket end to end and confirm the requester still receives mail and Slack still posts.

---

## Prompt B — queue mode

The orchestrator currently processes exactly one ticket: Op1's output mapping takes
`prioritized_tickets[0]` and discards the rest. **Operator 6 cannot detect a flood in a queue of
one.** This is the prerequisite for everything Op6 does.

```
Convert this orchestrator from single-ticket to queue processing.

PROBLEM
Step 1's output mapping selects only prioritized_tickets[0] and discards the
rest, so the run handles one ticket. Round 2 requires running the queue, and the
major-incident detector cannot correlate a flood from a single ticket.

ADD AN INPUT
  max_tickets_per_run (number, required, default 5)
    How many tickets from the top of the prioritized queue to action in one run.

CHANGE STEP 1's OUTPUT MAPPING
Keep the FULL prioritized_tickets array as `queue`. Do not reduce it to one
element. Then take the first max_tickets_per_run entries as `batch`, the tickets
this run will action. Raise NO_TICKETS_AVAILABLE only when `queue` is empty.

PROCESS THE BATCH
Steps 2 through 6 run per ticket in `batch`. For each ticket, carry issue_key and
row_id through every step and keep the existing DATA_INTEGRITY_ERROR check. A
failure on one ticket must not abort the others — record the failure against that
ticket and continue.

Collect per-ticket outcomes into `run_summary` with counts of auto_resolved,
escalated, blocked and failed.

RETRIES
Retry a failed operator call once before recording the failure.

Keep the existing branch structure per ticket: auto-resolved goes straight to
notification, needs-review goes to the human form, approved goes to execute then
notify, rejected goes to notify.
```

**Verify:** run with `max_tickets_per_run = 3` and confirm three tickets are actioned in one run
and `run_summary` totals to 3. Then set it to 1 and confirm the old behaviour still works.

---

## Prompt C — add Operator 5 and Operator 6 at the front

Adds the parallel fan-out the architecture score looks for, and gives Op1 real SLA states.

```
Add two operator agents to the front of this orchestrator, running in parallel.

NEW STEP: step_0_sla — trigger the "Operator 5 — SLA & Business-Hours Engine"
agent. Inputs: pass through the orchestrator's sla_targets,
at_risk_window_minutes, default_region and as_of. Leave its issue_keys input
empty so it evaluates the whole backlog. Save its output to `sla_states`.

NEW STEP: step_0_incidents — trigger the "Operator 6 — Major-Incident Detector"
agent. Inputs: pass through flood_threshold_count, flood_window_minutes,
correlation_confidence_threshold, include_relationship_types,
recurring_error_min_count and as_of. Save its output to `incident_clusters`.

These two steps have NO dependency on each other. Both start at the beginning of
the run and execute IN PARALLEL. Set start_at to both of them. step_1_sweep
depends on BOTH and runs after they have finished — a fan-out followed by a
fan-in.

ADD THESE ORCHESTRATOR INPUTS so they can be passed down:
  sla_targets (textarea)                    default: VIP: 4h response / 24h resolution; Non-VIP: 8h response / 48h resolution
  at_risk_window_minutes (number)           default: 120
  default_region (text)                     default: Global
  as_of (text, optional)                    demo value: 2026-07-25T00:00:00Z
  flood_threshold_count (number)            default: 5
  flood_window_minutes (number)             default: 120
  correlation_confidence_threshold (number) default: 0.7
  include_relationship_types (textarea)     default: is caused by, relates to
  recurring_error_min_count (number)        default: 20

USE THE RESULTS
step_1_sweep: pass `sla_states` into Operator 1 so triage ranks on the computed
business-hours SLA rather than the stale customfield_10030 label. Where a ticket
appears in `sla_states`, its computed_sla_state takes precedence.

step_2_diag onward: for each ticket in `batch`, attach its cluster from
`incident_clusters` if it is a member. A ticket belonging to a cluster whose
recommended_action is declare_major_incident must be handled as part of that
incident, not as an isolated ticket — set major_incident_key on it and include
the parent and sibling count in the context passed downstream.

Do not modify Operator 5 or Operator 6. They are verified against fixtures and
must be called unchanged.
```

**Verify:** `step_0_sla` and `step_0_incidents` both appear in `start_at` with empty
`depends_on`, and `step_1_sweep` depends on both. The run should report `ITSM-2180` with 23
members and `ITSM-2199` with 7.

---

## Prompt D — add Operator 7 as the change gate

Puts the CAB decision between "technically safe" and "actually executed", which is where the
Workbench path is born.

```
Add the change-approval gate between remediation analysis and execution.

NEW STEP: step_4_gate — trigger the "Operator 7 — Change / CAB Approval Gate"
agent, for each ticket in `batch` whose remediation analysis proposes an action.

It runs AFTER step_3_rem and BEFORE any execution or human review.
Inputs: issue_key from the ticket, plus the orchestrator inputs
blocking_statuses, escalating_statuses, require_cab_for_risk,
auto_approve_risk_levels and require_change_record_for_production.
Save its output to `gate_result`.

ADD THESE ORCHESTRATOR INPUTS:
  blocking_statuses (textarea)                    default: Rejected, Rolled Back
  escalating_statuses (textarea)                  default: Pending CAB Approval
  require_cab_for_risk (textarea)                 default: High, Medium
  auto_approve_risk_levels (textarea)             default: Low
  require_change_record_for_production (checkbox) default: true

BRANCH ON gate_result.decision — three ways:

  allow    -> continue to the existing flow. If step_3_rem judged the fix safe,
              proceed to step_5_exec. Otherwise proceed to human review as before.

  escalate -> route to human review (step_4_rev). The review item MUST carry
              gate_result.reason, change_id, risk, status, approver,
              policy_conflict and prior_rollback so the reviewer can see WHY it
              needs approval. Never execute on this branch.

  block    -> do not execute and do not ask for approval. Go straight to
              notification with outcome BLOCKED, including gate_result.reason.
              If prior_rollback is true, state that a previous change was rolled
              back. If terminal is true, state the change was rejected.

Operator 3 decides whether a fix is TECHNICALLY SAFE. Operator 7 decides whether
it is PERMITTED. Both must pass before anything executes. An allow from Operator
7 never overrides an unsafe verdict from Operator 3.

Do not modify Operator 7. It is verified against a fixture and must be called
unchanged.
```

**Verify:** run `ITSM-2180` — it must reach the gate and **escalate** (Pending CAB Approval),
landing in human review with `change_id = CHG-0001` visible. Run `ITSM-2214` — it must **block**
with `prior_rollback` and never reach execution. Run `ITSM-2303` — it must **allow**.

---

## Final check — the whole point of all four prompts

After D, confirm from the orchestrator definition:

```
subworkflow_call workflow_ids referenced: 7 distinct operators
start_at:            step_0_sla AND step_0_incidents   (parallel)
step_1_sweep:        depends_on both                    (fan-in)
step_4_gate:         three-way conditional branch
inline code steps:   none that talk to Supabase, Outlook or Slack directly
```

Then run the demo spine end to end:

```
Op5 + Op6 in parallel -> Op6 detects INC-9001 (23 tickets, 4 groups)
  -> Op1 triages on true business-hours SLA
    -> Op2 diagnoses ITSM-2180
      -> Op3 proposes a fix
        -> Op7 gates it: escalate, awaiting CAB (CHG-0001)
          -> human review with full incident context
            -> approve -> Op3 executes -> Op4 notifies
```

One trigger touching all seven operators, parallel execution, a conditional gate and a human
loop. That is the run to rehearse for Sunday.
