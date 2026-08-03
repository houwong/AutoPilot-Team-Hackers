# Round 2 Build Plan — Track 3: Service Desk Command Center

**Team Hackers** · Chee Hou (`houwong`) · Ve Song
**Track:** 3 — Customer Support / Stalled-Ticket Resolver
**Outcome metric we commit to moving:** SLA compliance % (primary), MTTR (secondary)

| Phase | Date | Theme |
|---|---|---|
| 0 | Mon 3 Aug (today) | Baseline lock + Round 2 data |
| 1 | Tue 4 Aug | Rewire existing operators + vertical slice |
| 2 | Wed 5 Aug | Queue mode + policy engine + Workbench |
| 3 | Thu 6 Aug | CAB approval + Insights + Data Manager |
| 4 | Fri 7 Aug | Trap coverage + clean-clone hardening |
| 5 | Sat 8 Aug | Offline build at APU · **code freeze 23:59** |
| 6 | Sun 9 Aug | Grand Finale · 10–12 min live showcase |

---

## 1. Round 1 baseline — audited 3 Aug from the live Auto API

Five workflows exist under org `Team Hackers`. **Nothing here gets deleted or rebuilt. Every
item below is a rewire or an addition.**

| Workflow | ID | Ver |
|---|---|---|
| IT Ticker Orchestrator | `019f7943-03f8-7000-8313-d9ae873d1197` | 4 |
| Operator 1: Backlog Sweep & Triage | `019f7441-4a4b-7000-b133-e05f2baea0c7` | 2 |
| Operator 2: Diagnosis | `019f7486-e47c-7000-afa3-129bce27f4cd` | 5 |
| Operator 3: Safe Remediation | `019f7401-8d0e-7000-ba92-2325d75fe3fd` | 2 |
| Operator 4: Requester Notification | `019f7432-3c5b-7000-b62a-9ba69d7bcd1e` | 3 |

### What is already strong — protect this
- **Real decomposition.** Steps 1–3 use `subworkflow_call` against the operator IDs. Not a
  mega-agent; clears the gate.
- **Real branching.** Orchestrator forks auto-resolved / needs-review, then approved / rejected.
- **Real parallelism.** Op2 fans out to 4 concurrent Supabase fetches and fans back in to one
  LLM diagnose step. This is exactly what "orchestration depth" scores.
- **Human loop exists** via `human_input_form`.
- **Integration floor already met**: Supabase (system of record), Outlook (channel), Slack
  (channel + human loop), LLM.
- **Op3 verifies its own writes** — re-queries the row and compares expected vs observed.
- **Data-fidelity guards** — `DATA_INTEGRITY_ERROR` on issue_key/row_id mismatch, explicit
  anti-fabrication instructions.

### Gap 1 — four steps bypass the operators they name
| Orchestrator step | Mode today | Target state |
|---|---|---|
| `step_1_sweep` | ✅ subworkflow → Op1 | keep |
| `step_2_diag` | ✅ subworkflow → Op2 | keep |
| `step_3_rem` | ✅ subworkflow → Op3 | keep |
| `step_4_rev` | human form | becomes Command Center Workbench (see §2) |
| `step_5_exec` | ❌ inline, 6,729 chars | → `subworkflow_call` Op3 |
| `step_6_notif_auto` | ❌ inline | → `subworkflow_call` Op4 |
| `step_6_notif_manual` | ❌ inline, 10,350 chars | → `subworkflow_call` Op4 |
| `step_6_notif_rejected` | ❌ inline, 9,625 chars | → `subworkflow_call` Op4 |

Operator 4 is fully built and **never invoked** — the orchestrator re-implements notification
inline. So we have **3 wired operators, not 4**. Rewiring is config, not code, and it is the
cheapest scoring gain in the build. Op4 itself is not modified.

### Gap 2 — the orchestrator runs one ticket per execution
Its own description: *"fetches exactly one highest-priority active ticket"*, and Op1's output
mapping takes `prioritized_tickets[0]` and discards the rest.

**A one-ticket orchestrator cannot detect the INC-9001 flood**, which is the headline trap in
our dataset. Queue mode is a redesign of the orchestrator's entry path, not an add-on. Budget
real time for it (Phase 2).

### Gap 3 — all six new Round 2 tables are unused
In use: `issues`, `users_directory`, `knowledge_base`, `assets_access` (the Round 1 four).
Unused: `Ticket_Comments`, `CSAT_Surveys`, `Change_Requests`, `Incident_Problem_Links`,
`SLA_Calendar`, `Team_Roster`. Each maps to a seeded trap.

