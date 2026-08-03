# Operator 5 test fixture — real tickets, known-correct output

Every row below is a real record in Supabase. Expected values were produced by
[`app/services/sla.py`](../app/services/sla.py) (35 passing tests), not by hand.

If Operator 5 disagrees with this table, the operator is wrong.

> ## ✅ PASSED — 3 Aug 2026, build 5
> All eight tickets match exactly on **elapsed minutes**, **SLA state** and **breach_at**.
> Totals: 6 Breached · 2 Within SLA · 0 At risk · 6 discrepancies · 0 missing.
> `breach_at` is reported in UTC and converts exactly to the region wall-clock times below.
>
> It took **five builds and five defects to get here — every one of them silent.** See
> [the failure-mode log](round2-operator-prompts.md#known-failure-modes--observed-in-the-first-build-3-aug).

---

## ⚠️ Read this before testing: `as_of` is mandatory

The data pack was generated in **early-to-mid July 2026**. Against today's clock every
ticket is weeks past target, so **every ticket breaches** and the dashboard reads 100%
breach — a meaningless demo.

Operator 5 therefore needs an **`as_of` input** (default: now) so runs can be anchored to
the dataset's own timeframe. Every number here uses:

```
as_of = 2026-07-20T04:00:00Z
```
which is 12:00 in KL-HQ / Penang / Singapore and 04:00 UTC for Remote — the last day the
data covers. Use the same value and the numbers below reproduce exactly.

---

## Workflow inputs

```
workflowId               = <Operator 5 workflow id>
inputs[sla_targets]      = VIP: 4h response / 24h resolution; Non-VIP: 8h response / 48h resolution
inputs[at_risk_window_minutes] = 120
inputs[default_region]   = Global
inputs[as_of]            = 2026-07-20T04:00:00Z
inputs[issue_keys]       = ITSM-2000,ITSM-2003,ITSM-2004,ITSM-2013,ITSM-2027,ITSM-2036,ITSM-2091,ITSM-2005
```

Resolution targets resolve to **1440 min** (VIP) and **2880 min** (non-VIP).

---

## Expected output

| issue_key | region | VIP | `Created` (verbatim) | elapsed | target | to_breach | **state** | `customfield_10030` | discrepancy | breach_at |
|---|---|---|---|---:|---:|---:|---|---|---|---|
| ITSM-2000 | Penang | no | `Jul 14 2026` | 2220 | 2880 | +660 | **Within SLA** | Within SLA | — | 2026-07-21 14:30 |
| ITSM-2003 | Penang | no | `2026-07-05 00:00:00` | 4770 | 2880 | −1890 | **Breached** | At risk | **yes** | 2026-07-14 14:30 |
| ITSM-2004 | Singapore | no | `2026-07-15 00:00:00` | 1800 | 2880 | +1080 | **Within SLA** | Within SLA | — | 2026-07-22 12:00 |
| ITSM-2013 | Singapore | yes | `2026-07-09 00:00:00` | 3960 | 1440 | −2520 | **Breached** | At risk | **yes** | 2026-07-13 15:00 |
| ITSM-2027 | KL-HQ | yes | `2026-07-15 00:00:00` | 1800 | 1440 | −360 | **Breached** | Within SLA | **yes** | 2026-07-17 15:00 |
| ITSM-2036 | Remote | yes | `2026-07-09 00:00:00` | 16080 | 1440 | −14640 | **Breached** | At risk | **yes** | 2026-07-10 00:00 |
| ITSM-2091 | Remote | yes | `03/07/2026` | 24720 | 1440 | −23280 | **Breached** | Within SLA | **yes** | 2026-07-04 00:00 |
| ITSM-2005 | Remote | no | `2026-07-12 00:00:00` | 11760 | 2880 | −8880 | **Breached** | At risk | **yes** | 2026-07-14 00:00 |

`elapsed` and `to_breach` are **business** minutes. `breach_at` is region wall-clock.

**Expected totals: 6 Breached · 2 Within SLA · 0 At risk · 6 discrepancies.**

A run reporting **0 discrepancies** has not read `customfield_10030`. Every one of these eight
tickets has a non-null value in that column (`Within SLA` or `At risk`, no padding) — verified
directly in Supabase. See the column-name trap in
[`round2-operator-prompts.md`](round2-operator-prompts.md#known-failure-modes--observed-in-the-first-build-3-aug).

---

## What each row is actually testing

| Ticket | Proves |
|---|---|
| **ITSM-2000** | `Jul 14 2026` month-name format parses. Penang's 17:30 close → `breach_at` at 14:30, not 15:00 |
| **ITSM-2091** | `03/07/2026` parses **day-first** (3 July). Month-first would shift it 4 months and change everything |
| **ITSM-2003** | Spans **7 July, a Penang-only holiday**. That day must contribute 0 minutes |
| **ITSM-2027** | Breaches by only **360 minutes** — a tight boundary that a wrong calendar flips |
| **ITSM-2004** | Plain Within-SLA control, no edge cases |

### The control pair — run this one first

**ITSM-2036** and **ITSM-2013** were both created `2026-07-09` and are both VIP.

| | region | rule | elapsed |
|---|---|---|---:|
| ITSM-2036 | Remote | 24×7 follow-the-sun | **16080** |
| ITSM-2013 | Singapore | 09:00–18:00 Mon–Fri | **3960** |

Identical ticket age, **4× difference**, entirely from the business calendar. If Operator 5
returns similar numbers for these two, it is subtracting timestamps and ignoring
`sla_calendar` — the exact failure Round 2 added that table to expose.

---

## The discrepancy signal

`customfield_10030` disagrees with computed reality on **6 of these 8** tickets, and it is a
static label rather than a live calculation — `ITSM-2091` is marked *Within SLA* while sitting
24,720 business minutes past a 1,440-minute target.

This is not a bug to fix. It is the strongest AI Insight available in this dataset:

> *"N tickets carry an SLA status that disagrees with business-hours reality; the stated field
> has not been recalculated since intake."*

Computed from data the agent actually processed, with a clear action path — exactly what the
15-point Insights criterion asks for. Operator 5 must report `stated_sla_status` alongside
`computed_sla_state` and set `discrepancy` rather than letting either value win.

---

## Reproducing

```bash
docker compose exec backend python -m pytest tests/test_sla.py -q     # 35 passed
```

To recompute the table, evaluate each ticket through
`app.services.sla.evaluate(created, as_of_local, calendar, target, 120)` with the calendar
rows from `sla_calendar`. Region comes from the **reporter**:
`issues."Reporter"` → `users_directory.display_name` → `location` → `sla_calendar.region`.
Never from the assignment group — that mapping is one-to-many and ambiguous.
