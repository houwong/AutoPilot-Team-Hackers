#!/usr/bin/env node

// Stable plan-facing entry point; the implementation lives in the importable
// underscore-named module so Python/Node tooling can reference it consistently.
import './build_queue_planner_artifacts.mjs'