Related: Op1 is instructed *"Use customfield_10030 exactly… Do not recalculate SLA hours."*
Correct for Round 1; `SLA_Calendar` was added in Round 2 precisely to require business-hours
computation. See Operator 5.

### Gap 4 — no run history
`GET /workflow-runs` returns 0 rows. The dashboard has nothing to show until we start running
and persisting. Our own DB is the only history we will have.

### The asset we already have: policies as workflow inputs
The operators already accept `priority_ranking_order`, `sla_thresholds`,
`kb_confidence_threshold`, `stalled_days_threshold`, `routing_mapping_json` as inputs. **These
are our Round 2 policies.** The Command Center stores current values, a business user edits
them in the browser, the backend passes them on the next `execute` call — behaviour changes
with no code and no redeploy. That is the exact demo a judge asks for, and it means we are much
closer on the 20-point Policies criterion than the operator count suggests.

---

## 2. Two architecture decisions to make today

### 2.1 The human loop must move into our Workbench
Today the pause happens in Auto's `human_input_form`. The rubric requires the **Command Center
Workbench** to be where a real exception is cleared.

**Recommended: split the orchestrator into two runs.**
```
Run A  sweep → diagnose → remediation ANALYSIS → return outcome
         ↓ backend evaluates policy, creates exception_item
         ↓ human approves/modifies/rejects in OUR Workbench
Run B  execute (Op3) → notify (Op4)      ← triggered by backend on approval
```
Orchestration stays on Auto, the human queue lives in our Command Center, and no long-running
Auto wait or inbound tunnel is required. Keep `step_4_rev` in place as a fallback path.

### 2.2 Can Auto reach our backend?
If an Operator calls `POST /api/policies/evaluate` mid-flow, Auto's cloud must reach us —
`localhost:8001` will not work.

- **Option A:** `cloudflared` tunnel; operators call the policy endpoint directly. Strongest
  narrative — the gate sits inside the agent's execution.
- **Option B:** backend-mediated (the two-run split above). Policy is evaluated between runs,
  still strictly *before* the action executes. No tunnel.

**Go with B as the primary** — it composes with §2.1 and removes a demo-day dependency. Build
A only if time allows, as a bonus talking point.

---

## 3. Verified API contract

Base host is **`auto-workflow-api.supervity.ai`** — *not* `auto.supervity.ai` from the docs.

```
POST https://auto-workflow-api.supervity.ai/api/v1/workflow-runs/execute
POST https://auto-workflow-api.supervity.ai/api/v1/workflow-runs/execute/stream   ← SSE

Authorization:   Bearer <AUTO_API_KEY from .env>
x-source:        external            ← REQUIRED. Omit it and every call is 401
x-active-org:    Team Hackers
x-user-timezone: Asia/Kuala_Lumpur
```

**Body is `multipart/form-data`, not JSON:**
```
workflowId                      = 019f7441-4a4b-7000-b133-e05f2baea0c7
inputs[priority_ranking_order]  = <text>
inputs[sla_thresholds]          = <text>
```

Read endpoints that work on this host (verified): `GET /workflows`,
`GET /workflows/:id` (full step graph), `GET /workflows/:id/versions`, `GET /workflow-runs`.

SSE events: `activity-run`, `workflow-run`, `thinking`, `result`, `error`.
Rate limit: **60 req/min per IP** — batch the incident flood, don't fire per ticket.

### Our backend's own contract
```
POST /api/agent/runs              → opens SSE, persists agent_run
POST /api/policies/evaluate       → { decision: allow|deny|escalate, policy_id, reason, evaluation_id }
GET  /api/exceptions              → Workbench queue
POST /api/exceptions/{id}/resolve → records decision, triggers Run B
GET  /api/integrations            → Data Manager health
```

### Tables (write together, Day 0, then frozen)
`agent_run` · `operator_execution` · `policy` · `policy_evaluation` · `exception_item` ·
`insight` · `integration`. The template's `audit_logs` already exists — use
`app.services.audit` for the decision trail rather than inventing a second one.

---

## 4. Work split

**Ve Song — Auto & integrations.** Owns all workflow edits, Supabase loading, Outlook/Slack.
Repo files: `app/services/auto_client.py`, `app/routers/agent.py`, `app/routers/integrations.py`, `scripts/seed_round2.py`.

**Chee Hou — Command Center.** Policies, Insights, Workbench, Dashboard, Data Manager, all frontend.
Repo files: `app/routers/policies.py`, `app/routers/insights.py`, `app/routers/exceptions.py`, `app/services/sla.py`, `frontend/**`.

Branches `agent/*` and `cc/*`, merged to `main` daily. `main` must always start clean via
`.\scripts\start.ps1`. `.env` never committed.

---

## 5. Phases

