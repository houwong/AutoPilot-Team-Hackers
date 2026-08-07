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

function Counter({ label, value }: { label: string; value: number }) {
  return (
    <div className='rounded-lg border bg-background/60 p-3'>
      <p className='text-[10px] font-semibold uppercase tracking-wider text-muted-foreground'>{label}</p>
      <p className='mt-1 text-2xl font-semibold tabular-nums'>{value}</p>
    </div>
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

  return (
    <div className='space-y-6'>
      <div className='flex flex-wrap items-end justify-between gap-4'>
        <div>
          <p className='text-micro uppercase tracking-widest text-brand-muted'>Command Center</p>
          <h1 className='text-display-3 font-bold tracking-tight text-brand-navy'>Processed Tickets</h1>
          <p className='mt-2 max-w-2xl text-muted-foreground'>
            Preview a safe batch, process one ticket at a time, and keep a permanent outcome history.
          </p>
        </div>
        <Link href='/'><Button variant='outline'>Back to Dashboard</Button></Link>
      </div>

      {error && <Card className='border-red-500/30 bg-red-500/5'><CardContent className='p-4 text-sm text-red-600'>{error}</CardContent></Card>}

      <Card>
        <CardHeader className='flex flex-row items-center justify-between space-y-0'>
          <div>
            <CardTitle className='text-base'>Queue control</CardTitle>
            <p className='mt-1 text-xs text-muted-foreground'>The scheduler checks every five minutes and starts at most one run.</p>
          </div>
          <span className={`rounded-full px-3 py-1 text-xs font-semibold ${campaign ? stateClass(campaign.status) : 'bg-slate-500/10 text-slate-700'}`}>
            {campaign ? humanise(campaign.status) : 'No active campaign'}
          </span>
        </CardHeader>
        <CardContent className='space-y-4'>
          <div className='grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-8'>
            <Counter label='Total' value={counts.total ?? 0} />
            <Counter label='Pending' value={counts.pending ?? 0} />
            <Counter label='Running' value={counts.running ?? 0} />
            <Counter label='Human' value={counts.awaiting_human ?? 0} />
            <Counter label='Auto' value={counts.auto_remediated ?? 0} />
            <Counter label='Approved' value={counts.human_approved ?? 0} />
            <Counter label='Blocked' value={counts.blocked ?? 0} />
            <Counter label='Failed' value={(counts.failed ?? 0) + (counts.completed_unknown ?? 0)} />
          </div>
          <div className='flex flex-wrap gap-2'>
            {!preview && <Button onClick={makePreview} disabled={busy}><Icons.listFilter className='mr-2 h-4 w-4' />Preview next 10</Button>}
            {preview && (
              <Button onClick={() => action(() => queue.confirm(preview.campaign.id))} disabled={busy}>
                <Icons.arrowRight className='mr-2 h-4 w-4' />Confirm and start
              </Button>
            )}
            {campaign?.status === 'running' && <Button variant='outline' onClick={() => action(() => queue.pause(campaign.id))} disabled={busy}>Pause</Button>}
            {campaign?.status === 'paused' && <Button variant='outline' onClick={() => action(() => queue.resume(campaign.id))} disabled={busy}>Resume</Button>}
            {campaign?.status === 'running' && <Button variant='outline' onClick={() => action(() => queue.tick())} disabled={busy}>Process next now</Button>}
            {(campaign?.status === 'running' || campaign?.status === 'paused') && <Button variant='ghost' onClick={() => action(() => queue.cancel(campaign.id))} disabled={busy}>Cancel pending</Button>}
          </div>
          {campaign?.last_tick_at && <p className='text-xs text-muted-foreground'>Last scheduler tick {relativeTime(campaign.last_tick_at)}. A running ticket is never started twice.</p>}
        </CardContent>
      </Card>

      {preview && (
        <Card className='border-brand-cornflower/40 bg-brand-cornflower/5'>
          <CardHeader><CardTitle className='text-base'>Preview — confirmation required</CardTitle></CardHeader>
          <CardContent>
            <p className='mb-3 text-sm text-muted-foreground'>No Supervity run has started. Confirming this list will process these exact tickets and may update live Supabase records.</p>
            <div className='flex flex-wrap gap-2'>{preview.items.map((item) => <span key={item.issue_key} className='rounded-md border bg-background px-2 py-1 font-mono text-xs'>{item.issue_key}</span>)}</div>
            {preview.items.length === 0 && <p className='text-sm text-muted-foreground'>No eligible tickets were found.</p>}
            <Button variant='ghost' className='mt-3' onClick={() => setPreview(null)}>Discard preview</Button>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className='flex flex-row items-center justify-between space-y-0'>
          <div><CardTitle className='text-base'>Processing history</CardTitle><p className='mt-1 text-xs text-muted-foreground'>Terminal tickets are excluded from future previews unless explicitly requeued.</p></div>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder='Search issue key' className='h-9 w-44 rounded-md border border-input bg-background px-3 font-mono text-xs' />
        </CardHeader>
        <CardContent className='p-0'>
          <div className='overflow-x-auto'>
            <table className='w-full text-left text-sm'>
              <thead className='border-y bg-muted/30 text-xs text-muted-foreground'><tr><th className='px-5 py-3'>Ticket</th><th className='px-5 py-3'>Source</th><th className='px-5 py-3'>State</th><th className='px-5 py-3'>Attempts</th><th className='px-5 py-3'>Run</th><th className='px-5 py-3'>Completed</th><th className='px-5 py-3'></th></tr></thead>
              <tbody>
                {items.map((item) => <tr key={item.id} className='border-b last:border-0'>
                  <td className='px-5 py-3 font-mono font-semibold'>{item.issue_key}</td>
                  <td className='px-5 py-3 text-xs text-muted-foreground'>{item.source_priority ?? 'unknown'} · {item.source_status ?? 'unknown'}</td>
                  <td className='px-5 py-3'><span className={`rounded-full px-2 py-1 text-xs font-medium ${stateClass(item.state)}`}>{humanise(item.state)}</span>{item.last_error && <p className='mt-1 max-w-xs text-xs text-red-600'>{item.last_error}</p>}</td>
                  <td className='px-5 py-3 tabular-nums'>{item.attempt_count}</td>
                  <td className='px-5 py-3'>{item.latest_run_id ? <span className='font-mono text-xs text-brand-cornflower' title={item.latest_run_id}>{item.latest_run_id.slice(0, 8)}</span> : '—'}</td>
                  <td className='px-5 py-3 text-xs text-muted-foreground'>{item.completed_at ? relativeTime(item.completed_at) : '—'}</td>
                  <td className='px-5 py-3 text-right'>{item.id > 0 && terminalStates.has(item.state) && <Button variant='ghost' size='sm' disabled={busy} onClick={() => { const reason = window.prompt(`Why requeue ${item.issue_key}?`); if (reason?.trim()) action(() => queue.requeue(item.id, reason.trim())) }}>Requeue</Button>}</td>
                </tr>)}
                {items.length === 0 && <tr><td colSpan={7} className='px-5 py-10 text-center text-sm text-muted-foreground'>No processed tickets recorded yet.</td></tr>}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
