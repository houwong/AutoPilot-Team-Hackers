# Demo script - 6 minutes + 2 minutes Q&A

**Track 3, Customer Support / Service Desk. Team Hackers.**

Two browser tabs open before you start:

- Tab 1: `http://localhost:3001` (Dashboard)
- Tab 2: `http://localhost:3001/workbench`

Everything below is real and currently true. Do not quote a number this script
does not contain.

---

## Before you walk up (do this at 10:45, not on stage)

1. `docker compose up -d` and confirm all three services are healthy.
2. Run the pre-flight: `docker compose exec backend python scripts/check_orchestrator.py` - expect **37/37**.
3. Run `docker compose exec backend python scripts/check_queue_planner.py` and
   expect two `PASS` lines for the validator targets.
4. Open all five pages once. The production frontend is pre-built, but this
   warms authentication and API requests before the demo.
5. Confirm the Workbench has the **ITSM-2180 CAB approval** item open. If it does
   not, see "If the CAB item is missing" at the bottom.
6. Do **not** approve or reject ITSM-2180 while rehearsing. It consumes the
   scenario.

---

## 0:00 - 0:25 | Open, and start the agent working

> "This is a service desk with **460 tickets**. **372** are open, **69** have
> already breached SLA. Our AI Employee works that backlog. I'm going to start
> it on a real ticket now, and talk while it runs."

**Do:** type `ITSM-2180` into the dashboard input, click **Run**.

The trace appears. Leave it. It takes about two minutes and you will come back
to it at the end.

> "That's one orchestrator delegating to seven operators. You can watch it
> happen - and notice the first two run **in parallel**."

---

## 0:25 - 1:10 | Handling data: the integrations

**Go to:** Data Manager.

> "Five connected systems, all healthy. Supabase is our system of record - 460
> tickets, the user directory, the knowledge base, change records. Slack and
> Outlook are the channels. Supervity Auto runs the agent itself."

> "The part I want you to look at is the **verification** column. Every
> integration reports *how* we know it is healthy, not just a green light."

Point at the words on screen:

> "**Probed** means we called it just now and timed it. **Observed** means we
> hold no credentials for that channel, so its health comes from the last
> operator run that actually used it - including that operator's own per-channel
> delivery result."

> "That distinction caught a real bug. Outlook reported healthy for an entire day
> while every single send was failing on a mailbox quota, because we were only
> checking that the workflow step completed. A step completing is not a message
> arriving. Now we read the operator's delivery status, and if the channel has
> not been exercised in 24 hours we say **unverified** rather than green."

*Why this lands: it is the difference between claiming integrations are real and
proving it.*

---

## 1:10 - 2:40 | The Workbench: evidence, and the human decision

**Go to:** Workbench. Open the **ITSM-2180** item.

> "This is where the agent stops. ITSM-2180 is a major incident - the payroll
> portal is down. The agent diagnosed it, then hit the change gate and refused to
> act."

Point at the change record block:

> "It found change record **CHG-0001**, risk **High**, status **Pending CAB
> Approval**. Our policy says a pending CAB change escalates. So Operator 3 -
> remediation - **never ran**. Nothing touched production."

> "The reviewer gets the gate's own reason, the change record, and the policy
> values that were in force at the moment of the decision. Not a summary we
> wrote afterwards - the actual evidence."

**Now the important sentence:**

> "When I approve here, two things happen. The CAB decision is written back to
> Supabase, so the system of record changes. And the **paused Supervity run is
> resumed** - we submit the human-review form the workflow is waiting on, and it
> carries on from exactly where it stopped."

> "That matters because early on we got this wrong. We recorded the decision and
> started a *second* run, while the original sat parked forever. The human's
> decision never completed the workflow it belonged to. Now it does."

**Do NOT click Approve** unless a judge asks. If they do, click it and say:

> "Watch the run resume - execute, then notify. And notice the Workbench item now
> records whether the ticket **actually changed**, separately from which path the
> agent took. Those are two different questions and we answer both."

---

## 2:40 - 3:40 | AI Policies: why they matter

**Go to:** AI Policies.

> "**21 policies**, in five groups. These are not settings - they are the rules
> the agent works inside, and they are evaluated **before** it acts, not
> reported afterwards."

Point at `kb_confidence_threshold`:

> "This one is the confidence bar for auto-remediation. It is **0.85** today.
> A business user changes it here, with no code and no redeploy, and the very
> next run behaves differently."

Point at `escalating_statuses`:

> "This is the one that stopped ITSM-2180. 'Pending CAB Approval' is listed as an
> escalating status, so the gate escalates instead of acting."

> "And every evaluation is logged with the value that was in force at the time -
> so a decision made last week still reads correctly after someone changes the
> rule today."

**If a judge wants to test it:** offer the input box on this page - change a
threshold, run a ticket from the same screen, show the different outcome.

> "The reason this matters: without it, the agent's judgement is buried in a
> workflow only an engineer can change. With it, the desk manager owns the risk
> appetite."

