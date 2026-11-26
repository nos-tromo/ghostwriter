import { afterEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PURPOSE_HEADER, PurposeBar, clearAllPurposes, setPurpose } from '@infra/ui'
import { ApiError, PURPOSE_SCOPE, apiBase } from './client'
import {
  createChat,
  deleteChat,
  attachmentPreview,
  fileBlob,
  getAttachment,
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
            kind: 'document',
            status: 'ready',
            length: null,
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

  it('reads one attachment after a wait, and its picture as a blob of a page', async () => {
    setPurpose('ghostwriter', { reason: 'review' })
    const fetchFn = serve({
      'GET /ghostwriter/api/chats/c1/attachments/a%201?wait=20': () =>
        json({ id: 'a 1', kind: 'image', status: 'ready', expired: false }),
      'GET /ghostwriter/api/chats/c1/attachments/a1/preview?page=1': () =>
        new Response('png', { status: 200, headers: { 'content-type': 'image/png' } }),
      'GET /ghostwriter/api/chats/c1/attachments/a1/preview?page=3': () =>
        json({ detail: 'This file has expired.' }, 410),
    })
    const controller = new AbortController()
    expect(await getAttachment('c1', 'a 1', 20, controller.signal)).toMatchObject({ kind: 'image', status: 'ready' })
    expect(await (await attachmentPreview('c1', 'a1')).text()).toBe('png')
    await expect(attachmentPreview('c1', 'a1', 3)).rejects.toMatchObject({ name: 'ApiError', status: 410 })

    const calls = sent(fetchFn)
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      'GET /ghostwriter/api/chats/c1/attachments/a%201?wait=20',
      'GET /ghostwriter/api/chats/c1/attachments/a1/preview?page=1',
      'GET /ghostwriter/api/chats/c1/attachments/a1/preview?page=3',
    ])
    for (const call of calls) expect(call.headers[PURPOSE_HEADER]).toBe('reason=review')
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

  it('sends the same file again after a 428, once a purpose is stated', async () => {
    let tries = 0
    const fetchFn = serve({
      'POST /ghostwriter/api/chats/c1/attachments': () =>
        ++tries === 1
          ? json({ detail: 'A stated purpose is required for this request.' }, 428)
          : json({ id: 'a1', file_id: 'f1', name: 'sales.csv' }, 201),
    })
    const user = userEvent.setup()
    render(createElement(PurposeBar, { scope: PURPOSE_SCOPE, required: true }))

    const upload = uploadAttachment('c1', new File(['a,b'], 'sales.csv', { type: 'text/csv' }))
    await user.type(await screen.findByLabelText('Case reference'), 'case-0001{Enter}')
    expect((await upload).id).toBe('a1')

    const [refused, resent] = sent(fetchFn)
    expect(refused.headers[PURPOSE_HEADER]).toBeUndefined()
    expect(resent.headers[PURPOSE_HEADER]).toBe('case_ref=case-0001')
    // The multipart body is built once and sent twice, whole.
    expect(resent.body).toBe(refused.body)
    const file = (resent.body as FormData).get('file') as File
    expect(file.name).toBe('sales.csv')
    expect(await file.text()).toBe('a,b')
  })

  it('sends the same recording again after a 428, once a purpose is stated', async () => {
    let tries = 0
    const fetchFn = serve({
      'POST /ghostwriter/api/transcribe': () =>
        ++tries === 1
          ? json({ detail: 'A stated purpose is required for this request.' }, 428)
          : json({ text: 'Summe der Spalte B' }),
    })
    const user = userEvent.setup()
    render(createElement(PurposeBar, { scope: PURPOSE_SCOPE, required: true }))

    const text = transcribe(new Blob(['synthetic voice']), 'audio/ogg;codecs=opus')
    await user.type(await screen.findByLabelText('Reason'), 'Prüfung{Enter}')
    expect(await text).toBe('Summe der Spalte B')

    const [refused, resent] = sent(fetchFn)
    expect(resent.headers[PURPOSE_HEADER]).toBe('reason=Pr%C3%BCfung')
    expect(resent.body).toBe(refused.body)
    const recording = (resent.body as FormData).get('file') as File
    expect(recording.type).toBe('audio/ogg;codecs=opus')
    expect(await recording.text()).toBe('synthetic voice')
  })

  it('reads a file that is gone as an ApiError, not as a blob', async () => {
    serve({ 'GET /ghostwriter/api/chats/c1/files/f1': () => json({ detail: 'The file has expired.' }, 410) })
    await expect(fileBlob('c1', 'f1')).rejects.toMatchObject({ name: 'ApiError', status: 410 })
  })
})
