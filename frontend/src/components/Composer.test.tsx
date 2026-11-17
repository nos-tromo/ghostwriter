import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, createEvent, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClientProvider } from '@tanstack/react-query'
import { PURPOSE_HEADER, PurposeBar, clearAllPurposes } from '@infra/ui'
import type { AppConfig, AttachmentOut } from '../api/types'
import { LanguageContext } from '../i18n/LanguageContext'
import { ChatPane } from './ChatPane'
import {
  CHAT,
  CONFIG,
  callsTo,
  detail,
  json,
  message,
  renderWithClient,
  serve,
  sseStream,
  type Answer,
  type Call,
} from '../test/fixtures'
import { installMedia, uninstallMedia } from '../test/media'

afterEach(() => {
  vi.useRealTimers()
  clearAllPurposes()
  sessionStorage.clear()
  uninstallMedia()
})

const ATTACHMENTS = '/ghostwriter/api/chats/c1/attachments'
const MESSAGES = '/ghostwriter/api/chats/c1/messages'
const TRANSCRIBE = '/ghostwriter/api/transcribe'
const FINAL = { message_id: 'm2', user_message_id: 'm1', chat: CHAT, stopped: false }
const QUESTION = message({ id: 'm1', role: 'user', content: 'Summe der Spalte B?' })
const ANSWER = message({ id: 'm2', role: 'assistant', content: 'Summe: 42' })

function attachment(id: string, name: string, size = 3): AttachmentOut {
  return { id, file_id: `F-${id}`, name, size, mime: 'text/csv', created_at: '2026-11-02T09:00:00Z', expired: false }
}

/** A file of `size` bytes as the browser reports it, without allocating them. */
function file(name: string, size?: number): File {
  const made = new File(['a,b'], name)
  if (size !== undefined) Object.defineProperty(made, 'size', { value: size })
  return made
}

const uploaded = (call: Call) => ((call.body as FormData).get('file') as File).name

function drop(target: Element, files: File[]) {
  fireEvent.dragOver(target, { dataTransfer: { files, types: ['Files'] } })
  fireEvent.drop(target, { dataTransfer: { files, types: ['Files'] } })
}

const textbox = () => screen.getByRole('textbox', { name: 'Write a message' })
const composer = () => textbox().closest('form') as HTMLElement
/** A file's chip in the composer, by its name. */
const chip = (name: string) => within(composer()).getByText(name).closest('li') as HTMLElement

/** Chat c1 opened, empty unless `routes` says otherwise. */
function openChat(routes: Record<string, Answer> = {}, config: AppConfig = CONFIG) {
  const calls = serve({ 'GET /ghostwriter/api/chats/c1': detail([]), 'GET /ghostwriter/api/chats': [CHAT], ...routes })
  const view = renderWithClient(<ChatPane chatId="c1" config={config} />)
  return { calls, ...view }
}

