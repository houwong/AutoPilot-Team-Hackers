#!/usr/bin/env python
"""Dump raw SSE event payloads so the persistence layer can map them correctly."""
import asyncio
import json
import sys

sys.path.insert(0, "/app")

from app.services.auto_client import AutoClient  # noqa: E402

WF_OP7 = "019fcac2-3eb9-7000-b024-b93084231a43"


async def main() -> None:
    client = AutoClient()
    async for ev in client.stream(WF_OP7, {"issue_key": "ITSM-2214"}):
        if ev.event in ("thinking", "ping"):
            continue
        print(f"=== event: {ev.event} ===")
        print(f"    top-level keys: {list(ev.data.keys())}")
        print("    " + json.dumps(ev.data, indent=2)[:900].replace("\n", "\n    "))
        print()


if __name__ == "__main__":
    asyncio.run(main())
