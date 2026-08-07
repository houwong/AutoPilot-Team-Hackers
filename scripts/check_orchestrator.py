#!/usr/bin/env python
"""
Structural checks on the Auto orchestrator — run after ANY change to it.

Every one of these has broken at least once, and each failure was silent: the
run reported success while doing nothing, or the agent acted on defaults it
should never have seen. Reading the definition catches them in seconds; a live
run takes two minutes and only tells you about the path it happened to take.

    docker compose exec backend python scripts/check_orchestrator.py

Exits non-zero if any check fails, so it can gate a demo.
"""
import os
import re
import sys

import httpx

BASE = os.getenv("AUTO_BASE_URL", "https://auto-workflow-api.supervity.ai")
ORCH = os.getenv("AUTO_WF_ORCHESTRATOR", "019fd826-9991-7000-873c-ea6fca4c660b")


def fetch(path: str) -> dict:
    """
    Uses httpx rather than urllib deliberately: the API's edge rejects the
    default `Python-urllib/3.x` User-Agent with a 403 that looks exactly like an
    auth failure. httpx's default UA is accepted, and it is what auto_client
    already uses.
    """
    r = httpx.get(
        f"{BASE}/api/v1{path}",
        headers={
            "Authorization": f"Bearer {os.environ['AUTO_API_KEY']}",
            "x-source": "external",
            "x-active-org": os.getenv("AUTO_ORG", "Team Hackers"),
            "x-user-timezone": os.getenv("AUTO_TIMEZONE", "Asia/Kuala_Lumpur"),
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


def main() -> int:
    try:
        doc = fetch(f"/workflows/{ORCH}")
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: cannot read orchestrator {ORCH}: {exc}")
        print("If this is 'Workflow not found', the workspace was rebuilt and the IDs")
        print("changed. Re-read them from GET /api/v1/workflows and update .env.")
        return 2

    wf = doc["workflow"]
    steps = {s["id"]: s for s in wf["steps"]}
    print(f"orchestrator v{doc['version']}  isDraft={doc['isDraft']}  steps={len(steps)}\n")

    # 1. The escalate/block branches must be reachable.
    #    depends_on is an AND. Listing step_3_rem here means an escalated ticket
    #    can never reach the human form: the branch is taken, the target cannot
    #    run, and Auto reports 'succeeded' having done nothing. Broken 4 Aug,
    #    fixed, then reintroduced by the 5 Aug rebuild.
    for sid in ("step_4_rev", "step_6_notif_rejected"):
        deps = set(steps.get(sid, {}).get("depends_on") or [])
        check(
            deps == {"step_4_gate"},
            f"{sid} depends only on step_4_gate",
            f"got {sorted(deps)} — escalate/block branches are dead ends",
        )

    # 2. No whitelist may filter an operator's result on the way out.
    #    step_2_diag once kept 9 of Operator 2's 20+ fields, dropping
    #    kb_confidence and vip, so Operator 3 ran on its defaults and escalated
    #    every ticket regardless of content.
    for sid in ("step_2_diag", "step_0_sla", "step_0_incidents"):
        code = (steps.get(sid, {}).get("subworkflow_call") or {}).get(
            "output_mapping_code_cell"
        ) or ""
        check(
            "target_keys" not in code,
            f"{sid} passes the whole operator result through",
            "a target_keys whitelist is dropping fields",
        )

    # 3. Parallel fan-out and fan-in at the front.
    start = set(wf.get("start_at") or [])
    check(
        start == {"step_0_sla", "step_0_incidents"},
        "Op5 and Op6 start in parallel",
        f"start_at = {sorted(start)}",
    )
    check(
        set(steps.get("step_1_sweep", {}).get("depends_on") or [])
        == {"step_0_sla", "step_0_incidents"},
        "triage fans in from both",
    )

    # 4. Every operator is delegated to, and nothing talks to an integration
    #    directly from the orchestrator.
    called = {
        (s.get("subworkflow_call") or {}).get("workflow_id")
        for s in wf["steps"]
        if s.get("subworkflow_call")
    }
    check(len(called) == 7, "7 distinct operators wired", f"found {len(called)}")

    inline = [
        s["id"]
        for s in wf["steps"]
        if not s.get("subworkflow_call")
        and not s.get("is_human_input_step")
        and (s.get("code_cell") or "")
    ]
    check(not inline, "no inline step touching integrations", f"inline: {inline}")

    # 5. The gate branches three ways and precedes remediation.
    gate_next = {
        (n.get("condition") or {}).get("id"): n.get("step_id")
        for n in (steps.get("step_4_gate", {}).get("next_steps") or [])
    }
    check(
        len(gate_next) == 3,
        "change gate branches three ways",
        f"conditions: {sorted(k for k in gate_next if k)}",
    )
    check(
        "step_4_gate" in (steps.get("step_3_rem", {}).get("depends_on") or []),
        "gate runs before remediation",
        "Operator 3 executes when it judges a change safe, so a gate placed "
        "after it would approve a change already applied",
    )

    # 6. target_issue_key must not carry a default, or every run targets one ticket.
    #
    #    Read `default`, NOT `value`. An input carries both: `default` is what the
    #    engine substitutes when the field is omitted, while `value` is only the
    #    Auto UI remembering the last key a human typed into the Run dialog. They
    #    differ, and confusing them raises a false alarm — verified 6 Aug by
    #    triggering with no target while value was 'ITSM-2180': the run picked
    #    ITSM-2325 off the queue, so `value` had no effect on execution.
    tik = next((i for i in wf["inputs"] if i["name"] == "target_issue_key"), None)
    check(tik is not None, "target_issue_key input exists")
    if tik:
        check(
            not (tik.get("default") or "").strip(),
            "target_issue_key has no default",
            f"default {tik.get('default')!r} — every run would target that ticket",
        )

    # 7. Every notification step must be reached by a branch condition.
    #    step_6_notif_escalated declares depends_on step_4_gate but no next_steps
    #    edge points at it, and Auto schedules a step as soon as its dependencies
    #    complete. It therefore fired on a run whose gate decision was 'allow' —
    #    an escalation alert for a ticket nobody escalated.
    targeted = {
        n.get("step_id")
        for s in wf["steps"]
        for n in (s.get("next_steps") or [])
    }
    for sid in ("step_6_notif_escalated", "step_6_notif_rejected", "step_6_notif_auto"):
        check(
            sid in targeted,
            f"{sid} is reached by a branch condition",
            "no next_steps edge points at it, so it fires on every run",
        )

    # 8. Notification mappers must agree on priority, and must fall back to
    #    Operator 2.
    #
    #    step_1_sweep sets ticket_data['priority'] = None whenever the source row
    #    has no priority column. step_6_notif_auto handles that by falling back to
    #    diag_result; step_6_notif_escalated did not, and its normaliser returned
    #    "Medium" for a missing value. ITSM-2180 — "MAJOR INCIDENT: Payroll portal
    #    outage", Highest in Supabase, Highest in diag_result — was announced to
    #    Slack as Medium. The same ticket read Critical on the auto path and
    #    Medium on the escalate path, and escalations are where priority matters
    #    most.
    #
    #    Both faults are invisible at runtime: the notification sends, Slack
    #    reports SUCCESS, and only the number is wrong.
    #
    #    Cover EVERY notification step, not the two that were noticed first.
    #    step_6_notif_rejected carried the identical rename and the identical
    #    "Medium" default for a whole session without being flagged, purely
    #    because it was missing from this tuple — and it only surfaced when the
    #    block branch was finally exercised. A check that inspects a subset of
    #    the steps sharing a defect will certify the ones it skipped.
    for sid in (
        "step_6_notif_auto",
        "step_6_notif_escalated",
        "step_6_notif_rejected",
        "step_6_notif_manual",
    ):
        cell = (steps.get(sid, {}).get("subworkflow_call") or {}).get(
            "input_mapping_code_cell"
        ) or ""
        if not cell:
            continue
        at = cell.find('"priority"')
        expr = cell[at : at + 260] if at >= 0 else ""
        check(
            "diag" in expr,
            f"{sid} falls back to diag_result for priority",
            "reads ticket_data only, which step_1_sweep sets to None when the "
            "source row has no priority column",
        )
        # Whether the rename is visible depends on the outcome this mapper
        # sends. Of Operator 4's six steps only notify_human_review_both
        # renders priority in Slack, and it is reached by outcome
        # HUMAN_REVIEW_REQUIRED — which step_6_notif_escalated and
        # step_6_notif_rejected both send. On those the wrong word reaches a
        # human. step_6_notif_auto sends AUTO_RESOLVED, which routes to an
        # email that never shows priority, so its rename is inert until that
        # message changes.
        #
        # Match the literal only where it is ASSIGNED as the outcome, not
        # anywhere in the cell: step_6_notif_manual lists HUMAN_REVIEW_REQUIRED
        # in an outcome_style_map used purely to pick a badge colour, and a
        # substring test read that as "this mapper escalates". It actually
        # sends AUTO_RESOLVED or VERIFICATION_FAILED, neither of which renders
        # priority.
        at_outcome = cell.find('"outcome"')
        visible = "HUMAN_REVIEW_REQUIRED" in cell[at_outcome : at_outcome + 120]
        check(
            '"Critical"' not in cell and "'Critical'" not in cell,
            f"{sid} does not rename priority",
            "a Highest->Critical table disagrees with the review form, Supabase "
            "and Jira, and nothing routes on the renamed value. "
            + (
                "This mapper sends HUMAN_REVIEW_REQUIRED, so the renamed word "
                "reaches Slack"
                if visible
                else "Inert today — this mapper's outcome routes to a message "
                "that does not render priority"
            ),
        )

    # 9. The human review form must read priority the same way the notifications
    #    do.
    #
    #    step_1_sweep sets ticket_data['priority'] = None rather than omitting the
    #    key, so `.get("priority", "N/A")` never returns its default — the key is
    #    present, holding None — and the reviewer's form renders the literal word
    #    None. The fallback to diag_result is what actually fills it.
    #
    #    Match on any assignment to a name ending in `priority`, not on a fixed
    #    variable name: Auto renames its generated locals on every regeneration
    #    (step_4_rev_priority -> priority in v31), and an anchor tied to one
    #    spelling reports a false failure on correct code.
    form = steps.get("step_4_rev", {}).get("code_cell") or ""
    assigns = [ln for ln in form.splitlines() if re.search(r"priority\s*=[^=]", ln)]
    check(
        any("diag" in ln for ln in assigns),
        "step_4_rev form falls back to diag_result for priority",
        'reads ticket_data only; `.get("priority", "N/A")` cannot fire its '
        "default because step_1_sweep stores None under an existing key",
    )

    # 10. The version Auto runs must be the version we just inspected.
    #
    #     Every check above reads GET /workflows/{id}, which returns the current
    #     definition. What executes is the version flagged isDefault. They have
    #     matched every time so far — `isDraft: True` only means an editor draft
    #     session is open, it does not gate execution — but if they ever diverge,
    #     this script would pass while the agent ran different code, which is the
    #     worst failure mode available to it.
    #
    #     Compare as strings: `version` on the workflow is a str and
    #     `versionNumber` on a version record is an int, so == is False even when
    #     both read 33.
    try:
        versions = fetch(f"/workflows/{ORCH}/versions").get("versions") or []
        default = next((v["versionNumber"] for v in versions if v.get("isDefault")), None)
        check(
            str(default) == str(doc.get("version")),
            "the default version is the one checked",
            f"checked v{doc.get('version')} but Auto runs v{default}",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, "the default version is the one checked", f"could not read versions: {exc}")

    # 11. Same guarantee for every operator the orchestrator delegates to.
    #
    #     `isDraft: True` does NOT hold an edit back — each save commits a version
    #     and makes it default, and the draft flag only means an editing session
    #     is open beside it. Proven on Operator 3: it wrote to a live ticket
    #     before approval at v2, stopped at v3, and stayed stopped at v4, with
    #     isDraft true throughout. The behaviour tracked the version, not the
    #     flag.
    #
    #     What WOULD bite is isDefault pointing at an older version than the one
    #     being edited: the definition you read would not be the code that runs,
    #     and every check above would pass while vouching for the wrong thing.
    #     That is the condition worth asserting — for the operators too, not just
    #     the orchestrator, since the safety guard that stops the agent writing
    #     before a human approves lives inside Operator 3.
    for step in wf["steps"]:
        wid = (step.get("subworkflow_call") or {}).get("workflow_id")
        if not wid:
            continue
        try:
            op = fetch(f"/workflows/{wid}")
            op_versions = fetch(f"/workflows/{wid}/versions").get("versions") or []
            op_default = next(
                (v["versionNumber"] for v in op_versions if v.get("isDefault")), None
            )
            check(
                str(op_default) == str(op.get("version")),
                f"{step['id']} runs the latest version of {op.get('name', wid)[:34]}",
                f"latest is v{op.get('version')} but Auto runs v{op_default}",
            )
        except Exception as exc:  # noqa: BLE001
            check(False, f"{step['id']} operator version readable", str(exc)[:120])

    width = max(len(n) for _, n, _ in results)
    failed = 0
    for ok, name, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {'' if ok else detail}")
        failed += 0 if ok else 1

    print(f"\n{len(results) - failed}/{len(results)} passed")
    if failed:
        print("\nDo not demo until these pass. Each one fails silently at runtime.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
