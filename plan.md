# Automatic Ticket Queue and Processed-Ticket History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two Command Center capabilities: safely process tickets through the existing Supervity Orchestrator as a controlled queue, and clearly show every ticket’s processing history and final outcome.

**Architecture:** PostgreSQL stores queue campaigns, queue items, run links, and outcomes. The backend selects explicit ticket keys from Supabase and starts exactly one existing Orchestrator run at a time. A five-minute scheduler tick advances the confirmed batch. The Command Center provides preview/confirmation, queue controls, processed-ticket history, and explicit requeue actions.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, PostgreSQL, Next.js/TypeScript, Supabase, Supervity Auto API, PowerShell/Windows Task Scheduler.

## Implementation status (2026-08-07)

- [x] Feature branch created: `feature/ticket-queue-history`.
- [x] Queue schema, backend queue service/API, AgentRun identity, Processed Tickets page, Dashboard summary, scheduler scripts, and runbook implemented.
- [x] Database migration applied locally; 47 backend tests, TypeScript validation, and 37/37 Orchestrator checks pass.
- [x] Manual target runs reject duplicate active targets and unresolved Workbench items; Workbench follow-ups remain allowed.
- [ ] A full multi-ticket batch is not exhausted automatically; it would modify real Supabase tickets and must be started only after reviewing the preview in the UI.

## Global Constraints

- Do not modify or rebuild the seven Supervity Operators or current Orchestrator.
- Every Orchestrator run must receive an explicit `Target Issue Key`.
- Never allow two active runs for the same queue campaign.
- Do not treat a successful API request as a successful remediation; classify outcomes from actual run steps.
- Tickets already completed, blocked, or rejected must not be selected again automatically.
- Reprocessing requires an explicit administrator Requeue action and a reason.
- Human-review tickets must remain parked until the existing Workbench process completes them.
- Supabase remains the source of truth for ticket content/status. PostgreSQL stores Command Center queue and audit state.
- Outlook quota failures must be displayed honestly but must not change the ticket-processing outcome.
- Preserve existing dirty/untracked files and unrelated user changes.

## 1. Database and Run Identity

### Queue campaign model

- [ ] Add an Alembic migration and SQLAlchemy model for `queue_campaigns`.
- [ ] Store `id`, `name`, `source` (`manual` or `schedule`), `status` (`preview`, `running`, `paused`, `completed`, `cancelled`), `batch_limit` (default `10`), `created_by`, `created_at`, `confirmed_at`, `started_at`, `completed_at`, and `last_tick_at`.
- [ ] Calculate counters from queue items instead of duplicating mutable counter columns.

### Queue item model

- [ ] Add `queue_items` with `id`, `campaign_id`, `issue_key`, Supabase snapshot fields (`source_status`, `source_priority`, `source_updated_at`), `state`, `outcome`, `latest_run_id`, `attempt_count`, `last_error`, `requeued_from_id`, `requeue_reason`, and lifecycle timestamps.
- [ ] Support these states: `preview`, `pending`, `running`, `awaiting_human`, `auto_remediated`, `human_approved`, `blocked`, `human_rejected`, `failed`, `skipped_closed`, `cancelled`, and `completed_unknown`.
- [ ] Add a unique constraint on `(campaign_id, issue_key)` and indexes on `issue_key`, `state`, `latest_run_id`, and timestamps.

### Agent-run linkage

- [ ] Extend `AgentRun` with `selected_issue_key` and `queue_item_id`.
- [ ] Keep the existing `issue_keys` JSON field for compatibility.
- [ ] For explicit-target runs, save `selected_issue_key` before execution.
- [ ] For legacy blank-target runs, extract the actual key from Operator 1’s final structured result and backfill `selected_issue_key` and `issue_keys`.
- [ ] Update Dashboard displays to use `selected_issue_key` before falling back to `issue_keys[0]`.

## 2. Queue Selection and Execution

### Preview eligibility

- [ ] Build a queue service that queries Supabase without modifying tickets.
- [ ] Include only active statuses: `Open`, `In Progress`, `Waiting for support`, and `Waiting for customer`.
- [ ] Exclude closed/resolved/cancelled tickets, tickets with active `pending`, `running`, or `awaiting_human` runs, tickets with unresolved Workbench items, and tickets previously ending in `auto_remediated`, `human_approved`, `blocked`, or `human_rejected`.
- [ ] Exclude failed tickets that exhausted retries until an administrator explicitly requeues them.
- [ ] Sort by priority (Highest → High → Medium → Low → unknown), then oldest `Updated` timestamp, then stable `row_id`/issue-key tie-breaker.
- [ ] Default preview size is 10 tickets.
- [ ] Preview must show the exact tickets that will be processed, with no writes to Supabase or Supervity.
- [ ] Confirmation converts the preview snapshot to `pending`; later Supabase changes must not silently replace tickets in that confirmed batch.

### Queue tick

- [ ] Implement a reusable queue-tick service.
- [ ] Synchronize existing `running` and `awaiting_human` items with AgentRun and Workbench state.
- [ ] Return a no-op when the campaign is paused, completed, or already has an active run.
- [ ] Transactionally claim one pending item using row locking such as `FOR UPDATE SKIP LOCKED`.
- [ ] Recheck the ticket’s current Supabase status; mark a now-closed ticket `skipped_closed`.
- [ ] Start the existing Orchestrator with the item’s explicit issue key, link the AgentRun and QueueItem, and leave other items pending.
- [ ] Start no more than one ticket per tick.

### Completion classification

