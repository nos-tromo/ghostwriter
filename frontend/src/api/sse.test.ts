import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearAllPurposes } from '@infra/ui'
import { streamSse, type SseEvent } from './sse'

afterEach(() => {
  clearAllPurposes()
  vi.unstubAllGlobals()
})

/** A response whose body arrives in the given chunks. */
function body(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)))
      controller.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'content-type': 'text/event-stream' } })
}

describe('streamSse', () => {
  it('yields frames only: the keep-alive comments of a quiet answer are skipped, also split across chunks', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        body([
          ': keep-alive\n\n',
          'event: token\ndata: {"text":"Sum"}\n\n: keep',
          '-alive\n\nevent: token\ndata: {"text":"me"}\n\n',
          ': keep-alive\n\nevent: final\ndata: {"stopped":false}\n\n',
        ]),
      ),
    )
    const events: SseEvent[] = []
    for await (const event of streamSse('/chats/c1/messages', { content: 'Summe?' })) events.push(event)
    expect(events).toEqual([
      { event: 'token', data: { text: 'Sum' } },
      { event: 'token', data: { text: 'me' } },
      { event: 'final', data: { stopped: false } },
    ])
  })
})
