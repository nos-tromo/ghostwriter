import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getPurpose, requestPurpose } from '@infra/ui'
import { uploadAttachment } from '../api/chats'
import { ApiError, PURPOSE_SCOPE, isPurposeRefusal } from '../api/client'
import type { AppConfig, AttachmentOut } from '../api/types'
import { nextMark, readMarkOf, useChat } from './useChats'

/** Why a file is not attached: its type, its size, the upload, workbench's
 *  space, the files already waiting, or a purpose the user did not state. */
export type PendingError = 'type' | 'size' | 'upload' | 'storage_full' | 'too_many_pending' | 'purpose'

/** A file in the composer, from the drop or the pick until a message sends it. */
export interface PendingFile {
  key: string
  name: string
  size: number
  status: 'uploading' | 'done' | 'error'
  attachment?: AttachmentOut
  error?: PendingError
  /** When the upload was done, on the page's order of events (`nextMark`). */
  doneMark?: number
}

export interface Attachments {
  pending: PendingFile[]
  add(files: File[]): void
  /** Take the attached files out of the composer, as a message sends them;
   *  returns them, so `restore` can put them back. Uploads still running stay. */
  clear(): PendingFile[]
  /** Put back what `clear` took: the message that was to send them was refused. */
  restore(files: PendingFile[]): void
  /** Take a refused file out of view. */
  dismiss(key: string): void
}

/** The audit actions the attachment route records: a purpose required for
 *  either is required for an upload. */
const UPLOAD_ACTIONS: ReadonlySet<string> = new Set(['collect', 'disclose'])

/** A name's extension as the backend reads it (`.pdf`), lower-cased; a dot-file has none. */
function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(dot).toLowerCase() : ''
}

/** Whether a file of this name is of a type the deployment takes, in any case. */
export function isAttachable(name: string, config: AppConfig): boolean {
  const extension = extensionOf(name)
  return extension !== '' && config.attachment_types.some((type) => type.toLowerCase() === extension)
}

/** Why the server refused a file, by its code, never by the status alone. */
function refusalOf(error: unknown): PendingError {
  if (error instanceof ApiError) {
    if (error.code === 'too_large') return 'size'
    if (error.code === 'storage_full') return 'storage_full'
    if (error.code === 'too_many_pending') return 'too_many_pending'
  }
  return isPurposeRefusal(error) ? 'purpose' : 'upload'
}

/** A file attached earlier, still waiting in the chat for a message. */
function waiting(attachment: AttachmentOut): PendingFile {
  return { key: attachment.id, name: attachment.name, size: attachment.size, status: 'done', attachment }
}

/**
 * The files the next message of a chat sends: checked against the
 * deployment's types and size first, then uploaded at once, one after
 * another. Parallel uploads could each pass the backend's count of waiting
 * files and be refused only after workbench had stored them.
 *
 * Where a purpose is required for attaching and none is stated, the purpose
 * prompt opens before the upload starts, so a large body is never sent only
 * to be refused unread. A file the server refuses says why, by the refusal's
 * code (`too_large`, `storage_full`).
 *
 * Files attached earlier and not sent yet, as the chat lists them, are shown
 * and sent too, so leaving a chat and coming back loses none. An attached
 * file the chat, read again since, no longer lists has been sent, from
 * another tab: its chip goes.
 *
 * @param chatId - The chat, or null for none (nothing is uploaded).
 * @param config - The deployment's switches and limits.
 * @returns The files and what can be done with them.
 */
