import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { clearAllPurposes } from '@infra/ui'
import { LanguageContext } from '../i18n/LanguageContext'
import type { AttachmentOut } from '../api/types'
import type { ChatStream } from './useChatStream'
import { useChats } from './useChats'
import {
  CHAT,
  CONFIG,
  callsTo,
  detail,
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
const FINAL = { message_id: 'm2', user_message_id: 'm1', chat: CHAT, stopped: false }
const SALES: AttachmentOut = {
  id: 'a1',
  file_id: 'F0',
  name: 'sales.xlsx',
  size: 4096,
  mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  created_at: '2026-11-02T09:00:00Z',
  expired: false,
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

  it('asks the server to stop, and aborts the stream when the conversation goes away', async () => {
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
    view.unmount()
    expect(post.signal?.aborted).toBe(true)
  })
})
