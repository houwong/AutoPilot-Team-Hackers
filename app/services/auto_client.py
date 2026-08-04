# app/services/auto_client.py
"""
Client for the Supervity Auto workflow API.

Everything here was verified against the live platform on 3-4 Aug 2026, because
the published documentation is wrong in three places that each cost hours:

  1. The API host is `auto-workflow-api.supervity.ai`, NOT the `auto.supervity.ai`
     used in the docs. Management endpoints on the docs host return
     `400 {"message":"Unexpected Server Error"}` even unauthenticated.
  2. The body is `multipart/form-data`, not JSON. Workflow inputs are sent as
     form fields named `inputs[<name>]`.
  3. `x-source: external` is REQUIRED. Without it every call returns a bare
     `401 {"error":"Unauthorized"}` that looks exactly like a bad key. With it,
     a malformed request returns `400 {"error":"workflowId is required"}`,
     which is how you know auth succeeded.

There are no webhooks or callbacks. Auto never calls us back, and a Workflow API
key cannot read run history — so this client drives the run and persists every
event as it arrives. `agent_runs` and `operator_executions` are the only record
of what happened that the Command Center will ever have.

Rate limit: 60 requests/minute per IP.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx

log = logging.getLogger(__name__)

AUTO_BASE_URL = os.getenv("AUTO_BASE_URL", "https://auto-workflow-api.supervity.ai")
AUTO_API_KEY = os.getenv("AUTO_API_KEY", "")
AUTO_ORG = os.getenv("AUTO_ORG", "")
AUTO_TIMEZONE = os.getenv("AUTO_TIMEZONE", "Asia/Kuala_Lumpur")

EXECUTE_PATH = "/api/v1/workflow-runs/execute"
STREAM_PATH = "/api/v1/workflow-runs/execute/stream"

# SSE event names emitted by /execute/stream.
EV_ACTIVITY = "activity-run"  # one operator step started/finished
EV_WORKFLOW = "workflow-run"  # the run itself changed state
EV_THINKING = "thinking"  # model narration; ignore for persistence
EV_RESULT = "result"  # final output
EV_ERROR = "error"


class AutoError(RuntimeError):
    """Raised when Auto rejects a request or the stream reports an error."""


@dataclass
class AutoEvent:
    """
    One SSE event, already parsed.

    Every event nests its payload under `content` — verified against the live
    stream on 4 Aug:

        workflow-run  content.{workflowId, workflowRunId, status, inputs}
        activity-run  content.{workflowRunId, activityRunId, stepId, status,
                               attempt, kind, outputs}
        result        {success, message, workflowRun:{... activityRuns[] ...}}

    Note `activity-run` carries `stepId` but NOT `stepName`; step names only
    appear in the final `result` payload's `activityRuns`.
    """

    event: str
    data: dict[str, Any] = field(default_factory=dict)
    raw: str = ""

    @property
    def payload(self) -> dict[str, Any]:
        """Unwrap the `content` envelope; fall back to the raw data."""
        content = self.data.get("content")
        return content if isinstance(content, dict) else self.data

    @property
    def is_terminal(self) -> bool:
        return self.event in (EV_RESULT, EV_ERROR)

    # --- convenience accessors -------------------------------------------
    @property
    def auto_run_id(self) -> Optional[str]:
        """Auto's own run id — store on agent_run so runs can be traced back."""
        p = self.payload
        if p.get("workflowRunId"):
            return str(p["workflowRunId"])
        wr = self.data.get("workflowRun") or {}
        return str(wr["id"]) if wr.get("id") else None

    @property
    def activity_run_id(self) -> Optional[str]:
        v = self.payload.get("activityRunId")
        return str(v) if v else None

    @property
    def step_id(self) -> Optional[str]:
        v = self.payload.get("stepId")
        return str(v) if v else None

    @property
    def status(self) -> Optional[str]:
        return self.payload.get("status")

    @property
    def attempt(self) -> Optional[int]:
        """Auto's retry counter for this step; >1 means the step was retried."""
        v = self.payload.get("attempt")
        return int(v) if isinstance(v, (int, str)) and str(v).isdigit() else None

    @property
    def outputs(self) -> Optional[dict]:
        v = self.payload.get("outputs")
        return v if isinstance(v, dict) else None

    @property
    def kind(self) -> Optional[str]:
        """`step` for real work; branching steps also emit condition evaluations."""
        return self.payload.get("kind")

    @property
    def is_condition(self) -> bool:
        """
        True when this event is a branch condition rather than the step itself.

        Auto emits one activity-run per condition on a branching step, reusing
        the parent's stepId — step_4_gate produced four for one execution. Their
        outputs look like {"output": "False\\n", "conditionMet": false} and will
        silently overwrite the real step output if not filtered out.
        """
        outputs = self.outputs or {}
        if "conditionMet" in outputs:
            return True
        return self.kind is not None and self.kind != "step"

    @property
    def step_names(self) -> dict[str, str]:
        """
        stepId -> stepName, available only on the terminal `result` event.
        Use it to backfill readable names onto the operator_execution rows.
        """
        wr = self.data.get("workflowRun") or {}
        return {
            a["stepId"]: a.get("stepName") or a["stepId"]
            for a in (wr.get("activityRuns") or [])
            if a.get("stepId")
        }


