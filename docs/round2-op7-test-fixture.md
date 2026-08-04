# Operator 7 test fixture — Change / CAB Approval Gate

Expected decisions for all 13 `change_requests` rows, computed from live Supabase data
on 3 Aug 2026. If Operator 7 disagrees with this table, the operator is wrong.

Operator 7 **decides only**. It never writes to `change_requests` or `issues`.

---

## Build it as a deterministic cell from the start

Operator 5 took five builds and Operator 6 took five, and in both cases the fix was the same:
move counting and rule evaluation out of the language model and into a code cell with
assertions. Op7 is entirely lookups and comparisons over **13 rows** — there is nothing here
for a model to reason about. Only the human-readable `reason` text should come from the LLM.

---

## Workflow inputs

```
workflowId = <Operator 7 workflow id>
inputs[blocking_statuses]                    = Rejected, Rolled Back
inputs[escalating_statuses]                  = Pending CAB Approval
inputs[require_cab_for_risk]                 = High, Medium
inputs[auto_approve_risk_levels]             = Low
inputs[require_change_record_for_production] = true
inputs[issue_key]                            = ITSM-2180
```

> **`Pending CAB Approval` must NOT be in `blocking_statuses`.** It is the escalation case — the
> whole reason this operator exists is to route those to the Workbench. Blocking them removes
> the human-in-the-loop path that the gate condition requires.

Match all status/risk strings **stripped and casefolded**. Op6 lost two clusters to a leading
space in a comma-separated input; the same hazard applies here.

---

## Rule precedence — order matters

Evaluate in exactly this order and stop at the first match:

| # | condition | decision | flags |
|---|---|---|---|
| 1 | no change record found, and `require_change_record_for_production` | `block` | `requires_new_change_request` |
| 2 | `status` = `Rejected` | `block` | `terminal` |
| 3 | `status` = `Rolled Back` | `block` | `prior_rollback` |
| 4 | `status` = `Pending CAB Approval`, and (`cab_approval_required` false **and** risk in auto-approve) | `escalate` | `policy_conflict` |
| 5 | `status` = `Pending CAB Approval` | `escalate` | — |
| 6 | `status` = `Implemented`, `cab_approval_required` true, no approver | `escalate` | — |
| 7 | `status` = `Implemented` | `allow` | — |
| 8 | anything else | `escalate` | — |

**Status always beats risk.** A `Low`-risk change in a pending state is still pending. Rule 4
exists because one row has exactly that contradiction.

---

## Expected decisions — all 13 rows

| change_id | issue_key | risk | status | cab | approver | **decision** | flags |
|---|---|---|---|---|---|---|---|
| CHG-0001 | ITSM-2180 | High | Pending CAB Approval | true | — | **escalate** | — |
| CHG-0002 | ITSM-2211 | High | Pending CAB Approval | true | — | **escalate** | — |
| CHG-0003 | ITSM-2212 | High | Pending CAB Approval | true | — | **escalate** | — |
| CHG-0004 | ITSM-2213 | High | Pending CAB Approval | true | — | **escalate** | — |
| CHG-0005 | ITSM-2214 | Medium | Rolled Back | true | Priya Ismail | **block** | `prior_rollback` |
| CHG-0006 | ITSM-2215 | Medium | Rolled Back | true | Kenji Tanaka | **block** | `prior_rollback` |
| CHG-0007 | ITSM-2216 | Medium | Rolled Back | true | Kenji Tanaka | **block** | `prior_rollback` |
| CHG-0008 | ITSM-2303 | High | Implemented | true | Kevin Aziz | **allow** | — |
| CHG-0009 | ITSM-2065 | Medium | Rejected | true | Priya Ismail | **block** | `terminal` |
| CHG-0010 | ITSM-2132 | High | Implemented | false | Kenji Tanaka | **allow** | — |
| CHG-0011 | ITSM-2004 | Low | Pending CAB Approval | false | Kenji Tanaka | **escalate** | `policy_conflict` |
| CHG-0012 | ITSM-2351 | High | Rejected | false | Kenji Tanaka | **block** | `terminal` |
| CHG-0013 | ITSM-2262 | High | Implemented | false | Ravi Menon | **allow** | — |

**Totals: 5 escalate · 5 block · 3 allow.**

### Plus the no-record case

`ITSM-2000` has no row in `change_requests`.

| issue_key | decision | flags |
|---|---|---|
| ITSM-2000 | **block** | `requires_new_change_request` |

Set `require_change_record_for_production = false` and the same ticket must return **allow**.
That single toggle is a clean live demonstration of no-code policy configurability.

---

## The three rows that matter most

**CHG-0011 — the policy conflict.** `ITSM-2004` is `Low` risk with `cab_approval_required =
false`, which says auto-approve; but its status is `Pending CAB Approval`, which says wait. Two
rules give contradictory instructions. The correct answer is **escalate with
`policy_conflict = true`** — this is the seeded *policy conflict* exception type, and it belongs
in the Workbench. An operator that returns `allow` here has let risk override status.

**CHG-0005/6/7 — the rollback trap.** Three changes previously reached `Rolled Back`. A fix that
has already failed and been reversed must never be auto-retried. `prior_rollback = true` is the
signal the Workbench item should carry.

**CHG-0001 — the cross-operator thread.** This change belongs to `ITSM-2180`, the parent of the
major incident Operator 6 detects (`INC-9001`, 23 tickets, 4 assignment groups). It is `High`
risk, CAB-required, and has **no approver**.

That is your demo spine, end to end:

```
Op6 detects INC-9001 (23 tickets, one root cause)
  -> remediation proposed for ITSM-2180
     -> Op7 gate: escalate, awaiting CAB approval
        -> Workbench item with full incident context
           -> human approves
              -> Run B executes and notifies
```

One trigger touching the major-incident detector, the CAB gate, the Workbench and the two-run
split. Rehearse this path.

---

## Acceptance test

Run each `issue_key` below and check the decision:

```
ITSM-2180, ITSM-2211, ITSM-2212, ITSM-2213   -> escalate  (awaiting CAB)
ITSM-2214, ITSM-2215, ITSM-2216              -> block     (prior_rollback)
ITSM-2065, ITSM-2351                         -> block     (terminal)
ITSM-2303, ITSM-2132, ITSM-2262              -> allow
ITSM-2004                                    -> escalate  (policy_conflict)
ITSM-2000                                    -> block     (requires_new_change_request)
```

Assertions for the code cell:

```python
assert decisions["ITSM-2180"] == "escalate"
assert decisions["ITSM-2214"] == "block" and flags["ITSM-2214"]["prior_rollback"]
assert decisions["ITSM-2065"] == "block" and flags["ITSM-2065"]["terminal"]
assert decisions["ITSM-2004"] == "escalate" and flags["ITSM-2004"]["policy_conflict"]
assert decisions["ITSM-2303"] == "allow"
assert Counter(decisions.values()) == {"escalate": 5, "block": 5, "allow": 3}
```

An operator returning `allow` for `ITSM-2004`, or `block` for any of ITSM-2180/2211/2212/2213,
has the precedence wrong.
