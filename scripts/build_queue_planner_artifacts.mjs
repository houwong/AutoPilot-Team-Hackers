#!/usr/bin/env node

/** Build deterministic, import-ready Supervity workflow bundles. */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const outputDir = path.join(root, 'supervity', 'queue-planner')
const operatorSource = fs.readFileSync(
  path.join(root, 'scripts', 'queue_planner_operator_code.py'),
  'utf8',
)
const operatorMatch = operatorSource.match(/OPERATOR_CODE = r'''([\s\S]*)'''\s*$/)
if (!operatorMatch) throw new Error('Could not extract OPERATOR_CODE source')
const operatorCode = operatorMatch[1]

// Auto may assign a fresh workflow id on import. Re-run with these variables
// after each import so the planner points at the exact captured IDs.
const NEW_OPERATOR_ID = process.env.AUTO_WF_OP1_PLANNER?.trim() || '019fd290-bdf0-7008-8c41-7e9d9f2a1b01'
const QUEUE_PLANNER_ID = process.env.AUTO_WF_QUEUE_PLANNER?.trim() || '019fd290-bdf0-7009-8c41-7e9d9f2a1b02'
const OP5_ID = '019fd290-bdf0-7001-b84c-b24f8a66c031'
const OP6_ID = '019fd290-bdf0-7002-992a-58afae950865'

const input = (name, type, label, description, defaultValue) => ({
  name,
  description,
  type,
  label,
  required: false,
  default: defaultValue,
  is_hidden: false,
  multiple: false,
})

const exportShell = (id, versionId, definition) => ({
  exportFormatVersion: '1.0',
  exportedAt: '2026-08-08T00:00:00Z',
  rootWorkflowId: id,
  versionExportMode: 'all',
  workflows: [{
    sourceWorkflowId: id,
    sourceDefaultVersionNumber: 1,
    versions: [{
      sourceVersionId: versionId,
      versionNumber: 1,
      commitMessage: 'Deterministic Section 4.1 queue planning workflow',
      definition,
    }],
  }],
  schedules: [],
  integrationsManifest: {
    services: ['python', 'supabase'],
    envNames: ['SUPABASE_URL', 'SUPABASE_TOKEN'],
  },
})

const operatorDefinition = {
  name: 'Operator 1 — Queue Planning Triage',
  description: 'Read-only deterministic ranking of the active backlog using Operator 5 SLA evidence and Operator 6 incident evidence.',
  business_functions: ['IT Service Management', 'Queue Planning'],
  envs: [],
  inputs: [
    input('sla_states_json', 'textarea', 'Operator 5 SLA Evidence', 'Stringified final output from Operator 5.', '{}'),
    input('incident_clusters_json', 'textarea', 'Operator 6 Incident Evidence', 'Stringified final output from Operator 6.', '{}'),
    input('priority_ranking_order', 'textarea', 'Priority Ranking Order', 'Six canonical SLA/VIP tiers, in order.', 'Breached VIP, Breached Non-VIP, At risk VIP, At risk Non-VIP, Within SLA VIP, Within SLA Non-VIP'),
    input('max_candidates', 'number', 'Maximum Candidates', 'Maximum number of ranked tickets to return.', '1000'),
  ],
  start_at: ['step_queue_planning'],
  steps: [{
    id: 'step_queue_planning',
    name: 'Deterministic Queue Planning',
    description: 'Fetch active tickets and rank them using validated SLA and incident evidence.',
    services: ['python', 'supabase'],
    depends_on: [],
    code_cell: operatorCode,
    next_steps: [],
  }],
  pip_packages: ['httpx'],
  integration_overrides: {},
}

const op5Input = `import json

async def step_operator_5_input_mapping():
    payload = {
        "sla_targets": user_inputs.get("sla_targets"),
        "at_risk_window_minutes": user_inputs.get("at_risk_window_minutes"),
        "default_region": user_inputs.get("default_region"),
        "as_of": user_inputs.get("as_of"),
        "issue_keys": "",
    }
    globals()["_subworkflow_inputs_step_operator_5"] = payload
    print(json.dumps(payload, default=str))

await step_operator_5_input_mapping()
`

const op5Output = `import json

async def step_operator_5_output_mapping():
    raw = globals().get("_subworkflow_outputs_step_operator_5")
    if not raw:
        raise ValueError("MISSING_OPERATOR_5_OUTPUT")
    globals()["sla_states_json"] = raw
    print(json.dumps(raw, default=str))

await step_operator_5_output_mapping()
`

const op6Input = `import json

async def step_operator_6_input_mapping():
    payload = {
        "flood_threshold_count": user_inputs.get("flood_threshold_count"),
        "flood_window_minutes": user_inputs.get("flood_window_minutes"),
        "correlation_confidence_threshold": user_inputs.get("correlation_confidence_threshold"),
        "include_relationship_types": user_inputs.get("include_relationship_types"),
        "recurring_error_min_count": user_inputs.get("recurring_error_min_count"),
        "as_of": user_inputs.get("as_of"),
    }
    globals()["_subworkflow_inputs_step_operator_6"] = payload
    print(json.dumps(payload, default=str))

await step_operator_6_input_mapping()
`

