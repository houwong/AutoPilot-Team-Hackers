'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Icons } from '@/components/ui/icons'
import {
  humanise,
  queue,
  relativeTime,
  type QueueCampaign,
  type QueueItem,
} from '@/lib/command-center'

const terminalStates = new Set([
  'auto_remediated',
  'human_approved',
  'blocked',
  'human_rejected',
  'failed',
  'completed_unknown',
  'skipped_closed',
])

function stateClass(state: string) {
  if (state === 'auto_remediated' || state === 'human_approved') {
    return 'bg-emerald-500/10 text-emerald-700'
  }
  if (state === 'awaiting_human' || state === 'pending' || state === 'running') {
    return 'bg-amber-500/10 text-amber-700'
  }
  if (state === 'blocked' || state === 'human_rejected' || state === 'failed') {
    return 'bg-red-500/10 text-red-700'
  }
  return 'bg-slate-500/10 text-slate-700'
}

/**
 * Did the service-desk ticket actually change?
 *
 * `state` says which path the agent took; this says whether Supabase agrees. A
 * campaign once reported ITSM-2003 as `human_approved` while the ticket still
 * read "Waiting for support" with nothing written — the run had genuinely
 * completed its notification branch, and no screen showed the difference.
 *
 * `not_applicable` is deliberately neutral rather than green: nothing was
 * written because nothing should have been, which is a correct outcome but not
 * the same claim as "we changed the ticket".
 */
function VerificationBadge({ item }: { item: QueueItem }) {
  if (!item.verification) return null
  const styles: Record<string, string> = {
    verified: 'bg-emerald-500/10 text-emerald-700',
    verification_failed: 'bg-red-500/10 text-red-700',
    not_applicable: 'bg-slate-500/10 text-slate-600',
    unknown: 'bg-amber-500/10 text-amber-700',
  }
  const labels: Record<string, string> = {
    verified: 'Ticket verified',
    verification_failed: 'Ticket did NOT change',
    not_applicable: 'No ticket change expected',
    unknown: 'Not verified',
  }
  return (
    <span
      className={`mt-1 inline-block rounded-full px-2 py-0.5 text-[11px] font-medium ${
        styles[item.verification] ?? styles.unknown
      }`}
      title={item.verification_detail?.reason ?? undefined}
    >
      {labels[item.verification] ?? item.verification}
    </span>
  )
}

