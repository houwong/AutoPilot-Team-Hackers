'use client'

/**
 * The AI Workbench — the human queue.
 *
 * Every item arrives with the context needed to decide without leaving the
 * page: what the agent proposed, why the change gate stopped it, and — when the
 * ticket belongs to a major incident — how large that incident is.
 *
 * Approving writes the decision to the change record in Supabase, so Operator 7
 * returns a different answer on the follow-up run. The decision changes the
 * system of record, not just an audit trail.
 */

import { useCallback, useEffect, useState } from 'react'
import { motion } from 'framer-motion'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Icons } from '@/components/ui/icons'
import { cn } from '@/lib/utils'
import {
  type ExceptionDetail,
  type ExceptionItem,
  type ExceptionStats,
  decisionClasses,
  humanise,
  relativeTime,
  severityClasses,
  workbench,
} from '@/lib/command-center'

// =============================================================================
// SMALL PIECES
// =============================================================================

function Pill({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium',
        className
      )}
    >
      {children}
    </span>
  )
}

function StatTile({
  label,
  value,
  tone = 'default',
}: {
  label: string
  value: number | string
  tone?: 'default' | 'critical' | 'good'
}) {
  return (
    <Card>
      <CardContent className='p-4'>
        <p className='text-xs uppercase tracking-wide text-muted-foreground'>{label}</p>
        <p
          className={cn(
            'mt-1 text-2xl font-semibold tabular-nums',
            tone === 'critical' && 'text-red-600 dark:text-red-400',
            tone === 'good' && 'text-emerald-600 dark:text-emerald-400'
          )}
        >
          {value}
        </p>
      </CardContent>
    </Card>
  )
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className='flex items-start justify-between gap-4 border-b border-border/50 py-2 last:border-0'>
      <span className='text-sm text-muted-foreground'>{label}</span>
      <span className='text-right text-sm font-medium'>{value ?? '—'}</span>
    </div>
  )
}

// =============================================================================
// QUEUE
// =============================================================================

function QueueRow({
  item,
  active,
  onSelect,
}: {
  item: ExceptionItem
  active: boolean
  onSelect: () => void
}) {
  return (
    <button
      onClick={onSelect}
      className={cn(
        'w-full rounded-lg border p-3 text-left transition-colors',
        active ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted/50'
      )}
    >
      <div className='flex items-center justify-between gap-2'>
        <Pill className={severityClasses(item.severity)}>{humanise(item.severity)}</Pill>
        <span className='text-xs text-muted-foreground'>{relativeTime(item.created_at)}</span>
      </div>
      <p className='mt-2 line-clamp-2 text-sm font-medium'>{item.title}</p>
      <div className='mt-2 flex items-center gap-2 text-xs text-muted-foreground'>
        <span className='font-mono'>{item.primary_issue_key}</span>
        <span>·</span>
        <span>{humanise(item.exception_type)}</span>
        {item.status === 'resolved' && (
          <>
            <span>·</span>
            <span className='text-emerald-600 dark:text-emerald-400'>
              {humanise(item.resolution)}
            </span>
          </>
        )}
      </div>
    </button>
  )
}

// =============================================================================
// DETAIL
// =============================================================================

