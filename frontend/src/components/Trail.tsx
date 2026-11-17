import { useId, useState } from 'react'
import { DisclosureButton, StatusIcon, cn, type StatusIconStatus } from '@infra/ui'
import type { StepOut, StepStatus } from '../api/types'
import type { Strings } from '../i18n'
import { useT } from '../i18n/LanguageContext'
import { CodeBlock } from './MarkdownProse'

type Key = keyof Strings
type Translate = ReturnType<typeof useT>

/** Each workbench tool's label: with what it worked on (`{detail}`), and
 *  without, for a step that named nothing or whose content has expired. */
const TOOL_LABELS = new Map<string, { named: Key; unnamed: Key }>([
  ['list_files', { named: 'trail.list_files', unnamed: 'trail.list_files' }],
  ['read_document', { named: 'trail.read_document', unnamed: 'trail.read_document_unnamed' }],
  ['search_files', { named: 'trail.search_files', unnamed: 'trail.search_files_unnamed' }],
  ['run_python', { named: 'trail.run_python', unnamed: 'trail.run_python' }],
  ['inspect_template', { named: 'trail.inspect_template', unnamed: 'trail.inspect_template_unnamed' }],
  ['fill_template', { named: 'trail.fill_template', unnamed: 'trail.fill_template_unnamed' }],
  ['write_table', { named: 'trail.write_table', unnamed: 'trail.write_table_unnamed' }],
  ['write_document', { named: 'trail.write_document', unnamed: 'trail.write_document_unnamed' }],
])

/** A step's status as the shared status marker draws it, and its name. */
const STATUS: Record<StepStatus, { icon: StatusIconStatus; label: Key }> = {
  running: { icon: 'running', label: 'trail.status_running' },
  ok: { icon: 'done', label: 'trail.status_ok' },
  error: { icon: 'failed', label: 'trail.status_error' },
}

/** What a step did, in words; a tool this app does not know is named as it is. */
function stepLabel(step: StepOut, t: Translate): string {
  const labels = TOOL_LABELS.get(step.tool)
  if (!labels) return t('trail.other', { tool: step.tool })
  return step.detail ? t(labels.named, { detail: step.detail }) : t(labels.unnamed)
}

/** The code a `run_python` step ran, out of the JSON its arguments are; null when they hold none. */
function pythonCode(step: StepOut): string | null {
  if (step.tool !== 'run_python' || step.arguments === null) return null
  try {
    const parsed: unknown = JSON.parse(step.arguments)
    const code = parsed && typeof parsed === 'object' ? (parsed as { code?: unknown }).code : undefined
    return typeof code === 'string' ? code : null
  } catch {
    return null
  }
}

const PRE = 'max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-background p-2 text-xs text-foreground outline-none focus-visible:ring-1 focus-visible:ring-primary'

/** A step's arguments and result, behind a disclosure of their own; the code of `run_python` highlighted. */
function StepDetails({ step, labelId }: { step: StepOut; labelId: string }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const code = pythonCode(step)
  return (
    <>
      <DisclosureButton
        expanded={open}
        controls={panelId}
        label={t('trail.details')}
        aria-describedby={labelId}
        onClick={() => setOpen((value) => !value)}
        className="h-6 w-6"
      />
      {open && (
        <div id={panelId} className="basis-full space-y-1">
          {code !== null ? (
            <CodeBlock code={code} language="python" />
          ) : (
            step.arguments && (
              <pre tabIndex={0} className={PRE}>
                {step.arguments}
              </pre>
            )
          )}
          {step.result && (
            <pre tabIndex={0} className={PRE}>
              {step.result}
            </pre>
          )}
        </div>
      )}
    </>
  )
}

function StepRow({ step, fileDays }: { step: StepOut; fileDays: number }) {
  const t = useT()
  const labelId = useId()
  const status = STATUS[step.status]
  const hasDetails = !step.expired && (step.arguments !== null || step.result !== null)
  return (
    <li className="flex items-start gap-2">
      <StatusIcon status={status.icon} label={t(status.label)} />
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">
        <span id={labelId} className="min-w-0 flex-1 break-words">
          {stepLabel(step, t)}
        </span>
        {hasDetails && <StepDetails step={step} labelId={labelId} />}
        {step.expired && <span className="basis-full italic">{t('trail.expired', { days: fileDays })}</span>}
      </div>
    </li>
  )
}

function StepList({ steps, fileDays, id, className }: { steps: StepOut[]; fileDays: number; id?: string; className?: string }) {
  return (
    <ul id={id} className={cn('space-y-1', className)}>
      {steps.map((step) => (
        <StepRow key={step.id} step={step} fileDays={fileDays} />
      ))}
    </ul>
  )
}

/**
 * The tool steps behind an answer: collapsed behind "{n} steps" once the answer
 * is stored, and open while it is being written (`live`), so the user sees each
 * step run. Each step shows its status and what it did; its arguments and
 * result sit behind a disclosure of their own until they expire with the
 * chat's files (`fileDays`).
 */
export function Trail({ steps, fileDays, live = false }: { steps: StepOut[]; fileDays: number; live?: boolean }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const listId = useId()
  if (steps.length === 0) return null
  if (live) return <StepList steps={steps} fileDays={fileDays} className="text-xs text-muted-foreground" />
  const count = steps.length === 1 ? t('trail.steps_one') : t('trail.steps', { n: steps.length })
  return (
    <div className="text-xs text-muted-foreground">
      <div className="flex items-center gap-2">
        {/* The button carries the same words as its name. */}
        <span aria-hidden="true" className="flex-1">
          {count}
        </span>
        <DisclosureButton
          expanded={open}
          controls={listId}
          label={count}
          onClick={() => setOpen((value) => !value)}
          className="h-6 w-6"
        />
      </div>
      {open && <StepList id={listId} steps={steps} fileDays={fileDays} className="mt-2" />}
    </div>
  )
}
