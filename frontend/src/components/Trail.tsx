import { useId, useState } from 'react'
import { DisclosureText, StatusIcon, cn, type StatusIconStatus } from '@infra/ui'
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
  ['view_file', { named: 'trail.view_file', unnamed: 'trail.view_file_unnamed' }],
  ['search_files', { named: 'trail.search_files', unnamed: 'trail.search_files_unnamed' }],
  ['run_python', { named: 'trail.run_python', unnamed: 'trail.run_python' }],
  ['inspect_template', { named: 'trail.inspect_template', unnamed: 'trail.inspect_template_unnamed' }],
  ['fill_template', { named: 'trail.fill_template', unnamed: 'trail.fill_template_unnamed' }],
  ['write_table', { named: 'trail.write_table', unnamed: 'trail.write_table_unnamed' }],
  ['write_document', { named: 'trail.write_document', unnamed: 'trail.write_document_unnamed' }],
  ['write_presentation', { named: 'trail.write_presentation', unnamed: 'trail.write_presentation_unnamed' }],
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

/**
 * A step's arguments and result, once its text is clicked; the code of
 * `run_python` highlighted. The panel keeps to the column: a long result
 * wraps and a long code line scrolls in its block, rather than the panel
 * growing to fit them.
 */
function StepPanel({ step, id }: { step: StepOut; id: string }) {
  const code = pythonCode(step)
  return (
    <div id={id} className="space-y-1">
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
  )
}

function StepRow({ step, fileDays }: { step: StepOut; fileDays: number }) {
  const t = useT()
  const panelId = useId()
  const [open, setOpen] = useState(false)
  const status = STATUS[step.status]
  const hasDetails = !step.expired && (step.arguments !== null || step.result !== null)
  const label = stepLabel(step, t)
  return (
    <li className="flex items-start gap-2">
      <StatusIcon status={status.icon} label={t(status.label)} />
      <div className="min-w-0 flex-1 space-y-1">
        {hasDetails ? (
          <DisclosureText expanded={open} controls={panelId} onClick={() => setOpen((value) => !value)}>
            {label}
          </DisclosureText>
        ) : (
          <span className="block break-words">{label}</span>
        )}
        {step.expired && <span className="block italic">{t('trail.expired', { days: fileDays })}</span>}
        {hasDetails && open && <StepPanel step={step} id={panelId} />}
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
 * result open from the step's text until they expire with the chat's files
 * (`fileDays`). Both disclosures are their own words (`DisclosureText`), with
 * no chevron beside them.
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
      <DisclosureText expanded={open} controls={listId} onClick={() => setOpen((value) => !value)}>
        {count}
      </DisclosureText>
      {open && <StepList id={listId} steps={steps} fileDays={fileDays} className="mt-2" />}
    </div>
  )
}
