// Copied from docint's frontend/src/api/sse.ts, without its GET variant: every
// stream here answers a POST (a message, a regenerate).
import { ApiError, apiErrorOf, apiFetch } from './client'

export interface SseEvent {
  event: string
  data: unknown
}

/**
 * POST a JSON body and yield the SSE frames the server streams back.
 *
 * Read through `fetch` rather than `EventSource`, which can send neither a
 * body nor the `X-Audit-Purpose` header; `apiFetch` adds the purpose and
 * resends once after a `428`, which the JSON string body allows.
 *
 * @param path - API path (the base is applied by `apiFetch`).
 * @param body - The JSON body.
 * @param signal - Optional abort signal closing the connection.
 * @yields Parsed SSE events.
 */
export async function* streamSse(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): AsyncGenerator<SseEvent, void, unknown> {
  const res = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })
  if (!res.ok) throw await apiErrorOf(res)
  if (!res.body) throw new ApiError(res.status, `SSE request failed: ${res.status}`)
  yield* readSseBody(res.body)
}

/** The frame-reading loop: frames end with a blank line, and may arrive split across chunks. */
async function* readSseBody(body: ReadableStream<Uint8Array>): AsyncGenerator<SseEvent, void, unknown> {
  const reader = body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let sep: number
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const event = parseFrame(frame)
      if (event) yield event
    }
  }
}

function parseFrame(frame: string): SseEvent | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (dataLines.length === 0) return null
  const raw = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(raw) }
  } catch {
    return { event, data: raw }
  }
}