export default function ProcessedTicketsPage() {
  const [campaign, setCampaign] = useState<QueueCampaign | null>(null)
  const [preview, setPreview] = useState<{ campaign: QueueCampaign; items: QueueItem[] } | null>(null)
  const [items, setItems] = useState<QueueItem[]>([])
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [active, history] = await Promise.all([
        queue.active(),
        queue.items({ search: search.trim() || undefined }),
      ])
      setCampaign(active.campaign)
      setItems(history.items)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load queue history')
    }
  }, [search])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, 10000)
    return () => clearInterval(timer)
  }, [refresh])

  async function action(fn: () => Promise<unknown>) {
    setBusy(true)
    try {
      await fn()
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Queue action failed')
    } finally {
      setBusy(false)
    }
  }

  async function makePreview() {
    setBusy(true)
    try {
      const result = await queue.preview(10)
      setPreview({ campaign: result.campaign, items: result.items })
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create preview')
    } finally {
      setBusy(false)
    }
  }

  const counts = campaign?.counts ?? {}
  const total = counts.total ?? 0
  const done = counts.processed ?? 0
  const running = counts.running ?? 0
  const waitingOnPeople = counts.awaiting_human ?? 0
  const queued = counts.pending ?? 0
  const needsAttention =
    waitingOnPeople + (counts.blocked ?? 0) + (counts.failed ?? 0) + (counts.completed_unknown ?? 0)

  return (
    <div className='space-y-10'>
      <div className='flex flex-wrap items-end justify-between gap-4'>
        <div>
          <h1 className='text-display-3 font-bold tracking-tight text-brand-navy'>Processed Tickets</h1>
          <p className='mt-2 max-w-xl text-muted-foreground'>
            One ticket at a time. Nothing runs until you confirm the batch.
          </p>
        </div>
        <Link href='/'><Button variant='outline'>Back to Dashboard</Button></Link>
      </div>

      {error && (
        <div className='rounded-lg border border-red-500/30 bg-red-500/5 p-4 text-sm text-red-700'>{error}</div>
      )}

      {/* The batch, as one reading.
          This replaced eight equal-weight counters. Total and Failed had the
          same visual weight, so the page answered "here are some numbers"
          rather than the two questions an operator actually arrives with: is it
          moving, and does anything need me. The bar carries composition; only
          non-zero exceptions get called out. */}
      <section className='border-t pt-6'>
        <div className='flex flex-wrap items-baseline justify-between gap-4'>
          <div className='flex items-baseline gap-3'>
            <span className='text-sm font-semibold uppercase tracking-wide text-brand-navy'>
              {campaign ? humanise(campaign.status) : 'No batch running'}
            </span>
            {total > 0 && (
              <span className='text-sm text-muted-foreground'>batch of {total}</span>
            )}
            {campaign?.planner_mode && campaign.planner_mode !== 'legacy' && (
              <span
                className={`rounded-full px-2 py-1 text-xs font-medium ${
                  campaign.planner_stale
                    ? 'bg-amber-500/10 text-amber-800'
                    : 'bg-brand-cornflower/10 text-brand-cornflower'
                }`}
              >
                {campaign.planner_stale
                  ? 'Queue Planner evidence is stale'
                  : 'Ranked by Queue Planner'}
                {campaign.planner_run_id && (
                  <span className='ml-1 font-mono'>run {campaign.planner_run_id.slice(0, 8)}</span>
                )}
              </span>
            )}
          </div>
          <div className='flex flex-wrap gap-2'>
            {!preview && !campaign && (
              <Button onClick={makePreview} disabled={busy}>
                <Icons.listFilter className='mr-2 h-4 w-4' />Preview next 10
              </Button>
            )}
            {preview && (
              <Button onClick={() => action(() => queue.confirm(preview.campaign.id))} disabled={busy}>
                <Icons.arrowRight className='mr-2 h-4 w-4' />Confirm and start
              </Button>
            )}
            {campaign?.status === 'running' && <Button onClick={() => action(() => queue.tick())} disabled={busy}>Process next now</Button>}
            {campaign?.status === 'running' && <Button variant='outline' onClick={() => action(() => queue.pause(campaign.id))} disabled={busy}>Pause</Button>}
            {campaign?.status === 'paused' && <Button onClick={() => action(() => queue.resume(campaign.id))} disabled={busy}>Resume</Button>}
            {(campaign?.status === 'running' || campaign?.status === 'paused') && <Button variant='ghost' onClick={() => action(() => queue.cancel(campaign.id))} disabled={busy}>Cancel pending</Button>}
          </div>
        </div>

        {total > 0 ? (
          <>
            <div className='mt-4 flex h-2 w-full overflow-hidden rounded-full bg-muted'>
              {done > 0 && <div className='bg-emerald-500' style={{ width: `${(done / total) * 100}%` }} />}
              {running > 0 && <div className='bg-brand-cornflower' style={{ width: `${(running / total) * 100}%` }} />}
              {waitingOnPeople > 0 && <div className='bg-amber-500' style={{ width: `${(waitingOnPeople / total) * 100}%` }} />}
            </div>
            <p className='mt-2 text-sm text-muted-foreground'>
              <span className='font-semibold text-brand-navy tabular-nums'>{done}</span> done
              {running > 0 && <> · <span className='tabular-nums'>{running}</span> running</>}
              {waitingOnPeople > 0 && <> · <span className='tabular-nums'>{waitingOnPeople}</span> waiting on a person</>}
              {queued > 0 && <> · <span className='tabular-nums'>{queued}</span> queued</>}
            </p>
          </>
        ) : (
          <p className='mt-3 max-w-xl text-sm text-muted-foreground'>
            Preview builds a batch ranked by SLA state, not stored priority. Nothing
            is sent to Supervity and nothing is written until you confirm it.
          </p>
        )}

        {needsAttention > 0 && (
          <Link
            href='/workbench'
            className='mt-4 inline-flex items-center gap-2 text-sm font-medium text-amber-700 hover:underline'
          >
            <Icons.alertTriangle className='h-4 w-4' />
            {needsAttention} {needsAttention === 1 ? 'ticket needs' : 'tickets need'} a decision
            <Icons.arrowRight className='h-3.5 w-3.5' />
          </Link>
        )}

        {campaign?.last_tick_at && (
          <p className='mt-4 text-xs text-muted-foreground'>
            Last scheduler tick {relativeTime(campaign.last_tick_at)}. One run at a time — a
            ticket already in flight is never started twice.
          </p>
        )}
      </section>

      {preview && (
        <Card className='border-brand-cornflower/40 bg-brand-cornflower/5'>
          <CardHeader><CardTitle className='text-base'>Preview — confirmation required</CardTitle></CardHeader>
          <CardContent>
            <p className='mb-3 text-sm text-muted-foreground'>No Supervity run has started. Confirming this list will process these exact tickets and may update live Supabase records.</p>
            {preview.campaign.planner_stale && (
              <p className='mb-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-800'>This preview uses a stale Queue Planner result. Confirm only if the frozen evidence is still acceptable.</p>
            )}
            {/* Show the order's frozen planner evidence, not just the keys. A
                breached Low-priority ticket can outrank a Highest ticket within
                target; the frozen evidence makes that intentional order reviewable.
                */}
            <ol className='space-y-1'>
              {preview.items.map((item, i) => (
                <li key={item.issue_key} className='flex flex-wrap items-baseline gap-2 text-xs'>
                  <span className='w-5 tabular-nums text-muted-foreground'>{item.rank_position ?? i + 1}.</span>
                  <span className='rounded-md border bg-background px-2 py-1 font-mono'>{item.issue_key}</span>
                  <span className='text-muted-foreground'>stored {item.source_priority ?? 'unknown'}</span>
                  {typeof item.ranking_evidence?.major_incident_key === 'string' && (
                    <span className='rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-800'>
                      {item.ranking_evidence.major_incident_action === 'attach_to_existing' ? 'Incident' : 'Major incident'} {item.ranking_evidence.major_incident_key} · {typeof item.ranking_evidence.incident_ticket_count === 'number' ? item.ranking_evidence.incident_ticket_count : 0} tickets
                    </span>
                  )}
                  {item.ranking_reason && (
                    <span className='text-brand-cornflower'>— {item.ranking_reason}</span>
                  )}
                </li>
              ))}
            </ol>
            {preview.items.some((i) => i.ranked_by === 'source_priority') && (
              <p className='mt-2 text-xs text-amber-700'>
                Operator 1 was unreachable, so this batch is ordered by the stored
                Priority column rather than by SLA state.
              </p>
            )}
            {preview.items.length === 0 && <p className='text-sm text-muted-foreground'>No eligible tickets were found.</p>}
            <Button variant='ghost' className='mt-3' onClick={() => setPreview(null)}>Discard preview</Button>
          </CardContent>
        </Card>
      )}

      {/* History as rows, not a seven-column table.
          The table gave Attempts and Run id the same weight as the outcome, so
          the eye had to hunt for the one thing that matters. Each ticket is now
          a row that reads in one line — key, what happened, whether the ticket
          actually changed — with the run id and attempt count demoted to the
          metadata they are. */}
      <section className='border-t pt-6'>
        <div className='flex flex-wrap items-end justify-between gap-4'>
          <div>
            <h2 className='text-lg font-semibold text-brand-navy'>History</h2>
            <p className='mt-1 text-sm text-muted-foreground'>
              A finished ticket never re-enters a batch unless someone requeues it with a reason.
            </p>
          </div>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder='Search issue key'
            aria-label='Search processed tickets by issue key'
            className='h-9 w-48 rounded-md border border-input bg-background px-3 font-mono text-xs'
          />
        </div>

        <ul className='mt-5 divide-y'>
          {items.map((item) => (
            <li key={item.id} className='group flex flex-wrap items-start gap-x-6 gap-y-2 py-4'>
              <div className='min-w-[8rem]'>
                <p className='font-mono text-sm font-semibold text-brand-navy'>{item.issue_key}</p>
                <p className='mt-0.5 text-xs text-muted-foreground'>
                  {item.completed_at ? relativeTime(item.completed_at) : 'in flight'}
                </p>
              </div>

              <div className='min-w-[12rem] flex-1'>
                <div className='flex flex-wrap items-center gap-2'>
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${stateClass(item.state)}`}>
                    {humanise(item.state)}
                  </span>
                  <VerificationBadge item={item} />
                </div>
                {/* Why it was ranked here. The stored priority is deliberately
                    not what decided the order, so showing one without the other
                    reads as a bug. */}
                {item.ranking_reason && (
                  <p className='mt-1.5 text-xs text-brand-cornflower'>{item.ranking_reason}</p>
                )}
                {item.last_error && (
                  <p className='mt-1.5 max-w-prose text-xs text-red-700'>{item.last_error}</p>
                )}
              </div>

              <div className='flex items-center gap-4 text-xs text-muted-foreground'>
                {item.attempt_count > 1 && (
                  <span title={`${item.attempt_count} attempts`}>{item.attempt_count}×</span>
                )}
                {item.latest_run_id && (
                  <span className='font-mono' title={item.latest_run_id}>
                    {item.latest_run_id.slice(0, 8)}
                  </span>
                )}
                {item.id > 0 && terminalStates.has(item.state) && (
                  <Button
                    variant='ghost'
                    size='sm'
                    disabled={busy}
                    className='opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100'
                    onClick={() => {
                      const reason = window.prompt(`Why requeue ${item.issue_key}?`)
                      if (reason?.trim()) action(() => queue.requeue(item.id, reason.trim()))
                    }}
                  >
                    Requeue
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>

        {items.length === 0 && (
          <div className='py-14 text-center'>
            <p className='text-sm font-medium text-brand-navy'>Nothing processed yet</p>
            <p className='mx-auto mt-1 max-w-sm text-sm text-muted-foreground'>
              Preview a batch to see which tickets the agent would take next, and why
              each one is ranked where it is.
            </p>
          </div>
        )}
      </section>
    </div>
  )
}
