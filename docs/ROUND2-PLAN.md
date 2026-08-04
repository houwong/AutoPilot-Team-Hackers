# Round 2 Build Plan — Track 3: Service Desk Command Center

**Team Hackers** · Chee Hou (`houwong`) · Ve Song
**Outcome metric:** SLA compliance % (primary), MTTR (secondary)

> ## Status — end of Tue 4 Aug
> **The agent and the backend are done. The frontend is not started.**
>
> All four gate conditions are met and demonstrable through the API. What a judge will
> actually *look at* — the dashboard, Workbench, Policies and Insights pages — does not exist
> yet. That is roughly **35 rubric points** plus the "live dashboard" gate.
>
> Three days of remote build left: Wed, Thu, Fri. Then Sat at APU with a 23:59 freeze.

---

## 1. Where we are

### Completed work — checked off by day

**Mon 3 Aug — foundations**
- [x] Round 2 data pack loaded into Supabase: 10 tables, 885 rows, additive upsert
- [x] `row_id` continuity preserved and Round 1 rows back-filled with the new columns
- [x] Line endings pinned (`.gitattributes`) — the backend had been crash-looping for 2 hours
- [x] Stack verified: postgres, backend, frontend all healthy
- [x] Round 1 agent audited live: found Op4 never invoked, orchestrator single-ticket
- [x] Command Center models + Alembic migration `d4e5f6g7h8i9` for all 7 tables
- [x] `app/services/sla.py` business-hours engine + **35 passing tests**
- [x] Fixed `tests/` never being mounted, and `test_main.py` broken on httpx 0.28

**Tue 4 Aug — the agent**
- [x] **Operator 5** SLA & Business-Hours Engine — passed fixture on build 5
- [x] **Operator 6** Major-Incident Detector — passed fixture on build 3 (deterministic cell)
- [x] **Operator 7** Change / CAB Approval Gate — passed all 7 branches on build 3
- [x] Fixtures written for Op5, Op6, Op7 with known-correct expected values
- [x] **Prompt A** — rewired 4 inline steps; Op4 went from 0 references to 3
- [x] **Prompt B** — queue mode attempted and abandoned; reverted to v7
- [x] **Prompt C** — Op5 ∥ Op6 added as parallel fan-out, fan-in at triage
- [x] **Prompt D** — Op7 added as a three-way gate *before* remediation
- [x] Fixed the dead escalate branch (`depends_on` is an AND)
- [x] `target_issue_key` added for on-demand runs
- [x] Orchestrator now coordinates **7 operators**, v13

**Tue 4 Aug — the backend**
- [x] `auto_client.py` — multipart + SSE, verified against the live platform
- [x] Thin-wire spike proven end to end (`scripts/auto_smoke.py`)
- [x] `POST /api/agent/runs` + persistence of runs and operator steps
- [x] 21 policies seeded, driving every run, typed and overridable
- [x] Workbench: parking, exception creation, resolve → `change_requests` write → follow-up run
- [x] Fixed condition events overwriting operator output (9 rows → 6)
- [x] Fixed Op7 returning `change_id: null`, which was looping approvals
- [x] [architecture.md](architecture.md) written — doubles as the submission's diagram

**Gate conditions — all four met**
- [x] Solves the business problem end to end
- [x] Genuine human in the loop, and the decision completes the workflow
- [x] Connected to real systems (Supabase, Outlook, Slack)
- [x] Works live

---

### What that means in place

**The agent — 7 operators on Auto, orchestrated**

| | | |
|---|---|---|
| Op1 Backlog Sweep & Triage | `019f7441` | Round 1 |
| Op2 Diagnosis | `019f7486` | Round 1 |
| Op3 Safe Remediation | `019f7401` | Round 1 |
| Op4 Requester Notification | `019f7432` | Round 1 — **now actually invoked** |
| Op5 SLA & Business-Hours Engine | `019fc6bd` | new, fixture-verified |
| Op6 Major-Incident Detector | `019fc7cb` | new, fixture-verified |
| Op7 Change / CAB Approval Gate | `019fcac2` | new, fixture-verified |

Orchestrator v13: parallel fan-out (Op5 ∥ Op6), fan-in at triage, three-way CAB gate before
remediation, two-way branches at remediation and review, `target_issue_key` for on-demand runs.
No inline code touching Supabase, Outlook or Slack — every external call goes through an
operator. See [architecture.md](architecture.md).

**The data** — 885 rows across 10 Supabase tables, `row_id` continuity preserved, Round 1 rows
back-filled. See §5.

**The backend**
- `app/services/auto_client.py` — multipart + SSE, verified against the live platform
- `POST /api/agent/runs` → drives a run, persists `agent_runs` + `operator_executions`
- 21 policies seeded and passed as workflow inputs on every run
- Workbench: parks on human pause, builds the exception with gate + incident context,
  approval writes to `change_requests` and triggers the follow-up run
- `app/services/sla.py` + 35 passing tests
- Models and migration for all 7 Command Center tables