### Phase 0 — Mon 3 Aug (remainder of today)
**Ve Song**
- [ ] **Publish a stable version of all 5 workflows** — everything is `isDraft: true`. This is
      the rollback point before any rewiring. Do this before touching anything.
- [ ] Load the 6 new tables into Supabase: `ticket_comments`, `csat_surveys`,
      `change_requests`, `incident_problem_links`, `sla_calendar`, `team_roster`
- [ ] Confirm `POST /workflow-runs/execute` works with **multipart/form-data** + the 4 headers

**Chee Hou**
- [ ] `app/models/` + one Alembic migration for the 7 tables (§3)
- [ ] Workflow IDs into `.env`
- [ ] `app/services/sla.py` — business-hours SLA from `sla_calendar` (timezone + holidays),
      with unit tests covering a holiday and an after-hours case

**Done when:** stable versions exist, Supabase has all 10 tables, SLA function passes tests.

---

### Phase 1 — Tue 4 Aug · Rewire + vertical slice
**Ve Song**
- [ ] Rewire `step_5_exec` → `subworkflow_call` Op3
- [ ] Rewire `step_6_notif_auto` / `_manual` / `_rejected` → `subworkflow_call` Op4
- [ ] **Operator 5 — SLA & Business-Hours Engine** (new): computes true SLA state from
      `sla_calendar` + timezone + holidays; replaces Op1's read of `customfield_10030`
- [ ] Confirm all 4 existing operators still pass end-to-end after rewiring

**Chee Hou**
- [ ] `auto_client.py` — multipart POST + SSE consumption
- [ ] `POST /api/agent/runs`; one `operator_execution` row per `activity-run` event
- [ ] Dashboard wired to live KPIs — **no seeded numbers left anywhere**

**Done when:** 5 operators genuinely invoked, one run visible end to end, dashboard moves.

---

### Phase 2 — Wed 5 Aug · Queue mode + policies + Workbench
**Ve Song**
- [ ] **Orchestrator queue mode** — process a batch, not `prioritized_tickets[0]`
- [ ] **Operator 6 — Major-Incident Detector**: clusters via `incident_problem_links`, opens
      parent INC-9001, drives comms until closed
- [ ] Implement the two-run split (§2.1): Run A ends at analysis, Run B executes on approval

**Chee Hou**
- [ ] Policy engine — evaluates before the action, every call logged to `policy_evaluation`
- [ ] Policies page: **no-code editing** of the five existing workflow inputs; saved values
      passed to Auto on the next run
- [ ] Workbench: queue, detail with correlated tickets + recommendation, Approve/Modify/Reject,
      resolution triggers Run B

**Ship these 3 policies minimum** (all map to existing workflow inputs):
1. *Auto-remediate only where `x_auto_safe` and confidence ≥ `kb_confidence_threshold`* — threshold editable
2. *VIP within N business-minutes of breach escalates to on-call* — both values editable
3. *Production changes require CAB approval when `cab_approval_required`* — toggleable

**Done when:** you lower `kb_confidence_threshold` from 0.85 to 0.50 in the browser, re-run the
same ticket, it auto-resolves instead of escalating, and both evaluations appear in the log.
**Rehearse this exact sequence — it is the most likely judge request in the room.**

---

### Phase 3 — Thu 6 Aug · CAB, Insights, Data Manager
**Ve Song**
- [ ] **Operator 7 — Change/CAB Approval**: gates production changes on
      `change_requests.cab_approval_required`; sits in front of execute
- [ ] Retries on operator failure; confirm parallel fan-out still holds under queue mode
- [ ] Integration health data flowing for Supabase / Outlook / Slack

**Chee Hou**
- [ ] Insights from real processed data: recurring known-error cluster (VPN/SSO), major incident
      forming, KB gap where no article exists, SLA-breach forecast, uneven load from `team_roster`
- [ ] Each insight carries severity + a concrete action path
- [ ] **Data Manager page — not in the template, build it.** System, category, purpose, health, last check

**Done when:** 7 operators, 3 integrations green, insights that change as more tickets run.

---

### Phase 4 — Fri 7 Aug · Traps + hardening
Tick each only once demonstrated live:
- [ ] Stalled provisioning ticket
- [ ] Recurring known-error across users (VPN/SSO)
- [ ] Mis-routed ticket bouncing between assignment groups
- [ ] VIP after-hours near SLA breach *(needs Operator 5)*
- [ ] Major incident flood, one root cause INC-9001 *(needs queue mode + Operator 6)*
- [ ] Change/CAB approval required before fix *(needs Operator 7)*
- [ ] Failed remediation rolled back
- [ ] Duplicate tickets, same user
- [ ] Same issue in EN/ES/ZH/FR

