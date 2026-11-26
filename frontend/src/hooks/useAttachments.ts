import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { getPurpose, requestPurpose } from '@infra/ui'
import { deleteAttachment, getAttachment, uploadAttachment } from '../api/chats'
import { ApiError, PURPOSE_SCOPE, isPurposeRefusal } from '../api/client'
import type { AppConfig, AttachmentOut } from '../api/types'
import { extensionOf, clipKind } from '../lib/format'
import { chatKey, nextMark, readMarkOf, useChat } from './useChats'
import { CONFIG_KEY } from './useConfig'

/** Why a file is not attached: its type, its size, the upload, workbench's
 *  space, the files already waiting, a purpose the user did not state, or a
 *  clip the server no longer takes (video and audio went away since the page read the config). */
export type PendingError = 'type' | 'size' | 'upload' | 'storage_full' | 'too_many_pending' | 'purpose' | 'clip'

/** A file in the composer, from the drop or the pick until a message sends it.
 *  `processing` is a stored picture still being prepared, or a clip being transcribed; `done` a file that can
 *  be sent, also one the server could not read (its `attachment.status` is `failed`). */
export interface PendingFile {
  key: string
  name: string
  size: number
  /** A picture, by the deployment's `image_types`; a clip, by its `media_types` (`video` or `audio`, by its
   *  suffix); every other file is a document. A clip has no picture, no local one and no preview. */
  kind: 'document' | 'image' | 'video' | 'audio'
  status: 'uploading' | 'processing' | 'done' | 'error'
  attachment?: AttachmentOut
  error?: PendingError
  /** When the upload was done, on the page's order of events (`nextMark`). */
  doneMark?: number
  /** An object URL of a picture the browser can draw, made when it was added and
   *  revoked when it is removed, sent or the composer goes. */
  localUrl?: string
  /** It is being taken back: deleted on the server, then out of view. */
  removing?: boolean
  /** Its taking back failed; it stays, and can be sent or taken back again. */
  removeFailed?: boolean
}