### Not started ❌

- **Frontend** — dashboard still shows the template's seeded numbers; no Policies, Workbench,
  Insights or Data Manager pages
- **AI Insights** — engine and page
- **Data Manager** — backend and page
- **Trap coverage**, clean-clone test, full demo run-through

### Outstanding chores

- [ ] **Publish stable versions of all 8 Auto workflows.** Everything is still `isDraft: true`.
      Op6 broke once after passing and there was nothing to revert to.
- [ ] **Add Ve Song as a collaborator** on `AutoPilot-Team-Hackers`
- [ ] Clear the `target_issue_key` default (currently `ITSM-2180`, so every run targets it)
- [ ] Op5/Op6 still declare a `SUPABASE_TOKEN` env alongside the native integration; Op7 and Op1
      use the integration alone. Tidy for Data Manager consistency.

---

## 2. The remaining schedule

### Wed 5 Aug — make it visible

The whole day is frontend. Nothing else moves the score as much.

- [ ] **Workbench page** — the queue, item detail with gate reasoning + incident blast radius,
      Approve / Modify / Reject. API is done and tested; this is the gate condition a judge
      needs to *see*.
- [ ] **Dashboard wired to live data** — backlog by SLA risk, MTTR, auto-resolution rate, open
      incidents, exception queue. Sources: `/api/agent/runs`, `/api/exceptions/stats/summary`.
      **Delete every seeded fixture on the way past.**
- [ ] **Policies page** — edit the 21 rows, grouped by operator (SLA / Incident / Change) rather
      than 21 flat fields. Must show that a change takes effect on the next run.

**Done when:** a judge can trigger a run, watch the dashboard move, see the exception arrive,
and clear it — without touching the API directly.

### Thu 6 Aug — Insights and Data Manager

- [ ] **Insights engine** over `agent_runs`, `operator_executions`, `policy_evaluations`,
      `exception_items` — recurring known-error clusters, major incident forming, KB gaps,
      SLA-breach forecast, uneven team load. Each with severity and a concrete action path.
- [ ] **Data Manager** — `/api/integrations` + page. Supabase, Outlook, Slack with live health.
      Not in the template; must be built.
- [ ] **AI Manager** — answers from real records, can re-trigger an operator. Lowest priority
      of the three; 4 points.

### Fri 7 Aug — traps and hardening

- [ ] The nine seeded traps, each demonstrated live (§6)
- [ ] **Clean-clone test on Windows** — the `.gitattributes` fix means it should work, but prove it
- [ ] **General-case test** — each of us runs 3 tickets the other never prepared
- [ ] Publish final stable versions of all 8 workflows
- [ ] Submission note: outcome metric, integrations, Auto workspace link

### Sat 8 Aug — APU, freeze 23:59

| Time | |
|---|---|
| 11:00–14:00 | Fix whatever Friday exposed |
| 14:00–16:00 | Bonus only if everything is green: self-learning from Workbench overrides, forecasting |
| 16:00–16:30 | Clean-clone verification |
| 16:30–17:00 | Office hours |
| 17:00–17:30 | Timed run-through |
| **17:30** | **Feature freeze.** Bug fixes and rehearsal only |
| ~23:00 | Final verification, then freeze |

### Sun 9 Aug — Grand Finale

Arrive 10:00–10:45, 15 min set-up, judging from 11:00. Slot is 10–12 min.

---

## 3. The demo — rehearse this exact run

```
1. (30s)  Dashboard — Arjun's morning. Backlog by SLA risk, open incidents, exception queue.
2. (60s)  POST a run for ITSM-2180 from the Command Center.
          Op5 ∥ Op6 fan out in parallel — watch the operator trace appear live.
3. (90s)  Op6 has found INC-9001: 23 tickets, 21 reporters, 4 assignment groups, one root
          cause. Including the same outage reported in Spanish, Chinese and French.
4. (90s)  Op7 gates it: ESCALATE, CHG-0001, Pending CAB Approval. Op3 never runs —
          nothing touched production.
5. (2m)   The Workbench item: gate reasoning, change record, incident blast radius, and the
          policy values in force. Approve it.
          CHG-0001 → Implemented. A follow-up run starts. The gate now allows.
6. (90s)  THE POLICY EDIT. Change require_change_record_for_production to true, re-run the
          same ticket, watch it block instead. Both evaluations in the log, each showing the
          threshold as it was at decision time.
7. (60s)  Insights: 11 recurring known errors — 44 "Shared drive access" tickets over 23 days
          is a KB gap, not an incident. Plus the SLA discrepancy finding.
8. (30s)  Data Manager — Supabase, Outlook, Slack, all green.
```

Presentation polish scores zero. Expect an unrehearsed ticket — `target_issue_key` handles it.

---

## 4. Rubric position

