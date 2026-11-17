import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { stopChat } from '../api/chats'
import { ApiError } from '../api/client'
import { streamSse } from '../api/sse'
import type { AttachmentOut, ErrorFrame, MessageIn, MessageOut, RegenerateIn, ResultOut, StepOut } from '../api/types'
import { chatKey, chatsKey } from './useChats'

/** The answer being written, as its frames have built it so far. */
export interface DraftAnswer {
  text: string
  steps: StepOut[]
  results: ResultOut[]
  error: ErrorFrame['code'] | null
  /** The answer's reasoning switch: until its first token, the model is thinking. */
  reasoning: boolean
}

/** Why the last answer ended early (`answer`, an `error` frame or a stream cut
 *  off), or never started (`request`); kept in view until the next one. */
export type StreamFailure = { kind: 'answer'; code: ErrorFrame['code'] } | { kind: 'request'; error: unknown }

/** The message being sent, as its bubble shows it: its text and the files it binds. */
export type PendingMessage = Pick<MessageOut, 'content' | 'attachments'>

export interface ChatStream {
  streaming: boolean
  draft: DraftAnswer | null
  /** The message being sent, until the chat is read again; null for a regenerate. */
  pendingUser: PendingMessage | null
  failure: StreamFailure | null
  /** Send a message with the chat's pending attachments it binds (sent by their ids).
   *  Resolves false when the server refused it with an error status, so it was
   *  not stored; true once it was taken, also when its answer ended early. */
  send(content: string, attachments: readonly AttachmentOut[], reasoning: boolean): Promise<boolean>
  regenerate(reasoning: boolean): Promise<void>
  stop(): Promise<void>
}

type State = Pick<ChatStream, 'streaming' | 'draft' | 'pendingUser' | 'failure'>

const IDLE: State = { streaming: false, draft: null, pendingUser: null, failure: null }
const ERROR_CODES: ReadonlySet<string> = new Set<ErrorFrame['code']>(['model', 'audit', 'internal'])

/** The draft with one frame applied: a token appends, a step is upserted by
 *  its id (`running`, then `ok` or `error`), a result appends, an error sets
 *  its code. */
function applyFrame(draft: DraftAnswer, event: string, data: unknown): DraftAnswer {
  switch (event) {
    case 'token': {
      const text = (data as { text?: unknown } | null)?.text
      return typeof text === 'string' ? { ...draft, text: draft.text + text } : draft
    }
    case 'step': {
      const step = data as StepOut
      const known = draft.steps.some((s) => s.id === step.id)
      return { ...draft, steps: known ? draft.steps.map((s) => (s.id === step.id ? step : s)) : [...draft.steps, step] }
    }
    case 'result':
      return { ...draft, results: [...draft.results, data as ResultOut] }
    case 'error': {
      const code = (data as { code?: unknown } | null)?.code
      return { ...draft, error: typeof code === 'string' && ERROR_CODES.has(code) ? (code as ErrorFrame['code']) : 'internal' }
    }
    default:
      return draft
  }
}

/**
 * A chat's answer as it streams: send a message or regenerate the last answer,
 * read its frames into a draft, and stop it.
 *
 * Once `final` has come, the chat and the list are read again (the answer is
 * stored, the chat has moved up the list) and the draft gives way to the
 * stored messages. One answer at a time. An unmount, or another chat, closes
 * the connection, which stops the answer on the server; what it wrote so far
 * is stored there.
 *
 * @param chatId - The chat, or null for none.
 * @returns The stream's state and its actions.
 */
export function useChatStream(chatId: string | null): ChatStream {
  const qc = useQueryClient()
  const [state, setState] = useState<State>(IDLE)
  // The run in flight: its number (bumped to orphan it), whether one runs, and its connection.
  const runRef = useRef(0)
  const busyRef = useRef(false)
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(
    () => () => {
      runRef.current += 1
      busyRef.current = false
      controllerRef.current?.abort()
      controllerRef.current = null
      setState(IDLE)
    },
    [chatId],
  )

  const run = useCallback(
    async (
      route: 'messages' | 'regenerate',
      body: MessageIn | RegenerateIn,
      pendingUser: PendingMessage | null,
    ): Promise<boolean> => {
      if (chatId === null || busyRef.current) return false
      busyRef.current = true
      const me = ++runRef.current
      const live = () => runRef.current === me
      const controller = new AbortController()
      controllerRef.current = controller

      let draft: DraftAnswer = { text: '', steps: [], results: [], error: null, reasoning: body.reasoning }
      let failure: StreamFailure | null = null
      let ended = false
      setState({ streaming: true, draft, pendingUser, failure: null })
      try {
        const frames = streamSse(`/chats/${encodeURIComponent(chatId)}/${route}`, body, controller.signal)
        for await (const { event, data } of frames) {
          // `final` is always last; nothing after it counts.
          if (ended) continue
          if (event === 'final') {
            ended = true
            continue
          }
          draft = applyFrame(draft, event, data)
          const next = draft
          if (live()) setState((current) => ({ ...current, draft: next }))
        }
        if (!ended) failure = { kind: 'answer', code: 'internal' }
      } catch (error) {
        if (!controller.signal.aborted) failure = { kind: 'request', error }
      }
      if (draft.error) failure = { kind: 'answer', code: draft.error }
      // Refused with a status, never begun: a cut connection may have been taken.
      const taken = !(failure?.kind === 'request' && failure.error instanceof ApiError)

      // What the server stored, even of an answer cut short: the chat with its
      // messages, and the list, which a new answer or a regenerate reorders.
      await Promise.all([
        qc.invalidateQueries({ queryKey: chatKey(chatId) }),
        qc.invalidateQueries({ queryKey: chatsKey }),
      ])
      if (!live()) return taken
      busyRef.current = false
      controllerRef.current = null
      setState({ ...IDLE, failure })
      return taken
    },
    [chatId, qc],
  )

  const send = useCallback(
    (content: string, attachments: readonly AttachmentOut[], reasoning: boolean) =>
      run(
        'messages',
        { content, attachment_ids: attachments.map((a) => a.id), reasoning } satisfies MessageIn,
        { content, attachments: [...attachments] },
      ),
    [run],
  )

  const regenerate = useCallback(
    async (reasoning: boolean) => {
      await run('regenerate', { reasoning } satisfies RegenerateIn, null)
    },
    [run],
  )

  const stop = useCallback(async () => {
    if (chatId === null || !busyRef.current) return
    try {
      await stopChat(chatId)
    } catch {
      // The server also stops an answer whose connection closes; `final` is lost then.
      controllerRef.current?.abort()
    }
  }, [chatId])

  return { ...state, send, regenerate, stop }
}
