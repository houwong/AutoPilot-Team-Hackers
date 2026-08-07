'use client'

/**
 * AI Policies — the rules a business owns.
 *
 * Each row is an Auto workflow input. Editing one changes what the agent does on
 * the next run: no code, no redeploy, no restart.
 *
 * This page replaced a version built on hardcoded demo policies. Every rule here
 * is read from the database and passed to the agent on its next run.
 *
 * Evaluations record the value that was in force at the moment of the decision,
 * so a decision made before a rule changed still reads correctly afterwards.
 */

import { useCallback, useEffect, useState } from 'react'
import { motion } from 'framer-motion'

import { apiClient } from '@/lib/api-client'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Icons } from '@/components/ui/icons'
import { cn } from '@/lib/utils'
import { agent, decisionClasses, relativeTime } from '@/lib/command-center'

// =============================================================================
// TYPES
// =============================================================================

interface PolicyRow {
  id: number
  key: string
  name: string
  description: string | null
  value: string | null
  value_type: string | null
  default_value: string | null
  active: boolean | null
  updated_by: string | null
  updated_at: string | null
  group: string | null
  steers: string | null
  is_modified: boolean
}

interface Grouped {
  groups: { name: string; policies: PolicyRow[] }[]
}

interface Evaluation {
  id: number
  policy_key: string | null
  policy_name: string | null
  action: string | null
  issue_key: string | null
  decision: string | null
  reason: string | null
  policy_value_at_eval: string | null
  evaluated_at: string | null
}

// =============================================================================
// ONE RULE
// =============================================================================

