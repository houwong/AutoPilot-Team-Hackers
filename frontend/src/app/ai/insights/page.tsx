'use client'

/**
 * AI Insights — patterns a person would not assemble by hand.
 *
 * This page replaced a version built on hardcoded demo insights. Every finding
 * here is computed from data the agent actually processed or from the current
 * state of the system of record, and each carries the evidence it was derived
 * from so a reader can check the claim rather than take it on trust.
 *
 * An insight that cannot be acted on is not worth showing, so each one ends in
 * a specific next step.
 */

import { useCallback, useEffect, useState } from 'react'
import { motion } from 'framer-motion'

import { apiClient } from '@/lib/api-client'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Icons } from '@/components/ui/icons'
import { cn } from '@/lib/utils'
import { relativeTime, severityClasses, type Severity } from '@/lib/command-center'

// =============================================================================
// TYPES
// =============================================================================

interface InsightRow {
  id: number
  insight_type: string | null
  severity: Severity | null
  title: string
  body: string | null
  evidence: Record<string, unknown> | null
  confidence: number | null
  action_path: string | null
  action_type: string | null
  dismissed: boolean | null
  generated_at: string | null
}

interface Stats {
  total: number
  by_severity: Record<string, number>
  by_type: Record<string, number>
  actionable: number
  by_action: Record<string, number>
  last_generated_at: string | null
}

const ACTION_LABEL: Record<string, string> = {
  author_kb_article: 'Author a KB article',
  open_incident: 'Declare a major incident',
  create_policy: 'Change a policy',
  investigate: 'Investigate',
  none: '',
}

const TYPE_ICON: Record<string, React.ElementType> = {
  pattern: Icons.trendingUp,
  anomaly: Icons.alertTriangle,
  recommendation: Icons.lightbulb,
}

// =============================================================================
// EVIDENCE
// =============================================================================

function EvidenceValue({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className='text-muted-foreground'>none</span>
    if (typeof value[0] === 'object') {
      return (
        <div className='space-y-1'>
          {(value as Record<string, unknown>[]).slice(0, 6).map((row, i) => (
            <div key={i} className='flex flex-wrap gap-x-3 font-mono text-xs'>
              {Object.entries(row).map(([k, v]) => (
                <span key={k}>
                  <span className='text-muted-foreground'>{k}=</span>
                  {String(v)}
                </span>
              ))}
            </div>
          ))}
        </div>
      )
    }
    return <span className='font-mono text-xs'>{value.slice(0, 12).join(', ')}</span>
  }
  if (value && typeof value === 'object') {
    return (
      <span className='font-mono text-xs'>
        {Object.entries(value as Record<string, unknown>)
          .map(([k, v]) => `${k}: ${v}`)
          .join('  ')}
      </span>
    )
  }
  return <span className='font-mono text-xs'>{String(value)}</span>
}

function Evidence({ evidence }: { evidence: Record<string, unknown> }) {
  return (
    <details className='mt-3 rounded-md border border-border/60 p-3'>
      <summary className='cursor-pointer text-xs font-medium text-muted-foreground'>
        Evidence
      </summary>
      <div className='mt-2 space-y-2'>
        {Object.entries(evidence).map(([k, v]) => (
          <div key={k} className='grid gap-1 sm:grid-cols-[160px_1fr]'>
            <span className='text-xs text-muted-foreground'>{k.replace(/_/g, ' ')}</span>
            <EvidenceValue value={v} />
          </div>
        ))}
      </div>
    </details>
  )
}

// =============================================================================
// ONE INSIGHT
// =============================================================================

function InsightCard({
  insight,
  onDismissed,
}: {
  insight: InsightRow
  onDismissed: () => void
}) {
  const [busy, setBusy] = useState(false)
  const Icon = TYPE_ICON[insight.insight_type ?? 'pattern'] ?? Icons.info
  const action = ACTION_LABEL[insight.action_type ?? 'none']

  async function dismiss() {
    setBusy(true)
    try {
      await apiClient.post(`/api/insights/${insight.id}/dismiss`)
      onDismissed()
    } finally {
      setBusy(false)
    }
  }

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
      <Card
        className={cn(
          'h-full',
          insight.severity === 'critical' && 'border-red-500/40',
          insight.severity === 'warning' && 'border-amber-500/40'
        )}
      >
        <CardContent className='space-y-3 p-5'>
          <div className='flex items-start justify-between gap-3'>
            <div className='flex items-start gap-3'>
              <div
                className={cn(
                  'mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border',
                  severityClasses(insight.severity)
                )}
              >
                <Icon className='h-4 w-4' />
              </div>
              <div className='min-w-0'>
                <h3 className='text-sm font-semibold leading-snug'>{insight.title}</h3>
                <div className='mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground'>
                  <span className='capitalize'>{insight.insight_type}</span>
                  {insight.confidence != null && (
                    <>
                      <span>·</span>
                      <span>{Math.round(insight.confidence * 100)}% confidence</span>
                    </>
                  )}
                </div>
              </div>
            </div>
            <button
              onClick={dismiss}
              disabled={busy}
              className='shrink-0 text-muted-foreground hover:text-foreground'
              aria-label='dismiss'
            >
              <Icons.close className='h-4 w-4' />
            </button>
          </div>

          {insight.body && (
            <p className='text-sm leading-relaxed text-muted-foreground'>{insight.body}</p>
          )}

          {insight.action_path && (
            <div className='rounded-md border border-brand-cornflower/30 bg-brand-cornflower/5 p-3'>
              {action && <p className='text-xs font-medium text-brand-cornflower'>{action}</p>}
              <p className='mt-1 text-sm'>{insight.action_path}</p>
            </div>
          )}

          {insight.evidence && Object.keys(insight.evidence).length > 0 && (
            <Evidence evidence={insight.evidence} />
          )}
        </CardContent>
      </Card>
    </motion.div>
  )
}

