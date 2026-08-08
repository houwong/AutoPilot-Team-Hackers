# Section 4.1 New Queue-Planning Operator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Do not begin implementation until explicitly authorized.

**Goal:** Create a new, deterministic queue-planning operator and a new read-only Queue Planner workflow without modifying or using the existing Operator 1 for planning.

**Architecture:** Existing Operator 5 and Operator 6 run in parallel. A genuinely new Operator 1 Queue Planning workflow consumes their evidence and ranks the active backlog. A new Queue Planner workflow owns that read-only graph. The existing Operator 1 and execution orchestrator remain unchanged.

**Tech Stack:** Supervity Auto workflow artifacts, deterministic Python code cells, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Next.js/TypeScript, pytest.

## Global Constraints

- Create a genuinely new operator with a new workflow ID.
- The new Queue Planner must reference the new operator, never the existing Operator 1.
- Do not modify the existing Operator 1 or execution orchestrator.
- Reuse existing Operator 5 and Operator 6 unchanged.
- Prefer deterministic Python code cells and direct JSON editing over AI-generated prompts.
- Do not use an LLM in the new planning operator.
- Create a new Git branch before changing code, artifacts, or workflows.
- Preserve `docs/queue-planning-and-state-consistency-notes.md` unless separately requested.
- Generate and validate import artifacts before importing or publishing workflows.
- Keep an explicit manual rollback mode; never fall back to legacy ranking automatically.

---

## Task 0: Branch and Plan Checkpoint

- [x] Create `feat/queue-planner-4-1` from merged HEAD `432798154b0c17037d7d04812164276ade5c9d8c`.
- [x] Replace the root `plan.md` with this approved plan.
- [ ] Commit only the plan before implementation code.
- [ ] Confirm the untracked planning notes remain unmodified and uncommitted.

## Task 1: Create a New Deterministic Planning Operator

Create `supervity/queue-planner/operator-1-queue-planning.import.json` with workflow name `Operator 1 — Queue Planning Triage`.

Inputs:

```text
sla_states_json
incident_clusters_json
priority_ranking_order
max_candidates
```

Requirements:

- Create a new workflow ID during import.
- Fetch active ticket fields from Supabase using deterministic Python/integration code.
- Parse Operator 5 and Operator 6 evidence using deterministic Python.
- Do not read `customfield_10030` for ranking.
- Do not call `call_ai_llm`.
- Do not delegate to the existing Operator 1.
- Filter exactly `Open`, `In Progress`, `Waiting for support`, and `Waiting for customer`.
- Require valid Operator 5 evidence for every active ticket.
- Normalize both current and legacy Operator 6 field shapes.

Ranking order:

1. Operator 5 SLA/VIP tier.
2. Actionable major incident within the same tier.
3. Incident ticket count descending.
4. Incident VIP count descending.
5. Highest/Critical, High, Medium, Low, unknown.
6. Oldest Updated timestamp.
7. Issue key.

Output strict JSON containing the full ranked active backlog, capped at 1,000, with SLA, incident, and tie-break evidence.

Add an offline validator that fails if the artifact references the old Operator 1, contains `call_ai_llm`, reads stored SLA for ranking, omits required output fields, or contains write/notification operations.

## Task 2: Create the New Read-Only Queue Planner

Create `supervity/queue-planner/queue-planner.import.json` with workflow name `Queue Planner — Read Only`.

```text
Existing Operator 5 ─┐
                     ├→ New Queue Planning Operator → End
Existing Operator 6 ─┘
```

Inputs:

```text
sla_targets
at_risk_window_minutes
default_region
as_of
flood_threshold_count
flood_window_minutes
correlation_confidence_threshold
include_relationship_types
recurring_error_min_count
priority_ranking_order
max_candidates
```

Requirements:

- Build mappings through direct code-cell editing.
- Operator 5 and Operator 6 start in parallel.
- The new planning operator waits for both.
- Parse Operator 5's final `tickets` and `effective_as_of` fields exactly.
- Parse Operator 6's final `clusters` field exactly.
- Raise an error for missing or malformed operator output.
- End immediately after ranking.
- Never invoke Operators 2, 3, 4, or 7.
- Never write Supabase or send Outlook/Slack messages.

Import sequence:

1. Import the new planning operator.
2. Record its assigned ID as `AUTO_WF_OP1_PLANNER`.
3. Generate the Queue Planner artifact with that exact ID.
4. Import it and record `AUTO_WF_QUEUE_PLANNER`.
5. Validate that neither ID equals the existing Operator 1 ID.

## Task 3: Add Dual-Path Backend Integration

Add an explicit mode:

```text
QUEUE_PLANNER_MODE=legacy
QUEUE_PLANNER_MODE=supervity_v2
```

Implement:

```python
async def legacy_ranked_backlog(db) -> PlannerResult: ...
async def v2_ranked_backlog(db) -> PlannerResult: ...
async def ranked_backlog(db) -> PlannerResult: ...
```

Rules:

- `legacy` retains the current implementation for manual rollback.
- `supervity_v2` calls only `AUTO_WF_QUEUE_PLANNER`.
- Never switch modes automatically.
- Reject invalid mode values explicitly.
- A v2 failure never invokes legacy mode.

Cache behavior:

```text
Fresh cache: 60 seconds
Failure cache: 600 seconds
Cache key: normalized policy fingerprint
```

If v2 fails, use a same-policy cache younger than 600 seconds and mark it stale; otherwise return HTTP 503 without cancelling or replacing an existing preview.

Environment additions:

```text
QUEUE_PLANNER_MODE=legacy
AUTO_WF_OP1_PLANNER=
AUTO_WF_QUEUE_PLANNER=
QUEUE_PLANNER_FRESH_TTL=60
QUEUE_PLANNER_STALE_TTL=600
```

## Task 4: Persist and Display Ranking Evidence

Add nullable, backward-compatible fields.

`queue_campaigns`:

```text
planner_mode
planner_run_id
planner_generated_at
planner_effective_as_of
planner_stale
planner_policy_snapshot JSON
```

`queue_items`:

```text
rank_position
ranking_evidence JSON
```

Keep existing `sla_status`, `vip`, `priority_rank`, `ranked_by`, and `ranking_reason` fields.

Supported `ranked_by` values:

```text
operator_1
source_priority
queue_planner
queue_planner_stale
```

Frontend behavior:

- Legacy mode retains its current explanation and warning.
- v2 shows Operator 5 SLA/VIP evidence.
- v2 shows incident key, action, and blast radius.
- Stale evidence displays an amber warning.
- HTTP 503 leaves the previous preview visible.
- Items are ordered by frozen `rank_position`.

## Task 5: Verification and Controlled Rollout

Automated tests must prove:

- the new operator ID differs from the old Operator 1 ID;
- the Queue Planner references only the new planning operator;
- no new artifact contains `call_ai_llm`;
- Operator 5 overrides conflicting stored SLA;
- incomplete Operator 5 evidence fails closed;
- major incidents move only within their SLA/VIP tier;
- ordering is deterministic through every tie-breaker;
- cache reuse requires the same policy fingerprint;
- expired cache returns 503 without creating a campaign;
- legacy and v2 modes both serialize correctly;
- mode switching is always manual.

Run:

```powershell
docker compose exec -T backend python -m pytest -q
docker compose exec -T frontend npm run typecheck
docker compose exec -T frontend npm run build
```

Read-only live verification:

1. Hash canonical Supabase snapshots before the run.
2. Execute the new Queue Planner once.
3. Confirm Operator 5, Operator 6, and the new planning operator are the only workflows invoked.
4. Confirm Supabase hashes remain identical.
5. Confirm no Outlook or Slack message was generated.
6. Create and discard one browser preview.
7. Confirm no ticket execution started.

Deployment:

1. Apply the additive migration.
2. Deploy with `QUEUE_PLANNER_MODE=legacy`.
3. Import and validate both new workflows.
4. Configure their new IDs.
5. Switch manually to `QUEUE_PLANNER_MODE=supervity_v2`.
6. Restart only the backend and run a preview smoke test.

Rollback:

1. Set `QUEUE_PLANNER_MODE=legacy`.
2. Restart only the backend.
3. Keep the additive database migration.
4. Leave the new workflows imported but unused.
5. Deploy the previous application revision only if the feature-flag rollback is insufficient.

Rollback must not require editing or restoring the existing Operator 1 or execution orchestrator.