export function useAttachments(chatId: string | null, config: AppConfig): Attachments {
  const { data: chat } = useChat(chatId)
  const [local, setLocal] = useState<PendingFile[]>([])
  // Waiting files of the chat that a message has taken, by attachment id.
  const [taken, setTaken] = useState<ReadonlySet<string>>(() => new Set())
  const queueRef = useRef<Array<{ key: string; file: File }>>([])
  const runningRef = useRef(false)
  const seqRef = useRef(0)
  const aliveRef = useRef(true)
  const controllerRef = useRef<AbortController | null>(null)
  const needsPurpose = config.require_purpose.some((action) => UPLOAD_ACTIONS.has(action))

  useEffect(() => {
    aliveRef.current = true
    const controller = new AbortController()
    controllerRef.current = controller
    return () => {
      aliveRef.current = false
      queueRef.current = []
      controller.abort()
    }
  }, [])

  const settle = useCallback((keys: ReadonlySet<string>, change: Partial<PendingFile>) => {
    setLocal((files) => files.map((f) => (keys.has(f.key) ? { ...f, ...change } : f)))
  }, [])

  const drain = useCallback(async () => {
    if (chatId === null || runningRef.current) return
    runningRef.current = true
    try {
      while (aliveRef.current && queueRef.current.length > 0) {
        if (needsPurpose && getPurpose(PURPOSE_SCOPE) === null && !(await requestPurpose(PURPOSE_SCOPE))) {
          if (!aliveRef.current) return
          settle(new Set(queueRef.current.splice(0).map((q) => q.key)), { status: 'error', error: 'purpose' })
          continue
        }
        const next = queueRef.current.shift()
        if (!next || !aliveRef.current) return
        try {
          const attachment = await uploadAttachment(chatId, next.file, controllerRef.current?.signal)
          // Named and sized as stored: the server reduces a name to a safe basename.
          const stored = {
            status: 'done' as const,
            attachment,
            name: attachment.name,
            size: attachment.size,
            doneMark: nextMark(),
          }
          if (aliveRef.current) settle(new Set([next.key]), stored)
        } catch (error) {
          if (aliveRef.current) settle(new Set([next.key]), { status: 'error', error: refusalOf(error) })
        }
      }
    } finally {
      runningRef.current = false
    }
  }, [chatId, needsPurpose, settle])

  const add = useCallback(
    (files: File[]) => {
      if (chatId === null || !config.attachments || files.length === 0) return
      const added: PendingFile[] = []
      for (const file of files) {
        const key = `file-${++seqRef.current}`
        const base = { key, name: file.name, size: file.size }
        if (!isAttachable(file.name, config)) added.push({ ...base, status: 'error', error: 'type' })
        else if (file.size > config.max_attachment_bytes) added.push({ ...base, status: 'error', error: 'size' })
        else {
          added.push({ ...base, status: 'uploading' })
          queueRef.current.push({ key, file })
        }
      }
      setLocal((current) => [...current, ...added])
      void drain()
    },
    [chatId, config, drain],
  )

  const pending = useMemo(() => {
    const listed = new Set((chat?.pending_attachments ?? []).map((a) => a.id))
    const readMark = readMarkOf(chat)
    // Only a read begun after the upload was done can tell it is no longer waiting.
    const sent = (f: PendingFile) =>
      f.status === 'done' &&
      f.attachment !== undefined &&
      f.doneMark !== undefined &&
      readMark !== undefined &&
      readMark > f.doneMark &&
      !listed.has(f.attachment.id)
    const current = local.filter((f) => !sent(f))
    const known = new Set(current.flatMap((f) => (f.attachment ? [f.attachment.id] : [])))
    const earlier = (chat?.pending_attachments ?? []).filter(
      (a) => !a.expired && !known.has(a.id) && !taken.has(a.id),
    )
    return [...earlier.map(waiting), ...current]
  }, [chat, local, taken])

  const clear = useCallback(() => {
    const done = pending.filter((f) => f.status === 'done')
    setTaken((ids) => new Set([...ids, ...done.flatMap((f) => (f.attachment ? [f.attachment.id] : []))]))
    setLocal((files) => files.filter((f) => f.status === 'uploading'))
    return done
  }, [pending])

  const restore = useCallback((files: PendingFile[]) => {
    const ids = new Set(files.flatMap((f) => (f.attachment ? [f.attachment.id] : [])))
    setTaken((current) => new Set([...current].filter((id) => !ids.has(id))))
    setLocal((current) => [...files.filter((f) => !current.some((c) => c.key === f.key)), ...current])
  }, [])

  const dismiss = useCallback((key: string) => {
    setLocal((files) => files.filter((f) => f.key !== key || f.status !== 'error'))
  }, [])

  return { pending, add, clear, restore, dismiss }
}
