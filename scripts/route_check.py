#!/usr/bin/env python
"""Diagnose which routers actually registered."""
import sys

sys.path.insert(0, "/app")

print("--- importing app.routers ---")
import app.routers as R  # noqa: E402

print("exports:", [n for n in dir(R) if n.endswith("_router")])

print("\n--- agent router ---")
try:
    from app.routers.agent import router as ar  # noqa: E402

    print("agent router prefix:", ar.prefix)
    print("agent router routes:", [(r.path, list(r.methods)) for r in ar.routes])
except Exception as exc:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    print("AGENT ROUTER IMPORT FAILED:", exc)

print("\n--- app routes ---")
from app.main import app  # noqa: E402

paths = sorted({getattr(r, "path", "?") for r in app.routes})
print(f"total routes on app: {len(paths)}")
for p in paths:
    print("   ", p)