function PolicyRule({
  policy,
  onSaved,
}: {
  policy: PolicyRow
  onSaved: (message: string) => void
}) {
  const [value, setValue] = useState(policy.value ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => setValue(policy.value ?? ''), [policy.value])

  const dirty = value !== (policy.value ?? '')
  const isBool = policy.value_type === 'boolean'
  const isLong = (policy.value ?? '').length > 60 || policy.value_type === 'json'

  async function save(next?: string) {
    setBusy(true)
    setError(null)
    try {
      await apiClient.patch(`/api/policies/${policy.key}`, {
        value: next ?? value,
        updated_by: 'Command Center',
      })
      onSaved(`${policy.name} updated — effective on the next run`)
    } catch (e) {
      // Put the field back to what the server still holds, so the page never
      // shows a value the agent will not actually use.
      setValue(policy.value ?? '')
      setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  async function reset() {
    setBusy(true)
    setError(null)
    try {
      await apiClient.post(`/api/policies/${policy.key}/reset`)
      onSaved(`${policy.name} reset to its default`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not reset')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className={cn('h-full', policy.is_modified && 'border-brand-cornflower/60')}>
      <CardContent className='flex h-full flex-col gap-3 p-4'>
        <div className='flex items-start justify-between gap-3'>
          <div className='min-w-0'>
            <p className='text-sm font-medium'>{policy.name}</p>
            <p className='truncate font-mono text-xs text-muted-foreground'>{policy.key}</p>
          </div>
          {policy.is_modified && (
            <span className='shrink-0 rounded-full border border-brand-cornflower/30 bg-brand-cornflower/10 px-2 py-0.5 text-xs text-brand-cornflower'>
              changed
            </span>
          )}
        </div>

        {policy.description && (
          <p className='text-xs leading-relaxed text-muted-foreground'>{policy.description}</p>
        )}

        <div className='mt-auto space-y-2'>
          {isBool ? (
            <div className='flex items-center gap-2'>
              {['true', 'false'].map((opt) => (
                <Button
                  key={opt}
                  size='sm'
                  variant={value === opt ? 'default' : 'outline'}
                  disabled={busy}
                  onClick={() => {
                    setValue(opt)
                    save(opt)
                  }}
                >
                  {opt}
                </Button>
              ))}
            </div>
          ) : isLong ? (
            <textarea
              value={value}
              onChange={(e) => setValue(e.target.value)}
              rows={3}
              className='w-full rounded-md border border-input bg-background p-2 font-mono text-xs'
            />
          ) : (
            <input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              className='w-full rounded-md border border-input bg-background p-2 text-sm'
            />
          )}

          {error && <p className='text-xs text-red-600'>{error}</p>}

          {!isBool && (
            <div className='flex items-center gap-2'>
              <Button size='sm' onClick={() => save()} disabled={!dirty || busy}>
                {busy && <Icons.loader className='mr-2 h-3 w-3 animate-spin' />}
                Save
              </Button>
              {policy.is_modified && (
                <Button size='sm' variant='ghost' onClick={reset} disabled={busy}>
                  Reset
                </Button>
              )}
              {dirty && <span className='text-xs text-amber-600'>unsaved</span>}
            </div>
          )}

          {policy.updated_by && policy.updated_by !== 'seed' && (
            <p className='text-xs text-muted-foreground'>
              changed by {policy.updated_by} · {relativeTime(policy.updated_at)}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

// =============================================================================
// PAGE
// =============================================================================

export default function PoliciesPage() {
  const [grouped, setGrouped] = useState<Grouped | null>(null)
  const [evals, setEvals] = useState<Evaluation[]>([])
  const [toast, setToast] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Run the agent from this page, so a change to a rule can be tried
  // immediately on a chosen ticket. The target is deliberately NOT a policy: a
  // policy persists and applies to every run, whereas this is the scope of one
  // run. Storing it as a rule is what made an older orchestrator carry
  // ITSM-2180 as a default and quietly target the same ticket every time.
  const [target, setTarget] = useState('')
  const [triggering, setTriggering] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [g, e] = await Promise.all([
        apiClient.get<Grouped>('/api/policies/grouped'),
        apiClient.get<Evaluation[]>('/api/policies/evaluations?limit=25'),
      ])
      setGrouped(g)
      setEvals(e)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not reach the Command Center API')
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const runAgent = useCallback(async () => {
    setTriggering(true)
    try {
      const run = await agent.trigger({
        target_issue_key: target.trim() || undefined,
        trigger: 'manual',
      })
      setToast(
        target.trim()
          ? `Run started on ${target.trim()} with the current policy values.`
          : `Run started on the top of the queue — run ${run.run_id.slice(0, 8)}.`
      )
    } catch (err) {
      // A closed ticket is refused with 409 and an unknown one with 404. Show
      // the reason rather than a generic failure: both are the agent declining
      // deliberately, not something going wrong.
      setError(err instanceof Error ? err.message : 'Could not start a run')
    } finally {
      setTriggering(false)
    }
  }, [target])

  const all = grouped?.groups.flatMap((g) => g.policies) ?? []
  const changed = all.filter((p) => p.is_modified).length

  return (
    <div className='space-y-6'>
      <div className='flex flex-wrap items-end justify-between gap-4'>
        <div>
          <h1 className='text-2xl font-semibold'>AI Policies</h1>
          <p className='mt-1 text-sm text-muted-foreground'>
            The rules the agent works inside. Change one and the next run behaves
            differently — no code, no redeploy.
          </p>
        </div>
        <div className='flex flex-wrap items-center gap-2'>
          <input
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !triggering) runAgent()
            }}
            placeholder='e.g. ITSM-2180'
            aria-label='Target ticket key'
            className='h-9 w-56 rounded-md border border-input bg-background px-3 font-mono text-sm'
          />
          <Button onClick={runAgent} disabled={triggering}>
            {triggering ? (
              <Icons.loader className='mr-2 h-4 w-4 animate-spin' />
            ) : (
              <Icons.zap className='mr-2 h-4 w-4' />
            )}
            {target.trim() ? `Run ${target.trim()}` : 'Run with these rules'}
          </Button>
          <Button variant='outline' size='sm' onClick={refresh}>
            <Icons.refresh className='mr-2 h-4 w-4' />
            Refresh
          </Button>
        </div>
      </div>

      {error && (
        <Card className='border-red-500/30 bg-red-500/5'>
          <CardContent className='p-4 text-sm text-red-600'>{error}</CardContent>
        </Card>
      )}
      {toast && (
        <Card className='border-emerald-500/30 bg-emerald-500/5'>
          <CardContent className='flex items-center justify-between p-4 text-sm'>
            <span>{toast}</span>
            <button onClick={() => setToast(null)} aria-label='dismiss'>
              <Icons.close className='h-4 w-4' />
            </button>
          </CardContent>
        </Card>
      )}

      <div className='grid gap-3 sm:grid-cols-3'>
        {[
          ['Rules in force', all.length],
          ['Changed from default', changed],
          ['Decisions logged', evals.length],
        ].map(([label, n]) => (
          <Card key={label as string}>
            <CardContent className='p-4'>
              <p className='text-xs uppercase tracking-wide text-muted-foreground'>
                {label as string}
              </p>
              <p className='mt-1 text-2xl font-semibold tabular-nums'>{n as number}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {grouped?.groups.map((group, i) => (
        <motion.section
          key={group.name}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: i * 0.05 }}
          className='space-y-3'
        >
          <div className='flex flex-wrap items-baseline gap-3'>
            <h2 className='text-lg font-semibold'>{group.name}</h2>
            <span className='text-xs text-muted-foreground'>{group.policies[0]?.steers}</span>
          </div>
          <div className='grid gap-3 md:grid-cols-2 xl:grid-cols-3'>
            {group.policies.map((p) => (
              <PolicyRule
                key={p.key}
                policy={p}
                onSaved={(m) => {
                  setToast(m)
                  refresh()
                }}
              />
            ))}
          </div>
        </motion.section>
      ))}

      <section className='space-y-3'>
        <div>
          <h2 className='text-lg font-semibold'>Decision log</h2>
          <p className='text-sm text-muted-foreground'>
            Every evaluation records the value that was in force at the time, so a decision
            still reads correctly after the rule has been changed.
          </p>
        </div>
        <Card>
          <CardContent className='p-0'>
            {evals.length === 0 ? (
              <p className='p-6 text-center text-sm text-muted-foreground'>
                No decisions recorded yet. Run the agent and gate decisions appear here.
              </p>
            ) : (
              <div className='divide-y divide-border'>
                {evals.map((e) => (
                  <div key={e.id} className='flex flex-wrap items-center gap-3 p-3 text-sm'>
                    <span
                      className={cn(
                        'rounded-full border px-2 py-0.5 text-xs font-medium',
                        decisionClasses(e.decision)
                      )}
                    >
                      {e.decision}
                    </span>
                    <span className='font-mono text-xs'>{e.issue_key}</span>
                    <span className='min-w-0 flex-1 truncate text-muted-foreground'>
                      {e.reason}
                    </span>
                    <span className='text-xs text-muted-foreground'>
                      {e.policy_key} ={' '}
                      <span className='font-mono'>{e.policy_value_at_eval}</span>
                    </span>
                    <span className='w-16 shrink-0 text-right text-xs text-muted-foreground'>
                      {relativeTime(e.evaluated_at)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </section>
    </div>
  )
}