Then:
- [ ] **Clean-clone test** — fresh clone, `.env` from example, `.\scripts\start.ps1`, demo works
- [ ] **General-case test** — each of us runs 3 tickets the other never prepared
- [ ] Publish final stable versions of all 7 workflows
- [ ] Submission note: outcome metric + integrations; Auto workspace link ready

---

### Phase 5 — Sat 8 Aug · Offline build at APU
Check-in 10:00–10:45. **Code freeze 23:59.**

| Time | Plan |
|---|---|
| 11:00–13:00 | Fix whatever Friday exposed |
| 14:00–16:30 | Bonus only if everything above is green: self-learning from Workbench overrides, forecasting, rollback operator |
| 16:30–17:00 | Office hours — resolve anything ambiguous with organizers |
| 17:00–17:30 | Timed full run-through |
| 17:30–19:00 | **Feature freeze.** Bug fixes and rehearsal only |
| ~23:00 | Final clean-clone verification, then freeze |

---

### Phase 6 — Sun 9 Aug · Grand Finale
Arrive 10:00–10:45. 15 min set-up. Slot is 10–12 min: 8–10 demo, 2–3 Q&A.

1. *(30s)* Dashboard — Arjun's morning, backlog by SLA risk, live
2. *(90s)* Inbound ticket → Orchestrator fans out to 7 operators → resolved, with the trace
3. *(2m)* Major incident: flood of tickets, one root cause, INC-9001 opened and correlated
4. *(2m)* **The policy edit** — change a threshold on screen, re-run, different behaviour, both evaluations logged
5. *(2m)* VIP after-hours near breach → our Workbench with full context → resolved live → Run B continues
6. *(1m)* Insights: recurring known-error cluster + its action path
7. *(30s)* Data Manager — three systems green

Presentation polish scores **zero**. Expect an unrehearsed case and questions on your own architecture.

---

## 6. Rubric map

| Criterion | Pts | Earned in |
|---|---|---|
| Business output | 30 | Phases 1–4; the quantified SLA number comes from Phase 3 insights |
| Architecture on Auto | 20 | Phases 1–3 — 7 operators (8), fan-out/branching/retry (7), Data Manager (5) |
| Customizability & Policies | 20 | Phase 2 — gate-before-act (8), no-code edit (7), audit (5) |
| AI Insights | 15 | Phase 3 — real data (6), non-trivial + severity (5), action path (4) |
| Command Center & live demo | 15 | Phases 1 + 6 |
| **Bonus** | +10 | Phase 5 |

**Gate runs first:** solves the real problem end to end · genuine human in the loop · real
systems · works live.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| Rewiring breaks a working orchestrator | Publish stable versions **first** (Phase 0). Rewire in a new draft, revert in one click |
| Queue mode underestimated | It is a redesign of the entry path, not an add-on. Starts Wednesday morning, not Thursday |
| SLA computed from raw elapsed time | Phase 0, tested first. `sla_calendar` exists to punish this |
| Human loop stays only in Auto's form | Rubric wants it in our Workbench. Two-run split, Phase 2 |
| Policy displays but doesn't gate | Every acting operator consults the policy decision. Review Ve Song's operators Wednesday |
| Template demo data ships | Friday sweep: grep frontend for seeded policy/insight fixtures, delete |
| 60 req/min hit during flood | Batch the flood into one run. Test at full 30-ticket volume Friday |
| Workflows left as drafts | Publish stable versions Phase 0 and again Phase 4 |
| Auto's `auto.supervity.ai` docs host ≠ real API host | Use `auto-workflow-api.supervity.ai` everywhere (§3) |

---

## 8. Daily rhythm
- **09:00 standup (15 min):** shipped / blocked / does §3 still hold
- **21:00 integration check:** merge to `main`, clean-clone smoke test, one end-to-end run
- **Discord is the source of truth.** A live ruling overrides this plan and the guide.

---

## 9. Sources
- `Autopilot_Asia_Round2_Participant_Guide.pdf` (20 pp) · `ProblemStatement__Service_Desk.pdf` (3 pp)
- Dataset: 10 tables, 1,195 rows; `Field_Dictionary.csv` lists the 9 seeded traps
- Round 1 workflows audited live from `auto-workflow-api.supervity.ai` on 3 Aug 2026
- Template: `github.com/digitamizers/AutoPilot-Template` · Auto docs: `auto.supervity.ai/docs`

> ⚠️ [hackathon-brief.md](hackathon-brief.md) in this repo is generic template filler — wrong
> tracks, wrong rubric, no Data Manager, no integration floor. This plan supersedes it.
