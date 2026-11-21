import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, createEvent, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PURPOSE_HEADER, PurposeBar, clearAllPurposes, setPurpose } from '@infra/ui'
import type { AppConfig, AttachmentOut } from '../api/types'
import { LanguageContext } from '../i18n/LanguageContext'
import { ChatPane } from './ChatPane'
import {
  CHAT,
  CONFIG,
  VISION_CONFIG,
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
  return {
    id,
    file_id: `F-${id}`,
    name,
    size,
    mime: 'text/csv',
    created_at: '2026-11-02T09:00:00Z',
    expired: false,
    kind: 'document',
    status: 'ready',
  }
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

    // A file copied in a file manager comes with its name as text: the file
    // is attached, and its name is not pasted.
    const copied = createEvent.paste(textbox(), {
      clipboardData: {
        files: [file('Bericht.pdf')],
        types: ['text/plain', 'Files'],
        getData: (type: string) => (type === 'text/plain' ? 'Bericht.pdf' : ''),
      },
    })
    fireEvent(textbox(), copied)
    expect(copied.defaultPrevented).toBe(true)
    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(2))
    expect(uploaded(callsTo(calls, 'POST', ATTACHMENTS)[1])).toBe('Bericht.pdf')
  })

  it('refuses unsupported types and files over 50 MB before uploading', async () => {
    const { calls } = openChat({ [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'Bericht.PDF'), 201) })
    await screen.findByText('Ask anything, or attach a document.')

    const stay = 'Attached files stay in this chat until it is deleted, for 7 days at most.'
    drop(textbox(), [file('tool.exe'), file('big.pdf', 51 * 1024 * 1024)])
    expect(await screen.findByText('tool.exe: this file type is not supported.')).toBeInTheDocument()
    expect(screen.getByText('big.pdf is larger than 50 MB.')).toBeInTheDocument()
    expect(chip('tool.exe')).toHaveAttribute('data-status', 'error')
    expect(chip('big.pdf')).toHaveAttribute('data-status', 'error')
    // Nothing stays in the chat when every file was refused.
    expect(screen.queryByText(stay)).not.toBeInTheDocument()
    // The extension is matched in any case.
    drop(textbox(), [file('Bericht.PDF')])
    await waitFor(() => expect(chip('Bericht.PDF')).toHaveAttribute('data-status', 'done'))
    expect(screen.getByText(stay)).toBeInTheDocument()
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
        if (name === 'more.csv') return json({ detail: 'Too many.', code: 'too_many_pending' }, 409)
        // A 413 without the code is not the file's size.
        return json({ detail: 'Request Entity Too Large' }, 413)
      },
    })
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('big.xlsx'), file('full.csv'), file('more.csv'), file('odd.csv')])

    expect(await screen.findByText('big.xlsx is larger than 50 MB.')).toBeInTheDocument()
    expect(
      await screen.findByText(
        'Your files fill the space workbench allows; delete a chat or wait for files to expire.',
      ),
    ).toBeInTheDocument()
    expect(await screen.findByText('Send the attached files before adding more.')).toBeInTheDocument()
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
    // The chat, read again after the refusal, lists the file as still waiting.
    let waiting: AttachmentOut[] = []
    const { calls } = openChat({
      'GET /ghostwriter/api/chats/c1': () => json({ ...detail([]), pending_attachments: waiting }),
      [`POST ${ATTACHMENTS}`]: () => {
        waiting = [attachment('a1', 'sales.xlsx')]
        return json(attachment('a1', 'sales.xlsx'), 201)
      },
      [`POST ${MESSAGES}`]: () => json({ detail: 'An answer is still being written in this chat.', code: 'busy' }, 409),
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

  it('drops attached files that another tab sent, once the chat is read again', async () => {
    let listed: AttachmentOut[] = []
    let holdRead: Promise<void> | null = null
    const { client } = openChat({
      'GET /ghostwriter/api/chats/c1': async () => {
        const pending = listed
        if (holdRead) await holdRead
        return json({ ...detail([]), pending_attachments: pending })
      },
      [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'sales.xlsx'), 201),
    })
    await screen.findByText('Ask anything, or attach a document.')
    const reread = () => client.invalidateQueries({ queryKey: ['chat', 'c1'] })

    // A read that began before the file was attached cannot list it: the chip stays.
    let release!: () => void
    holdRead = new Promise<void>((done) => (release = done))
    const early = reread()
    drop(textbox(), [file('sales.xlsx')])
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))
    release()
    await early
    holdRead = null
    expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done')

    // A read that lists it as waiting keeps it, once.
    listed = [attachment('a1', 'sales.xlsx')]
    await act(reread)
    expect(within(composer()).getAllByText('sales.xlsx')).toHaveLength(1)

    // Another tab sent it: the next read no longer lists it, and its chip goes.
    listed = []
    await act(reread)
    await waitFor(() => expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
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

  it('says when no microphone could be used, and keeps the microphone for another try', async () => {
    const media = installMedia({ failure: 'NotFoundError' })
    openChat()
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Dictate' }))
    expect(await screen.findByText('No microphone could be used.')).toBeInTheDocument()
    const mic = screen.getByRole('button', { name: 'Dictate' })
    expect(mic).toBeEnabled()
    await user.click(mic)
    expect(await screen.findByRole('button', { name: 'Stop dictating' })).toBeInTheDocument()
    expect(media.getUserMedia).toHaveBeenCalledTimes(2)
    expect(screen.queryByText('No microphone could be used.')).not.toBeInTheDocument()
  })

  it('aborts a transcription still running when the composer goes', async () => {
    installMedia()
    const { calls, unmount } = openChat({ [`POST ${TRANSCRIBE}`]: () => new Promise<Response>(() => {}) })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.click(screen.getByRole('button', { name: 'Dictate' }))
    await user.click(await screen.findByRole('button', { name: 'Stop dictating' }))
    await waitFor(() => expect(callsTo(calls, 'POST', TRANSCRIBE)).toHaveLength(1))
    const [sent] = callsTo(calls, 'POST', TRANSCRIBE)
    expect(sent.signal?.aborted).toBe(false)
    unmount()
    expect(sent.signal?.aborted).toBe(true)
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

describe('Composer pictures', () => {
  const POLL = `${ATTACHMENTS}/a1?wait=20`
  const PREVIEW = `${ATTACHMENTS}/a1/preview?page=1`
  const HEIC = 'harbour-photo.heic'
  // Every object URL made, and every one revoked, in order.
  let made: string[]
  let revoked: string[]

  beforeEach(() => {
    made = []
    revoked = []
    vi.spyOn(URL, 'createObjectURL').mockImplementation(() => {
      const url = `blob:local-${made.length + 1}`
      made.push(url)
      return url
    })
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation((url) => {
      revoked.push(url)
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  function picture(id: string, name: string, fields: Partial<AttachmentOut> = {}): AttachmentOut {
    return { ...attachment(id, name, 2048), mime: 'image/png', kind: 'image', ...fields }
  }

  const image = (name: string, type = 'image/png') => new File(['png'], name, { type })
  /** A tile's list item, by the name its face carries. */
  const tile = (name: string) => within(composer()).getByRole('button', { name }).closest('li') as HTMLElement
  const tileStatus = (item: HTMLElement) => item.querySelector('[data-kind]')?.getAttribute('data-status')
  const hint = 'Sending waits until the attached files are ready.'

  it('shows a dropped picture at once, prepares it, and sends once it is ready', async () => {
    let ready!: () => void
    const held = new Promise<void>((done) => (ready = done))
    let polls = 0
    const sse = sseStream()
    const { calls } = openChat(
      {
        [`POST ${ATTACHMENTS}`]: () => json(picture('a1', 'harbour-photo.png', { status: 'processing' }), 201),
        [`GET ${POLL}`]: async () => {
          polls += 1
          if (polls === 1) return json(picture('a1', 'harbour-photo.png', { status: 'processing' }))
          await held
          return json(picture('a1', 'harbour-photo.png'))
        },
        [`POST ${MESSAGES}`]: () => sse.response(),
      },
      VISION_CONFIG,
    )
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.type(textbox(), 'Was zeigt das Bild?')

    drop(textbox(), [image('harbour-photo.png')])
    // Its picture is there before anything has been sent.
    const shown = composer().querySelector('li img')
    expect(shown).toHaveAttribute('src', 'blob:local-1')
    expect(tileStatus(tile('harbour-photo.png'))).toBe('uploading')

    await waitFor(() => expect(callsTo(calls, 'GET', POLL)).toHaveLength(2))
    expect(tile('harbour-photo.png')).toHaveAttribute('data-status', 'processing')
    expect(tileStatus(tile('harbour-photo.png'))).toBe('processing')
    expect(screen.getByText(hint)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.click(textbox())
    await user.keyboard('{Enter}')
    expect(callsTo(calls, 'POST', MESSAGES)).toHaveLength(0)

    ready()
    await waitFor(() => expect(tile('harbour-photo.png')).toHaveAttribute('data-status', 'done'))
    expect(tileStatus(tile('harbour-photo.png'))).toBe('ready')
    expect(screen.queryByText(hint)).not.toBeInTheDocument()
    expect(callsTo(calls, 'GET', POLL)).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    await user.keyboard('{Enter}')
    expect(callsTo(calls, 'POST', MESSAGES).map((c) => c.body)).toEqual([
      { content: 'Was zeigt das Bild?', attachment_ids: ['a1'], reasoning: false },
    ])
  })

  it('picks pictures through the paperclip', async () => {
    const { calls, container } = openChat(
      { [`POST ${ATTACHMENTS}`]: () => json(picture('a1', 'harbour-photo.png'), 201) },
      VISION_CONFIG,
    )
    await screen.findByText('Ask anything, or attach a document.')
    const picker = container.querySelector('input[type="file"]') as HTMLInputElement
    expect(picker.getAttribute('accept')?.split(',')).toContain('.png')

    await userEvent.setup().upload(picker, image('harbour-photo.png'))
    await waitFor(() => expect(tile('harbour-photo.png')).toHaveAttribute('data-status', 'done'))
    expect(callsTo(calls, 'POST', ATTACHMENTS).map(uploaded)).toEqual(['harbour-photo.png'])
    expect(composer().querySelector('li img')).toHaveAttribute('src', 'blob:local-1')
  })

  it('fetches the thumbnail of a HEIC photo once it is ready', async () => {
    setPurpose('ghostwriter', { reason: 'review' })
    let polls = 0
    const { calls } = openChat(
      {
        [`POST ${ATTACHMENTS}`]: () =>
          json(picture('a1', HEIC, { status: 'processing', mime: 'image/heic' }), 201),
        [`GET ${POLL}`]: () => {
          polls += 1
          return json(picture('a1', HEIC, { mime: 'image/heic' }))
        },
        [`GET ${PREVIEW}`]: () => new Response(new Blob(['jpeg'], { type: 'image/jpeg' }), { status: 200 }),
      },
      VISION_CONFIG,
    )
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [image(HEIC, 'image/heic')])
    // A browser cannot draw it: no local picture, and nothing is fetched before it is ready.
    expect(made).toEqual([])
    expect(composer().querySelector('li img')).toBeNull()

    await waitFor(() => expect(composer().querySelector('li img')).not.toBeNull())
    expect(polls).toBe(1)
    const [preview] = callsTo(calls, 'GET', PREVIEW)
    expect(preview.headers[PURPOSE_HEADER]).toBe('reason=review')
    expect(callsTo(calls, 'GET', PREVIEW)).toHaveLength(1)
    expect(composer().querySelector('li img')).toHaveAttribute('src', 'blob:local-1')
    expect(tile(HEIC)).toHaveAttribute('data-status', 'done')
  })

  it('marks a picture that could not be read, and still sends it', async () => {
    const sse = sseStream()
    const { calls } = openChat(
      {
        [`POST ${ATTACHMENTS}`]: () => json(picture('a1', 'harbour-photo.png', { status: 'processing' }), 201),
        [`GET ${POLL}`]: () => json(picture('a1', 'harbour-photo.png', { status: 'failed' })),
        [`POST ${MESSAGES}`]: () => sse.response(),
      },
      VISION_CONFIG,
    )
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [image('harbour-photo.png')])

    // Said below the row (the tile's own marker carries it as well, for a screen reader).
    expect(
      await screen.findByText('harbour-photo.png could not be read; the model will be told.', { selector: 'p' }),
    ).toBeInTheDocument()
    expect(tileStatus(tile('harbour-photo.png'))).toBe('failed')
    expect(screen.queryByText(hint)).not.toBeInTheDocument()
    const send = screen.getByRole('button', { name: 'Send' })
    expect(send).toBeEnabled()
    await userEvent.setup().click(send)
    expect(callsTo(calls, 'POST', MESSAGES).map((c) => c.body)).toEqual([
      { content: '', attachment_ids: ['a1'], reasoning: false },
    ])
  })

  it('warns when more pictures wait than go to the model', async () => {
    openChat(
      {
        [`POST ${ATTACHMENTS}`]: (call: Call) => {
          const name = uploaded(call)
          return json(picture(`id-${name}`, name), 201)
        },
      },
      { ...VISION_CONFIG, max_images: 2 },
    )
    await screen.findByText('Ask anything, or attach a document.')
    const warning = 'At most 2 images go to the model with one message; the others are only named.'

    drop(textbox(), [image('quay-1.png'), image('quay-2.png')])
    await waitFor(() => expect(tile('quay-2.png')).toHaveAttribute('data-status', 'done'))
    expect(screen.queryByText(warning)).not.toBeInTheDocument()

    drop(textbox(), [image('quay-3.png')])
    expect(await screen.findByText(warning)).toBeInTheDocument()
  })

  it('opens a thumbnail in the lightbox and closes it with Escape', async () => {
    openChat({ [`POST ${ATTACHMENTS}`]: () => json(picture('a1', 'harbour-photo.png'), 201) }, VISION_CONFIG)
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [image('harbour-photo.png')])
    await waitFor(() => expect(tile('harbour-photo.png')).toHaveAttribute('data-status', 'done'))

    const face = within(composer()).getByRole('button', { name: 'harbour-photo.png' })
    await user.click(face)
    const dialog = screen.getByRole('dialog', { name: 'harbour-photo.png' })
    expect(within(dialog).getByRole('img', { name: 'harbour-photo.png' })).toHaveAttribute('src', 'blob:local-1')
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(face).toHaveFocus()
    // The picture is still the composer's own.
    expect(revoked).toEqual([])
  })

  it('follows a waiting attachment the chat lists as processing', async () => {
    let ready!: () => void
    const held = new Promise<void>((done) => (ready = done))
    const waiting = { ...detail([]), pending_attachments: [picture('a1', 'harbour-photo.png', { status: 'processing' })] }
    const { calls } = openChat(
      {
        'GET /ghostwriter/api/chats/c1': waiting,
        [`GET ${POLL}`]: async () => {
          await held
          return json(picture('a1', 'harbour-photo.png'))
        },
        [`GET ${PREVIEW}`]: () => new Response(new Blob(['png'], { type: 'image/png' }), { status: 200 }),
      },
      VISION_CONFIG,
    )
    await waitFor(() => expect(callsTo(calls, 'GET', POLL)).toHaveLength(1))
    // Nothing was uploaded, and the picture is not at hand yet.
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(0)
    expect(chip('harbour-photo.png')).toHaveAttribute('data-status', 'processing')
    expect(screen.getByText(hint)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()

    ready()
    await waitFor(() => expect(composer().querySelector('li img')).not.toBeNull())
    expect(tile('harbour-photo.png')).toHaveAttribute('data-status', 'done')
    expect(screen.queryByText(hint)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    expect(callsTo(calls, 'GET', POLL)).toHaveLength(1)
    expect(callsTo(calls, 'GET', PREVIEW)).toHaveLength(1)
  })

  it('waits two seconds after a network error and asks again, and gives up on a 404', async () => {
    let polls = 0
    let listed: AttachmentOut[] = []
    const { calls, client } = openChat(
      {
        'GET /ghostwriter/api/chats/c1': () => json({ ...detail([]), pending_attachments: listed }),
        [`POST ${ATTACHMENTS}`]: (call: Call) =>
          json(picture(uploaded(call) === 'quay-1.png' ? 'a1' : 'a2', uploaded(call), { status: 'processing' }), 201),
        [`GET ${POLL}`]: () => {
          polls += 1
          if (polls === 1) throw new TypeError('network down')
          return json(picture('a1', 'quay-1.png'))
        },
        [`GET ${ATTACHMENTS}/a2?wait=20`]: () => json({ detail: 'not found' }, 404),
      },
      VISION_CONFIG,
    )
    await screen.findByText('Ask anything, or attach a document.')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    drop(textbox(), [image('quay-1.png')])
    await waitFor(() => expect(polls).toBe(1))
    await act(async () => {
      vi.advanceTimersByTime(1_500)
    })
    expect(polls).toBe(1)
    await act(async () => {
      vi.advanceTimersByTime(600)
    })
    await waitFor(() => expect(tile('quay-1.png')).toHaveAttribute('data-status', 'done'))
    expect(polls).toBe(2)

    // A picture the server no longer holds is not waited for again.
    drop(textbox(), [image('quay-2.png')])
    expect(await screen.findByRole('alert')).toHaveTextContent('quay-2.png could not be attached.')
    expect(callsTo(calls, 'GET', `${ATTACHMENTS}/a2?wait=20`)).toHaveLength(1)
    expect(tile('quay-2.png')).toHaveAttribute('data-status', 'error')

    // The chat lists it still: it is no second tile, and once dismissed it does not come back.
    listed = [picture('a2', 'quay-2.png', { status: 'processing' })]
    const reread = () => client.invalidateQueries({ queryKey: ['chat', 'c1'] })
    await act(reread)
    expect(within(composer()).getAllByRole('button', { name: 'quay-2.png' })).toHaveLength(1)
    await userEvent.setup().click(within(tile('quay-2.png')).getByRole('button', { name: 'Remove quay-2.png' }))
    await act(reread)
    expect(within(composer()).queryByText('quay-2.png')).not.toBeInTheDocument()
    expect(composer().querySelector('li[data-status="processing"]')).toBeNull()
    expect(callsTo(calls, 'GET', `${ATTACHMENTS}/a2?wait=20`)).toHaveLength(1)
  })

  it('stops following when the composer goes', async () => {
    const { calls, unmount } = openChat(
      {
        [`POST ${ATTACHMENTS}`]: () => json(picture('a1', 'harbour-photo.png', { status: 'processing' }), 201),
        [`GET ${POLL}`]: () => new Promise<Response>(() => {}),
      },
      VISION_CONFIG,
    )
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [image('harbour-photo.png')])
    await waitFor(() => expect(callsTo(calls, 'GET', POLL)).toHaveLength(1))
    const [poll] = callsTo(calls, 'GET', POLL)
    expect(poll.signal?.aborted).toBe(false)
    unmount()
    expect(poll.signal?.aborted).toBe(true)
  })

  it('revokes every local picture once: on dismissal, when a message takes it, and when the composer goes', async () => {
    const sse = sseStream()
    const { unmount } = openChat(
      {
        [`POST ${ATTACHMENTS}`]: (call: Call) => {
          const name = uploaded(call)
          if (name === 'quay-2.png') return json({ detail: 'Storage is full.', code: 'storage_full' }, 507)
          return json(picture(`id-${name}`, name), 201)
        },
        [`POST ${MESSAGES}`]: () => sse.response(),
      },
      VISION_CONFIG,
    )
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    // One that is sent, one that is refused and dismissed, one still waiting at the end.
    drop(textbox(), [image('quay-1.png'), image('quay-2.png')])
    await waitFor(() => expect(tile('quay-1.png')).toHaveAttribute('data-status', 'done'))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Your files fill the space workbench allows; delete a chat or wait for files to expire.',
    )
    expect(made).toEqual(['blob:local-1', 'blob:local-2'])
    expect(revoked).toEqual([])

    await user.click(within(tile('quay-2.png')).getByRole('button', { name: 'Remove quay-2.png' }))
    expect(revoked).toEqual(['blob:local-2'])

    await user.type(textbox(), 'Und?{Enter}')
    expect(revoked).toEqual(['blob:local-2', 'blob:local-1'])

    drop(textbox(), [image('quay-3.png')])
    await waitFor(() => expect(made).toHaveLength(3))
    unmount()
    expect(revoked.sort()).toEqual(made.slice().sort())
    expect(new Set(revoked).size).toBe(revoked.length)
  })

  it('attaches a lone pasted screenshot under a dated name, and two in one second apart', async () => {
    vi.setSystemTime(new Date(2026, 10, 20, 9, 5, 7))
    const { calls } = openChat(
      {
        [`POST ${ATTACHMENTS}`]: (call: Call) => {
          const name = uploaded(call)
          return json(picture(`id-${name}`, name), 201)
        },
      },
      VISION_CONFIG,
    )
    await screen.findByText('Ask anything, or attach a document.')
    const screenshot = () =>
      createEvent.paste(textbox(), {
        clipboardData: { files: [image('image.png')], types: ['Files'], getData: () => '' },
      })

    const first = screenshot()
    fireEvent(textbox(), first)
    expect(first.defaultPrevented).toBe(true)
    const second = screenshot()
    fireEvent(textbox(), second)
    expect(second.defaultPrevented).toBe(true)

    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(2))
    expect(callsTo(calls, 'POST', ATTACHMENTS).map(uploaded)).toEqual([
      'screenshot-20261120-090507.png',
      'screenshot-20261120-090507-2.png',
    ])
    await waitFor(() => expect(tile('screenshot-20261120-090507-2.png')).toHaveAttribute('data-status', 'done'))

    // A file copied in a file manager keeps its own name; so does a picture of another type.
    const copied = createEvent.paste(textbox(), {
      clipboardData: {
        files: [image('harbour-photo.png')],
        types: ['text/plain', 'Files'],
        getData: (type: string) => (type === 'text/plain' ? 'harbour-photo.png' : ''),
      },
    })
    fireEvent(textbox(), copied)
    expect(copied.defaultPrevented).toBe(true)
    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(3))
    expect(uploaded(callsTo(calls, 'POST', ATTACHMENTS)[2])).toBe('harbour-photo.png')
  })

  it('keeps text that comes with a picture as text, also with pictures on', async () => {
    const { calls } = openChat({ [`POST ${ATTACHMENTS}`]: () => json(picture('a1', 'image.png'), 201) }, VISION_CONFIG)
    await screen.findByText('Ask anything, or attach a document.')

    const rich = createEvent.paste(textbox(), {
      clipboardData: {
        files: [image('image.png')],
        types: ['text/plain', 'text/html', 'Files'],
        getData: (type: string) => (type === 'text/plain' ? 'Spalte B' : ''),
      },
    })
    fireEvent(textbox(), rich)
    expect(rich.defaultPrevented).toBe(false)
    expect(within(composer()).queryByText('image.png')).not.toBeInTheDocument()
    expect(composer().querySelector('li')).toBeNull()
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(0)
    expect(made).toEqual([])

    // Another file that comes with text is still attached.
    const office = createEvent.paste(textbox(), {
      clipboardData: {
        files: [image('image.png'), file('sales.csv')],
        types: ['text/plain', 'Files'],
        getData: (type: string) => (type === 'text/plain' ? 'Spalte B' : ''),
      },
    })
    fireEvent(textbox(), office)
    expect(office.defaultPrevented).toBe(false)
    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS).map(uploaded)).toEqual(['sales.csv']))
  })
})

describe('the reasoning switch', () => {
  it('starts off, per chat, and says its state in its name', async () => {
    serve({ 'GET /ghostwriter/api/chats/c1': detail([]), 'GET /ghostwriter/api/chats/c2': detail([]) })
    const user = userEvent.setup()
    const { rerender } = renderWithClient(<ChatPane key="c1" chatId="c1" config={CONFIG} />)
    const off = await screen.findByRole('button', { name: 'Reasoning: Off — faster, plain answer' })
    expect(off).toHaveAttribute('aria-pressed', 'false')

    await user.click(off)
    const on = screen.getByRole('button', { name: 'Reasoning: On — the model thinks before answering' })
    expect(on).toHaveAttribute('aria-pressed', 'true')

    // Another chat starts off again.
    rerender(<ChatPane key="c2" chatId="c2" config={CONFIG} />)
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
      [`POST ${MESSAGES}`]: () => json({ detail: 'An answer is still being written in this chat.', code: 'busy' }, 409),
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
