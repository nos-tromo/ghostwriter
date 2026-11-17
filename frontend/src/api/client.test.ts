import { afterEach, describe, expect, it, vi } from 'vitest'
import { PURPOSE_HEADER, clearAllPurposes, setPurpose } from '@infra/ui'
import { ApiError, apiBase } from './client'
import {
  createChat,
  deleteChat,
  fileBlob,
  getChat,
  getConfig,
  getWhoami,
  listChats,
  renameChat,
  stopChat,
  transcribe,
  uploadAttachment,
} from './chats'

afterEach(() => {
  clearAllPurposes()
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

const CHAT = {
  id: 'c1',
  title: 'Quarterly figures',
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-02T09:00:00Z',
  expires_at: '2027-01-30T09:00:00Z',
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

/** Stub fetch with one answer per `METHOD path`; every call is kept. */
function serve(routes: Record<string, () => Response>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const route = routes[`${init?.method ?? 'GET'} ${String(input)}`]
    return route ? route() : json({ detail: 'not found' }, 404)
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

function sent(fn: ReturnType<typeof serve>) {
  return fn.mock.calls.map(([input, init]) => ({
    method: init?.method ?? 'GET',
    url: String(input),
    headers: (init?.headers ?? {}) as Record<string, string>,
    body: init?.body,
  }))
}

describe('apiBase', () => {
  it('derives the API base from the sub-path the SPA is served under', () => {
    expect(apiBase('/ghostwriter/')).toBe('/ghostwriter/api')
  })
  it('falls back to root for BASE_URL "/"', () => {
    expect(apiBase('/')).toBe('/api')
  })
})

describe('the API client', () => {
  it('sends every request under /ghostwriter/api with the stated purpose', async () => {
    setPurpose('ghostwriter', { reason: 'review' })
    const fetchFn = serve({
      'GET /ghostwriter/api/chats': () => json([CHAT]),
      'POST /ghostwriter/api/chats': () => json(CHAT, 201),
      'GET /ghostwriter/api/chats/c1': () => json({ chat: CHAT, messages: [], pending_attachments: [] }),
      'PATCH /ghostwriter/api/chats/c1': () => json({ ...CHAT, title: 'Q3 Zahlen' }),
      'DELETE /ghostwriter/api/chats/c1': () => new Response(null, { status: 204 }),
      'POST /ghostwriter/api/chats/c1/stop': () => new Response(null, { status: 204 }),
      'POST /ghostwriter/api/chats/c1/attachments': () =>
        json(
          {
            id: 'a1',
            file_id: 'f1',
            name: 'sales.csv',
            size: 3,
            mime: 'text/csv',
            created_at: '2026-11-02T09:00:00Z',
            expired: false,
          },
          201,
        ),
      'GET /ghostwriter/api/chats/c1/files/f1': () =>
        new Response('a,b', { status: 200, headers: { 'content-type': 'text/csv' } }),
      'POST /ghostwriter/api/transcribe': () => json({ text: 'Summe der Umsätze' }),
      'GET /ghostwriter/api/config': () => json({ ui_language: 'de' }),
      'GET /ghostwriter/api/whoami': () => json({ username: 'user-a', display_name: null }),
    })

    expect(await listChats()).toEqual([CHAT])
    expect(await createChat()).toEqual(CHAT)
    expect((await getChat('c1')).chat).toEqual(CHAT)
    expect((await renameChat('c1', 'Q3 Zahlen')).title).toBe('Q3 Zahlen')
    await expect(deleteChat('c1')).resolves.toBeUndefined()
    await expect(stopChat('c1')).resolves.toBeUndefined()
    expect((await uploadAttachment('c1', new File(['a,b'], 'sales.csv', { type: 'text/csv' }))).file_id).toBe('f1')
    expect(await (await fileBlob('c1', 'f1')).text()).toBe('a,b')
    expect(await transcribe(new Blob(['…']), 'audio/webm')).toBe('Summe der Umsätze')
    expect((await getConfig()).ui_language).toBe('de')
    expect((await getWhoami()).username).toBe('user-a')

    const calls = sent(fetchFn)
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      'GET /ghostwriter/api/chats',
      'POST /ghostwriter/api/chats',
      'GET /ghostwriter/api/chats/c1',
      'PATCH /ghostwriter/api/chats/c1',
      'DELETE /ghostwriter/api/chats/c1',
      'POST /ghostwriter/api/chats/c1/stop',
      'POST /ghostwriter/api/chats/c1/attachments',
      'GET /ghostwriter/api/chats/c1/files/f1',
      'POST /ghostwriter/api/transcribe',
      'GET /ghostwriter/api/config',
      'GET /ghostwriter/api/whoami',
    ])
    for (const call of calls) expect(call.headers[PURPOSE_HEADER]).toBe('reason=review')
    expect(calls[3].body).toBe(JSON.stringify({ title: 'Q3 Zahlen' }))
  })

  it('sends a file as the multipart field "file", under its own name and type', async () => {
    const fetchFn = serve({
      'POST /ghostwriter/api/chats/c1/attachments': () => json({ id: 'a1' }, 201),
      'POST /ghostwriter/api/transcribe': () => json({ text: 'Hallo' }),
    })
    await uploadAttachment('c1', new File(['a,b'], 'sales.csv', { type: 'text/csv' }))
    await transcribe(new Blob(['…']), 'audio/ogg; codecs=opus')

    const [upload, recording] = sent(fetchFn)
    const attached = (upload.body as FormData).get('file') as File
    expect(attached.name).toBe('sales.csv')
    expect(await attached.text()).toBe('a,b')
    expect((recording.body as FormData).get('file')).toMatchObject({ type: 'audio/ogg; codecs=opus' })
    // The browser sets the multipart boundary itself.
    expect(upload.headers).not.toHaveProperty('content-type')
  })

  it("hands a refusal's status, detail and code to the caller", async () => {
    serve({
      'POST /ghostwriter/api/chats/c1/attachments': () =>
        json({ detail: 'The file is larger than 50 MB.', code: 'too_large' }, 413),
      'DELETE /ghostwriter/api/chats/c1': () => json({ detail: 'Stop the answer before deleting the chat.' }, 409),
    })
    await expect(uploadAttachment('c1', new File(['x'], 'big.pdf'))).rejects.toMatchObject({
      name: 'ApiError',
      status: 413,
      detail: 'The file is larger than 50 MB.',
      code: 'too_large',
    } satisfies Partial<ApiError>)
    await expect(deleteChat('c1')).rejects.toMatchObject({ status: 409, code: null })
  })

  it('reads a file that is gone as an ApiError, not as a blob', async () => {
    serve({ 'GET /ghostwriter/api/chats/c1/files/f1': () => json({ detail: 'The file has expired.' }, 410) })
    await expect(fileBlob('c1', 'f1')).rejects.toMatchObject({ name: 'ApiError', status: 410 })
  })
})
