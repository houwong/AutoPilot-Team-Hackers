# Continue Queue After Human Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow later pending tickets in a confirmed Queue campaign to continue while earlier tickets remain parked in Human Review, without allowing the same ticket to run twice.

**Architecture:** Command Center remains the Queue owner and Supervity continues to receive one explicit `Target Issue Key` per run. A `running` Queue item blocks creation of another new Queue run, while an `awaiting_human` item remains linked to its Workbench decision but no longer blocks a different pending item. Existing same-issue active-run and unresolved-exception guards remain unchanged.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, pytest, Next.js documentation UI, Windows Task Scheduler.

## Global Constraints

- Do not modify the seven Supervity Operators or the Orchestrator.
- Do not change the existing scheduler interval of five minutes.
- Never start a second run for the same `issue_key` while it has an active run or unresolved Workbench exception.
- Start at most one new Queue analysis run per tick.
- Preserve every parked Human Review and its original `AgentRun`, `ExceptionItem`, and `QueueItem` linkage.
- A campaign remains `running` until all `pending`, `running`, and `awaiting_human` items become terminal.
- Do not change the existing frontend layout or visual design.
- Unit and structural tests must not call Supervity or modify Supabase.
- Any final live tick requires explicit approval because it starts a real Supervity run.

---

## File Structure

- Modify `app/routers/queue.py`: decide whether a campaign has a currently executing item before a tick claims the next ticket.
- Modify `tests/test_queue.py`: prove a parked Human Review does not block a different pending ticket and a real running item still does.
- Modify `docs/ticket-queue-runbook.md`: document the new continuation rule and its safety boundary.
- Modify `docs/system-flow.html`: update the bilingual Human Review explanation without changing the page layout.

### Task 1: Queue continuation behavior

**Files:**
- Modify: `app/routers/queue.py:49-88`
- Test: `tests/test_queue.py`

**Interfaces:**
- Consumes: `_active_run_item(db: Session, campaign_id: int) -> QueueItem | None`, `_tick_campaign(db, background, campaign)`.
- Produces: `_active_run_item` returns only an item in `QueueItemState.RUNNING`; an `AWAITING_HUMAN` item is parked but is not a campaign-wide execution lock.

- [x] **Step 1: Add router imports and a failing parked-review test**

Add this import to `tests/test_queue.py`:

```python
from app.routers import queue as queue_router
```

Add this test:

```python
def test_parked_human_review_does_not_block_next_queue_tick(db):
    campaign = QueueCampaign(
        name="continue-after-review",
        source="manual",
        status=QueueCampaignStatus.RUNNING.value,
        batch_limit=2,
    )
    db.add(campaign)
    db.flush()
    db.add_all(
        [
            QueueItem(
                campaign_id=campaign.id,
                issue_key="ITSM-REVIEW",
                state=QueueItemState.AWAITING_HUMAN.value,
                attempt_count=1,
            ),
            QueueItem(
                campaign_id=campaign.id,
                issue_key="ITSM-NEXT",
                state=QueueItemState.PENDING.value,
                attempt_count=0,
            ),
        ]
    )
    db.commit()

    assert queue_router._active_run_item(db, campaign.id) is None
```

- [x] **Step 2: Run the new test and verify the current behavior fails**

Run:

```powershell
docker compose exec -T backend pytest -q tests/test_queue.py::test_parked_human_review_does_not_block_next_queue_tick
```

Expected: FAIL because `_active_run_item` currently returns the `awaiting_human` item.

- [x] **Step 3: Add a running-item regression test**

```python
def test_running_item_still_blocks_next_queue_tick(db):
    campaign = QueueCampaign(
        name="one-active-run",
        source="manual",
        status=QueueCampaignStatus.RUNNING.value,
        batch_limit=2,
    )
    db.add(campaign)
    db.flush()
    running = QueueItem(
        campaign_id=campaign.id,
        issue_key="ITSM-RUNNING",
        state=QueueItemState.RUNNING.value,
        attempt_count=1,
    )
    db.add(running)
    db.commit()

    assert queue_router._active_run_item(db, campaign.id).id == running.id
```

- [x] **Step 4: Implement the minimal active-item rule**

Replace `_active_run_item` with:

```python
def _active_run_item(db: Session, campaign_id: int) -> QueueItem | None:
    # Human Review parks only its own ticket. A different pending ticket may
    # continue, while create_agent_run still prevents same-key duplicates.
    return (
        db.query(QueueItem)
        .filter(
            QueueItem.campaign_id == campaign_id,
            QueueItem.state == QueueItemState.RUNNING.value,
        )
        .order_by(QueueItem.id)
        .first()
    )
```

Do not change `synchronize_campaign`, `claim_next`, or `create_agent_run`. Their existing responsibilities remain:

```text
synchronize_campaign -> keeps parked reviews and terminal outcomes current
claim_next            -> claims only one pending row with a database lock
create_agent_run      -> blocks duplicate active runs for the same issue key
```

- [x] **Step 5: Run focused tests**

Run:

```powershell
docker compose exec -T backend pytest -q tests/test_queue.py tests/test_agent.py
```

Expected: all tests pass, including both new queue-lock tests and the existing duplicate-target 409 tests.

- [x] **Step 6: Commit the behavior change**

```powershell
git add app/routers/queue.py tests/test_queue.py
git commit -m "feat: continue queue past parked reviews"
```

### Task 2: Bilingual operational documentation

**Files:**
- Modify: `docs/ticket-queue-runbook.md`
- Modify: `docs/system-flow.html`

**Interfaces:**
- Consumes: the Queue rule implemented in Task 1.
- Produces: English and Chinese documentation that distinguishes same-ticket protection from campaign continuation.

- [x] **Step 1: Update the runbook behavior statement**

Add this paragraph under `## Outcomes` in `docs/ticket-queue-runbook.md`:

```markdown
A ticket in `awaiting_human` remains parked and cannot be started again, but it
does not block a different `pending` ticket in the same campaign. The scheduler
continues to start at most one new analysis run per tick. The campaign completes
only after every parked review and pending ticket reaches a terminal outcome.
```

- [x] **Step 2: Update the bilingual System Flow copy**

Change the `OPEN HUMAN REVIEW` card in `docs/system-flow.html` to communicate both rules:

```html
<b>未解决人工审核 / OPEN HUMAN REVIEW</b>
<p>
  同一个 ticket 不允许再次启动；它停在 Workbench 时，后续 pending ticket 可以继续运行。
  <br>
  <span style="color:var(--m)">
    The same ticket cannot start again; while it is parked in Workbench,
    later pending tickets may continue.
  </span>
</p>
```

Do not change the language toggle, colors, layout, flow nodes, or other copy.

- [x] **Step 3: Verify documentation content and formatting**

Run:

```powershell
rg -n "later pending tickets may continue|后续 pending ticket 可以继续运行" docs/system-flow.html docs/ticket-queue-runbook.md
git diff --check
```

Expected: both languages are present and `git diff --check` returns no errors.

- [x] **Step 4: Commit documentation**

```powershell
git add docs/ticket-queue-runbook.md docs/system-flow.html
git commit -m "docs: explain queue continuation during review"
```

### Task 3: Full verification and controlled activation

**Files:**
- Verify only; no additional source files.

**Interfaces:**
- Consumes: Task 1 Queue behavior and Task 2 documentation.
- Produces: regression evidence and a safe activation decision.

- [x] **Step 1: Run the complete automated regression suite**

```powershell
docker compose exec -T backend pytest -q
docker compose exec -T backend python scripts/check_orchestrator.py
docker compose exec -T frontend npx tsc --noEmit
python -m compileall -q app tests
git diff --check
```

Expected:

```text
Backend tests pass
Orchestrator structural checks remain 37 / 37
TypeScript exits 0
Python compile exits 0
No whitespace errors
```

- [x] **Step 2: Verify the live campaign without starting a ticket**

```powershell
Invoke-RestMethod "http://localhost:8001/api/queue/campaigns/active" | ConvertTo-Json -Depth 10
Invoke-RestMethod "http://localhost:8001/api/queue/items?search=2042" | ConvertTo-Json -Depth 10
Invoke-RestMethod "http://localhost:8001/api/agent/runs?status=running" | ConvertTo-Json -Depth 10
```

Expected before activation: ITSM-2042 remains `awaiting_human`; later campaign items remain `pending`; no same-key duplicate run exists.

- [ ] **Step 3: Activate only after explicit live-run approval**

Live activation remains intentionally unchecked: no live Queue tick has been
started in this implementation session.

Before recreating the backend container, temporarily disable the scheduler so deployment cannot race a tick:

```powershell
Disable-ScheduledTask -TaskName "AutoPilot Ticket Queue Tick"
docker compose up -d --build --force-recreate backend
Enable-ScheduledTask -TaskName "AutoPilot Ticket Queue Tick"
```

Do not call `/api/queue/tick` until the user explicitly approves starting the next real ticket.

- [ ] **Step 4: Perform one controlled live tick after approval**

```powershell
.\scripts\queue_tick.ps1
```

Expected:

```text
started = true
ITSM-2042 remains awaiting_human
the next pending issue key becomes running
no second ITSM-2042 run is created
```

- [ ] **Step 5: Final audit**

```powershell
git status --short --branch
git log -3 --oneline --decorate
```

Confirm that only the planned files changed and report the exact live issue key, run ID, Queue counts, and any Human Review items. Do not merge or push without separate authorization.

## Self-Review

- Spec coverage: parked reviews continue to exist, later tickets continue, same-ticket duplicates remain blocked, one new run is started per tick, campaign completion still waits for reviews, and documentation is bilingual.
- Placeholder scan: no placeholder markers or unspecified implementation steps remain.
- Type consistency: all state names use existing `QueueItemState` values; no database migration or API schema change is required.
- Scope boundary: no Operator, Orchestrator, Supabase schema, frontend layout, or scheduler interval change is included.
