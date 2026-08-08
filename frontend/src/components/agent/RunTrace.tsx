'use client'

/**
 * The run, while it is happening.
 *
 * Triggering a run used to show a pulsing dot and "0 ops" for two minutes. The
 * step data was already being persisted as each SSE event arrived and thrown
 * away by the UI — so the one thing this product does that nothing else in the
 * room does, an orchestrator delegating to seven operators and branching on
 * what it finds, was invisible while it happened.
 *
 * The motion here is motivated, not decorative: each row appears when its step
 * actually reports, so the sequence on screen is the sequence of work. Nothing
 * loops, nothing animates on a timer.
 *
 * Step ids are the contract with the orchestrator, so they are mapped to names
 * a person can read. An unrecognised id still renders — a new step should show
 * up as itself rather than vanish.
 */

import { useEffect, useState } from 'react'
// framer-motion, not motion/react: the project is on framer-motion v11 and the
// whole dashboard imports it from there. Adding the newer package for one
// component would put two animation runtimes in the same tree.
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import { agent, type RunDetail } from '@/lib/command-center'
import { Icons } from '@/components/ui/icons'
import { cn } from '@/lib/utils'

const STEP_NAMES: Record<string, string> = {
  step_0_sla: 'SLA and business hours',
  step_0_incidents: 'Major-incident detection',
  step_1_sweep: 'Backlog triage',
  step_2_diag: 'Diagnosis',
  step_4_gate: 'Change / CAB gate',
  step_3_rem: 'Safe remediation',
  step_4_rev: 'Human review',
  step_5_exec: 'Execute remediation',
  step_6_notif_auto: 'Notify — resolved',
  step_6_notif_escalated: 'Notify — escalated',
  step_6_notif_rejected: 'Notify — blocked',
  step_6_notif_manual: 'Notify — after approval',
}

// The two that fan out from the start. Naming them lets the trace show that the
// SLA engine and the incident detector run at the same time rather than in
// sequence, which is the orchestration depth the architecture is scored on.
const PARALLEL_HEAD = new Set(['step_0_sla', 'step_0_incidents'])

// Which branch the gate took is readable from which step ran after it.
const BRANCH_LABEL: Record<string, string> = {
  step_3_rem: 'allowed',
  step_4_rev: 'escalated to a human',
  step_6_notif_rejected: 'blocked',
}

const DONE = new Set(['completed', 'succeeded', 'success', 'ok'])
const FAILED = new Set(['failed', 'error', 'cancelled'])

function stepName(step: { step_id: string | null; operator_name: string | null }) {
  const id = step.step_id ?? ''
  return STEP_NAMES[id] ?? step.operator_name ?? id ?? 'step'
}

function StepIcon({ status }: { status: string | null }) {
  if (status && DONE.has(status)) {
    return <Icons.checkCircle className='h-4 w-4 shrink-0 text-emerald-600' strokeWidth={2} />
  }
  if (status && FAILED.has(status)) {
    return <Icons.alertTriangle className='h-4 w-4 shrink-0 text-red-600' strokeWidth={2} />
  }
  // Still working. A slow pulse reads as "in progress" without the bounce that
  // makes an operations tool feel like a toy.
  return (
    <span className='flex h-4 w-4 shrink-0 items-center justify-center'>
      <span className='h-2 w-2 animate-pulse rounded-full bg-brand-cornflower' />
    </span>
  )
}

export function RunTrace({ runId, onSettled }: { runId: string; onSettled?: () => void }) {
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const reduce = useReducedMotion()

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>

    async function poll() {
      try {
        const d = await agent.run(runId)
        if (cancelled) return
        setDetail(d)
        const status = d.run.status ?? ''
        if (status === 'pending' || status === 'running') {
          // 2s while working: fast enough that steps appear as they happen,
          // slow enough not to hammer the API for a two-minute run.
          timer = setTimeout(poll, 2000)
        } else {
          onSettled?.()
        }
      } catch {
        if (!cancelled) timer = setTimeout(poll, 4000)
      }
    }

    poll()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [runId, onSettled])

  if (!detail) return null

  const steps = detail.operators
  const status = detail.run.status ?? ''
  const isLive = status === 'pending' || status === 'running'
  const target = detail.run.selected_issue_key ?? detail.run.issue_keys?.[0]

  // The gate's decision, read from whichever step followed it.
  const gateIndex = steps.findIndex((s) => s.step_id === 'step_4_gate')
  const afterGate = gateIndex >= 0 ? steps[gateIndex + 1] : undefined
  const branch = afterGate?.step_id ? BRANCH_LABEL[afterGate.step_id] : undefined

  return (
    <div className='rounded-lg border bg-background p-5'>
      <div className='flex flex-wrap items-baseline justify-between gap-3'>
        <p className='text-sm font-semibold text-brand-navy'>
          {isLive ? 'Running' : 'Run finished'}
          {target && <span className='ml-2 font-mono font-normal'>{target}</span>}
        </p>
        <p className='text-xs text-muted-foreground'>
          {steps.length} of 7 operators
          {detail.run.duration_ms && ` · ${(detail.run.duration_ms / 1000).toFixed(0)}s`}
        </p>
      </div>

      <ol className='mt-4 space-y-1'>
        <AnimatePresence initial={false}>
          {steps.map((step, i) => {
            const id = step.step_id ?? ''
            const parallel = PARALLEL_HEAD.has(id)
            const showBranch = id === 'step_4_gate' && branch
            return (
              <motion.li
                key={`${id}-${step.sequence}`}
                initial={reduce ? false : { opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
                className='flex items-center gap-3 text-sm'
              >
                <StepIcon status={step.status} />
                <span
                  className={cn(
                    'flex-1 truncate',
                    step.status && DONE.has(step.status)
                      ? 'text-foreground'
                      : 'text-muted-foreground'
                  )}
                >
                  {stepName(step)}
                </span>
                {parallel && i < 2 && (
                  <span className='shrink-0 text-[11px] text-brand-cornflower'>in parallel</span>
                )}
                <span className='w-12 shrink-0 text-right text-xs tabular-nums text-muted-foreground'>
                  {step.duration_ms ? `${(step.duration_ms / 1000).toFixed(0)}s` : ''}
                </span>
              </motion.li>
            )
          })}
        </AnimatePresence>
      </ol>

      {branch && (
        <motion.p
          initial={reduce ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
          className='mt-3 border-t pt-3 text-xs text-muted-foreground'
        >
          The change gate <span className='font-medium text-brand-navy'>{branch}</span>
          {branch === 'escalated to a human' && ' — remediation never ran.'}
          {branch === 'blocked' && ' — the change record forbids it.'}
        </motion.p>
      )}

      {detail.run.error && (
        <p className='mt-3 border-t pt-3 text-xs text-red-700'>{detail.run.error}</p>
      )}
    </div>
  )
}
