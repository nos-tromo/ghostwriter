import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getPurpose, requestPurpose } from '@infra/ui'
import { getAttachment, uploadAttachment } from '../api/chats'
import { ApiError, PURPOSE_SCOPE, isPurposeRefusal } from '../api/client'
import type { AppConfig, AttachmentOut } from '../api/types'
import { nextMark, readMarkOf, useChat } from './useChats'

/** Why a file is not attached: its type, its size, the upload, workbench's
 *  space, the files already waiting, or a purpose the user did not state. */
export type PendingError = 'type' | 'size' | 'upload' | 'storage_full' | 'too_many_pending' | 'purpose'

/** A file in the composer, from the drop or the pick until a message sends it.
 *  `processing` is a stored picture still being prepared; `done` a file that can
 *  be sent, also one the server could not read (its `attachment.status` is `failed`). */
export interface PendingFile {
  key: string
  name: string
  size: number
  /** A picture, by the deployment's `image_types`; every other file is a document. */
  kind: 'document' | 'image'
  status: 'uploading' | 'processing' | 'done' | 'error'
  attachment?: AttachmentOut
  error?: PendingError
  /** When the upload was done, on the page's order of events (`nextMark`). */
  doneMark?: number
  /** An object URL of a picture the browser can draw, made when it was added and
   *  revoked when it is dismissed, sent or the composer goes. */
  localUrl?: string
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

/** The picture types a browser draws itself: only these show at once, from the file in hand. */
const DRAWN_AT_ONCE: ReadonlySet<string> = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'])

/** How long one request waits for a picture to be prepared (the backend's cap). */
const POLL_WAIT_S = 20

/** How long to wait before asking again after a request that did not get through. */
const RETRY_MS = 2_000

/** The least time from one request to the next after a `processing` answer, should one come early
 *  (a proxy that drops `?wait`): the long poll never turns into a loop. */
const PROCESSING_FLOOR_MS = 500

/** A name's extension as the backend reads it (`.pdf`), lower-cased; a dot-file has none. */
function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(dot).toLowerCase() : ''
}

/** Whether a file of this name is a picture, by the deployment's picture types, in any case. */
export function isImageName(name: string, config: AppConfig): boolean {
  const extension = extensionOf(name)
  return extension !== '' && config.image_types.some((type) => type.toLowerCase() === extension)
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
  return {
    key: attachment.id,
    name: attachment.name,
    size: attachment.size,
    kind: attachment.kind === 'image' ? 'image' : 'document',
    status: attachment.status === 'processing' ? 'processing' : 'done',
    attachment,
  }
}

