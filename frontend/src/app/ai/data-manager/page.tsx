'use client'

/**
 * Data Manager — a live registry of every system this build is connected to.
 *
 * Health is checked when the page is opened, not on a schedule, and the page
 * says HOW each result was established:
 *
 *   probed    we called the system just now and timed it
 *   observed  the Command Center holds no credentials for it, so status comes
 *             from the last operator run that used it
 *
 * Outlook and Slack are reached by Operator 4 through Auto's managed
 * connections. Showing a green light for something we have not actually checked
 * would be worse than saying how we know.
 */

import { useCallback, useEffect, useState } from 'react'
import { motion } from 'framer-motion'

import { apiClient } from '@/lib/api-client'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Icons } from '@/components/ui/icons'
import { cn } from '@/lib/utils'
import { relativeTime } from '@/lib/command-center'

// =============================================================================
// TYPES
// =============================================================================

type Status = 'healthy' | 'degraded' | 'down' | 'unknown'

interface IntegrationRow {
  name: string
  category: string
  purpose: string
  status: Status
  verification: 'probed' | 'observed' | 'internal'
  latency_ms: number | null
  last_seen_at: string | null
  last_check_at: string
  detail: Record<string, unknown> | null
}

interface Payload {
  integrations: IntegrationRow[]
  summary: {
    total: number
    healthy: number
    degraded: number
    down: number
    unknown: number
    categories_live: string[]
    meets_round2_floor: boolean
  }
}

const CATEGORY_LABEL: Record<string, string> = {
  system_of_record: 'System of record',
  channel: 'Channel',
  human_loop: 'Human loop',
}

const STATUS_STYLE: Record<Status, string> = {
  healthy: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30 dark:text-emerald-400',
  degraded: 'bg-amber-500/10 text-amber-600 border-amber-500/30 dark:text-amber-400',
  down: 'bg-red-500/10 text-red-600 border-red-500/30 dark:text-red-400',
  unknown: 'bg-slate-500/10 text-slate-600 border-slate-500/30 dark:text-slate-400',
}

const DOT: Record<Status, string> = {
  healthy: 'bg-emerald-500',
  degraded: 'bg-amber-500',
  down: 'bg-red-500',
  unknown: 'bg-slate-400',
}

// =============================================================================
// PIECES
// =============================================================================

function IntegrationCard({ row }: { row: IntegrationRow }) {
  return (
    <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
      <Card className='h-full'>
        <CardContent className='space-y-3 p-5'>
          <div className='flex items-start justify-between gap-3'>
            <div className='min-w-0'>
              <div className='flex items-center gap-2'>
                <span className={cn('h-2.5 w-2.5 shrink-0 rounded-full', DOT[row.status])} />
                <h3 className='truncate text-sm font-semibold'>{row.name}</h3>
              </div>
              <p className='mt-1 text-xs text-muted-foreground'>
                {CATEGORY_LABEL[row.category] ?? row.category}
              </p>
            </div>
            <span
              className={cn(
                'shrink-0 rounded-full border px-2 py-0.5 text-xs font-medium capitalize',
                STATUS_STYLE[row.status]
              )}
            >
              {row.status}
            </span>
          </div>

          <p className='text-sm leading-relaxed text-muted-foreground'>{row.purpose}</p>

          <div className='space-y-1 border-t border-border/50 pt-3 text-xs'>
            <div className='flex justify-between'>
              <span className='text-muted-foreground'>Verified by</span>
              <span className='font-medium'>
                {row.verification === 'probed'
                  ? 'direct call'
                  : row.verification === 'observed'
                    ? 'last operator run'
                    : 'internal'}
              </span>
            </div>
            {row.latency_ms != null && (
              <div className='flex justify-between'>
                <span className='text-muted-foreground'>Response</span>
                <span className='font-medium tabular-nums'>{row.latency_ms} ms</span>
              </div>
            )}
            {row.last_seen_at && (
              <div className='flex justify-between'>
                <span className='text-muted-foreground'>Last seen</span>
                <span className='font-medium'>{relativeTime(row.last_seen_at)}</span>
              </div>
            )}
            {row.detail?.note ? (
              <p className='pt-1 text-amber-600'>{String(row.detail.note)}</p>
            ) : null}
            {row.detail?.error ? (
              <p className='pt-1 text-red-600'>{String(row.detail.error)}</p>
            ) : null}
          </div>

          {row.detail && Object.keys(row.detail).length > 0 && (
            <details className='rounded-md border border-border/60 p-2'>
              <summary className='cursor-pointer text-xs text-muted-foreground'>
                Detail
              </summary>
              <div className='mt-2 space-y-1'>
                {Object.entries(row.detail).map(([k, v]) => (
                  <div key={k} className='flex justify-between gap-3 text-xs'>
                    <span className='text-muted-foreground'>{k.replace(/_/g, ' ')}</span>
                    <span className='truncate font-mono'>{String(v)}</span>
                  </div>
                ))}
              </div>
            </details>
          )}
        </CardContent>
      </Card>
    </motion.div>
  )
}

