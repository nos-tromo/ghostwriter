import { RemoveButton, Spinner, cn } from '@infra/ui'
import type { PendingError, PendingFile } from '../hooks/useAttachments'
import type { Strings } from '../i18n'
import { useLang, useT } from '../i18n/LanguageContext'
import { formatSize } from '../lib/format'

const ERROR_KEYS: Record<PendingError, keyof Strings> = {
  type: 'composer.error_type',
  size: 'composer.error_size',
  upload: 'composer.error_upload',
  storage_full: 'composer.error_storage_full',
  purpose: 'composer.error_purpose',
}

/**
 * The files the next message sends, one chip each: a spinner while it
 * uploads, its size once attached, and why it is not, where it was refused.
 * A refused file can be taken out of view; nothing of it was stored.
 */
export function AttachmentChips({ pending, onDismiss }: { pending: PendingFile[]; onDismiss: (key: string) => void }) {
  const t = useT()
  const lang = useLang()
  if (pending.length === 0) return null
  return (
    <ul className="flex flex-wrap gap-1">
      {pending.map((file) => (
        <li key={file.key} data-status={file.status} className="max-w-full">
          <span
            className={cn(
              'inline-flex max-w-full items-center gap-1 rounded-full border bg-background px-2 py-0.5 text-xs',
              file.status === 'error' ? 'border-danger' : 'border-border',
            )}
          >
            {file.status === 'uploading' && (
              <Spinner className="h-3 w-3 shrink-0" label={t('composer.uploading', { name: file.name })} />
            )}
            <span title={file.name} className="min-w-0 truncate">
              {file.name}
            </span>
            {file.status === 'done' && (
              <span className="shrink-0 text-muted-foreground">{formatSize(file.size, lang)}</span>
            )}
            {file.status === 'error' && (
              <RemoveButton
                label={t('composer.remove', { name: file.name })}
                className="h-5 w-5"
                onClick={() => onDismiss(file.key)}
              />
            )}
          </span>
          {file.status === 'error' && file.error && (
            <p role="alert" className="px-2 text-xs text-danger">
              {t(ERROR_KEYS[file.error], { name: file.name })}
            </p>
          )}
        </li>
      ))}
    </ul>
  )
}
