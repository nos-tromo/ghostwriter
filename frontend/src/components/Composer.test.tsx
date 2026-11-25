import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, createEvent, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PURPOSE_HEADER, PurposeBar, clearAllPurposes, setPurpose } from '@infra/ui'
import type { AppConfig, AttachmentLength, AttachmentOut, MessageOut } from '../api/types'
import { useConfig } from '../hooks/useConfig'
import { LanguageContext } from '../i18n/LanguageContext'
import { ChatPane } from './ChatPane'
import {
  CHAT,
  CONFIG,
  MEDIA_CONFIG,
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
    length: null,
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
function openChat(routes: Record<string, Answer> = {}, config: AppConfig = CONFIG, onNewChat: () => void = () => {}) {
  const calls = serve({ 'GET /ghostwriter/api/chats/c1': detail([]), 'GET /ghostwriter/api/chats': [CHAT], ...routes })
  const view = renderWithClient(<ChatPane chatId="c1" config={config} onNewChat={onNewChat} />)
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
    // The file period is said once, in the page's footer (RetentionNote), never above the composer.
    expect(screen.queryByText(/7 days/)).not.toBeInTheDocument()
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

    drop(textbox(), [file('tool.exe'), file('big.pdf', 51 * 1024 * 1024)])
    expect(await screen.findByText('tool.exe: this file type is not supported.')).toBeInTheDocument()
    expect(screen.getByText('big.pdf is larger than 50 MB.')).toBeInTheDocument()
    expect(chip('tool.exe')).toHaveAttribute('data-status', 'error')
    expect(chip('big.pdf')).toHaveAttribute('data-status', 'error')
    // The extension is matched in any case.
    drop(textbox(), [file('Bericht.PDF')])
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
        <ChatPane chatId="c1" config={{ ...CONFIG, require_purpose: ['collect'] }} onNewChat={() => {}} />
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

  it("keeps the name of a lone pasted PNG that is not the browser's image.png", async () => {
    const { calls } = openChat(
      { [`POST ${ATTACHMENTS}`]: (call: Call) => json(picture('a1', uploaded(call)), 201) },
      VISION_CONFIG,
    )
    await screen.findByText('Ask anything, or attach a document.')
    const pasted = createEvent.paste(textbox(), {
      clipboardData: { files: [image('harbour-chart.png')], types: ['Files'], getData: () => '' },
    })
    fireEvent(textbox(), pasted)
    expect(pasted.defaultPrevented).toBe(true)
    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(1))
    expect(uploaded(callsTo(calls, 'POST', ATTACHMENTS)[0])).toBe('harbour-chart.png')
  })

  it('does not bring back a picture whose poll was refused, once a message went instead', async () => {
    let listed: AttachmentOut[] = []
    const sse = sseStream()
    const { calls, client } = openChat(
      {
        'GET /ghostwriter/api/chats/c1': () => json({ ...detail([]), pending_attachments: listed }),
        [`POST ${ATTACHMENTS}`]: (call: Call) => json(picture('a1', uploaded(call), { status: 'processing' }), 201),
        [`GET ${POLL}`]: () => json({ detail: 'forbidden' }, 403),
        [`POST ${MESSAGES}`]: () => sse.response(),
      },
      VISION_CONFIG,
    )
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [image('quay-1.png')])
    expect(await screen.findByRole('alert')).toHaveTextContent('quay-1.png could not be attached.')
    expect(tile('quay-1.png')).toHaveAttribute('data-status', 'error')

    // The chat still lists it as being prepared; a message goes without it, its tile not dismissed.
    listed = [picture('a1', 'quay-1.png', { status: 'processing' })]
    const reread = () => client.invalidateQueries({ queryKey: ['chat', 'c1'] })
    await act(reread)
    await user.type(textbox(), 'Und?{Enter}')
    await waitFor(() => expect(callsTo(calls, 'POST', MESSAGES)).toHaveLength(1))
    expect(callsTo(calls, 'POST', MESSAGES)[0].body).toEqual({ content: 'Und?', attachment_ids: [], reasoning: false })
    await act(reread)
    expect(composer().querySelector('li')).toBeNull()
    expect(callsTo(calls, 'GET', POLL)).toHaveLength(1)
  })

  it('asks again no sooner than half a second after a processing answer', async () => {
    let polls = 0
    openChat(
      {
        [`POST ${ATTACHMENTS}`]: () => json(picture('a1', 'harbour-photo.png', { status: 'processing' }), 201),
        [`GET ${POLL}`]: () => {
          polls += 1
          return json(picture('a1', 'harbour-photo.png', polls < 3 ? { status: 'processing' } : {}))
        },
      },
      VISION_CONFIG,
    )
    await screen.findByText('Ask anything, or attach a document.')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    drop(textbox(), [image('harbour-photo.png')])
    await waitFor(() => expect(polls).toBe(1))
    await act(async () => {
      vi.advanceTimersByTime(400)
    })
    expect(polls).toBe(1)
    await act(async () => {
      vi.advanceTimersByTime(200)
    })
    await waitFor(() => expect(polls).toBe(2))
    await act(async () => {
      vi.advanceTimersByTime(600)
    })
    await waitFor(() => expect(tile('harbour-photo.png')).toHaveAttribute('data-status', 'done'))
    expect(polls).toBe(3)
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

describe('Composer clips', () => {
  const FILE = `${ATTACHMENTS}/a1`
  const POLL = `${FILE}?wait=20`
  const hint = 'Video and audio take longer: they are transcribed before they are sent.'

  function clip(id: string, name: string, fields: Partial<AttachmentOut> = {}): AttachmentOut {
    const mime = name.endsWith('.mp4') ? 'video/mp4' : 'audio/mp4'
    return { ...attachment(id, name, 4_194_304), mime, kind: 'media', ...fields }
  }

  const video = (name: string) => new File(['mp4'], name, { type: 'video/mp4' })
  const audio = (name: string) => new File(['m4a'], name, { type: 'audio/mp4' })
  const kindOf = (name: string) => chip(name).querySelector('[data-kind]')?.getAttribute('data-kind')

  /** The composer under a config the page reads itself, as the app does; `config` answers `GET /config`. */
  function Page({ chatId }: { chatId: string }) {
    const { data: config } = useConfig()
    return config ? <ChatPane chatId={chatId} config={config} onNewChat={() => {}} /> : null
  }

  it('shows a dropped clip as a video tile while it is transcribed, and sends once it is ready', async () => {
    let ready!: () => void
    const held = new Promise<void>((done) => (ready = done))
    const sse = sseStream()
    const { calls } = openChat(
      {
        [`POST ${ATTACHMENTS}`]: () => json(clip('a1', 'harbour-tour.mp4', { status: 'processing' }), 201),
        [`GET ${POLL}`]: async () => {
          await held
          return json(clip('a1', 'harbour-tour.mp4'))
        },
        [`POST ${MESSAGES}`]: () => sse.response(),
      },
      MEDIA_CONFIG,
    )
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.type(textbox(), 'Was wird gesagt?')

    drop(textbox(), [video('harbour-tour.mp4')])
    await waitFor(() => expect(callsTo(calls, 'GET', POLL)).toHaveLength(1))
    // No picture of it, now or later: the tile's icon only, and no preview is asked.
    expect(kindOf('harbour-tour.mp4')).toBe('video')
    expect(chip('harbour-tour.mp4')).toHaveAttribute('data-status', 'processing')
    expect(composer().querySelector('li img')).toBeNull()
    expect(within(chip('harbour-tour.mp4')).getByLabelText('Transcribing harbour-tour.mp4…')).toBeInTheDocument()
    expect(screen.getByText(hint)).toBeInTheDocument()
    expect(screen.queryByText('Sending waits until the attached files are ready.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.click(textbox())
    await user.keyboard('{Enter}')
    expect(callsTo(calls, 'POST', MESSAGES)).toHaveLength(0)

    ready()
    await waitFor(() => expect(chip('harbour-tour.mp4')).toHaveAttribute('data-status', 'done'))
    expect(kindOf('harbour-tour.mp4')).toBe('video')
    expect(screen.queryByText(hint)).not.toBeInTheDocument()
    expect(composer().querySelector('li img')).toBeNull()
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    await user.keyboard('{Enter}')
    expect(callsTo(calls, 'POST', MESSAGES).map((c) => c.body)).toEqual([
      { content: 'Was wird gesagt?', attachment_ids: ['a1'], reasoning: false },
    ])
    expect(calls.filter((c) => c.url.includes('/preview'))).toEqual([])
  })

  it('shows an audio file as an audio tile', async () => {
    const { calls } = openChat(
      {
        [`POST ${ATTACHMENTS}`]: () => json(clip('a1', 'voice-memo.m4a', { status: 'processing' }), 201),
        [`GET ${POLL}`]: () => json(clip('a1', 'voice-memo.m4a')),
      },
      MEDIA_CONFIG,
    )
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [audio('voice-memo.M4A')])
    expect(kindOf('voice-memo.M4A')).toBe('audio')
    await waitFor(() => expect(chip('voice-memo.m4a')).toHaveAttribute('data-status', 'done'))
    expect(kindOf('voice-memo.m4a')).toBe('audio')
    expect(calls.filter((c) => c.url.includes('/preview'))).toEqual([])
  })

  it('opens no lightbox for a clip', async () => {
    const { calls } = openChat(
      { [`POST ${ATTACHMENTS}`]: () => json(clip('a1', 'harbour-tour.mp4'), 201) },
      MEDIA_CONFIG,
    )
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [video('harbour-tour.mp4')])
    await waitFor(() => expect(chip('harbour-tour.mp4')).toHaveAttribute('data-status', 'done'))

    // Its face is no button, and a click on it opens nothing.
    expect(within(chip('harbour-tour.mp4')).queryByRole('button', { name: 'harbour-tour.mp4' })).not.toBeInTheDocument()
    await user.click(screen.getByText('harbour-tour.mp4'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(calls.filter((c) => c.url.includes('/preview'))).toEqual([])
  })

  it('follows a clip the chat lists as processing', async () => {
    let ready!: () => void
    const held = new Promise<void>((done) => (ready = done))
    const waiting = { ...detail([]), pending_attachments: [clip('a1', 'harbour-tour.mp4', { status: 'processing' })] }
    const { calls } = openChat(
      {
        'GET /ghostwriter/api/chats/c1': waiting,
        [`GET ${POLL}`]: async () => {
          await held
          return json(clip('a1', 'harbour-tour.mp4'))
        },
      },
      MEDIA_CONFIG,
    )
    await waitFor(() => expect(callsTo(calls, 'GET', POLL)).toHaveLength(1))
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(0)
    expect(kindOf('harbour-tour.mp4')).toBe('video')
    expect(chip('harbour-tour.mp4')).toHaveAttribute('data-status', 'processing')
    expect(screen.getByText(hint)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()

    ready()
    await waitFor(() => expect(chip('harbour-tour.mp4')).toHaveAttribute('data-status', 'done'))
    expect(screen.queryByText(hint)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    expect(calls.filter((c) => c.url.includes('/preview'))).toEqual([])
  })

  it('shows a ready clip the chat lists after a reload as a video tile, and asks no preview', async () => {
    const sse = sseStream()
    const waiting = { ...detail([]), pending_attachments: [clip('a1', 'harbour-tour.mp4')] }
    const { calls } = openChat(
      { 'GET /ghostwriter/api/chats/c1': waiting, [`POST ${MESSAGES}`]: () => sse.response() },
      MEDIA_CONFIG,
    )
    await waitFor(() => expect(chip('harbour-tour.mp4')).toHaveAttribute('data-status', 'done'))
    expect(kindOf('harbour-tour.mp4')).toBe('video')
    expect(composer().querySelector('li img')).toBeNull()
    expect(within(chip('harbour-tour.mp4')).queryByRole('button', { name: 'harbour-tour.mp4' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()

    // It is sent as it is, and still no preview is asked.
    await userEvent.setup().click(screen.getByRole('button', { name: 'Send' }))
    expect(callsTo(calls, 'POST', MESSAGES).map((c) => c.body)).toEqual([
      { content: '', attachment_ids: ['a1'], reasoning: false },
    ])
    expect(callsTo(calls, 'GET', POLL)).toHaveLength(0)
    expect(calls.filter((c) => c.url.includes('/preview'))).toEqual([])
  })

  it('takes a clip back while it is transcribed', async () => {
    let answer!: () => void
    const held = new Promise<void>((done) => (answer = done))
    // The follow's late answer, a 404 now; `read` says its body was pulled, that is, the follow is reading it.
    let read = false
    const lateAnswer = () =>
      new Response(
        new ReadableStream<Uint8Array>(
          {
            pull(stream) {
              read = true
              stream.enqueue(new TextEncoder().encode(JSON.stringify({ detail: 'Attachment not found.' })))
              stream.close()
            },
          },
          { highWaterMark: 0 },
        ),
        { status: 404, headers: { 'content-type': 'application/json' } },
      )
    const { calls } = openChat(
      {
        [`POST ${ATTACHMENTS}`]: () => json(clip('a1', 'harbour-tour.mp4', { status: 'processing' }), 201),
        [`GET ${POLL}`]: async () => {
          await held
          return lateAnswer()
        },
        [`DELETE ${FILE}`]: null,
      },
      MEDIA_CONFIG,
    )
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [video('harbour-tour.mp4')])
    await waitFor(() => expect(callsTo(calls, 'GET', POLL)).toHaveLength(1))
    expect(chip('harbour-tour.mp4')).toHaveAttribute('data-status', 'processing')

    await user.click(within(chip('harbour-tour.mp4')).getByRole('button', { name: 'Remove harbour-tour.mp4' }))
    await waitFor(() => expect(within(composer()).queryByText('harbour-tour.mp4')).not.toBeInTheDocument())
    expect(callsTo(calls, 'DELETE', FILE)).toHaveLength(1)
    // Send stays as without it: nothing to send, and the follow's late answer brings no error tile back.
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    expect(screen.queryByText(hint)).not.toBeInTheDocument()
    answer()
    await waitFor(() => expect(read).toBe(true))
    await act(async () => {})
    expect(within(composer()).queryByText('harbour-tour.mp4')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('offers no clips without Nextext', async () => {
    const { calls, container } = openChat({ [`POST ${ATTACHMENTS}`]: () => json(clip('a1', 'harbour-tour.mp4'), 201) })
    await screen.findByText('Ask anything, or attach a document.')
    const picker = container.querySelector('input[type="file"]') as HTMLInputElement
    expect(picker.getAttribute('accept')?.split(',')).not.toContain('.mp4')

    drop(textbox(), [video('harbour-tour.mp4')])
    expect(await screen.findByText('harbour-tour.mp4: this file type is not supported.')).toBeInTheDocument()
    expect(chip('harbour-tour.mp4')).toHaveAttribute('data-status', 'error')
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(0)
  })

  it('says a clip cannot be attached now when the server refuses it, and reads the offer again', async () => {
    let offered = true
    const calls = serve({
      'GET /ghostwriter/api/chats/c1': detail([]),
      'GET /ghostwriter/api/chats': [CHAT],
      'GET /ghostwriter/api/config': () => json(offered ? MEDIA_CONFIG : CONFIG),
      [`POST ${ATTACHMENTS}`]: () => {
        // Nextext went down since the page read the config.
        offered = false
        return json({ detail: 'This file type is not supported.' }, 415)
      },
    })
    const { container } = renderWithClient(<Page chatId="c1" />)
    await screen.findByText('Ask anything, or attach a document.')
    const picker = () => container.querySelector('input[type="file"]') as HTMLInputElement
    expect(picker().getAttribute('accept')?.split(',')).toContain('.mp4')
    expect(callsTo(calls, 'GET', '/ghostwriter/api/config')).toHaveLength(1)

    drop(textbox(), [video('harbour-tour.mp4')])
    expect(
      await screen.findByText('harbour-tour.mp4 cannot be attached now: video and audio are not available.'),
    ).toBeInTheDocument()
    expect(chip('harbour-tour.mp4')).toHaveAttribute('data-status', 'error')
    // The offer is read again, and the picker drops the clip types.
    await waitFor(() => expect(callsTo(calls, 'GET', '/ghostwriter/api/config')).toHaveLength(2))
    await waitFor(() => expect(picker().getAttribute('accept')?.split(',')).not.toContain('.mp4'))

    // Any other file refused with a 415 keeps the plain reason, and the offer is not read again.
    drop(textbox(), [file('sales.csv')])
    expect(await screen.findByText('sales.csv could not be attached.')).toBeInTheDocument()
    expect(callsTo(calls, 'GET', '/ghostwriter/api/config')).toHaveLength(2)
  })
})

describe('the reasoning switch', () => {
  it('starts off, per chat, and says its state in its name', async () => {
    serve({ 'GET /ghostwriter/api/chats/c1': detail([]), 'GET /ghostwriter/api/chats/c2': detail([]) })
    const user = userEvent.setup()
    const { rerender } = renderWithClient(<ChatPane key="c1" chatId="c1" config={CONFIG} onNewChat={() => {}} />)
    const off = await screen.findByRole('button', { name: 'Reasoning: Off — faster, plain answer' })
    expect(off).toHaveAttribute('aria-pressed', 'false')

    await user.click(off)
    const on = screen.getByRole('button', { name: 'Reasoning: On — the model thinks before answering' })
    expect(on).toHaveAttribute('aria-pressed', 'true')

    // Another chat starts off again.
    rerender(<ChatPane key="c2" chatId="c2" config={CONFIG} onNewChat={() => {}} />)
    expect(await screen.findByRole('button', { name: 'Reasoning: Off — faster, plain answer' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('says its state in German too', async () => {
    serve({ 'GET /ghostwriter/api/chats/c1': detail([]) })
    renderWithClient(
      <LanguageContext value="de">
        <ChatPane chatId="c1" config={CONFIG} onNewChat={() => {}} />
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

describe('Taking an attachment back', () => {
  const FILE = `${ATTACHMENTS}/a1`
  const POLL = `${FILE}?wait=20`
  const removeButton = (name: string) => within(chip(name)).getByRole('button', { name: `Remove ${name}` })

  function picture(id: string, name: string, fields: Partial<AttachmentOut> = {}): AttachmentOut {
    return { ...attachment(id, name, 2048), mime: 'image/png', kind: 'image', ...fields }
  }

  it('deletes a file on the server before it is sent, and then takes it out of view', async () => {
    const { calls } = openChat({
      [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'sales.xlsx'), 201),
      [`DELETE ${FILE}`]: null,
    })
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('sales.xlsx')])
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()

    await userEvent.setup().click(removeButton('sales.xlsx'))
    await waitFor(() => expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument())
    expect(callsTo(calls, 'DELETE', FILE)).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  it('keeps a file whose taking back failed, says so, and takes it back on a second try', async () => {
    let tries = 0
    const { calls } = openChat({
      [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'sales.xlsx'), 201),
      [`DELETE ${FILE}`]: () =>
        ++tries === 1
          ? json({ detail: 'The file could not be removed from workbench.' }, 502)
          : new Response(null, { status: 204 }),
    })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('sales.xlsx')])
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))

    await user.click(removeButton('sales.xlsx'))
    expect(await screen.findByText('sales.xlsx could not be removed.')).toBeInTheDocument()
    expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done')
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()

    await user.click(removeButton('sales.xlsx'))
    await waitFor(() => expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument())
    expect(screen.queryByText('sales.xlsx could not be removed.')).not.toBeInTheDocument()
    expect(callsTo(calls, 'DELETE', FILE)).toHaveLength(2)
  })

  it('holds sending while a file is being taken back', async () => {
    let release!: () => void
    const held = new Promise<void>((done) => (release = done))
    openChat({
      [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'sales.xlsx'), 201),
      [`DELETE ${FILE}`]: async () => {
        await held
        return new Response(null, { status: 204 })
      },
    })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    await user.type(textbox(), 'Summe')
    drop(textbox(), [file('sales.xlsx')])
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))

    await user.click(removeButton('sales.xlsx'))
    expect(await within(chip('sales.xlsx')).findByLabelText('Removing sales.xlsx…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    expect(within(chip('sales.xlsx')).queryByRole('button', { name: 'Remove sales.xlsx' })).not.toBeInTheDocument()

    release()
    await waitFor(() => expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  it('takes back a picture the chat lists as being prepared, and follows it no further', async () => {
    let answerPoll!: () => void
    const polled = new Promise<void>((done) => (answerPoll = done))
    let listed: AttachmentOut[] = [picture('a1', 'harbour-photo.png', { status: 'processing' })]
    const { calls } = openChat(
      {
        'GET /ghostwriter/api/chats/c1': () => json({ ...detail([]), pending_attachments: listed }),
        [`GET ${POLL}`]: async () => {
          await polled
          return json({ detail: 'Attachment not found.' }, 404)
        },
        [`DELETE ${FILE}`]: () => {
          listed = []
          return new Response(null, { status: 204 })
        },
      },
      VISION_CONFIG,
    )
    await waitFor(() => expect(callsTo(calls, 'GET', POLL)).toHaveLength(1))
    await userEvent.setup().click(removeButton('harbour-photo.png'))
    await waitFor(() => expect(within(composer()).queryByText('harbour-photo.png')).not.toBeInTheDocument())

    // The follow's answer, a 404 now, brings back no error tile.
    answerPoll()
    await new Promise((done) => setTimeout(done, 50))
    expect(within(composer()).queryByText('harbour-photo.png')).not.toBeInTheDocument()
    expect(callsTo(calls, 'DELETE', FILE)).toHaveLength(1)
  })

  it('treats a file the server holds no more as taken back, and keeps the focus in the composer', async () => {
    openChat({
      [`POST ${ATTACHMENTS}`]: () => json(attachment('a1', 'sales.xlsx'), 201),
      [`DELETE ${FILE}`]: () => json({ detail: 'Attachment not found.' }, 404),
    })
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('sales.xlsx')])
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))
    await userEvent.setup().click(removeButton('sales.xlsx'))
    await waitFor(() => expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument())
    expect(textbox()).toHaveFocus()
    expect(screen.queryByText('sales.xlsx could not be removed.')).not.toBeInTheDocument()
  })

  it('never uploads a queued file taken back before its turn', async () => {
    let store!: () => void
    const stored = new Promise<void>((done) => (store = done))
    const { calls } = openChat({
      [`POST ${ATTACHMENTS}`]: async (call: Call) => {
        await stored
        return json(attachment('a1', uploaded(call)), 201)
      },
    })
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('sales.xlsx'), file('wrong.xlsx')])
    await waitFor(() => expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(1))
    await userEvent.setup().click(removeButton('wrong.xlsx'))
    expect(within(composer()).queryByText('wrong.xlsx')).not.toBeInTheDocument()
    store()
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done'))
    expect(callsTo(calls, 'POST', ATTACHMENTS).map(uploaded)).toEqual(['sales.xlsx'])
  })

  it('brings back a file taken back while uploading when the server could not delete it', async () => {
    let store!: () => void
    const stored = new Promise<void>((done) => (store = done))
    openChat({
      [`POST ${ATTACHMENTS}`]: async () => {
        await stored
        return json(attachment('a1', 'sales.xlsx'), 201)
      },
      [`DELETE ${FILE}`]: () => json({ detail: 'The file could not be removed from workbench.' }, 502),
    })
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('sales.xlsx')])
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'uploading'))
    await userEvent.setup().click(removeButton('sales.xlsx'))
    expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument()
    store()
    expect(await screen.findByText('sales.xlsx could not be removed.')).toBeInTheDocument()
    expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'done')
  })

  it('takes back a file still uploading: it goes at once, and on the server as soon as it is stored', async () => {
    let store!: () => void
    const stored = new Promise<void>((done) => (store = done))
    const { calls } = openChat({
      [`POST ${ATTACHMENTS}`]: async () => {
        await stored
        return json(attachment('a1', 'sales.xlsx'), 201)
      },
      [`DELETE ${FILE}`]: null,
    })
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('sales.xlsx')])
    await waitFor(() => expect(chip('sales.xlsx')).toHaveAttribute('data-status', 'uploading'))

    await userEvent.setup().click(removeButton('sales.xlsx'))
    expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument()
    store()
    await waitFor(() => expect(callsTo(calls, 'DELETE', FILE)).toHaveLength(1))
    expect(within(composer()).queryByText('sales.xlsx')).not.toBeInTheDocument()
  })
})

describe('the context ring and note', () => {
  const NEARLY = 'This chat is nearly as long as the model can read at once. For a new topic, start a new chat.'
  const TRIMMED = 'Earlier parts of this chat are no longer sent to the model.'
  const CHAT_URL = 'GET /ghostwriter/api/chats/c1'
  const REASONING_OFF = 'Reasoning: Off — faster, plain answer'

  /** The assistant's answer with the estimate it stored, `null` for one without. */
  const answer = (id: string, estimate: number | null, fields: Partial<MessageOut> = {}): MessageOut =>
    message({ id, role: 'assistant', content: `Antwort ${id}`, context_estimate: estimate, ...fields })
  const question = (id: string) => message({ id, role: 'user', content: `Frage ${id}` })
  const trimmed: MessageOut['notices'] = [{ code: 'context_trimmed' }]

  it('shows no ring in an empty chat, nor after an answer stored without an estimate', async () => {
    openChat()
    await screen.findByText('Ask anything, or attach a document.')
    expect(screen.queryByRole('meter')).not.toBeInTheDocument()
    expect(screen.queryByText(NEARLY)).not.toBeInTheDocument()
  })

  it('shows no ring for a chat written before estimates were kept', async () => {
    openChat({ [CHAT_URL]: detail([question('q1'), answer('a1', null)]) })
    await screen.findByText('Antwort a1')
    expect(screen.queryByRole('meter')).not.toBeInTheDocument()
  })

  it('shows how full the chat is, by the switch', async () => {
    openChat({ [CHAT_URL]: detail([question('q1'), answer('a1', 20_400)]) })
    const user = userEvent.setup()
    const ring = await screen.findByRole('meter', { name: 'about 60 % of the context used' })
    expect(ring).toHaveAttribute('aria-valuenow', '60')
    // The reasoning switch has the smaller budget: the same estimate fills more of it.
    await user.click(screen.getByRole('button', { name: REASONING_OFF }))
    expect(screen.getByRole('meter', { name: 'about 78 % of the context used' })).toHaveAttribute('aria-valuenow', '78')
    expect(screen.queryByText(NEARLY)).not.toBeInTheDocument()
  })

  it('words the ring in German too', async () => {
    serve({ [CHAT_URL]: detail([question('q1'), answer('a1', 20_400)]) })
    renderWithClient(
      <LanguageContext value="de">
        <ChatPane chatId="c1" config={CONFIG} onNewChat={() => {}} />
      </LanguageContext>,
    )
    expect(await screen.findByRole('meter', { name: 'ca. 60 % des Kontexts belegt' })).toBeInTheDocument()
  })

  it('reads the latest answer only', async () => {
    openChat({ [CHAT_URL]: detail([question('q1'), answer('a1', 30_000), question('q2'), answer('a2', 3_400)]) })
    expect(await screen.findByRole('meter', { name: 'about 10 % of the context used' })).toBeInTheDocument()
    expect(screen.queryByText(NEARLY)).not.toBeInTheDocument()
  })

  it('shows the nearly-full note from 80 % with New chat', async () => {
    const onNewChat = vi.fn()
    openChat({ [CHAT_URL]: detail([question('q1'), answer('a1', 27_200)]) }, CONFIG, onNewChat)
    const user = userEvent.setup()
    const note = await screen.findByText(NEARLY)
    expect(screen.getByRole('meter', { name: 'about 80 % of the context used' })).toHaveAttribute('data-level', 'warn')
    expect(screen.queryByText(TRIMMED)).not.toBeInTheDocument()
    // Not announced: it is read in its place, like the line under an answer that is stored.
    expect(note.closest('[role="status"], [role="alert"], [aria-live]')).toBeNull()
    expect(onNewChat).not.toHaveBeenCalled()
    await user.click(within(composer()).getByRole('button', { name: 'New chat' }))
    expect(onNewChat).toHaveBeenCalledTimes(1)
  })

  it('decides the note on the unrounded ratio, as the ring does', async () => {
    // 27_166 of 34_000 is 0.799: the ring reads 80 and is below the warning level.
    const { client } = openChat({ [CHAT_URL]: detail([question('q1'), answer('a1', 27_166)]) })
    const ring = await screen.findByRole('meter', { name: 'about 80 % of the context used' })
    expect(ring).toHaveAttribute('aria-valuenow', '80')
    expect(ring).toHaveAttribute('data-level', 'normal')
    expect(screen.queryByText(NEARLY)).not.toBeInTheDocument()

    act(() => {
      client.setQueryData(['chat', 'c1'], detail([question('q1'), answer('a1', 27_200)]))
    })
    expect(await screen.findByText(NEARLY)).toBeInTheDocument()
    expect(screen.getByRole('meter')).toHaveAttribute('data-level', 'warn')
  })

  it('has the note follow the reasoning switch, and stops at a full ring', async () => {
    openChat({ [CHAT_URL]: detail([question('q1'), answer('a1', 22_000)]) })
    const user = userEvent.setup()
    await screen.findByRole('meter', { name: 'about 65 % of the context used' })
    expect(screen.queryByText(NEARLY)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: REASONING_OFF }))
    expect(screen.getByRole('meter', { name: 'about 85 % of the context used' })).toBeInTheDocument()
    expect(screen.getByText(NEARLY)).toBeInTheDocument()
  })

  it('caps an estimate over the budget at a full ring', async () => {
    openChat({ [CHAT_URL]: detail([question('q1'), answer('a1', 40_000)]) })
    expect(await screen.findByRole('meter', { name: 'about 100 % of the context used' })).toBeInTheDocument()
    expect(screen.getByText(NEARLY)).toBeInTheDocument()
  })

  it('shows the trimmed note for the latest answer only', async () => {
    const { client } = openChat({
      [CHAT_URL]: detail([question('q1'), answer('a1', 1_000, { notices: trimmed }), question('q2'), answer('a2', 1_000)]),
    })
    await screen.findByRole('meter')
    expect(screen.queryByText(TRIMMED)).not.toBeInTheDocument()

    act(() => {
      client.setQueryData(
        ['chat', 'c1'],
        detail([question('q1'), answer('a1', 1_000), question('q2'), answer('a2', 1_000, { notices: trimmed })]),
      )
    })
    expect(await screen.findByText(TRIMMED)).toBeInTheDocument()
    expect(screen.queryByText(NEARLY)).not.toBeInTheDocument()

    act(() => {
      client.setQueryData(['chat', 'c1'], detail([question('q1'), answer('a1', 27_200, { notices: trimmed })]))
    })
    expect(await screen.findByText(NEARLY)).toBeInTheDocument()
    expect(screen.getByText(TRIMMED)).toBeInTheDocument()
    expect(within(composer()).getAllByRole('button', { name: 'New chat' })).toHaveLength(1)
  })

  it('shows the trimmed note while the answer is written, and keeps the ring at its last reading', async () => {
    const sse = sseStream()
    const { client } = openChat({
      [CHAT_URL]: detail([question('q1'), answer('a1', 3_400)]),
      [`POST ${MESSAGES}`]: () => sse.response(),
    })
    const user = userEvent.setup()
    await screen.findByRole('meter', { name: 'about 10 % of the context used' })

    await user.type(textbox(), 'Und Spalte C?{Enter}')
    await screen.findByRole('button', { name: 'Stop' })
    expect(screen.queryByText(TRIMMED)).not.toBeInTheDocument()
    act(() => sse.push('start', { message_id: 'a2', user_message_id: 'q2' }))
    // A read of the chat meanwhile lists the turn's own rows, whose answer has no estimate yet.
    act(() => {
      client.setQueryData(['chat', 'c1'], detail([question('q1'), answer('a1', 3_400), question('q2'), answer('a2', null)]))
    })
    act(() => sse.push('notice', { code: 'context_trimmed' }))
    expect(await screen.findByText(TRIMMED)).toBeInTheDocument()
    expect(screen.getByRole('meter', { name: 'about 10 % of the context used' })).toBeInTheDocument()
    act(() => sse.end())
  })
})

describe('Composer long documents', () => {
  const FILE = `${ATTACHMENTS}/a1`
  const POLL = `${FILE}?wait=20`
  const TOO_LONG = 'Read only in parts'

  const length = (fields: Partial<AttachmentLength> = {}): AttachmentLength => ({
    state: 'measured',
    total: 400,
    unit: 'page',
    too_long: false,
    ...fields,
  })
  const PENDING = length({ state: 'pending', total: null, unit: null })
  const report = (fields: Partial<AttachmentOut> = {}): AttachmentOut => ({
    ...attachment('a1', 'long-report.pdf', 4096),
    mime: 'application/pdf',
    ...fields,
  })

  it('warns on the tile of a very long document, once it is measured, and sends meanwhile', async () => {
    let measured!: () => void
    const held = new Promise<void>((done) => (measured = done))
    const { calls } = openChat({
      [`POST ${ATTACHMENTS}`]: () => json(report({ length: PENDING }), 201),
      [`GET ${POLL}`]: async () => {
        await held
        return json(report({ length: length({ too_long: true }) }))
      },
    })
    await screen.findByText('Ask anything, or attach a document.')
    drop(textbox(), [file('long-report.pdf')])
    await waitFor(() => expect(chip('long-report.pdf')).toHaveAttribute('data-status', 'done'))

    // Ready at once: Send does not wait for the measure, which is followed meanwhile.
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    await waitFor(() => expect(callsTo(calls, 'GET', POLL)).toHaveLength(1))
    expect(screen.queryByText(TOO_LONG)).not.toBeInTheDocument()

    measured()
    // The document tile has no button face (nothing opens), so the warning is text in its place.
    expect(await within(chip('long-report.pdf')).findByText(TOO_LONG)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  it('says the warning in German too', async () => {
    serve({
      'GET /ghostwriter/api/chats/c1': { ...detail([]), pending_attachments: [report({ length: length({ too_long: true }) })] },
    })
    renderWithClient(
      <LanguageContext value="de">
        <ChatPane chatId="c1" config={CONFIG} onNewChat={() => {}} />
      </LanguageContext>,
    )
    expect(await screen.findByText('Nur in Teilen lesbar')).toBeInTheDocument()
  })

  it('shows no warning for a document that is not too long, or could not be measured', async () => {
    serve({
      'GET /ghostwriter/api/chats/c1': {
        ...detail([]),
        pending_attachments: [
          report({ length: length() }),
          report({ id: 'a2', name: 'tide-notes.csv', length: length({ state: 'none', total: null, unit: null }) }),
        ],
      },
    })
    renderWithClient(<ChatPane chatId="c1" config={CONFIG} onNewChat={() => {}} />)
    await screen.findByText('long-report.pdf')
    expect(screen.getByText('tide-notes.csv')).toBeInTheDocument()
    expect(screen.queryByText(TOO_LONG)).not.toBeInTheDocument()
  })

  it('follows a document the chat lists with its measure pending', async () => {
    const waiting = { ...detail([]), pending_attachments: [report({ length: PENDING })] }
    const { calls } = openChat({
      'GET /ghostwriter/api/chats/c1': waiting,
      [`GET ${POLL}`]: () => json(report({ length: length({ too_long: true }) })),
    })
    expect(await screen.findByText(TOO_LONG)).toBeInTheDocument()
    expect(callsTo(calls, 'GET', POLL)).toHaveLength(1)
    expect(callsTo(calls, 'POST', ATTACHMENTS)).toHaveLength(0)
  })

  it('stops following once a document is measured', async () => {
    let polls = 0
    openChat({
      [`POST ${ATTACHMENTS}`]: () => json(report({ length: PENDING }), 201),
      [`GET ${POLL}`]: () => {
        polls += 1
        return json(report({ length: length({ too_long: true }) }))
      },
    })
    await screen.findByText('Ask anything, or attach a document.')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    drop(textbox(), [file('long-report.pdf')])
    await waitFor(() => expect(polls).toBe(1))
    await screen.findByText(TOO_LONG)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(polls).toBe(1)
  })

  it('asks again no sooner than half a second after a pending answer, and ends with the measure', async () => {
    let polls = 0
    openChat({
      [`POST ${ATTACHMENTS}`]: () => json(report({ length: PENDING }), 201),
      [`GET ${POLL}`]: () => {
        polls += 1
        return json(report({ length: polls < 3 ? PENDING : length({ too_long: true }) }))
      },
    })
    await screen.findByText('Ask anything, or attach a document.')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    drop(textbox(), [file('long-report.pdf')])
    await waitFor(() => expect(polls).toBe(1))
    await act(async () => {
      vi.advanceTimersByTime(400)
    })
    expect(polls).toBe(1)
    await act(async () => {
      vi.advanceTimersByTime(200)
    })
    await waitFor(() => expect(polls).toBe(2))
    await act(async () => {
      vi.advanceTimersByTime(600)
    })
    expect(await screen.findByText(TOO_LONG)).toBeInTheDocument()
    expect(polls).toBe(3)
  })

  it('does not follow an attachment stored before measuring', async () => {
    const stored = { ...detail([]), pending_attachments: [report({ length: null })] }
    const { calls } = openChat({ 'GET /ghostwriter/api/chats/c1': stored })
    await screen.findByText('long-report.pdf')
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    expect(screen.queryByText(TOO_LONG)).not.toBeInTheDocument()
    expect(calls.filter((c) => c.url.includes('?wait='))).toEqual([])
  })

  it('stops following a document that a message took, once the answer in flight is in', async () => {
    let first!: () => void
    const held = new Promise<void>((done) => (first = done))
    const sse = sseStream()
    let polls = 0
    openChat({
      [`POST ${ATTACHMENTS}`]: () => json(report({ length: PENDING }), 201),
      [`POST ${MESSAGES}`]: () => sse.response(),
      [`GET ${POLL}`]: async () => {
        polls += 1
        await held
        return json(report({ length: PENDING }))
      },
    })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    drop(textbox(), [file('long-report.pdf')])
    await waitFor(() => expect(polls).toBe(1))
    await user.type(textbox(), 'Fasse zusammen{Enter}')
    await waitFor(() => expect(within(composer()).queryByText('long-report.pdf')).not.toBeInTheDocument())

    first()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(polls).toBe(1)
    act(() => sse.end())
  })

  it('follows a document again when the message that took it is refused', async () => {
    let first!: () => void
    const held = new Promise<void>((done) => (first = done))
    let refuse!: () => void
    const refused = new Promise<void>((done) => (refuse = done))
    let polls = 0
    // The chat, read again after the refusal, lists the file as still waiting.
    let waiting: AttachmentOut[] = []
    openChat({
      'GET /ghostwriter/api/chats/c1': () => json({ ...detail([]), pending_attachments: waiting }),
      [`POST ${ATTACHMENTS}`]: () => {
        waiting = [report({ length: PENDING })]
        return json(report({ length: PENDING }), 201)
      },
      [`POST ${MESSAGES}`]: async () => {
        await refused
        return json({ detail: 'An answer is still being written in this chat.', code: 'busy' }, 409)
      },
      [`GET ${POLL}`]: async () => {
        polls += 1
        if (polls === 1) await held
        return json(report({ length: polls < 3 ? PENDING : length({ too_long: true }) }))
      },
    })
    const user = userEvent.setup()
    await screen.findByText('Ask anything, or attach a document.')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    drop(textbox(), [file('long-report.pdf')])
    await waitFor(() => expect(polls).toBe(1))
    await user.type(textbox(), 'Fasse zusammen{Enter}')
    await waitFor(() => expect(within(composer()).queryByText('long-report.pdf')).not.toBeInTheDocument())

    // The answer to the first request comes while the file is out of the composer: following ends.
    first()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000)
    })
    expect(polls).toBe(1)

    // The message is refused and the file comes back, its measure still pending: it is followed again.
    refuse()
    expect(await within(composer()).findByText('long-report.pdf')).toBeInTheDocument()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000)
    })
    expect(await screen.findByText(TOO_LONG)).toBeInTheDocument()
    expect(polls).toBe(3)
  })
})