// =============================================================================
// PAGE
// =============================================================================

export default function DataManagerPage() {
  const [data, setData] = useState<Payload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setData(await apiClient.get<Payload>('/api/integrations'))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not reach the Command Center API')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const s = data?.summary

  return (
    <div className='space-y-6'>
      <div className='flex flex-wrap items-end justify-between gap-4'>
        <div>
          <h1 className='text-2xl font-semibold'>Data Manager</h1>
          <p className='mt-1 text-sm text-muted-foreground'>
            Every system this build is connected to, what it is used for, and whether it is
            reachable right now.
          </p>
        </div>
        <Button variant='outline' size='sm' onClick={refresh} disabled={loading}>
          {loading ? (
            <Icons.loader className='mr-2 h-4 w-4 animate-spin' />
          ) : (
            <Icons.refresh className='mr-2 h-4 w-4' />
          )}
          Re-check
        </Button>
      </div>

      {error && (
        <Card className='border-red-500/30 bg-red-500/5'>
          <CardContent className='p-4 text-sm text-red-600'>{error}</CardContent>
        </Card>
      )}

      {s && (
        <>
          {/* One line, not four cards.
              Four summary cards sat above a list of five integrations —
              summarising something short enough to count at a glance, while
              pushing the actual evidence (how each connection was verified)
              below the fold. The evidence is the differentiator here; the
              arithmetic is not. */}
          <p className='text-sm text-muted-foreground'>
            <span className='font-semibold text-brand-navy tabular-nums'>{s.total}</span> connected
            {' · '}
            <span className='font-semibold text-emerald-600 tabular-nums'>{s.healthy}</span> healthy
            {s.degraded > 0 && (
              <>
                {' · '}
                <span className='font-semibold text-amber-600 tabular-nums'>{s.degraded}</span> degraded
              </>
            )}
            {s.unknown > 0 && (
              <>
                {' · '}
                <span className='font-semibold tabular-nums'>{s.unknown}</span> unverified
              </>
            )}
            {' · across '}
            {s.categories_live.length} categories
          </p>

          {!s.meets_round2_floor && (
            <Card className='border-amber-500/40 bg-amber-500/5'>
              <CardContent className='p-4 text-sm'>
                <p className='font-medium'>
                  Not every category has a verified connection yet.
                </p>
                <p className='mt-1 text-muted-foreground'>
                  A channel is only marked healthy once an operator has actually used it.
                  Run the agent through to a notification — auto-resolve a ticket, or
                  approve one in the Workbench — and Outlook and Slack will report in.
                </p>
              </CardContent>
            </Card>
          )}
        </>
      )}

      <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-3'>
        {data?.integrations.map((row) => (
          <IntegrationCard key={row.name} row={row} />
        ))}
      </div>

      {data && (
        <p className='text-xs text-muted-foreground'>
          Checked on load, not on a schedule — a stale green light is worse than none.
        </p>
      )}
    </div>
  )
}