describe('Composer', () => {
  it('uploads a dropped file into the chat and sends it with the message', async () => {
    let release!: () => void
    const held = new Promise<void>((done) => (release = done))
    const sse = sseStream()
    const { calls } = openChat({
      [`POST ${ATTACHMENTS}`]: async () => {
        await held
        return json(attachment('a1', 'sales.xlsx', 4096), 201)
      },
      [`POST ${MESSAGES}`]: () => sse.response(),
    })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.type(textbox(), 'Summe')
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()

    const files = [file('sales.xlsx')]
    fireEvent.dragOver(textbox(), { dataTransfer: { files, types: ['Files'] } })
    expect(screen.getByText('Drop files to attach them')).toBeInTheDocument()
    fireEvent.drop(textbox(), { dataTransfer: { files, types: ['Files'] } })
    expect(screen.queryByText('Drop files to attach them')).not.toBeInTheDocument()

    // While it uploads, nothing is sent, by button or by Enter.
    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(1))
    expect(uploaded(callsTo(calls, 'POST', ATTACHMENTS)[0])).toBe('sales.xlsx')
    expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'uploading')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.keyboard('{Enter}')
    expect(callsTo(calls, 'POST', MESSAGES)).toHaveLength(0)

    release()
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))
    expect(chip('sales.xlsx')).toHaveTextContent('4 KB')
    expect(
      screen.getByText('Attached files stay in this chat until it is deleted, for 7 days at most.'),
    ).toBeInTheDocument()
    await user.click(textbox())
    await user.keyboard('{Enter}')
    expect(callsTo(calls, 'POST', MESSAGES).map((c) => c.body)).toEqual([
      { content: 'Summe', attachment_ids: ['a1'], reasoning: false },
    ])
    // The composer is empty again; the message being sent shows the file.
    expect(textbox()).toHaveValue('')
    expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument()
    const bubble = screen.getByText('Summe').closest('[data-role="user"]') as HTMLElement
    expect(within(bubble).getByText('sales.xlsx')).toBeInTheDocument()
  })

  it('uploads files pasted from the clipboard', async () => {
    const { calls } = openChat({ [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'sales.csv'), 201) })
    await screen.findByText('Ask anything, or attach a document.')

    const pasted = createEvent.paste(textbox(), {
      clipboardData: { files: [file('sales.csv')], types: ['Files'], getData: () => '' },
    })
    fireEvent(textbox(), pasted)
    expect(pasted.defaultPrevented).toBe(true)
    await waitFor(() => expect(chip('sales.csv')).toHaveAttribute('data-status', 'done'))
    expect(callsTo(calls, 'POST', ATTACHMENTS).map(uploaded)).toEqual(['sales.csv'])

    // Text is pasted as ever.
    const text = createEvent.paste(textbox(), {
      clipboardData: { files: [], types: ['text/plain'], getData: () => 'Spalte B' },
    })
    fireEvent(textbox(), text)
    expect(text.defaultPrevented).toBe(false)
    // So is text that comes with a picture of itself, as copied from an office
    // program: the picture is no attachment.
    const rich = createEvent.paste(textbox(), {
      clipboardData: {
        files: [new File(['png'], 'image.png', { type: 'image/png' })],
        types: ['text/plain', 'text/html', 'Files'],
        getData: (type: string) => (type === 'text/plain' ? 'Spalte B' : ''),
      },
    })
    fireEvent(textbox(), rich)
    expect(rich.defaultPrevented).toBe(false)
    expect(within(composer()).queryByText('image.png')).not.toBeInTheDocument()
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(1)
  })

  it('refuses unsupported types and files over 50 MB before uploading', async () => {
    const { calls } = openChat({ [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'Bericht.PDF'), 201) })
    await screen.findByText('Ask anything, or attach a document.')

    drop(textbox(), [file('tool.exe'), file('big.pdf', 51 * 1024 * 1024), file('Bericht.PDF')])
    expect(await screen.findByText('tool.exe: this file type is not supported.')).toBeInTheDocument()
    expect(screen.getByText('big.pdf is larger than 50 MB.')).toBeInTheDocument()
    expect(chip('tool.exe')).toHaveAttribute('data-status', 'error')
    expect(chip('big.pdf')).toHaveAttribute('data-status', 'error')
    // The extension is matched in any case.
    await waitFor(() => expect(chip('Bericht.PDF')).toHaveAttribute('data-status', 'done'))
    expect(callsTo(calls, 'POST', ATTACHMENTS).map(uploaded)).toEqual(['Bericht.PDF'])

    // A refused file can be taken out of view.
    await userEvent.setup().click(within(chip('tool.exe')).getByRole('button', { name: 'Remove tool.exe' }))
    expect(within(composer()).queryByText('tool.exe')).not.toBeInTheDocument()
  })

  it('says why the server refused a file, by the code it gives', async () => {
    openChat({
      [`POST ${ATTACHMENTS}`]: (call: Call) => {
        const name = uploaded(call)
        if (name === 'big.xlsx') return json({ detail: 'The file is larger than 50 MB.', code: 'too_large' }, 413)
        if (name === 'full.csv') return json({ detail: 'Storage is full.', code: 'storage_full' }, 507)
        // A 413 without the code is not the file's size.
        return json({ detail: 'Request Entity Too Large' }, 413)
      },
    })
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('big.xlsx'), file('full.csv'), file('odd.csv')])

    expect(await screen.findByText('big.xlsx is larger than 50 MB.')).toBeInTheDocument()
    expect(
      await screen.findByText(
        'Your files fill the space workbench allows; delete a chat or wait for files to expire.',
      ),
    ).toBeInTheDocument()
    expect(await screen.findByText('odd.csv could not be attached.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  it('uploads its files one after another', async () => {
    const waiting: Array<() => void> = []
    const { calls } = openChat({
      [`POST ${ATTACHMENTS}`]: async (call: Call) => {
        await new Promise<void>((done) => waiting.push(done))
        const name = uploaded(call)
        return json(attachment(`id-${name}`, name), 201)
      },
    })
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('a.csv'), file('b.csv')])
    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(1))
    // A second drop while the first file uploads waits its turn too.
    drop(textbox(), [file('c.csv')])
    await new Promise((done) => setTimeout(done, 20))
    expect(callsTo(calls, 'POST', ATTACHMENTS).map(uploaded)).toEqual(['a.csv'])
    expect(chip('b.csv')).toHaveAttribute('data-status', 'uploading')
    expect(chip('c.csv')).toHaveAttribute('data-status', 'uploading')

    waiting[0]()
    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(2))
    expect(chip('a.csv')).toHaveAttribute('data-status', 'done')
    await new Promise((done) => setTimeout(done, 20))
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(2)
    waiting[1]()
    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(3))
    waiting[2]()
    await waitFor(() => expect(chip('c.csv')).toHaveAttribute('data-status', 'done'))
    expect(callsTo(calls, 'POST', ATTACHMENTS).map(uploaded)).toEqual(['a.csv', 'b.csv', 'c.csv'])
  })

  it('asks for a purpose before an upload that needs one, and sends nothing without it', async () => {
    const calls = serve({
      'GET /ghostwriter/api/chats/c1': detail([]),
      [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'sales.xlsx'), 201),
    })
    const user = userEvent.setup()
    renderWithClient(
      <>
        <PurposeBar scope="ghostwriter" required />
        <ChatPane chatId="c1" config={{ ...CONFIG, require_purpose: ['collect'] }} />
      </>,
    )
    await screen.findByText('Ask anything, or attach a document.')

    drop(textbox(), [file('sales.xlsx')])
    const reason = await screen.findByLabelText('Reason')
    // The body is not sent until the purpose is there.
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(0)
    await user.type(reason, 'Prüfung{Enter}')
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))
    const [sent] = callsTo(calls, 'POST', ATTACHMENTS)
    expect(sent.headers[PURPOSE_HEADER]).toBe('reason=Pr%C3%BCfung')

    // Declined: the file is not attached, and nothing goes out.
    clearAllPurposes()
    drop(textbox(), [file('sales.csv')])
    await screen.findByLabelText('Reason')
    await user.keyboard('{Escape}')
    expect(
      await screen.findByText('sales.csv was not attached: attaching files needs a stated purpose.'),
    ).toBeInTheDocument()
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(1)
  })

  it('sends on Enter, adds a line on Shift+Enter, and turns into Stop while streaming', async () => {
    const sse = sseStream()
    const { calls } = openChat({
      [`POST ${MESSAGES}`]: () => sse.response(),
      'POST /ghostwriter/api/chats/c1/stop': null,
    })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.type(textbox(), '   ')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.clear(textbox())

    await user.type(textbox(), 'Summe{Shift>}{Enter}{/Shift}Spalte B')
    expect(textbox()).toHaveValue('Summe\nSpalte B')
    expect(callsTo(calls, 'POST', MESSAGES)).toHaveLength(0)
    await user.keyboard('{Enter}')
    expect(callsTo(calls, 'POST', MESSAGES).map((c) => c.body)).toEqual([
      { content: 'Summe\nSpalte B', attachment_ids: [], reasoning: false },
    ])
    expect(textbox()).toHaveValue('')

    const stop = await screen.findByRole('button', { name: 'Stop' })
    expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument()
    await user.click(stop)
    expect(callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/stop')).toHaveLength(1)
    sse.push('final', { ...FINAL, stopped: true })
    sse.end()
    expect(await screen.findByRole('button', { name: 'Send' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument()
  })

  it('gives the message and its files back when the server refuses it', async () => {
    const { calls } = openChat({
      [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'sales.xlsx'), 201),
      [`POST ${MESSAGES}`]: () => json({ detail: 'An answer is still being written in this chat.' }, 409),
    })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('sales.xlsx')])
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))
    await user.type(textbox(), 'Summe{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent('An answer is still being written in this chat.')
    expect(textbox()).toHaveValue('Summe')
    expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done')
    await user.keyboard('{Enter}')
    expect(callsTo(calls, 'POST', MESSAGES).map((c) => c.body)).toEqual([
      { content: 'Summe', attachment_ids: ['a1'], reasoning: false },
      { content: 'Summe', attachment_ids: ['a1'], reasoning: false },
    ])
  })

  it('shows the files attached earlier that still wait, and sends them', async () => {
    const sse = sseStream()
    const waiting = { ...detail([]), pending_attachments: [attachment('a1', 'sales.xlsx')] }
    const { calls } = openChat({
      'GET /ghostwriter/api/chats/c1': waiting,
      [`POST ${MESSAGES}`]: () => sse.response(),
    })
    const user = userEvent.setup()
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))
    await user.click(screen.getByRole('button', { name: 'Send' }))
    expect(callsTo(calls, 'POST', MESSAGES).map((c) => c.body)).toEqual([
      { content: '', attachment_ids: ['a1'], reasoning: false },
    ])
    expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument()
  })

  it('hides the microphone with speech off and the paperclip without workbench', async () => {
    installMedia()
    const { calls, container, unmount } = openChat({
      [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'sales.csv'), 201),
    })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    expect(screen.getByRole('button', { name: 'Dictate' })).toBeInTheDocument()
    const picker = container.querySelector('input[type="file"]') as HTMLInputElement
    expect(picker).toHaveAttribute('accept', '.csv,.pdf,.xlsx')
    expect(picker.multiple).toBe(true)
    expect(picker).not.toBeVisible()
    const open = vi.spyOn(picker, 'click')
    await user.click(screen.getByRole('button', { name: 'Attach files' }))
    expect(open).toHaveBeenCalledTimes(1)
    await user.upload(picker, file('sales.csv'))
    await waitFor(() => expect(chip('sales.csv')).toHaveAttribute('data-status', 'done'))
    unmount()

    const off = openChat({}, { ...CONFIG, speech_input: false, attachments: false })
    await screen.findByText('Ask anything.')
    expect(screen.queryByRole('button', { name: 'Dictate' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Attach files' })).not.toBeInTheDocument()
    expect(off.container.querySelector('input[type="file"]')).toBeNull()
    // Nor does a file dropped or pasted go anywhere.
    drop(textbox(), [file('sales.csv')])
    fireEvent.paste(textbox(), { clipboardData: { files: [file('sales.csv')], types: ['Files'], getData: () => '' } })
    expect(screen.queryByText('Drop files to attach them')).not.toBeInTheDocument()
    expect(within(composer()).queryByText('sales.csv')).not.toBeInTheDocument()
    expect(callsTo(off.calls, 'POST', ATTACHMENTS)).toHaveLength(0)
    // The reasoning switch and send stay.
    expect(screen.getByRole('button', { name: /^Reasoning:/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument()
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(1)
  })

  it('records, stops at the limit and inserts the transcript at the cursor', async () => {
    const media = installMedia()
    const { calls } = openChat({ [`POST ${TRANSCRIBE}`]: () => json({ text: ' der Spalte ' }) })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.type(textbox(), 'Summe B')
    // The cursor after "Summe ".
    ;(textbox() as HTMLTextAreaElement).setSelectionRange(6, 6)

    vi.useFakeTimers({ shouldAdvanceTime: true })
    fireEvent.click(screen.getByRole('button', { name: 'Dictate' }))
    expect(await screen.findByRole('button', { name: 'Stop dictating' })).toBeInTheDocument()
    expect(screen.getByText('120 s left')).toBeInTheDocument()
    await act(async () => {
      vi.advanceTimersByTime(119_000)
    })
    expect(screen.getByText('1 s left')).toBeInTheDocument()
    expect(callsTo(calls, 'POST', TRANSCRIBE)).toHaveLength(0)
    await act(async () => {
      vi.advanceTimersByTime(1_000)
    })
    vi.useRealTimers()

    await waitFor(() => expect(textbox()).toHaveValue('Summe der Spalte B'))
    const [sent] = callsTo(calls, 'POST', TRANSCRIBE)
    expect(((sent.body as FormData).get('file') as Blob).type).toBe('audio/webm;codecs=opus')
    expect(media.track.stop).toHaveBeenCalledTimes(1)
    // The transcript is the user's to edit and send; nothing went out.
    expect(callsTo(calls, 'POST', MESSAGES)).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Dictate' })).toBeInTheDocument()
    expect(screen.queryByText(/s left$/)).not.toBeInTheDocument()
  })

  it('says when the microphone was refused, and when the browser cannot record', async () => {
    installMedia({ denied: true })
    const { unmount } = openChat()
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Dictate' }))
    expect(await screen.findByText('Microphone access was denied.')).toBeInTheDocument()
    unmount()

    uninstallMedia()
    openChat()
    await screen.findByText('Ask anything, or attach a document.')
    const mic = screen.getByRole('button', { name: 'Dictate' })
    expect(mic).toBeDisabled()
    expect(mic).toHaveAttribute('title', 'This browser cannot record audio.')
  })
})

describe('the reasoning switch', () => {
  it('starts off, per chat, and says its state in its name', async () => {
    serve({ 'GET /ghostwriter/api/chats/c1': detail([]), 'GET /ghostwriter/api/chats/c2': detail([]) })
    const user = userEvent.setup()
    const { client, rerender } = renderWithClient(<ChatPane key="c1" chatId="c1" config={CONFIG} />)
    const off = await screen.findByRole('button', { name: 'Reasoning: Off — faster, plain answer' })
    expect(off).toHaveAttribute('aria-pressed', 'false')

    await user.click(off)
    const on = screen.getByRole('button', { name: 'Reasoning: On — the model thinks before answering' })
    expect(on).toHaveAttribute('aria-pressed', 'true')

    // Another chat starts off again.
    rerender(
      <QueryClientProvider client={client}>
        <ChatPane key="c2" chatId="c2" config={CONFIG} />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('button', { name: 'Reasoning: Off — faster, plain answer' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('says its state in German too', async () => {
    serve({ 'GET /ghostwriter/api/chats/c1': detail([]) })
    renderWithClient(
      <LanguageContext value="de">
        <ChatPane chatId="c1" config={CONFIG} />
      </LanguageContext>,
    )
    expect(await screen.findByRole('button', { name: 'Reasoning: Aus — schneller, direkte Antwort' })).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: /^Reasoning:/ }))
    expect(
      screen.getByRole('button', { name: 'Reasoning: An — das Modell denkt vor der Antwort nach' }),
    ).toHaveAttribute('aria-pressed', 'true')
  })

  it('sends reasoning: true with the message and the regenerate once it is on', async () => {
    const sse = sseStream()
    let regenerated = false
    const { calls } = openChat({
      'GET /ghostwriter/api/chats/c1': detail([QUESTION, ANSWER]),
      [`POST ${MESSAGES}`]: () => json({ detail: 'An answer is still being written in this chat.' }, 409),
      'POST /ghostwriter/api/chats/c1/regenerate': () => {
        regenerated = true
        return sse.response()
      },
    })
    const user = userEvent.setup()
    await screen.findByText('Summe: 42')
    await user.click(screen.getByRole('button', { name: 'Reasoning: Off — faster, plain answer' }))

    await user.type(textbox(), 'Und Spalte C?{Enter}')
    expect(callsTo(calls, 'POST', MESSAGES).map((c) => c.body)).toEqual([
      { content: 'Und Spalte C?', attachment_ids: [], reasoning: true },
    ])
    await screen.findByRole('alert')

    await user.click(screen.getByRole('button', { name: 'Regenerate' }))
    expect(regenerated).toBe(true)
    expect(callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/regenerate').map((c) => c.body)).toEqual([
      { reasoning: true },
    ])
    // The pending answer says it is thinking.
    expect(await screen.findByText('thinking…')).toBeInTheDocument()
  })
})