// =============================================================================
// PAGE
// =============================================================================

export default function InsightsPage() {
  const [insights, setInsights] = useState<InsightRow[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [rows, s] = await Promise.all([
        apiClient.get<InsightRow[]>('/api/insights'),
        apiClient.get<Stats>('/api/insights/stats/summary'),
      ])
      setInsights(rows)
      setStats(s)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not reach the Command Center API')
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  async function generate() {
    setGenerating(true)
    setError(null)
    try {
      // The data pack is from July 2026; anchoring the SLA maths to it keeps
      // the findings meaningful instead of reporting everything as breached.
      await apiClient.post('/api/insights/generate?as_of=2026-07-25T00:00:00Z')
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not generate insights')
    } finally {
      setGenerating(false)
    }
  }

  const shown = filter ? insights.filter((i) => i.severity === filter) : insights

  return (
    <div className='space-y-6'>
      <div className='flex flex-wrap items-end justify-between gap-4'>
        <div>
          <h1 className='text-2xl font-semibold'>AI Insights</h1>
          <p className='mt-1 text-sm text-muted-foreground'>
            Patterns computed from what the agent processed and the current state of the
            service desk. Every finding shows its evidence.
          </p>
        </div>
        <Button onClick={generate} disabled={generating}>
          {generating ? (
            <Icons.loader className='mr-2 h-4 w-4 animate-spin' />
          ) : (
            <Icons.sparkles className='mr-2 h-4 w-4' />
          )}
          Regenerate
        </Button>
      </div>

      {error && (
        <Card className='border-red-500/30 bg-red-500/5'>
          <CardContent className='p-4 text-sm text-red-600'>{error}</CardContent>
        </Card>
      )}

      <div className='grid gap-3 sm:grid-cols-2 lg:grid-cols-4'>
        {(
          [
            ['Findings', stats?.total ?? 0, null],
            ['Critical', stats?.by_severity?.critical ?? 0, 'critical'],
            ['Warnings', stats?.by_severity?.warning ?? 0, 'warning'],
            ['Actionable', stats?.actionable ?? 0, null],
          ] as [string, number, string | null][]
        ).map(([label, n, sev]) => (
          <Card
            key={label}
            className={cn(
              sev && 'cursor-pointer transition-colors',
              sev && filter === sev && 'border-primary'
            )}
            onClick={() => sev && setFilter(filter === sev ? null : sev)}
          >
            <CardContent className='p-4'>
              <p className='text-xs uppercase tracking-wide text-muted-foreground'>{label}</p>
              <p
                className={cn(
                  'mt-1 text-2xl font-semibold tabular-nums',
                  sev === 'critical' && 'text-red-600'
                )}
              >
                {n}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>

      {stats?.last_generated_at && (
        <p className='text-xs text-muted-foreground'>
          Last computed {relativeTime(stats.last_generated_at)}
          {filter && (
            <>
              {' · '}
              <button className='underline' onClick={() => setFilter(null)}>
                clear filter
              </button>
            </>
          )}
        </p>
      )}

      {shown.length === 0 ? (
        <Card>
          <CardContent className='p-10 text-center'>
            <Icons.sparkles className='mx-auto h-8 w-8 text-muted-foreground' />
            <p className='mt-3 text-sm font-medium'>Nothing to report yet</p>
            <p className='mt-1 text-xs text-muted-foreground'>
              Use Regenerate to compute findings from the current backlog and everything the
              agent has processed.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className='grid gap-4 lg:grid-cols-2'>
          {shown.map((i) => (
            <InsightCard key={i.id} insight={i} onDismissed={refresh} />
          ))}
        </div>
      )}
    </div>
  )
}