const op6Output = `import json

async def step_operator_6_output_mapping():
    raw = globals().get("_subworkflow_outputs_step_operator_6")
    if not raw:
        raise ValueError("MISSING_OPERATOR_6_OUTPUT")
    globals()["incident_clusters_json"] = raw
    print(json.dumps(raw, default=str))

await step_operator_6_output_mapping()
`

const plannerInput = `import json

async def step_queue_planner_input_mapping():
    sla = globals().get("sla_states_json")
    incidents = globals().get("incident_clusters_json")
    if not sla or not incidents:
        raise ValueError("MISSING_PLANNER_EVIDENCE")
    payload = {
        "sla_states_json": sla,
        "incident_clusters_json": incidents,
        "priority_ranking_order": user_inputs.get("priority_ranking_order"),
        "max_candidates": user_inputs.get("max_candidates"),
    }
    globals()["_subworkflow_inputs_step_queue_planner"] = payload
    print(json.dumps(payload, default=str))

await step_queue_planner_input_mapping()
`

const plannerOutput = `import json

async def step_queue_planner_output_mapping():
    raw = globals().get("_subworkflow_outputs_step_queue_planner")
    if not raw:
        raise ValueError("MISSING_QUEUE_PLANNER_OUTPUT")
    globals()["prioritized_tickets"] = raw
    print(json.dumps(raw, default=str))

await step_queue_planner_output_mapping()
`

const subworkflowStep = (id, name, workflowId, dependsOn, inputCode, outputCode) => ({
  id,
  name,
  description: `Read-only call to ${name}.`,
  services: ['python'],
  is_human_input_step: false,
  subworkflow_call: {
    workflow_id: workflowId,
    input_mapping_agent_prompt: `Map the exact inputs for ${name} into the namespaced payload.`,
    output_mapping_agent_prompt: `Preserve the complete output from ${name}.`,
    input_mapping_code_cell: inputCode,
    output_mapping_code_cell: outputCode,
  },
  depends_on: dependsOn,
  next_steps: [],
})

const plannerDefinition = {
  name: 'Queue Planner — Read Only',
  description: 'Run Operator 5 and Operator 6 in parallel, then invoke the new deterministic Queue Planning Triage operator.',
  business_functions: ['IT Service Management', 'Queue Planning'],
  envs: [],
  inputs: [
    input('sla_targets', 'textarea', 'SLA Targets', 'Operator 5 SLA targets.', 'VIP: 4h response / 24h resolution; Non-VIP: 8h response / 48h resolution'),
    input('at_risk_window_minutes', 'number', 'At Risk Window', 'Operator 5 at-risk window in minutes.', '120'),
    input('default_region', 'text', 'Default Region', 'Operator 5 default region.', 'Global'),
    input('as_of', 'text', 'As Of', 'Reference datetime for both evidence operators.', '2026-07-25T00:00:00Z'),
    input('flood_threshold_count', 'number', 'Flood Threshold', 'Operator 6 incident threshold.', '5'),
    input('flood_window_minutes', 'number', 'Flood Window', 'Operator 6 correlation window.', '120'),
    input('correlation_confidence_threshold', 'number', 'Correlation Threshold', 'Operator 6 confidence threshold.', '0.7'),
    input('include_relationship_types', 'textarea', 'Relationship Types', 'Operator 6 relationship types.', 'is caused by, relates to'),
    input('recurring_error_min_count', 'number', 'Recurring Error Minimum', 'Operator 6 recurring-error minimum.', '20'),
    input('priority_ranking_order', 'textarea', 'Priority Ranking Order', 'Six canonical SLA/VIP tiers, in order.', 'Breached VIP, Breached Non-VIP, At risk VIP, At risk Non-VIP, Within SLA VIP, Within SLA Non-VIP'),
    input('max_candidates', 'number', 'Maximum Candidates', 'Maximum number of ranked tickets.', '1000'),
  ],
  start_at: ['step_operator_5', 'step_operator_6'],
  steps: [
    subworkflowStep('step_operator_5', 'Operator 5 SLA Engine', OP5_ID, [], op5Input, op5Output),
    subworkflowStep('step_operator_6', 'Operator 6 Incident Detector', OP6_ID, [], op6Input, op6Output),
    subworkflowStep('step_queue_planner', 'new Queue Planning Triage operator', NEW_OPERATOR_ID, ['step_operator_5', 'step_operator_6'], plannerInput, plannerOutput),
  ],
  pip_packages: [],
  integration_overrides: {},
}

fs.mkdirSync(outputDir, { recursive: true })
fs.writeFileSync(
  path.join(outputDir, 'operator-1-queue-planning.import.json'),
  JSON.stringify(exportShell(NEW_OPERATOR_ID, '019fd290-bdf0-7010-8c41-7e9d9f2a1b10', operatorDefinition), null, 2) + '\n',
)
fs.writeFileSync(
  path.join(outputDir, 'queue-planner.import.json'),
  JSON.stringify(exportShell(QUEUE_PLANNER_ID, '019fd290-bdf0-7011-8c41-7e9d9f2a1b11', plannerDefinition), null, 2) + '\n',
)
console.log(`Wrote deterministic Queue Planner artifacts to ${outputDir}`)
