/**
 * Typed access to the Command Center API.
 *
 * These endpoints are the Command Center's own record of agent activity. Auto
 * has no webhooks and its Workflow API key cannot read run history, so a run
 * triggered in Auto's console is invisible here — always trigger from the
 * Command Center.
 */
import { apiClient } from './api-client'

// =============================================================================
// TYPES
// =============================================================================

export type Decision = 'allow' | 'deny' | 'escalate'
export type ExceptionStatus = 'open' | 'in_review' | 'resolved'
export type Severity = 'critical' | 'warning' | 'info'

export interface ExceptionItem {
  id: number
  exception_type: string | null
  severity: Severity | null
  primary_issue_key: string | null
  issue_keys: string[] | null
  title: string | null
  recommendation: string | null
  confidence: number | null
  status: ExceptionStatus | null
  resolution: string | null
  resolution_notes: string | null
  resolved_by: string | null
  resolved_at: string | null
  follow_up_run_id: string | null
  created_at: string | null
}

/** What Operator 7 decided, as stored on the exception. */
export interface GateResult {
  issue_key?: string
  change_id?: string | null
  risk?: string | null
  status?: string | null
  cab_approval_required?: boolean
  approver?: string | null
  decision?: Decision
  reason?: string
  terminal?: boolean
  prior_rollback?: boolean
  rollback_count?: number
  requires_new_change_request?: boolean
  policy_conflict?: boolean
}

/** The cluster Operator 6 found, when the ticket belongs to one. */
export interface IncidentCluster {
  parent_issue_key?: string
  linked_incident_label?: string | null
  child_issue_keys?: string[]
  ticket_count?: number
  distinct_reporters?: number
  vip_count?: number
  affected_assignment_groups?: string[]
  first_seen?: string
  last_seen?: string
  recommended_action?: string
  rationale?: string
}

export interface ExceptionContext {
  gate?: GateResult
  remediation?: Record<string, unknown>
  incident?: IncidentCluster | null
  policies_at_run?: Record<string, unknown>
}

export interface ExceptionDetail {
  exception: ExceptionItem
  context: ExceptionContext | null
  run: { run_id: string; status: string } | null
}

export interface ExceptionStats {
  total: number
  open: number
  resolved: number
  by_type: Record<string, number>
  by_severity: Record<string, number>
}

export interface ResolveResponse {
  exception_id: number
  resolution: string
  resolved_by: string
  change_record_updated: Record<string, unknown> | null
  follow_up_run_id: string | null
  message: string
}

export interface AgentRun {
  run_id: string
  auto_run_id: string | null
  workflow_id: string | null
  trigger: string | null
  phase: string | null
  status: string | null
  issue_keys: string[] | null
  selected_issue_key: string | null
  error: string | null
  duration_ms: number | null
  started_at: string | null
  ended_at: string | null
  operator_count: number
}

export interface OperatorStep {
  sequence: number
  operator_name: string | null
  step_id: string | null
  status: string | null
  duration_ms: number | null
  started_at: string | null
  ended_at: string | null
  error: string | null
}

export interface RunDetail {
  run: AgentRun
  inputs: Record<string, unknown> | null
  result: Record<string, unknown> | null
  operators: OperatorStep[]
}

export type QueueCampaignStatus = 'preview' | 'running' | 'paused' | 'completed' | 'cancelled'
export type QueueItemState =
  | 'preview'
  | 'pending'
  | 'running'
  | 'awaiting_human'
  | 'auto_remediated'
  | 'human_approved'
  | 'blocked'
  | 'human_rejected'
  | 'failed'
  | 'skipped_closed'
  | 'cancelled'
  | 'completed_unknown'

export interface QueueItem {
  id: number
  campaign_id: number
  issue_key: string
  source_status: string | null
  source_priority: string | null
  source_updated_at: string | null
  state: QueueItemState
  outcome: string | null
  latest_run_id: string | null
  attempt_count: number
  last_error: string | null
  requeued_from_id: number | null
  requeue_reason: string | null
  created_at: string | null
  started_at: string | null
  completed_at: string | null
  history_source?: 'queue' | 'manual'

  // `outcome` says which path the agent took. `verification` says whether the
  // service-desk ticket actually changed. They are separate on purpose: a
  // campaign once reported ITSM-2003 as human_approved while Supabase still
  // held it at "Waiting for support" with nothing written.
  verification?: 'verified' | 'verification_failed' | 'not_applicable' | 'unknown' | null
  verification_detail?: { reason?: string; mismatches?: unknown[] } | null
  verified_at?: string | null

  // Why the ticket sits where it does in the batch, frozen at preview time.
  sla_status?: string | null
  vip?: boolean | null
  priority_rank?: number | null
  ranked_by?: 'operator_1' | 'source_priority' | 'queue_planner' | 'queue_planner_stale' | null
  ranking_reason?: string | null
  rank_position?: number | null
  ranking_evidence?: Record<string, unknown> | null
}

