import { afterEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { clearAllPurposes } from '@infra/ui'
import { LanguageContext } from '../i18n/LanguageContext'
import type { AttachmentOut } from '../api/types'
import { useChatStreams, type ChatStreams } from './chatStreams'
import type { ChatStream } from './useChatStream'
import { useChats } from './useChats'
import {
  CHAT,
  CONFIG,
  callsTo,
  detail,
  json,
  message,
  renderWithClient,
  result,
  serve,
  sseStream,
  step,
  StreamedConversation,
} from '../test/fixtures'

afterEach(() => {
  clearAllPurposes()
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

const QUESTION = message({ id: 'm1', role: 'user', content: 'Summe der Spalte B?' })
const ANSWER = message({
  id: 'm2',
  role: 'assistant',
  content: 'Summe: 42',
  steps: [step({ id: 's1', tool: 'read_document', detail: 'sales.xlsx' })],
})

const READING = step({ id: 's1', tool: 'read_document', status: 'running', detail: 'sales.xlsx' })
const TABLE = result({
  id: 'r1',
  kind: 'table',
  file_id: 'F1',
  name: 'summe.csv',
  mime: 'text/csv',
  size: 120,
  preview: { columns: ['Monat', 'Betrag'], rows: [['Jan', '42']], row_count: 1, truncated: false },
})
const IMAGES_DROPPED = 'The model did not take the pictures; it answered without them.'
const FINAL = { message_id: 'm2', user_message_id: 'm1', chat: CHAT, stopped: false, context_estimate: null }
const SALES: AttachmentOut = {
  id: 'a1',
  file_id: 'F0',
  name: 'sales.xlsx',
  size: 4096,
  mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  created_at: '2026-11-02T09:00:00Z',
  expired: false,
  kind: 'document',
  status: 'ready',
  length: null,
}

/** A stand-in for Task 14's composer: buttons over the conversation's stream. */
function controls(stream: ChatStream) {
  return (
    <div>
      <button type="button" onClick={() => void stream.send('Summe der Spalte B?', [], false)}>
        Ask
      </button>
      <button type="button" onClick={() => void stream.send('Summe der Spalte B?', [], true)}>
        Ask and think
      </button>
      <button type="button" onClick={() => void stream.send('', [SALES], false)}>
        Send the file
      </button>
      <button type="button" onClick={() => void stream.stop()}>
        Halt
      </button>
    </div>
  )
}

/** Keeps the chat list observed, as the sidebar does, so its refetch can be seen. */
function ListObserver() {
  useChats()
  return null
}

/** Serve the chat first as `before`, and as `after` once the answer is done. */
function serveChat(before: ReturnType<typeof detail>, after: ReturnType<typeof detail>, post: string) {
  const sse = sseStream()
  let done = false
  const calls = serve({
    'GET /ghostwriter/api/chats/c1': () => new Response(JSON.stringify(done ? after : before), { status: 200 }),
    'GET /ghostwriter/api/chats': [CHAT],
    [`POST /ghostwriter/api/chats/c1/${post}`]: () => sse.response(),
    'POST /ghostwriter/api/chats/c1/stop': null,
  })
  return {
    calls,
    sse,
    finish(final: unknown = FINAL) {
      done = true
      sse.push('final', final)
      sse.end()
    },
  }
}

describe('useChatStream', () => {
  it('streams tokens, steps and results into the draft, then refetches the chat', async () => {
    const { calls, sse, finish } = serveChat(detail([]), detail([QUESTION, ANSWER]), 'messages')
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')

    await user.click(screen.getByRole('button', { name: 'Ask' }))
    expect(await screen.findByText('Summe der Spalte B?')).toBeInTheDocument()
    expect(callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/messages').map((c) => c.body)).toEqual([
      { content: 'Summe der Spalte B?', attachment_ids: [], reasoning: false },
    ])

    sse.push('step', READING)
    expect(await screen.findByText('Read sales.xlsx')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Running' })).toBeInTheDocument()
    sse.push('step', { ...READING, status: 'ok', result: 'Monat;Betrag' })
    expect(await screen.findByRole('img', { name: 'Done' })).toBeInTheDocument()
    // The same step, upserted by its id: one row.
    expect(screen.getAllByText('Read sales.xlsx')).toHaveLength(1)
    sse.push('result', TABLE)
    expect(await screen.findByText('summe.csv', { selector: 'caption' })).toBeInTheDocument()
    sse.push('token', { text: 'Summe' })
    expect(await screen.findByText('Summe')).toBeInTheDocument()
    expect(callsTo(calls, 'GET', '/ghostwriter/api/chats/c1')).toHaveLength(1)

    finish()
    // The stored answer replaces the draft: its trail is collapsed now.
    expect(await screen.findByText('Summe: 42')).toBeInTheDocument()
    expect(callsTo(calls, 'GET', '/ghostwriter/api/chats/c1')).toHaveLength(2)
    expect(screen.getByRole('button', { name: '1 step' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('summe.csv', { selector: 'caption' })).not.toBeInTheDocument()
  })

  it('skips the keep-alive comments a quiet answer carries', async () => {
    const { sse, finish } = serveChat(detail([]), detail([QUESTION, ANSWER]), 'messages')
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))

    sse.comment('keep-alive')
    sse.push('token', { text: 'Sum' })
    sse.comment('keep-alive')
    sse.comment('keep-alive')
    sse.push('token', { text: 'me' })
    expect(await screen.findByText('Summe')).toBeInTheDocument()
    expect(screen.queryByText(/keep-alive/)).not.toBeInTheDocument()
    finish()
    expect(await screen.findByText('Summe: 42')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says it is thinking while a reasoning answer holds its text, and that it is working otherwise', async () => {
    const thinking = serveChat(detail([]), detail([QUESTION, ANSWER]), 'messages')
    const first = renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')

    await user.click(screen.getByRole('button', { name: 'Ask and think' }))
    expect(await screen.findByText('thinking…')).toBeInTheDocument()
    expect(callsTo(thinking.calls, 'POST', '/ghostwriter/api/chats/c1/messages')[0].body).toEqual({
      content: 'Summe der Spalte B?',
      attachment_ids: [],
      reasoning: true,
    })
    // A tool step is no text: the answer is still thinking.
    thinking.sse.push('step', READING)
    await screen.findByText('Read sales.xlsx')
    expect(screen.getByText('thinking…')).toBeInTheDocument()
    thinking.sse.push('token', { text: 'Summe' })
    await screen.findByText('Summe')
    expect(screen.queryByText('thinking…')).not.toBeInTheDocument()
    thinking.finish()
    await screen.findByText('Summe: 42')
    first.unmount()

    serveChat(detail([]), detail([QUESTION, ANSWER]), 'messages')
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    expect(await screen.findByText('Working on an answer…')).toBeInTheDocument()
    expect(screen.queryByText('thinking…')).not.toBeInTheDocument()
  })

  it('regenerates in place of the last answer and moves the chat up the list', async () => {
    const OLD = message({ id: 'm2', role: 'assistant', content: 'Summe: 41' })
    const { calls, sse, finish } = serveChat(detail([QUESTION, OLD]), detail([QUESTION, ANSWER]), 'regenerate')
    renderWithClient(
      <>
        <ListObserver />
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={true} />
      </>,
    )
    const user = userEvent.setup()
    expect(await screen.findByText('Summe: 41')).toBeInTheDocument()
    await waitFor(() => expect(callsTo(calls, 'GET', '/ghostwriter/api/chats')).toHaveLength(1))

    await user.click(screen.getByRole('button', { name: 'Regenerate' }))
    expect(callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/regenerate').map((c) => c.body)).toEqual([
      { reasoning: true },
    ])
    // The answer being replaced is gone; the question stays, once.
    await waitFor(() => expect(screen.queryByText('Summe: 41')).not.toBeInTheDocument())
    expect(screen.getAllByText('Summe der Spalte B?')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Regenerate' })).not.toBeInTheDocument()
    sse.push('token', { text: 'Summe: 42' })
    await screen.findByText('Summe: 42')

    finish({ ...FINAL, user_message_id: null })
    await waitFor(() => expect(callsTo(calls, 'GET', '/ghostwriter/api/chats')).toHaveLength(2))
    expect(callsTo(calls, 'GET', '/ghostwriter/api/chats/c1')).toHaveLength(2)
    expect(await screen.findByRole('button', { name: 'Regenerate' })).toBeInTheDocument()
  })

  it('ends a step left running when the record failed, and keeps the reason in view', async () => {
    const { sse, finish } = serveChat(detail([]), detail([QUESTION, message({ id: 'm2', role: 'assistant' })]), 'messages')
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))

    sse.push('step', READING)
    await screen.findByRole('status', { name: 'Running' })
    sse.push('error', { code: 'audit', message: 'The answer stopped: it could not be recorded.' })
    sse.push('step', { ...READING, status: 'error' })
    expect(await screen.findByRole('img', { name: 'Failed' })).toBeInTheDocument()
    expect(screen.getAllByText('Read sales.xlsx')).toHaveLength(1)
    expect(screen.getByRole('alert')).toHaveTextContent('The answer stopped: it could not be recorded.')

    finish()
    // The step was never stored, so it is gone with the draft; the reason stays.
    await waitFor(() => expect(screen.queryByText('Read sales.xlsx')).not.toBeInTheDocument())
    expect(screen.getByRole('alert')).toHaveTextContent('The answer stopped: it could not be recorded.')
  })

  it('shows the files of a message sent without text in its bubble', async () => {
    const { calls, finish } = serveChat(
      detail([]),
      detail([message({ id: 'm1', role: 'user', attachments: [SALES] }), ANSWER]),
      'messages',
    )
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')

    await user.click(screen.getByRole('button', { name: 'Send the file' }))
    const bubble = (await screen.findByText('sales.xlsx')).closest('[data-role="user"]') as HTMLElement
    expect(bubble).toHaveTextContent('sales.xlsx4 KB')
    expect(callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/messages').map((c) => c.body)).toEqual([
      { content: '', attachment_ids: ['a1'], reasoning: false },
    ])

    finish()
    await screen.findByText('Summe: 42')
    expect(screen.getAllByText('sales.xlsx')).toHaveLength(1)
  })

  it('says why a message could not be sent, and that an answer is still being written', async () => {
    const busy = () =>
      new Response(JSON.stringify({ detail: 'An answer is still being written in this chat.', code: 'busy' }), {
        status: 409,
      })
    serve({ 'GET /ghostwriter/api/chats/c1': detail([]), 'POST /ghostwriter/api/chats/c1/messages': busy })
    const first = renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('An answer is still being written in this chat.')
    expect(screen.queryByText('Summe der Spalte B?')).not.toBeInTheDocument()
    first.unmount()

    // A regenerate refused the same way says the same.
    serve({ 'GET /ghostwriter/api/chats/c1': detail([QUESTION, ANSWER]), 'POST /ghostwriter/api/chats/c1/regenerate': busy })
    const second = renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    await user.click(await screen.findByRole('button', { name: 'Regenerate' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('An answer is still being written in this chat.')
    second.unmount()

    // A regenerate with no answer left to replace says so, by its code; a 409
    // without a code is not taken for either.
    const refused = (body: object) => () => new Response(JSON.stringify(body), { status: 409 })
    serve({
      'GET /ghostwriter/api/chats/c1': detail([QUESTION, ANSWER]),
      'POST /ghostwriter/api/chats/c1/regenerate': refused({ detail: 'There is no answer to regenerate.', code: 'no_answer' }),
    })
    const third = renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />
      </LanguageContext>,
    )
    await user.click(await screen.findByRole('button', { name: 'Neu erzeugen' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Es gibt keine Antwort, die neu erzeugt werden kann.')
    third.unmount()
    serve({
      'GET /ghostwriter/api/chats/c1': detail([QUESTION, ANSWER]),
      'POST /ghostwriter/api/chats/c1/regenerate': refused({ detail: 'Conflict.' }),
    })
    const fourth = renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    await user.click(await screen.findByRole('button', { name: 'Regenerate' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Something went wrong (409)')
    fourth.unmount()

    // Any other refusal stays the generic line with its status.
    serve({
      'GET /ghostwriter/api/chats/c1': detail([]),
      'POST /ghostwriter/api/chats/c1/messages': () => new Response(JSON.stringify({ detail: 'boom' }), { status: 500 }),
    })
    renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />
      </LanguageContext>,
    )
    await screen.findByText('Fragen Sie, was Sie möchten, oder hängen Sie ein Dokument an.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Etwas ist schiefgelaufen (500)')
  })

  it('asks the server to stop, and stops the stream when the app closes', async () => {
    const { calls, sse } = serveChat(detail([]), detail([]), 'messages')
    const view = renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    sse.push('token', { text: 'Sum' })
    await screen.findByText('Sum')

    await user.click(screen.getByRole('button', { name: 'Halt' }))
    expect(callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/stop')).toHaveLength(1)

    const [post] = callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/messages')
    expect(post.signal?.aborted).toBe(false)
    // As a closed tab does, the app going away closes the connection, which stops the answer.
    view.unmount()
    expect(post.signal?.aborted).toBe(true)
  })
})

/** Hands the app's registry of chat streams to the test. */
function RegistryProbe({ onStreams }: { onStreams: (streams: ChatStreams) => void }) {
  onStreams(useChatStreams())
  return null
}

/** Two chats, one open at a time, keyed as the app keys its pane; `onStreams` gets the registry. */
function TwoChats({ onStreams }: { onStreams: (streams: ChatStreams) => void }) {
  const [open, setOpen] = useState('c1')
  onStreams(useChatStreams())
  return (
    <>
      <button type="button" onClick={() => setOpen('c1')}>
        Open c1
      </button>
      <button type="button" onClick={() => setOpen('c2')}>
        Open c2
      </button>
      <StreamedConversation key={open} chatId={open} config={CONFIG} reasoning={false} composer={controls} />
    </>
  )
}

/** Chats c1 and c2, each empty until its answer is done, each answering over its own SSE body. */
function serveTwoChats() {
  const streams = { c1: sseStream(), c2: sseStream() }
  const done = { c1: false, c2: false }
  const answered = (id: 'c1' | 'c2') =>
    detail(
      [QUESTION, message({ id: `${id}-m2`, role: 'assistant', content: `Stored answer of ${id}` })],
      { ...CHAT, id },
    )
  const calls = serve({
    'GET /ghostwriter/api/chats/c1': () => json(done.c1 ? answered('c1') : detail([])),
    'GET /ghostwriter/api/chats/c2': () => json(done.c2 ? answered('c2') : detail([], { ...CHAT, id: 'c2' })),
    'GET /ghostwriter/api/chats': [CHAT],
    'POST /ghostwriter/api/chats/c1/messages': () => streams.c1.response(),
    'POST /ghostwriter/api/chats/c2/messages': () => streams.c2.response(),
  })
  const finish = (id: 'c1' | 'c2') => {
    done[id] = true
    streams[id].push('final', { ...FINAL, chat: { ...CHAT, id } })
    streams[id].end()
  }
  return { calls, streams, finish }
}

describe('a notice frame', () => {
  it('shows a notice live under the draft, then with the stored answer, and keeps it there', async () => {
    // The server stores the notice with its answer; a read of the chat brings it back.
    const NOTICED = { ...ANSWER, notices: [{ code: 'images_dropped' as const }] }
    const { sse, finish } = serveChat(detail([]), detail([QUESTION, NOTICED]), 'messages')
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    expect(screen.queryByText(IMAGES_DROPPED)).not.toBeInTheDocument()

    // Sent twice, said once; a code this app does not know is ignored.
    sse.push('notice', { code: 'images_dropped' })
    sse.push('notice', { code: 'images_dropped' })
    sse.push('notice', { code: 'something_new' })
    sse.push('token', { text: 'Ohne Bilder' })
    await screen.findByText('Ohne Bilder')
    expect(screen.getAllByText(IMAGES_DROPPED)).toHaveLength(1)
    // Live, it is announced politely; it is no error. A busy ancestor may hold an announcement back
    // while the answer is written, so the line stands outside the answer's own element.
    expect(screen.getByText(IMAGES_DROPPED)).toHaveAttribute('role', 'status')
    expect(screen.getByText(IMAGES_DROPPED).closest('[aria-busy="true"]')).toBeNull()
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    finish()
    // The stored answer replaces the draft and carries its own line: said once, no longer announced.
    expect(await screen.findByText('Summe: 42')).toBeInTheDocument()
    expect(screen.getAllByText(IMAGES_DROPPED)).toHaveLength(1)
    expect(screen.getByText(IMAGES_DROPPED)).not.toHaveAttribute('role')
    expect(screen.getByText(IMAGES_DROPPED).closest('[data-role="assistant"]')).not.toHaveAttribute('aria-busy')

    // The next message does not clear it: the earlier answer keeps its line.
    const NEXT = message({ id: 'm4', role: 'assistant', content: 'Wieder 42' })
    const second = serveChat(
      detail([QUESTION, NOTICED]),
      detail([QUESTION, NOTICED, message({ id: 'm3', role: 'user', content: 'Nochmal?' }), NEXT]),
      'messages',
    )
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    second.sse.push('token', { text: 'Wieder' })
    await screen.findByText('Wieder')
    expect(screen.getAllByText(IMAGES_DROPPED)).toHaveLength(1)
    expect(screen.getByText(IMAGES_DROPPED)).not.toHaveAttribute('role')
    second.finish({ ...FINAL, message_id: 'm4', user_message_id: 'm3' })
    expect(await screen.findByText('Wieder 42')).toBeInTheDocument()
    expect(screen.getAllByText(IMAGES_DROPPED)).toHaveLength(1)
  })

  it('keeps a tools_unavailable frame, said once, then with the stored answer', async () => {
    const TOOLS_UNAVAILABLE = 'The tools could not be reached for this answer, so attached files were not read.'
    const NOTICED = { ...ANSWER, steps: [], notices: [{ code: 'tools_unavailable' as const }] }
    const { sse, finish } = serveChat(detail([]), detail([QUESTION, NOTICED]), 'messages')
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))

    // Its frame carries the code alone; sent twice, it is said once.
    sse.push('notice', { code: 'tools_unavailable' })
    sse.push('notice', { code: 'tools_unavailable' })
    sse.push('token', { text: 'Ohne Werkzeuge' })
    await screen.findByText('Ohne Werkzeuge')
    expect(screen.getAllByText(TOOLS_UNAVAILABLE)).toHaveLength(1)
    expect(screen.getByText(TOOLS_UNAVAILABLE)).toHaveAttribute('role', 'status')
    expect(document.querySelector('[data-notice="tools_unavailable"]')).not.toBeNull()
    // Nothing to try again while the answer is written.
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()

    finish()
    expect(await screen.findByText('Summe: 42')).toBeInTheDocument()
    expect(screen.getAllByText(TOOLS_UNAVAILABLE)).toHaveLength(1)
    expect(screen.getByText(TOOLS_UNAVAILABLE)).not.toHaveAttribute('role')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('says a document notice once for a file, and once for each file', async () => {
    const { sse } = serveChat(detail([]), detail([QUESTION, ANSWER]), 'messages')
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))

    const unread = (file_id: string, name: string) => ({ code: 'document_unread', file_id, name })
    sse.push('notice', unread('F1', 'tide-notes.txt'))
    sse.push('notice', unread('F1', 'tide-notes.txt'))
    sse.push('notice', unread('F2', 'quay-ledger.xlsx'))
    sse.push('notice', { code: 'document_partial', file_id: 'F3', name: 'long-report.pdf', read: 9, total: 500, unit: 'page' })
    sse.push('notice', { code: 'document_partial', file_id: 'F3', name: 'long-report.pdf', read: 9, total: 500, unit: 'page' })
    sse.push('token', { text: 'Teilweise' })
    await screen.findByText('Teilweise')

    expect(screen.getAllByText("'tide-notes.txt' was not read for this answer.")).toHaveLength(1)
    expect(screen.getAllByText("'quay-ledger.xlsx' was not read for this answer.")).toHaveLength(1)
    expect(screen.getAllByText("Of 'long-report.pdf', 9 of 500 pages were read.")).toHaveLength(1)
  })

  it('does not say a trimmed context under the answer, live or stored', async () => {
    const TRIMMED = { ...ANSWER, notices: [{ code: 'context_trimmed' as const }] }
    const { sse, finish } = serveChat(detail([]), detail([QUESTION, TRIMMED]), 'messages')
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    sse.push('notice', { code: 'context_trimmed' })
    sse.push('token', { text: 'Gekürzt' })
    await screen.findByText('Gekürzt')
    expect(document.querySelector('[data-notice]')).toBeNull()
    finish()
    await screen.findByText('Summe: 42')
    expect(document.querySelector('[data-notice]')).toBeNull()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('names the notice in the language of the app', async () => {
    const { sse } = serveChat(detail([]), detail([QUESTION, ANSWER]), 'messages')
    renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />
      </LanguageContext>,
    )
    const user = userEvent.setup()
    await screen.findByText('Fragen Sie, was Sie möchten, oder hängen Sie ein Dokument an.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    sse.push('notice', { code: 'images_dropped' })
    const line = await screen.findByText('Das Modell hat die Bilder nicht angenommen und ohne sie geantwortet.')
    expect(line).toHaveAttribute('role', 'status')
  })
})

describe('answers in chats the user has left', () => {
  it('shows each message once on return, as the server lists the chat while its answer runs', async () => {
    const sse = sseStream()
    let sent = false
    const reading = step({ id: 's1', tool: 'read_document', status: 'ok', detail: 'sales.xlsx' })
    // What the backend's chat_detail returns once the send stored the question and opened the answer.
    const midStream = detail([
      message({ id: 'm1', role: 'user', content: 'Summe der Spalte B?' }),
      message({ id: 'm2', role: 'assistant', content: '', steps: [reading] }),
    ])
    const calls = serve({
      'GET /ghostwriter/api/chats/c1': () => json(sent ? midStream : detail([])),
      'GET /ghostwriter/api/chats/c2': () => json(detail([], { ...CHAT, id: 'c2' })),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages': () => {
        sent = true
        return sse.response()
      },
    })
    renderWithClient(<TwoChats onStreams={() => {}} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    sse.push('start', { message_id: 'm2', user_message_id: 'm1' })
    sse.push('step', reading)
    sse.push('token', { text: 'Zwischen' })
    await screen.findByText('Zwischen')

    await user.click(screen.getByRole('button', { name: 'Open c2' }))
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Open c1' }))
    // The chat is read anew mid-stream: the stored question and the empty answer it lists are the turn's own.
    await waitFor(() => expect(callsTo(calls, 'GET', '/ghostwriter/api/chats/c1').length).toBeGreaterThan(1))
    await screen.findByText('Zwischen')
    expect(screen.getAllByText('Summe der Spalte B?')).toHaveLength(1)
    const answers = document.querySelectorAll('[data-role="assistant"]')
    expect(answers).toHaveLength(1)
    expect(answers[0]).toHaveAttribute('aria-busy', 'true')
    expect(screen.getAllByText(/sales\.xlsx/)).toHaveLength(1)
  })

  it('keeps writing while another chat is open, and shows the live draft on return', async () => {
    const { calls, streams, finish } = serveTwoChats()
    let registry!: ChatStreams
    renderWithClient(<TwoChats onStreams={(r) => (registry = r)} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    streams.c1.push('token', { text: 'Zwischen' })
    await screen.findByText('Zwischen')
    expect(registry.running()).toEqual(['c1'])

    await user.click(screen.getByRole('button', { name: 'Open c2' }))
    await screen.findByText('Ask anything, or attach a document.')
    expect(screen.queryByText('Zwischen')).not.toBeInTheDocument()
    // Written while the user is away.
    streams.c1.push('token', { text: 'stand' })

    await user.click(screen.getByRole('button', { name: 'Open c1' }))
    const draft = await screen.findByText('Zwischenstand')
    expect(draft.closest('[aria-busy="true"]')).not.toBeNull()
    finish('c1')
    expect(await screen.findByText('Stored answer of c1')).toBeInTheDocument()
    const [post] = callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/messages')
    expect(post.signal?.aborted).toBe(false)
    expect(callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/stop')).toEqual([])
    // A finished stream drops out of the registry.
    await waitFor(() => expect(registry.running()).toEqual([]))
  })

  it('writes two chats\' answers at once', async () => {
    const { calls, streams, finish } = serveTwoChats()
    let registry!: ChatStreams
    renderWithClient(<TwoChats onStreams={(r) => (registry = r)} />)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    streams.c1.push('token', { text: 'Erste' })
    await screen.findByText('Erste')

    await user.click(screen.getByRole('button', { name: 'Open c2' }))
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    streams.c2.push('token', { text: 'Zweite' })
    await screen.findByText('Zweite')
    expect(registry.running().sort()).toEqual(['c1', 'c2'])

    finish('c2')
    expect(await screen.findByText('Stored answer of c2')).toBeInTheDocument()
    await waitFor(() => expect(registry.running()).toEqual(['c1']))
    await user.click(screen.getByRole('button', { name: 'Open c1' }))
    expect(await screen.findByText('Erste')).toBeInTheDocument()
    finish('c1')
    expect(await screen.findByText('Stored answer of c1')).toBeInTheDocument()
    for (const id of ['c1', 'c2']) {
      const [post] = callsTo(calls, 'POST', `/ghostwriter/api/chats/${id}/messages`)
      expect(post.signal?.aborted).toBe(false)
    }
    await waitFor(() => expect(registry.running()).toEqual([]))
  })
})

describe('a stream that goes silent', () => {
  afterEach(() => vi.useRealTimers())

  it('ends after 60 s without a byte, frees the chat and says so; a keep-alive counts as a byte', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { calls, sse } = serveChat(detail([]), detail([]), 'messages')
    let registry!: ChatStreams
    renderWithClient(
      <>
        <RegistryProbe onStreams={(r) => (registry = r)} />
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={controls} />
      </>,
    )
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    sse.push('token', { text: 'Sum' })
    await screen.findByText('Sum')

    await vi.advanceTimersByTimeAsync(50_000)
    sse.comment('keep-alive')
    await vi.advanceTimersByTimeAsync(50_000)
    expect(registry.running()).toEqual(['c1'])
    const [post] = callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/messages')
    expect(post.signal?.aborted).toBe(false)

    await vi.advanceTimersByTimeAsync(10_000)
    await waitFor(() => expect(registry.running()).toEqual([]))
    expect(post.signal?.aborted).toBe(true)
    expect(await screen.findByRole('alert')).toHaveTextContent('Something went wrong.')
    // The chat is free: the composer may send again.
    expect(screen.getByRole('button', { name: 'Ask' })).toBeEnabled()
  })
})