---

## 3:40 - 4:40 | AI Insights: what the agent noticed

**Go to:** AI Insights.

> "**14 insights**, every one computed from tickets this agent actually
> processed. Three are worth your time."

Point at each:

> "**287 of 363 open tickets carry an SLA status that disagrees with
> business-hours reality.** The ticket says 'within SLA'. Recomputed against
> working hours and regional holidays, it is not. That is most of the backlog
> being mis-prioritised, and no person was ever going to find it by hand."

> "**23 tickets trace to one root cause.** One incident, twenty-three tickets,
> twenty-three separate people being handled individually. That is the case for
> major-incident detection existing at all."

> "**29 of 38 knowledge base articles are empty placeholders**, and **92% of
> tickets have no article behind them.** That is not a ticket problem, it is the
> reason the agent has to escalate as often as it does - and it tells the manager
> exactly where to invest."

> "The last one is about us: **low confidence is the most common reason a human
> is needed.** The agent is telling you where it is weakest."

---

## 4:40 - 5:25 | Processed Tickets: running the real backlog

**Go to:** Processed Tickets.

> "One ticket at a time is a demo. This is how it runs the backlog."

Click **Preview next 10**.

> "Preview runs a separate, read-only Queue Planner. Operator 5 recomputes the
> business-hours SLA, Operator 6 adds incident context, and a new deterministic
> planning operator ranks the result. No ticket execution, Supabase write or
> notification starts until I confirm. That is why a **Low** priority ticket
> already breached for a VIP can outrank a **Highest** one still within target."

> "Confirm freezes that exact batch, so a later backlog refresh cannot silently
> swap the tickets out from under a decision someone already reviewed. Then one
> ticket starts at a time, and the scheduler picks up the rest every five
> minutes."

Point at the history:

> "Every processed ticket keeps its outcome, and separately, whether the
> **service desk row actually changed**. We had a case where the queue said
> 'human approved' while the ticket in Supabase had never moved. Those are now
> two different fields, because they are two different facts."

---

## 5:25 - 6:00 | Close, on the finished run

**Go back to:** the Dashboard. The trace from 0:25 is complete.

> "That is the run I started six minutes ago."

Point down the trace:

> "SLA engine and incident detection in parallel. Triage. Diagnosis. The change
> gate. Then it escalated, and remediation never ran."

> "**65 runs, 409 operator invocations, 42% handled with no human at all**, about
> 140 seconds each. The other 58% reached a person with the evidence to decide -
> which is the point, not a shortfall."

> "Seven execution operators, three read-only planning workflows, one execution
> orchestrator, five live integrations, 21 policies a business owns, and every
> decision traceable back to the rule that caused it."

---

# Q&A prep - 2 minutes

**"What if I ask it to run a ticket you didn't prepare?"**
Do it. Type any open ticket key on the dashboard. If they pick a closed one, it
refuses with a 409 and says why - the agent does not reopen finished work. That
refusal is a feature; say so.

**"How do you know the integrations are real?"**
Data Manager, verification column. And the Outlook quota story - a false green
that our own check caught.

**"What happens if the agent is wrong?"**
Two answers. It stops at the gate rather than acting, and remediation only writes
when it decides AUTO_REMEDIATE. And after the fact, we re-read Supabase and
compare against what the operator said it wrote - if they disagree, the item is
flagged rather than reported as success.

**"Is the AI Manager a chatbot?"**
No language model behind it, deliberately. Every answer is read from our own
records - runs, operator steps, Workbench items, policies - and each reply names
what it consulted. It cannot invent an explanation for a decision, because there
is nothing in it that can invent. It also triggers runs.

**"Why is autonomy only 42%?"**
Because 92% of tickets have no knowledge base article. The agent refuses to act
without a basis, which is the correct behaviour. The Insights page names that as
the constraint, and fixing the KB is what moves the number.

**"What was the hardest bug?"**
The agent wrote to live tickets *before* the human approved. By the time the
reviewer opened the item, production had already changed and rejecting could not
undo it. The write now sits behind the safe-execute branch only.

---

## If the CAB item is missing

ITSM-2180 only escalates while `CHG-0001` is `Pending CAB Approval`. If someone
approved it during rehearsal, reset it in Supabase `change_requests`:

    status = 'Pending CAB Approval', approver = null   where change_id = 'CHG-0001'

Then run ITSM-2180 once from the dashboard and the item returns in about three
minutes.

## Known flakiness - do not build on it

Operator 6 does not always return the incident cluster. When it does, the
Workbench item reads "part of Cluster ITSM-2180 (23 tickets)" and is worth
pointing at. When it does not, the same fact is on the **Insights** page as
"23 tickets trace to one root cause", which is computed by our backend and is
reliable. Tell the story from Insights and treat the Workbench cluster as a
bonus if it appears.

## Re-run before judging

Slack's health goes unverified after 24 hours without a Slack-bearing run.
Run ITSM-2180 once in the morning so the Data Manager is fully green.
