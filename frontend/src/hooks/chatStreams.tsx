import { createContext, use, useEffect, useState, type ReactNode } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { stopChat } from '../api/chats'
import { ApiError } from '../api/client'
import { streamSse } from '../api/sse'
import type { ErrorFrame, MessageIn, MessageOut, RegenerateIn, ResultOut, StepOut } from '../api/types'
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

/** A chat's answer as the view shows it. */
export interface StreamState {
  streaming: boolean
  draft: DraftAnswer | null
  /** The message being sent, until the chat is read again; null for a regenerate. */
  pendingUser: PendingMessage | null
  failure: StreamFailure | null
}

export const IDLE: StreamState = { streaming: false, draft: null, pendingUser: null, failure: null }

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

/** A stream being written: what the view shows, and its connection. */
interface Live {
  state: StreamState
  controller: AbortController
}

/**
 * The app's chat streams, one per chat at most, held apart from the open
 * chat's view: leaving a chat, or starting a new one, only detaches its view,
 * and the answer keeps being written; returning shows the live draft.
 *
 * Once `final` has come, the chat and the list are read again (the answer is
 * stored, the chat has moved up the list), then the stream drops out and the
 * stored messages take the draft's place. Why an answer ended early stays
 * with its chat until the next one starts. The stop asks the server; a stream
 * ends without one only when the app goes away (a closed tab), which the
 * server takes as a stop.
 */
export class ChatStreams {
  private readonly live = new Map<string, Live>()
  private readonly ended = new Map<string, StreamState>()
  private readonly listeners = new Map<string, Set<() => void>>()
  private readonly qc: QueryClient

  constructor(qc: QueryClient) {
    this.qc = qc
  }

  /** The chat's state: its stream, or how its last one ended; null when there is nothing to show. */
  get(chatId: string): StreamState | null {
    return this.live.get(chatId)?.state ?? this.ended.get(chatId) ?? null
  }

  /** The chats whose answer is being written. */
  running(): string[] {
    return [...this.live.keys()]
  }

  /** Call `listener` whenever the chat's state changes; returns the unsubscribe. */
  subscribe(chatId: string, listener: () => void): () => void {
    const set = this.listeners.get(chatId) ?? new Set()
    set.add(listener)
    this.listeners.set(chatId, set)
    return () => {
      set.delete(listener)
      if (set.size === 0) this.listeners.delete(chatId)
    }
  }

  private emit(chatId: string) {
    this.listeners.get(chatId)?.forEach((listener) => listener())
  }

  /**
   * Send a message or regenerate, and read the answer's frames into its draft.
   *
   * @returns null while the chat already streams; false when the server
   *   refused the request with an error status, so nothing was stored; true
   *   once it was taken, also when its answer ended early.
   */
  async start(
    chatId: string,
    route: 'messages' | 'regenerate',
    body: MessageIn | RegenerateIn,
    pendingUser: PendingMessage | null,
  ): Promise<boolean | null> {
    if (this.live.has(chatId)) return null
    const controller = new AbortController()
    let draft: DraftAnswer = { text: '', steps: [], results: [], error: null, reasoning: body.reasoning }
    const entry: Live = { controller, state: { streaming: true, draft, pendingUser, failure: null } }
    this.live.set(chatId, entry)
    this.ended.delete(chatId)
    this.emit(chatId)

    let failure: StreamFailure | null = null
    let finished = false
    try {
      const frames = streamSse(`/chats/${encodeURIComponent(chatId)}/${route}`, body, controller.signal)
      for await (const { event, data } of frames) {
        // `final` is always last; nothing after it counts.
        if (finished) continue
        if (event === 'final') {
          finished = true
          continue
        }
        draft = applyFrame(draft, event, data)
        entry.state = { ...entry.state, draft }
        this.emit(chatId)
      }
      if (!finished) failure = { kind: 'answer', code: 'internal' }
    } catch (error) {
      if (!controller.signal.aborted) failure = { kind: 'request', error }
    }
    if (draft.error) failure = { kind: 'answer', code: draft.error }
    // Refused with a status, never begun: a cut connection may have been taken.
    const taken = !(failure?.kind === 'request' && failure.error instanceof ApiError)

    // What the server stored, even of an answer cut short: the chat with its
    // messages, and the list, which a new answer or a regenerate reorders.
    await Promise.all([
      this.qc.invalidateQueries({ queryKey: chatKey(chatId) }),
      this.qc.invalidateQueries({ queryKey: chatsKey }),
    ])
    this.live.delete(chatId)
    if (failure) this.ended.set(chatId, { ...IDLE, failure })
    this.emit(chatId)
    return taken
  }

  /** Ask the server to stop the chat's answer; if it cannot be asked, close the connection, which stops it too. */
  async stop(chatId: string): Promise<void> {
    const entry = this.live.get(chatId)
    if (!entry) return
    try {
      await stopChat(chatId)
    } catch {
      // The server also stops an answer whose connection closes; `final` is lost then.
      entry.controller.abort()
    }
  }

  /** Close every connection, as the app goes away. */
  abortAll() {
    this.live.forEach((entry) => entry.controller.abort())
  }
}

const ChatStreamsContext = createContext<ChatStreams | null>(null)

/** Holds the app's chat streams above the open chat, so they outlive its view. */
export function ChatStreamsProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const [streams] = useState(() => new ChatStreams(qc))
  useEffect(() => () => streams.abortAll(), [streams])
  return <ChatStreamsContext value={streams}>{children}</ChatStreamsContext>
}

/** The app's chat streams. */
export function useChatStreams(): ChatStreams {
  const streams = use(ChatStreamsContext)
  if (streams === null) throw new Error('useChatStreams needs a ChatStreamsProvider')
  return streams
}