export interface QueueCampaign {
  id: number
  name: string
  source: string
  status: QueueCampaignStatus
  batch_limit: number
  created_by: string | null
  created_at: string | null
  confirmed_at: string | null
  started_at: string | null
  completed_at: string | null
  last_tick_at: string | null
  planner_mode: 'legacy' | 'queue_planner' | 'queue_planner_stale' | string | null
  planner_run_id: string | null
  planner_generated_at: string | null
  planner_effective_as_of: string | null
  planner_stale: boolean | null
  planner_policy_snapshot: Record<string, string> | null
  counts: Record<string, number>
}

// =============================================================================
// CALLS
// =============================================================================

export const workbench = {
  list: (status?: ExceptionStatus) =>
    apiClient.get<ExceptionItem[]>(
      `/api/exceptions${status ? `?status=${status}` : ''}`
    ),

  get: (id: number) => apiClient.get<ExceptionDetail>(`/api/exceptions/${id}`),

  stats: () => apiClient.get<ExceptionStats>('/api/exceptions/stats/summary'),

  /**
   * Record a human decision.
   *
   * Approval writes the outcome to `change_requests`, so Operator 7 returns a
   * different answer on the follow-up run. The human's decision changes the
   * system of record, not just our audit trail.
   */
  resolve: (
    id: number,
    body: {
      resolution: 'approved' | 'modified' | 'rejected'
      notes?: string
      resolved_by?: string
      rerun?: boolean
    }
  ) => apiClient.post<ResolveResponse>(`/api/exceptions/${id}/resolve`, body),
}

export const agent = {
  runs: (limit = 25) => apiClient.get<AgentRun[]>(`/api/agent/runs?limit=${limit}`),

  run: (runId: string) => apiClient.get<RunDetail>(`/api/agent/runs/${runId}`),

  trigger: (body: { target_issue_key?: string; trigger?: string }) =>
    apiClient.post<AgentRun>('/api/agent/runs', { trigger: 'manual', ...body }),
}

export const queue = {
  preview: (limit = 10) =>
    apiClient.post<{ campaign: QueueCampaign; items: QueueItem[]; warning: string }>(
      '/api/queue/campaigns/preview',
      { limit }
    ),
  confirm: (campaignId: number) =>
    apiClient.post<{ campaign: QueueCampaign; tick: Record<string, unknown> }>(
      `/api/queue/campaigns/${campaignId}/confirm`,
      {}
    ),
  active: () =>
    apiClient.get<{ campaign: QueueCampaign | null }>('/api/queue/campaigns/active'),
  campaign: (campaignId: number) =>
    apiClient.get<{ campaign: QueueCampaign; items: QueueItem[] }>(
      `/api/queue/campaigns/${campaignId}`
    ),
  pause: (campaignId: number) =>
    apiClient.post<{ campaign: QueueCampaign }>(`/api/queue/campaigns/${campaignId}/pause`, {}),
  resume: (campaignId: number) =>
    apiClient.post<{ campaign: QueueCampaign; tick: Record<string, unknown> }>(
      `/api/queue/campaigns/${campaignId}/resume`,
      {}
    ),
  cancel: (campaignId: number) =>
    apiClient.post<{ campaign: QueueCampaign }>(`/api/queue/campaigns/${campaignId}/cancel`, {}),
  tick: () => apiClient.post<Record<string, unknown>>('/api/queue/tick', {}),
  items: (params: { page?: number; state?: string; search?: string } = {}) => {
    const query = new URLSearchParams()
    query.set('page_size', '100')
    if (params.page) query.set('page', String(params.page))
    if (params.state) query.set('state', params.state)
    if (params.search) query.set('search', params.search)
    return apiClient.get<{ items: QueueItem[]; total: number; page: number; page_size: number }>(
      `/api/queue/items?${query.toString()}`
    )
  },
  requeue: (itemId: number, reason: string) =>
    apiClient.post<{ item: QueueItem; tick: Record<string, unknown> }>(
      `/api/queue/items/${itemId}/requeue`,
      { reason }
    ),
}

// =============================================================================
// HELPERS
// =============================================================================

export function severityClasses(severity: Severity | null | undefined) {
  switch (severity) {
    case 'critical':
      return 'bg-red-500/10 text-red-600 border-red-500/20 dark:text-red-400'
    case 'warning':
      return 'bg-amber-500/10 text-amber-600 border-amber-500/20 dark:text-amber-400'
    default:
      return 'bg-sky-500/10 text-sky-600 border-sky-500/20 dark:text-sky-400'
  }
}

export function decisionClasses(decision: string | null | undefined) {
  switch (decision) {
    case 'allow':
      return 'bg-emerald-500/10 text-emerald-600 border-emerald-500/20 dark:text-emerald-400'
    case 'escalate':
      return 'bg-amber-500/10 text-amber-600 border-amber-500/20 dark:text-amber-400'
    case 'deny':
      return 'bg-red-500/10 text-red-600 border-red-500/20 dark:text-red-400'
    default:
      return 'bg-slate-500/10 text-slate-600 border-slate-500/20 dark:text-slate-400'
  }
}

/** `cab_required` -> `CAB required` */
export function humanise(value: string | null | undefined) {
  if (!value) return '—'
  return value.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
}

export function relativeTime(iso: string | null | undefined) {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  const mins = Math.round((Date.now() - then) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  return `${Math.round(hrs / 24)}d ago`
}