| Criterion | Pts | Status |
|---|---:|---|
| Business output | 30 | Agent solves it end to end. **Quantified metric still needed** — comes from Insights |
| Architecture on Auto | 20 | 7 operators ✅ · fan-out/branching ✅ · **Data Manager missing (5)** |
| Customizability & Policies | 20 | Gate-before-act ✅ · logging ✅ · **UI missing (7)** |
| AI Insights | 15 | **Not started** |
| Command Center & live demo | 15 | **Frontend not started** |
| Bonus | +10 | Self-learning from Workbench overrides is the obvious one |

**Gate conditions: all four met** — solves the problem end to end, genuine human in the loop,
real systems, works live.

---

## 5. Reference

### Auto API — verified, not from the docs

Host is **`auto-workflow-api.supervity.ai`**, not the documented `auto.supervity.ai`.

```
POST /api/v1/workflow-runs/execute/stream      multipart/form-data, fields inputs[<name>]
Authorization: Bearer <AUTO_API_KEY>
x-source: external          ← REQUIRED; omitting it returns a bare 401
x-active-org: Team Hackers
x-user-timezone: Asia/Kuala_Lumpur
```

No webhooks. A Workflow API key cannot read run history. **The SSE stream is the only chance to
capture a run** — a run triggered in Auto's UI is invisible to the Command Center.

Events nest under `content`. `activity-run` carries `stepId`, `status`, `attempt`; step *names*
arrive only on the terminal `result`. Branching steps emit one `activity-run` per condition
reusing the parent's `stepId` — filter on `conditionMet`. A delegating step's output is only a
link to the sub-workflow run; the operator's result must be fetched from there.

Rate limit 60 req/min per IP.

### Data layer

Supabase `stalled-ticket-resolver` (`pkwvlnvxmawvgqzqvkcg`), RLS disabled, 885 rows.

`issues` 460 · `ticket_comments` 277 · `assets_access` 173 · `users_directory` 97 ·
`csat_surveys` 87 · `knowledge_base` 38 · `incident_problem_links` 31 · `change_requests` 13 ·
`team_roster` 12 · `sla_calendar` 5

Four facts the operators depend on:
- `row_id` continuity preserved: 1 → `ITSM-2000`, 180 → `ITSM-2179`, 181 → `ITSM-2180`
- Round 1 rows were back-filled with the new columns, not appended past
- 460 tickets, not 462 — two byte-identical duplicate rows were deduped
- **`issues."Created"` is `text` in three formats**: ISO, `DD/MM/YYYY` day-first, `Jul 20 2026`
- Region comes from the **reporter** (`Reporter → display_name → location → region`), never
  from the assignment group — that mapping is one-to-many

PostgREST parses `name (…)` as a join. Alias awkward columns:
`select=stated_sla:"customfield_10030 (Time to resolution)"`

### The nine seeded traps

| trap | covered by |
|---|---|
| Stalled provisioning | Op2 |
| Recurring known error (VPN/SSO) | Op6 — 11 patterns |
| Mis-routed ticket | Op2 routing map |
| VIP after-hours near breach | Op5 — business hours |
| Major incident flood, INC-9001 | Op6 — 23 tickets |
| CAB approval required | Op7 — CHG-0001 |
| Failed remediation rolled back | Op7 — `prior_rollback` |
| Duplicate tickets | Op6 — 3 pairs |
| Same issue in EN/ES/ZH/FR | Op6 — inside the INC-9001 cluster |

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| **Frontend not started with 3 days left** | Wednesday is frontend only. Workbench first, then dashboard, then Policies |
| Insights not started; 15 points and a rubric line | Thursday. The data already exists in `agent_runs` and `policy_evaluations` — it is queries, not new plumbing |
| All 8 workflows still drafts | Publish tonight. This has been open since Monday |
| A run triggered in Auto's UI shows nothing | **Always trigger from the Command Center.** No webhooks |
| Orchestrator run takes ~100s to reach the gate | Op5 and Op6 scan the full backlog. Budget it in a 10-min slot |
| Demo data mutated by testing | Approving writes to `change_requests`. Restore `CHG-0001` to `Pending CAB Approval` after any approval test |
| A judge asks for an unprepared ticket | `target_issue_key` — but clear its default first |
| Silent defects | Every operator defect so far produced confident, plausible, wrong output. Fixtures are the only reason they were caught. **Never validate on summary counts** |

---

## 7. Sources

- [architecture.md](architecture.md) — how the agent works
- [round2-op5-test-fixture.md](round2-op5-test-fixture.md) · [op6](round2-op6-test-fixture.md) ·
  [op7](round2-op7-test-fixture.md) — known-correct expected values
- [round2-operator-prompts.md](round2-operator-prompts.md) — operator build prompts + failure log
- [round2-orchestrator-prompts.md](round2-orchestrator-prompts.md) — orchestrator prompts A–D
- Auto docs `auto.supervity.ai/docs` · keys `auto.supervity.ai/u/api-keys`

> ⚠️ [hackathon-brief.md](hackathon-brief.md) is generic template filler — wrong tracks, wrong
> rubric, no Data Manager. This plan supersedes it.
