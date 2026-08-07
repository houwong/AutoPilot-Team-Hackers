# Ticket Queue Runbook

The queue is intentionally controlled by the Command Center. It never starts
an entire Supabase backlog in one Supervity call: each tick sends one explicit
`Target Issue Key` to the existing Orchestrator.

## Safe first run

1. Open **Processed Tickets** in the Command Center.
2. Click **Preview next 10**.
3. Check the exact issue keys, priority and source status.
4. Click **Confirm and start** only after reviewing the list.
5. The first ticket starts immediately; later tickets are started by the
   scheduler or **Process next now**.

The preview does not call Supervity or write to Supabase. Once confirmed, the
batch is frozen, so a later backlog refresh cannot silently replace its tickets.

## Scheduler setup (Windows)

Set a random `QUEUE_TICK_TOKEN` in the repository `.env`, then run PowerShell
from the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install_queue_scheduler.ps1 -IntervalMinutes 5
```

The scheduled task calls `POST /api/queue/tick` every five minutes. The API
accepts that request only with `X-Queue-Token`; the endpoint is idempotent and
does nothing while another ticket is running or a campaign is paused.

To test one tick without waiting:

```powershell
.\scripts\queue_tick.ps1
```

## Outcomes

The Processed Tickets page records `auto_remediated`, `awaiting_human`,
`human_approved`, `blocked`, `human_rejected`, `failed`, and
`completed_unknown`. A human-review item remains linked to its original run;
approving or rejecting it in Workbench updates the same history entry.

Terminal tickets are excluded from future previews. Use **Requeue** with a
reason only when an administrator intentionally wants to process one again.

## Verification

Run these checks before a demo:

```powershell
docker compose exec backend pytest -q
docker compose exec backend python scripts/check_orchestrator.py
docker compose exec frontend npx tsc --noEmit
```

Do not confirm a live batch merely to test the UI. Use the preview endpoint or
mocked queue tests for non-destructive checks; every confirmed AgentRun can
modify the live Supabase ticket.