- [ ] `step_6_notif_auto` completed → `auto_remediated`.
- [ ] `step_4_rev` waiting → `awaiting_human`.
- [ ] Approved review followed by `step_5_exec` and manual notification → `human_approved`.
- [ ] CAB gate decision `block` → `blocked`.
- [ ] Rejected human form → `human_rejected`.
- [ ] AgentRun failed/cancelled → `failed`.
- [ ] A run that succeeds without a recognized terminal path → `completed_unknown`, never an automated success.
- [ ] Record notification delivery separately from remediation outcome.
- [ ] Permit at most two attempts for transient technical failures; after that require explicit requeue.
- [ ] Mark a campaign `completed` when no pending or active items remain.

### Human-review lifecycle

- [ ] Mark a parked ticket `awaiting_human` and allow later tickets in the campaign to continue.
- [ ] Do not create another run for that ticket.
- [ ] Synchronize the resumed original run after Workbench submits the real Supervity form.
- [ ] Record the final result as `human_approved`, `human_rejected`, `failed`, or `completed_unknown`.

## 3. APIs and Scheduler

Add authenticated endpoints:

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/queue/campaigns/preview` | Generate a read-only preview, default limit 10 |
| `POST` | `/api/queue/campaigns/{id}/confirm` | Confirm and start the previewed batch |
| `GET` | `/api/queue/campaigns/active` | Return current campaign and counters |
| `GET` | `/api/queue/campaigns/{id}` | Return campaign details |
| `POST` | `/api/queue/campaigns/{id}/pause` | Stop new ticket starts |
| `POST` | `/api/queue/campaigns/{id}/resume` | Resume processing |
| `POST` | `/api/queue/campaigns/{id}/cancel` | Cancel only pending items |
| `POST` | `/api/queue/tick` | Synchronize state and start at most one ticket |
| `GET` | `/api/queue/items` | Paginated history with filters and search |
| `POST` | `/api/queue/items/{id}/requeue` | Explicitly create a replacement item |

- [ ] Require a non-empty reason for requeue and preserve the old queue record.
- [ ] Add `QUEUE_TICK_TOKEN` configuration for Windows Task Scheduler.
- [ ] Add a PowerShell tick script and installation/documentation for Task Scheduler.
- [ ] Configure the recommended interval as every five minutes.
- [ ] The scheduler only advances a confirmed running campaign; it never creates or confirms a batch automatically.
- [ ] Manual “Process next now” uses the same tick service.
- [ ] Make scheduler requests idempotent and safe when ticks overlap.

## 4. Command Center Interface

### Processed Tickets page

- [ ] Add a sidebar entry and `/processed` page.
- [ ] Add Preview next 10, Confirm and Start, Pause, Resume, Process next now, and Cancel controls.
- [ ] Display scheduler status and the five-minute interval.
- [ ] The confirmation screen must list all selected issue keys and warn that execution can modify live Supabase data.

### History table

- [ ] Display issue key, source priority/status, queue state, final outcome, run ID, Workbench/CAB involvement, remediation action, notification delivery, attempts, timestamps, and errors/warnings.
- [ ] Add outcome/state/campaign/date/issue-key filters and pagination.
- [ ] Link to run details and Workbench items where available.
- [ ] Make `completed_unknown` and notification failures visible warnings.
- [ ] Restrict Requeue to authorized users and require a confirmation plus reason.

### Dashboard summary

- [ ] Show active campaign progress, pending/running/awaiting-human counts, auto-remediated, human-approved, blocked/rejected, failed/unknown counts, and five recent completions.
- [ ] Link the summary to `/processed`.
- [ ] Replace misleading “whole queue” text with the actual selected ticket when recoverable.

## 5. Tests and Acceptance

### Backend tests

- [ ] Test preview eligibility, deterministic ordering, and zero side effects.
- [ ] Test confirmation snapshot, explicit target creation, and one-ticket-per-tick behavior.
- [ ] Test concurrent/repeated ticks cannot duplicate runs.
- [ ] Test paused/completed campaigns, closed-after-preview tickets, retries, and campaign completion.
- [ ] Test every outcome classification and the human-review lifecycle.
- [ ] Test global exclusion of terminal tickets and reason-required requeue.
- [ ] Test legacy blank-target issue-key backfill and migration upgrade/downgrade.
- [ ] Use mocked Supabase and Supervity responses; never modify live tickets in automated tests.

### Frontend tests

- [ ] Test preview → confirmation → running state.
- [ ] Test Pause, Resume, Process next, Cancel, filters, pagination, warnings, and Requeue confirmation.
- [ ] Run frontend type checking and production build.

### Existing-system regression

- [ ] Run the backend test suite.
- [ ] Run `docker compose exec backend python scripts/check_orchestrator.py`.
- [ ] Require all existing Orchestrator structural checks to remain green.
- [ ] Verify manual Dashboard runs and Workbench approvals still work.
- [ ] Confirm no Supervity workflow version or edge changed.

### Controlled end-to-end acceptance

- [ ] Preview exactly 10 safe test tickets.
- [ ] Confirm and invoke one tick; verify one explicit-target AgentRun.
- [ ] Let the five-minute scheduler advance the batch.
- [ ] Verify allow, escalate/human-review, and block outcomes.
- [ ] Complete one Workbench decision and verify the original paused run completes.
- [ ] Verify all outcomes appear on `/processed` and in the Dashboard summary.
- [ ] Start another preview and confirm terminal tickets are excluded.
- [ ] Requeue one ticket with a reason and confirm the old history remains visible.

## Completion Criteria

The feature is complete when a user can preview 10 tickets, confirm the live batch, process one ticket every five minutes through the current Orchestrator, pause/resume safely, handle human decisions through Workbench, and see an accurate permanent history without automatic duplicate processing.