function Detail({
  detail,
  onResolved,
}: {
  detail: ExceptionDetail
  onResolved: (message: string) => void
}) {
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const item = detail.exception
  const gate = detail.context?.gate
  const incident = detail.context?.incident
  const resolved = item.status === 'resolved'

  async function decide(resolution: 'approved' | 'modified' | 'rejected') {
    setBusy(resolution)
    setError(null)
    try {
      const res = await workbench.resolve(item.id, {
        resolution,
        notes: notes || undefined,
        resolved_by: 'Command Center',
        rerun: resolution !== 'rejected',
      })
      onResolved(res.message)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not record the decision')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className='space-y-4'>
      <div>
        <div className='flex flex-wrap items-center gap-2'>
          <Pill className={severityClasses(item.severity)}>{humanise(item.severity)}</Pill>
          <Pill className='border-border bg-muted text-muted-foreground'>
            {humanise(item.exception_type)}
          </Pill>
          {gate?.policy_conflict && (
            <Pill className='border-purple-500/20 bg-purple-500/10 text-purple-600 dark:text-purple-400'>
              Policy conflict
            </Pill>
          )}
          {gate?.prior_rollback && (
            <Pill className='border-red-500/20 bg-red-500/10 text-red-600 dark:text-red-400'>
              Previously rolled back
            </Pill>
          )}
        </div>
        <h2 className='mt-3 text-xl font-semibold'>{item.title}</h2>
        {item.recommendation && (
          <p className='mt-1 text-sm text-muted-foreground'>{item.recommendation}</p>
        )}
      </div>

      {/* What the gate decided */}
      {gate && (
        <Card>
          <CardHeader className='pb-2'>
            <CardTitle className='flex items-center gap-2 text-base'>
              <Icons.shield className='h-4 w-4' />
              Change gate
              {gate.decision && (
                <Pill className={decisionClasses(gate.decision)}>
                  {gate.decision.toUpperCase()}
                </Pill>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className='pt-0'>
            <Field
              label='Change'
              value={<span className='font-mono'>{gate.change_id ?? '—'}</span>}
            />
            <Field label='Risk' value={gate.risk} />
            <Field label='Status' value={gate.status} />
            <Field label='CAB required' value={gate.cab_approval_required ? 'Yes' : 'No'} />
            <Field label='Approver' value={gate.approver ?? 'Not yet approved'} />
            {(gate.rollback_count ?? 0) > 0 && (
              <Field label='Prior rollbacks' value={gate.rollback_count} />
            )}
          </CardContent>
        </Card>
      )}

      {/* Blast radius, when this ticket is part of an incident */}
      {incident && (
        <Card className='border-red-500/30'>
          <CardHeader className='pb-2'>
            <CardTitle className='flex items-center gap-2 text-base'>
              <Icons.alertTriangle className='h-4 w-4 text-red-500' />
              Part of a major incident
              {incident.linked_incident_label && (
                <span className='font-mono text-sm text-muted-foreground'>
                  {incident.linked_incident_label}
                </span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className='pt-0'>
            <div className='grid grid-cols-3 gap-3 pb-3'>
              <div>
                <p className='text-2xl font-semibold tabular-nums'>
                  {incident.ticket_count ?? (incident.child_issue_keys?.length ?? 0) + 1}
                </p>
                <p className='text-xs text-muted-foreground'>tickets</p>
              </div>
              <div>
                <p className='text-2xl font-semibold tabular-nums'>
                  {incident.distinct_reporters ?? '—'}
                </p>
                <p className='text-xs text-muted-foreground'>reporters</p>
              </div>
              <div>
                <p className='text-2xl font-semibold tabular-nums'>
                  {incident.affected_assignment_groups?.length ?? '—'}
                </p>
                <p className='text-xs text-muted-foreground'>teams</p>
              </div>
            </div>
            <Field
              label='Parent'
              value={<span className='font-mono'>{incident.parent_issue_key}</span>}
            />
            {incident.affected_assignment_groups?.length ? (
              <Field label='Teams' value={incident.affected_assignment_groups.join(', ')} />
            ) : null}
            {incident.rationale && <Field label='Why' value={incident.rationale} />}
          </CardContent>
        </Card>
      )}

      {/* Decision */}
      {resolved ? (
        <Card className='border-emerald-500/30 bg-emerald-500/5'>
          <CardContent className='p-4'>
            <p className='flex items-center gap-2 text-sm font-medium'>
              <Icons.checkCircle className='h-4 w-4 text-emerald-600' />
              {humanise(item.resolution)} by {item.resolved_by} · {relativeTime(item.resolved_at)}
            </p>
            {item.resolution_notes && (
              <p className='mt-2 text-sm text-muted-foreground'>{item.resolution_notes}</p>
            )}
            {item.follow_up_run_id && (
              <p className='mt-2 font-mono text-xs text-muted-foreground'>
                follow-up run {item.follow_up_run_id.slice(0, 8)}
              </p>
            )}
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader className='pb-2'>
            <CardTitle className='text-base'>Your decision</CardTitle>
          </CardHeader>
          <CardContent className='space-y-3 pt-0'>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder='Why — recorded against the change and the audit trail'
              rows={3}
              className='w-full rounded-md border border-input bg-background p-2 text-sm'
            />
            {error && <p className='text-sm text-red-600'>{error}</p>}
            <div className='flex flex-wrap gap-2'>
              <Button onClick={() => decide('approved')} disabled={!!busy}>
                {busy === 'approved' ? (
                  <Icons.loader className='mr-2 h-4 w-4 animate-spin' />
                ) : (
                  <Icons.check className='mr-2 h-4 w-4' />
                )}
                Approve &amp; continue
              </Button>
              <Button variant='outline' onClick={() => decide('modified')} disabled={!!busy}>
                <Icons.pencil className='mr-2 h-4 w-4' />
                Approve with changes
              </Button>
              <Button variant='outline' onClick={() => decide('rejected')} disabled={!!busy}>
                <Icons.close className='mr-2 h-4 w-4' />
                Reject
              </Button>
            </div>
            <p className='text-xs text-muted-foreground'>
              Approving records the decision against{' '}
              <span className='font-mono'>{gate?.change_id ?? 'the change record'}</span> and
              re-runs the agent, which will then be permitted to proceed.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Policy values in force when the agent decided */}
      {detail.context?.policies_at_run && (
        <details className='rounded-lg border border-border p-3'>
          <summary className='cursor-pointer text-sm font-medium'>
            Policies in force at decision time
          </summary>
          <div className='mt-2 max-h-64 overflow-auto'>
            {Object.entries(detail.context.policies_at_run).map(([k, v]) => (
              <Field
                key={k}
                label={k}
                value={<span className='font-mono text-xs'>{String(v)}</span>}
              />
            ))}
          </div>
        </details>
      )}
    </div>
  )
}

// =============================================================================
// PAGE
// =============================================================================

export default function WorkbenchPage() {
  const [items, setItems] = useState<ExceptionItem[]>([])
  const [stats, setStats] = useState<ExceptionStats | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<ExceptionDetail | null>(null)
  const [showResolved, setShowResolved] = useState(false)
  const [loading, setLoading] = useState(true)
  const [toast, setToast] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [list, s] = await Promise.all([
        workbench.list(showResolved ? undefined : 'open'),
        workbench.stats(),
      ])
      setItems(list)
      setStats(s)
      setError(null)
      setSelected((cur) => cur ?? list[0]?.id ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not reach the Command Center API')
    } finally {
      setLoading(false)
    }
  }, [showResolved])

  useEffect(() => {
    refresh()
    // The queue fills while the agent runs, so poll rather than making the
    // reviewer refresh by hand.
    const t = setInterval(refresh, 15000)
    return () => clearInterval(t)
  }, [refresh])

  useEffect(() => {
    if (selected == null) {
      setDetail(null)
      return
    }
    workbench
      .get(selected)
      .then(setDetail)
      .catch(() => setDetail(null))
  }, [selected])

  return (
    <div className='space-y-6 p-6'>
      <div className='flex flex-wrap items-end justify-between gap-4'>
        <div>
          <h1 className='text-2xl font-semibold'>AI Workbench</h1>
          <p className='text-sm text-muted-foreground'>
            Decisions the agent should not make alone.
          </p>
        </div>
        <div className='flex items-center gap-2'>
          <Button variant='outline' size='sm' onClick={() => setShowResolved((v) => !v)}>
            {showResolved ? 'Open only' : 'Show resolved'}
          </Button>
          <Button variant='outline' size='sm' onClick={refresh}>
            <Icons.refresh className='mr-2 h-4 w-4' />
            Refresh
          </Button>
        </div>
      </div>

      {stats && (
        <div className='grid gap-3 sm:grid-cols-2 lg:grid-cols-4'>
          <StatTile label='Open' value={stats.open} tone={stats.open > 0 ? 'critical' : 'good'} />
          <StatTile label='Resolved' value={stats.resolved} tone='good' />
          <StatTile label='Critical' value={stats.by_severity?.critical ?? 0} />
          <StatTile label='CAB required' value={stats.by_type?.cab_required ?? 0} />
        </div>
      )}

      {error && (
        <Card className='border-red-500/30 bg-red-500/5'>
          <CardContent className='p-4 text-sm text-red-600'>{error}</CardContent>
        </Card>
      )}

      {toast && (
        <Card className='border-emerald-500/30 bg-emerald-500/5'>
          <CardContent className='flex items-center justify-between p-4 text-sm'>
            <span>{toast}</span>
            <button onClick={() => setToast(null)} className='text-muted-foreground'>
              <Icons.close className='h-4 w-4' />
            </button>
          </CardContent>
        </Card>
      )}

      <div className='grid gap-6 lg:grid-cols-[360px_1fr]'>
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className='space-y-2'>
          {loading && <p className='text-sm text-muted-foreground'>Loading queue…</p>}
          {!loading && items.length === 0 && (
            <Card>
              <CardContent className='p-6 text-center'>
                <Icons.checkCircle className='mx-auto h-8 w-8 text-emerald-500' />
                <p className='mt-2 text-sm font-medium'>Queue is clear</p>
                <p className='mt-1 text-xs text-muted-foreground'>
                  Nothing is waiting on a human. Trigger a run and exceptions will appear here.
                </p>
              </CardContent>
            </Card>
          )}
          {items.map((item) => (
            <QueueRow
              key={item.id}
              item={item}
              active={item.id === selected}
              onSelect={() => setSelected(item.id)}
            />
          ))}
        </motion.div>

        <div>
          {detail ? (
            <Detail
              detail={detail}
              onResolved={(message) => {
                setToast(message)
                refresh()
                workbench
                  .get(detail.exception.id)
                  .then(setDetail)
                  .catch(() => {})
              }}
            />
          ) : (
            <Card>
              <CardContent className='p-10 text-center text-sm text-muted-foreground'>
                Select an item to review it.
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