/** Resolves after `ms`, or as soon as `signal` aborts. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer)
      signal.removeEventListener('abort', done)
      resolve()
    }
    const timer = setTimeout(done, ms)
    signal.addEventListener('abort', done, { once: true })
  })
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
 * another tab: its tile goes.
 *
 * A picture is prepared on the server after it is stored, and only a prepared
 * one can be sent. Each one still being prepared, uploaded here or listed by
 * the chat, is followed once, by requests that wait on the server until its
 * preparation ends (20 s at most, then asked again): it becomes `done`, with
 * the attachment as it then is, a `failed` one included. A `processing` answer
 * is asked again no sooner than 500 ms after its request began. A request that
 * does not get through, or a server error, is tried again after 2 s; any other
 * refusal (a 404 above all: the server no longer holds the file) settles the
 * file as an error, and the chat's list never brings it back as one still
 * being prepared. Leaving the chat stops it. A picture a browser can draw
 * shows from the file in hand at once (an object URL, never leaked); the
 * others, once prepared, from the server's rendition.
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
  // The object URL of each local picture, by file key, until it is revoked.
  const urlsRef = useRef<Map<string, string>>(new Map())
  // The attachments followed so far, by id: each is followed once.
  const followedRef = useRef<Set<string>>(new Set())
  // A followed attachment of the chat's own list, as it is now, by id.
  const [prepared, setPrepared] = useState<ReadonlyMap<string, AttachmentOut>>(() => new Map())
  const needsPurpose = config.require_purpose.some((action) => UPLOAD_ACTIONS.has(action))

  useEffect(() => {
    aliveRef.current = true
    const controller = new AbortController()
    controllerRef.current = controller
    const urls = urlsRef.current
    const followed = followedRef.current
    return () => {
      aliveRef.current = false
      queueRef.current = []
      followed.clear()
      controller.abort()
      for (const url of urls.values()) URL.revokeObjectURL(url)
      urls.clear()
    }
  }, [])

  const revoke = useCallback((key: string) => {
    const url = urlsRef.current.get(key)
    if (url === undefined) return
    urlsRef.current.delete(key)
    URL.revokeObjectURL(url)
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
            status: attachment.status === 'processing' ? ('processing' as const) : ('done' as const),
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
        const kind = isImageName(file.name, config) ? ('image' as const) : ('document' as const)
        const base = { key, name: file.name, size: file.size, kind }
        if (!isAttachable(file.name, config)) added.push({ ...base, status: 'error', error: 'type' })
        else if (file.size > config.max_attachment_bytes) added.push({ ...base, status: 'error', error: 'size' })
        else {
          // Its picture shows at once, before the server has seen a byte.
          let localUrl: string | undefined
          if (kind === 'image' && DRAWN_AT_ONCE.has(extensionOf(file.name))) {
            localUrl = URL.createObjectURL(file)
            urlsRef.current.set(key, localUrl)
          }
          added.push({ ...base, status: 'uploading', ...(localUrl !== undefined && { localUrl }) })
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
    // A listed attachment still preparing is as the follow found it, once it has.
    const found = earlier.map((a) => waiting(a.status === 'processing' ? (prepared.get(a.id) ?? a) : a))
    return [...found, ...current]
  }, [chat, local, taken, prepared])

  // An attachment whose preparation ended: a file of this composer takes it, and so
  // does the chat's list.
  const ended = useCallback((file: PendingFile, latest: AttachmentOut) => {
    setLocal((files) =>
      files.map((f) =>
        f.key === file.key ? { ...f, status: 'done', attachment: latest, name: latest.name, size: latest.size } : f,
      ),
    )
    setPrepared((current) => new Map(current).set(latest.id, latest))
  }, [])

  // An attachment the server no longer answers for: an error tile, in place of the file or
  // standing for the listed one. The chat's list may still name it as being prepared; it is
  // taken out of that list here, so once its tile goes (dismissed, or a message cleared it) it
  // never comes back as a tile still being prepared, which nothing would follow again.
  const lost = useCallback((file: PendingFile) => {
    const refused = { status: 'error' as const, error: 'upload' as const }
    setLocal((files) =>
      files.some((f) => f.key === file.key)
        ? files.map((f) => (f.key === file.key ? { ...f, ...refused } : f))
        : [...files, { ...file, ...refused }],
    )
    const id = file.attachment?.id
    if (id !== undefined) setTaken((ids) => new Set(ids).add(id))
  }, [])

  const follow = useCallback(
    async (file: PendingFile) => {
      const signal = controllerRef.current?.signal
      if (chatId === null || file.attachment === undefined || signal === undefined) return
      while (!signal.aborted) {
        const asked = Date.now()
        try {
          const latest = await getAttachment(chatId, file.attachment.id, POLL_WAIT_S, signal)
          if (latest.status === 'processing') {
            await pause(Math.max(0, PROCESSING_FLOOR_MS - (Date.now() - asked)), signal)
            continue
          }
          if (!signal.aborted) ended(file, latest)
          return
        } catch (error) {
          if (signal.aborted) return
          // A refusal is no network trouble: asking again would only be refused again.
          if (error instanceof ApiError && error.status < 500) {
            lost(file)
            return
          }
          await pause(RETRY_MS, signal)
        }
      }
    },
    [chatId, ended, lost],
  )

  useEffect(() => {
    for (const file of pending) {
      const id = file.attachment?.id
      if (file.status !== 'processing' || id === undefined || followedRef.current.has(id)) continue
      followedRef.current.add(id)
      void follow(file)
    }
  }, [pending, follow])

  const clear = useCallback(() => {
    const done = pending.filter((f) => f.status === 'done')
    // A message takes the files, and the composer lets go of every one that is not
    // still on its way: their pictures are the server's from here on (a refused
    // message gives the files back, and their tiles ask the server for them).
    for (const f of pending) if (f.status !== 'uploading' && f.status !== 'processing') revoke(f.key)
    setTaken((ids) => new Set([...ids, ...done.flatMap((f) => (f.attachment ? [f.attachment.id] : []))]))
    setLocal((files) => files.filter((f) => f.status === 'uploading' || f.status === 'processing'))
    return done.map((f) => ({ ...f, localUrl: undefined }))
  }, [pending, revoke])

  const restore = useCallback((files: PendingFile[]) => {
    const ids = new Set(files.flatMap((f) => (f.attachment ? [f.attachment.id] : [])))
    setTaken((current) => new Set([...current].filter((id) => !ids.has(id))))
    setLocal((current) => [...files.filter((f) => !current.some((c) => c.key === f.key)), ...current])
  }, [])

  const dismiss = useCallback(
    (key: string) => {
      const file = pending.find((f) => f.key === key)
      if (file?.status !== 'error') return
      setLocal((files) => files.filter((f) => f.key !== key || f.status !== 'error'))
      revoke(key)
      // One the server holds still, and the chat lists, stays out of view too.
      const id = file.attachment?.id
      if (id !== undefined) setTaken((ids) => new Set(ids).add(id))
    },
    [pending, revoke],
  )

  return { pending, add, clear, restore, dismiss }
}
