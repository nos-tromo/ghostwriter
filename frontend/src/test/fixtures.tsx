// Synthetic chats, messages, steps and results, a fetch stub and an SSE body
// for the conversation's tests. Invented data only.
import type { ComponentProps, ReactElement, ReactNode } from 'react'
import { vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AppConfig, ChatDetail, ChatOut, MessageOut, ResultOut, StepOut } from '../api/types'
import { Conversation } from '../components/Conversation'
import { ChatStreamsProvider } from '../hooks/chatStreams'
import { useChatStream, type ChatStream } from '../hooks/useChatStream'

export const CONFIG: AppConfig = {
  ui_language: 'en',
  require_purpose: [],
  speech_input: true,
  attachments: true,
  attachment_types: ['.csv', '.pdf', '.xlsx'],
  image_types: [],
  media_types: [],
  media: false,
  max_images: 8,
  vision: false,
  max_attachment_bytes: 50 * 1024 * 1024,
  max_recording_s: 120,
  max_recording_bytes: 10 * 1024 * 1024,
  chat_retention_days: 90,
  file_retention_days: 7,
  version: '0.1.0',
}

/** A deployment whose model sees pictures: `.png`, `.jpg` and `.heic` can be attached. */
export const VISION_CONFIG: AppConfig = {
  ...CONFIG,
  attachment_types: [...CONFIG.attachment_types, '.png', '.jpg', '.heic'],
  image_types: ['.png', '.jpg', '.heic'],
  vision: true,
}

/** A deployment that transcribes clips: `.mp4`, `.m4a` and `.webm` can be attached. */
export const MEDIA_CONFIG: AppConfig = {
  ...CONFIG,
  attachment_types: [...CONFIG.attachment_types, '.mp4', '.m4a', '.webm'],
  media_types: ['.mp4', '.m4a', '.webm'],
  media: true,
}

export const CHAT: ChatOut = {
  id: 'c1',
  title: 'Quarterly figures',
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-02T09:00:00Z',
  expires_at: '2027-01-30T09:00:00Z',
}

export function message(fields: Partial<MessageOut> & Pick<MessageOut, 'id' | 'role'>): MessageOut {
  return {
    content: '',
    created_at: '2026-11-02T09:00:00Z',
    attachments: [],
    steps: [],
    results: [],
    notices: null,
    context_estimate: null,
    ...fields,
  }
}

export function step(fields: Partial<StepOut> & Pick<StepOut, 'id' | 'tool'>): StepOut {
  return { status: 'ok', detail: null, arguments: null, result: null, expired: false, ...fields }
}

export function result(fields: Partial<ResultOut> & Pick<ResultOut, 'id' | 'kind'>): ResultOut {
  return { file_id: 'F', name: 'sales.xlsx', mime: 'application/octet-stream', size: 2048, preview: null, expired: false, ...fields }
}

export function detail(messages: MessageOut[], chat: ChatOut = CHAT): ChatDetail {
  return { chat, messages, pending_attachments: [] }
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

export type Call = { method: string; url: string; headers: Record<string, string>; body: unknown; signal?: AbortSignal }
/** A JSON body, `null` for 204, or a function answering the request itself. */
export type Answer = unknown | ((call: Call) => Response | Promise<Response>)

/** Stub fetch with one answer per `METHOD path`; every call is kept, its JSON body parsed. */
export function serve(routes: Record<string, Answer>): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: Call = {
        method: init?.method ?? 'GET',
        url: String(input),
        headers: (init?.headers ?? {}) as Record<string, string>,
        body: typeof init?.body === 'string' ? JSON.parse(init.body) : init?.body,
        signal: init?.signal ?? undefined,
      }
      calls.push(call)
      const key = `${call.method} ${call.url}`
      if (!(key in routes)) return json({ detail: 'not found' }, 404)
      const answer = routes[key]
      if (typeof answer === 'function') return (answer as (call: Call) => Response | Promise<Response>)(call)
      if (answer === null) return new Response(null, { status: 204 })
      return json(answer)
    }),
  )
  return calls
}

/** Stub the clipboard; returns its `writeText`, which every copy calls. */
export function stubClipboard() {
  const writeText = vi.fn(async (_text: string) => {})
  vi.spyOn(navigator, 'clipboard', 'get').mockReturnValue({ writeText } as unknown as Clipboard)
  return writeText
}

/** The calls to one route. */
export function callsTo(calls: Call[], method: string, url: string): Call[] {
  return calls.filter((call) => call.method === method && call.url === url)
}

/** An SSE response the test writes frames into, as the backend's `sse_frame` does. */
export function sseStream() {
  const encoder = new TextEncoder()
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
    },
  })
  return {
    response: () => new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    push(event: string, data: unknown) {
      controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))
    },
    /** A comment line, as the backend's keep-alive: no frame. */
    comment(text: string) {
      controller.enqueue(encoder.encode(`: ${text}\n\n`))
    },
    end() {
      controller.close()
    },
  }
}

/** Render under a fresh query client that does not retry, and the app's
 *  registry of chat streams; `rerender` keeps both. */
export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrap = (node: ReactElement) => (
    <QueryClientProvider client={client}>
      <ChatStreamsProvider>{node}</ChatStreamsProvider>
    </QueryClientProvider>
  )
  const view = render(wrap(ui))
  return { ...view, client, rerender: (next: ReactElement) => view.rerender(wrap(next)) }
}

/** The conversation over a stream of its own, as `ChatPane` gives it one;
 *  `composer` renders below it over the same stream. */
export function StreamedConversation({
  composer,
  ...props
}: Omit<ComponentProps<typeof Conversation>, 'stream' | 'composer'> & {
  composer?: (stream: ChatStream) => ReactNode
}) {
  const stream = useChatStream(props.chatId)
  return <Conversation {...props} stream={stream} composer={composer?.(stream)} />
}