export interface Attachments {
  pending: PendingFile[]
  add(files: File[]): void
  /** Take the attached files out of the composer, as a message sends them;
   *  returns them, so `restore` can put them back. Uploads still running stay. */
  clear(): PendingFile[]
  /** Put back what `clear` took: the message that was to send them was refused. */
  restore(files: PendingFile[]): void
  /** Take a file out of the composer, whatever its state: one the server holds is deleted there
   *  first, one on its way once it is stored, and a refused one simply goes. */
  remove(key: string): void
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

/** The least time from one request to the next after a `processing` or a `pending` answer, should one come
 *  early (a proxy that drops `?wait`): the long poll never turns into a loop. */
const PROCESSING_FLOOR_MS = 500

/** Whether a file of this name is a picture, by the deployment's picture types, in any case. */
export function isImageName(name: string, config: AppConfig): boolean {
  const extension = extensionOf(name)
  return extension !== '' && config.image_types.some((type) => type.toLowerCase() === extension)
}

/** Whether a file of this name is a video or audio clip, by the deployment's clip types, in any case. */
export function isClipName(name: string, config: AppConfig): boolean {
  const extension = extensionOf(name)
  return extension !== '' && config.media_types.some((type) => type.toLowerCase() === extension)
}

/** Whether a file is a clip, being or having been transcribed. */
export function isClip(file: Pick<PendingFile, 'kind'>): boolean {
  return file.kind === 'video' || file.kind === 'audio'
}

/** What a file of this name is to the composer: a clip, a picture or a document. */
function kindOf(name: string, config: AppConfig): PendingFile['kind'] {
  if (isClipName(name, config)) return clipKind(name)
  return isImageName(name, config) ? 'image' : 'document'
}

/** Whether a file of this name is of a type the deployment takes, in any case. */
export function isAttachable(name: string, config: AppConfig): boolean {
  const extension = extensionOf(name)
  return extension !== '' && config.attachment_types.some((type) => type.toLowerCase() === extension)
}

/** Why the server refused a file, by its code, never by the status alone; a clip it answers 415, a type not
 *  taken, is one that is no longer offered. */
function refusalOf(error: unknown, name: string, config: AppConfig): PendingError {
  if (error instanceof ApiError) {
    if (error.status === 415 && isClipName(name, config)) return 'clip'
    if (error.code === 'too_large') return 'size'
    if (error.code === 'storage_full') return 'storage_full'
    if (error.code === 'too_many_pending') return 'too_many_pending'
  }
  return isPurposeRefusal(error) ? 'purpose' : 'upload'
}

/** What a stored attachment is to the composer: the server says `media` for a clip, which its suffix tells apart. */
function kindOfAttachment(attachment: AttachmentOut): PendingFile['kind'] {
  if (attachment.kind === 'media') return clipKind(attachment.name)
  return attachment.kind === 'image' ? 'image' : 'document'
}

/** Whether a document's measure is still being taken: its status request waits on it as on a preparation. */
function measuring(attachment: AttachmentOut | undefined): boolean {
  return attachment?.length?.state === 'pending'
}

/** Whether a file is followed: still being prepared, or ready with its measure pending. */
function isFollowed(file: PendingFile): boolean {
  return file.status === 'processing' || (file.status === 'done' && measuring(file.attachment))
}

/** A file attached earlier, still waiting in the chat for a message. */
function waiting(attachment: AttachmentOut): PendingFile {
  return {
    key: attachment.id,
    name: attachment.name,
    size: attachment.size,
    kind: kindOfAttachment(attachment),
    status: attachment.status === 'processing' ? 'processing' : 'done',
    attachment,
  }
}

/** Delete an attachment the user takes back. Whether it is out of the composer's hands: deleted, held no more by
 *  the server (a 404), or sent meanwhile by a message (a 409 `attachment_sent`). */
async function takenBack(chatId: string, id: string): Promise<boolean> {
  try {
    await deleteAttachment(chatId, id)
    return true
  } catch (error) {
    return error instanceof ApiError && (error.status === 404 || error.code === 'attachment_sent')
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
 * code (`too_large`, `storage_full`). A clip it refuses with a 415 is one it no longer offers (Nextext went
 * down since the config was read): its error says so, and the config is read again, so the picker drops
 * the clip types.
 *
 * Files attached earlier and not sent yet, as the chat lists them, are shown
 * and sent too, so leaving a chat and coming back loses none. An attached
 * file the chat, read again since, no longer lists has been sent, from
 * another tab: its tile goes.
 *
 * A picture is prepared on the server after it is stored, and only a prepared
 * one can be sent; so is a video or audio clip, which is transcribed first and takes longer (a `video` or
 * `audio` file by its suffix, never drawn from the file in hand, never asked for a preview). Each one
 * still being prepared, uploaded here or listed by the chat, is followed once, by requests that wait on
 * the server until its preparation ends (20 s at most, then asked again): it becomes `done`, with
 * the attachment as it then is, a `failed` one included. A `processing` answer
 * is asked again no sooner than 500 ms after its request began. A request that
 * does not get through, or a server error, is tried again after 2 s; any other
 * refusal (a 404 above all: the server no longer holds the file) settles the
 * file as an error, and the chat's list never brings it back as one still
 * being prepared. Leaving the chat stops it.
 *
 * A document is ready as soon as it is stored, and is sent so: Send never waits for its measure
 * (`attachment.length.state`, `pending`). The measure is followed too, by the same requests, for as long as
 * the file is in the composer, until it is `measured` (the tile warns of a very long one) or `none`. A message
 * that takes the file ends it, and a refused one that gives the file back starts it again. A refusal of the
 * request leaves the file ready, only without its measure. A file stored before measuring began (`length`
 * null) is not followed.
 *
 * Any file can be taken back before a message sends it. One the server holds
 * is deleted there first (it is `removing` meanwhile, and nothing is sent); it
 * leaves the view only once that succeeded, or the server holds it no more (a
 * 404) or a message sent it meanwhile, and its following ends quietly. If the
 * delete fails, the file stays, marked `removeFailed`, to be sent or taken back
 * again. A file still on its way is taken out of the queue, or, being uploaded,
 * deleted as soon as it is stored; a refused file simply goes. A picture a browser can draw
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
  // The attachments in the composer as it was last drawn, by id: once a message takes a ready file, its
  // measure is no longer followed here.
  const inComposerRef = useRef<ReadonlySet<string>>(new Set())
  // A followed attachment of the chat's own list, as it is now, by id.
  const [prepared, setPrepared] = useState<ReadonlyMap<string, AttachmentOut>>(() => new Map())
  // Attachments being or having been taken back, by id: their following and their upload end quietly.
  const removedRef = useRef<Set<string>>(new Set())
  // Files taken back while on their way, by key: deleted as soon as their upload is stored.
  const abandonedRef = useRef<Set<string>>(new Set())
  // Files being taken back, and those whose taking back failed, by key.
  const [removing, setRemoving] = useState<ReadonlySet<string>>(() => new Set())
  const [removeFailed, setRemoveFailed] = useState<ReadonlySet<string>>(() => new Set())
  const queryClient = useQueryClient()
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
          if (abandonedRef.current.delete(next.key)) {
            // Taken back while on its way: it goes on the server as soon as it is there.
            removedRef.current.add(attachment.id)
            if (await takenBack(chatId, attachment.id)) {
              void queryClient.invalidateQueries({ queryKey: chatKey(chatId) })
              if (aliveRef.current) setTaken((ids) => new Set(ids).add(attachment.id))
              continue
            }
            // It could not be deleted: it comes back, said to be so, to be sent or taken back again.
            removedRef.current.delete(attachment.id)
            if (aliveRef.current) {
              setLocal((files) => [...files, { key: next.key, kind: kindOf(attachment.name, config), ...stored }])
              setRemoveFailed((keys) => new Set(keys).add(next.key))
            }
            continue
          }
          if (aliveRef.current) settle(new Set([next.key]), stored)
        } catch (error) {
          if (abandonedRef.current.delete(next.key)) continue
          const refusal = refusalOf(error, next.file.name, config)
          // The clip offer is stale (Nextext went down): it is read again, and the picker drops the clip types.
          if (refusal === 'clip') void queryClient.invalidateQueries({ queryKey: CONFIG_KEY })
          if (aliveRef.current) settle(new Set([next.key]), { status: 'error', error: refusal })
        }
      }
    } finally {
      runningRef.current = false
    }
  }, [chatId, config, needsPurpose, queryClient, settle])

  const add = useCallback(
    (files: File[]) => {
      if (chatId === null || !config.attachments || files.length === 0) return
      const added: PendingFile[] = []
      for (const file of files) {
        const key = `file-${++seqRef.current}`
        const kind = kindOf(file.name, config)
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
    // A listed attachment still preparing or measured is as the follow found it, once it has.
    const found = earlier.map((a) =>
      waiting(a.status === 'processing' || measuring(a) ? (prepared.get(a.id) ?? a) : a),
    )
    const marked = (f: PendingFile): PendingFile =>
      removing.has(f.key) || removeFailed.has(f.key)
        ? { ...f, ...(removing.has(f.key) && { removing: true }), ...(removeFailed.has(f.key) && { removeFailed: true }) }
        : f
    return [...found, ...current].map(marked)
  }, [chat, local, taken, prepared, removing, removeFailed])

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
  // taken out of that list here, so once its tile goes (removed, or a message cleared it) it
  // never comes back as a tile still being prepared, which nothing would follow again.
  const lost = useCallback((file: PendingFile) => {
    const refused = { status: 'error' as const, error: 'upload' as const }
    setLocal((files) =>
      files.some((f) => f.key === file.key)
        ? files.map((f) => (f.key === file.key ? { ...f, ...refused } : f))
        : [...files, { ...file, ...refused, removing: undefined, removeFailed: undefined }],
    )
    const id = file.attachment?.id
    if (id !== undefined) setTaken((ids) => new Set(ids).add(id))
  }, [])

  // One follower's requests, until the attachment's preparation and its measure end, the server refuses it,
  // or `gone` holds. A ready file is taken as such at once (Send does not wait for its measure), and its
  // measure is followed on, for as long as the file is in the composer. Resolves true when it ended there,
  // with the measure still pending: a file the composer gets back is followed again.
  const followed = useCallback(
    async (file: PendingFile, gone: () => boolean): Promise<boolean> => {
      const signal = controllerRef.current?.signal
      if (chatId === null || file.attachment === undefined || signal === undefined) return false
      const id = file.attachment.id
      // Whether the file has been taken as ready: from then on what is followed is only its measure.
      let ready = false
      while (!gone()) {
        if (ready && !inComposerRef.current.has(id)) return true
        const asked = Date.now()
        try {
          const latest = await getAttachment(chatId, id, POLL_WAIT_S, signal)
          if (latest.status === 'processing') {
            await pause(Math.max(0, PROCESSING_FLOOR_MS - (Date.now() - asked)), signal)
            continue
          }
          const stillMeasuring = measuring(latest)
          if (!gone() && (!ready || !stillMeasuring)) ended(file, latest)
          if (!stillMeasuring) return false
          ready = true
          await pause(Math.max(0, PROCESSING_FLOOR_MS - (Date.now() - asked)), signal)
        } catch (error) {
          if (gone()) return false
          // A refusal is no network trouble: asking again would only be refused again. A file already
          // taken as ready stays so, with its measure unknown.
          if (error instanceof ApiError && error.status < 500) {
            if (!ready) lost(file)
            return false
          }
          await pause(RETRY_MS, signal)
        }
      }
      return false
    },
    [chatId, ended, lost],
  )

  const follow = useCallback(
    async (file: PendingFile) => {
      const signal = controllerRef.current?.signal
      if (chatId === null || file.attachment === undefined || signal === undefined) return
      const id = file.attachment.id
      // Taken back: its answer, a 404 above all, is no news of the file. Its entry is let go, so a taking back
      // that fails has it followed again, by one follower.
      const gone = () => signal.aborted || removedRef.current.has(id)
      let left = false
      try {
        left = await followed(file, gone)
      } finally {
        if (left || removedRef.current.has(id)) followedRef.current.delete(id)
      }
    },
    [chatId, followed],
  )

  useEffect(() => {
    inComposerRef.current = new Set(pending.flatMap((f) => (f.attachment ? [f.attachment.id] : [])))
    for (const file of pending) {
      const id = file.attachment?.id
      if (!isFollowed(file) || id === undefined || followedRef.current.has(id)) continue
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
    // A file a message took is no longer one whose taking back failed.
    const sent = new Set(done.map((f) => f.key))
    setRemoveFailed((keys) => new Set([...keys].filter((key) => !sent.has(key))))
    return done.map((f) => ({ ...f, localUrl: undefined, removing: undefined, removeFailed: undefined }))
  }, [pending, revoke])

  const restore = useCallback((files: PendingFile[]) => {
    const ids = new Set(files.flatMap((f) => (f.attachment ? [f.attachment.id] : [])))
    setTaken((current) => new Set([...current].filter((id) => !ids.has(id))))
    setLocal((current) => [...files.filter((f) => !current.some((c) => c.key === f.key)), ...current])
  }, [])

  // Out of view for good: the server holds it no more, or a message has it.
  const forget = useCallback(
    (key: string, id: string | undefined) => {
      setLocal((files) => files.filter((f) => f.key !== key))
      revoke(key)
      if (id !== undefined) setTaken((ids) => new Set(ids).add(id))
    },
    [revoke],
  )

  const takeBack = useCallback(
    async (key: string, id: string) => {
      if (chatId === null) return
      const gone = await takenBack(chatId, id)
      // Read again even once the composer went, so the chat's cached list never offers the file again.
      if (gone) void queryClient.invalidateQueries({ queryKey: chatKey(chatId) })
      if (!aliveRef.current) return
      setRemoving((keys) => new Set([...keys].filter((k) => k !== key)))
      if (!gone) {
        // It stays: a follower that let it go for the delete starts again if it is still being prepared.
        removedRef.current.delete(id)
        setRemoveFailed((keys) => new Set(keys).add(key))
        return
      }
      forget(key, id)
    },
    [chatId, forget, queryClient],
  )

  const remove = useCallback(
    (key: string) => {
      const file = pending.find((f) => f.key === key)
      if (file === undefined || file.removing) return
      setRemoveFailed((keys) => new Set([...keys].filter((k) => k !== key)))
      if (file.status === 'uploading') {
        // Not sent yet: it never will be. Being sent: it is deleted once stored.
        const waiting = queueRef.current.length
        queueRef.current = queueRef.current.filter((q) => q.key !== key)
        if (queueRef.current.length === waiting) abandonedRef.current.add(key)
        forget(key, undefined)
        return
      }
      const id = file.attachment?.id
      if (id === undefined) {
        forget(key, undefined)
        return
      }
      removedRef.current.add(id)
      setRemoving((keys) => new Set(keys).add(key))
      void takeBack(key, id)
    },
    [pending, forget, takeBack],
  )

  return { pending, add, clear, restore, remove }
}
