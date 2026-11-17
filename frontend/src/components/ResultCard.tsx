import { useCallback, useEffect, useId, useState } from 'react'
import { Badge, DownloadButton, Spinner } from '@infra/ui'
import { fileBlob } from '../api/chats'
import { ApiError } from '../api/client'
import type { ResultOut } from '../api/types'
import { useLang, useT } from '../i18n/LanguageContext'
import { formatSize } from '../lib/format'
import { RequestError } from './RequestError'
import { TablePreview } from './TablePreview'

/** Whether a file is no longer there: expired (410), or not held by the chat any more (404). */
function isGone(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 410 || error.status === 404)
}

/** Save a blob under its name through a transient object URL, revoked once the click has been dispatched. */
function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * The image itself, fetched as a blob: an `<img src>` could not send the
 * stated purpose, so it shows an object URL made here and revoked on unmount.
 */
function InlineImage({ chatId, result, onGone }: { chatId: string; result: ResultOut; onGone: () => void }) {
  const t = useT()
  const [url, setUrl] = useState<string | null>(null)
  const [failure, setFailure] = useState<unknown>(null)
  useEffect(() => {
    const controller = new AbortController()
    let created: string | null = null
    fileBlob(chatId, result.file_id, controller.signal).then(
      (blob) => {
        if (controller.signal.aborted) return
        created = URL.createObjectURL(blob)
        setUrl(created)
      },
      (error: unknown) => {
        if (controller.signal.aborted) return
        if (isGone(error)) onGone()
        else setFailure(error)
      },
    )
    return () => {
      controller.abort()
      if (created) URL.revokeObjectURL(created)
    }
  }, [chatId, result.file_id, onGone])

  if (failure !== null) return <RequestError error={failure} />
  if (url === null) return <Spinner label={t('result.loading', { name: result.name })} />
  return (
    <img
      src={url}
      alt={result.name}
      className="max-h-96 w-auto max-w-full rounded border border-border bg-white object-contain"
    />
  )
}

/** The file's name and size, and its download. */
function FileLine({
  chatId,
  result,
  nameId,
  onGone,
}: {
  chatId: string
  result: ResultOut
  nameId: string
  onGone: () => void
}) {
  const t = useT()
  const lang = useLang()
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<unknown>(null)

  const download = async () => {
    setBusy(true)
    setFailure(null)
    try {
      saveBlob(await fileBlob(chatId, result.file_id), result.name)
    } catch (error) {
      if (isGone(error)) onGone()
      else setFailure(error)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <span id={nameId} title={result.name} className="min-w-0 flex-1 truncate">
          {result.name}
        </span>
        <span className="shrink-0 text-xs text-muted-foreground">{formatSize(result.size, lang)}</span>
        <DownloadButton
          label={t('result.download')}
          aria-describedby={nameId}
          busy={busy}
          onClick={() => void download()}
        />
      </div>
      {failure !== null && <RequestError error={failure} />}
    </div>
  )
}

/**
 * A file a tool wrote, below the answer: an image inline, a table's preview,
 * then for every file its name, size and download. Once workbench's period
 * for the file has passed, or a fetch finds it gone, the card is muted and
 * says so, with nothing left to fetch.
 */
export function ResultCard({ chatId, result }: { chatId: string; result: ResultOut }) {
  const t = useT()
  const nameId = useId()
  const [gone, setGone] = useState(false)
  const markGone = useCallback(() => setGone(true), [])

  if (result.expired || gone) {
    return (
      <div
        role="group"
        aria-labelledby={nameId}
        className="flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground"
      >
        <span id={nameId} title={result.name} className="min-w-0 flex-1 truncate">
          {result.name}
        </span>
        <Badge>{t('result.expired')}</Badge>
      </div>
    )
  }
  return (
    <div role="group" aria-labelledby={nameId} className="space-y-2 rounded-md border border-border bg-background p-3 text-sm">
      {result.kind === 'image' && <InlineImage chatId={chatId} result={result} onGone={markGone} />}
      {result.kind === 'table' && result.preview && <TablePreview preview={result.preview} caption={result.name} />}
      <FileLine chatId={chatId} result={result} nameId={nameId} onGone={markGone} />
    </div>
  )
}