def _headers() -> dict[str, str]:
    if not AUTO_API_KEY:
        raise AutoError("AUTO_API_KEY is not set")
    h = {
        "Authorization": f"Bearer {AUTO_API_KEY}",
        "x-source": "external",  # omit this and everything 401s
        "x-user-timezone": AUTO_TIMEZONE,
    }
    if AUTO_ORG:
        h["x-active-org"] = AUTO_ORG
    return h


def _form(workflow_id: str, inputs: dict[str, Any] | None) -> dict[str, tuple]:
    """
    Build the multipart body.

    Passing `files=` to httpx is what forces multipart/form-data. Each value is
    a (filename, content) tuple with filename None, which renders a plain text
    field rather than a file part.
    """
    form: dict[str, tuple] = {"workflowId": (None, workflow_id)}
    for key, value in (inputs or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            rendered = json.dumps(value)
        else:
            rendered = str(value)
        form[f"inputs[{key}]"] = (None, rendered)
    return form


def _parse_sse_block(block: str) -> Optional[AutoEvent]:
    """Parse one `event:`/`data:` block. Returns None for comments/keepalives."""
    event_name, data_lines = None, []
    for line in block.splitlines():
        if line.startswith(":"):  # comment / heartbeat
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not event_name and not data_lines:
        return None
    payload = "\n".join(data_lines)
    try:
        data = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        data = {"raw": payload}
    if not isinstance(data, dict):
        data = {"value": data}
    return AutoEvent(event=event_name or EV_RESULT, data=data, raw=payload)


class AutoClient:
    """Thin async client. One instance per run is fine; it holds no state."""

    def __init__(self, base_url: str | None = None, timeout: float = 900.0):
        self.base_url = (base_url or AUTO_BASE_URL).rstrip("/")
        self.timeout = timeout

    async def execute(self, workflow_id: str, inputs: dict[str, Any] | None = None) -> dict:
        """Synchronous execution. Blocks until the workflow finishes."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}{EXECUTE_PATH}",
                headers=_headers(),
                files=_form(workflow_id, inputs),
            )
        if r.status_code >= 400:
            raise AutoError(f"execute failed {r.status_code}: {r.text[:500]}")
        try:
            return r.json()
        except json.JSONDecodeError:
            return {"raw": r.text}

    async def get_run(self, run_id: str) -> dict:
        """
        Read one workflow run, including its activity runs and their outputs.

        Needed because an orchestrator step that delegates to a sub-workflow does
        NOT carry the operator's result in its own output — it carries only a
        link to the sub-workflow run. The decision JSON has to be fetched from
        there.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{self.base_url}/api/v1/workflow-runs/{run_id}", headers=_headers()
            )
        if r.status_code >= 400:
            raise AutoError(f"get_run failed {r.status_code}: {r.text[:400]}")
        return r.json()

    async def get_step_result(self, sub_run_id: str) -> dict:
        """Parse the operator's structured output out of a sub-workflow run."""
        run = await self.get_run(sub_run_id)
        for activity in run.get("activityRuns") or []:
            outputs = activity.get("outputs")
            if isinstance(outputs, dict):
                inner = outputs.get("output")
                if isinstance(inner, str) and inner.strip():
                    try:
                        return json.loads(inner)
                    except json.JSONDecodeError:
                        continue
                if isinstance(inner, dict):
                    return inner
        return {}

    async def stream(
        self, workflow_id: str, inputs: dict[str, Any] | None = None
    ) -> AsyncIterator[AutoEvent]:
        """
        Execute and yield events as they arrive.

        Preferred over `execute` for anything the dashboard shows: each
        `activity-run` event is one operator step, so the Command Center can
        display the orchestrator delegating in real time instead of waiting
        for a single blob at the end.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}{STREAM_PATH}",
                headers={**_headers(), "Accept": "text/event-stream"},
                files=_form(workflow_id, inputs),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")
                    raise AutoError(f"stream failed {response.status_code}: {body[:500]}")

                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    # SSE blocks are separated by a blank line.
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        ev = _parse_sse_block(block)
                        if ev is None:
                            continue
                        log.debug("auto event %s %s", ev.event, ev.status)
                        yield ev
                if buffer.strip():
                    ev = _parse_sse_block(buffer)
                    if ev is not None:
                        yield ev
