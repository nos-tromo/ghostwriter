import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PURPOSE_HEADER, clearAllPurposes, setPurpose } from '@infra/ui'
import type { AttachmentOut } from '../api/types'
import type { ChatStream } from '../hooks/useChatStream'
import { LanguageContext } from '../i18n/LanguageContext'
import {
  CHAT,
  CONFIG,
  StreamedConversation,
  VISION_CONFIG,
  callsTo,
  detail,
  json,
  message,
  renderWithClient,
  result,
  serve,
  sseStream,
  step,
  stubClipboard,
} from '../test/fixtures'

afterEach(() => vi.unstubAllGlobals())

describe('Conversation', () => {
  it('invites a question, and a document only when attachments are on', async () => {
    serve({ 'GET /ghostwriter/api/chats/c1': detail([]) })
    const first = renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    expect(await screen.findByText('Ask anything, or attach a document.')).toBeInTheDocument()
    first.unmount()

    const second = renderWithClient(<StreamedConversation chatId="c1" config={{ ...CONFIG, attachments: false }} reasoning={false} />)
    expect(await screen.findByText('Ask anything.')).toBeInTheDocument()
    expect(screen.queryByText(/attach a document/)).not.toBeInTheDocument()
    second.unmount()

    const german = renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />
      </LanguageContext>,
    )
    expect(await screen.findByText('Fragen Sie, was Sie möchten, oder hängen Sie ein Dokument an.')).toBeInTheDocument()
    german.unmount()

    renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={{ ...CONFIG, attachments: false }} reasoning={false} />
      </LanguageContext>,
    )
    expect(await screen.findByText('Fragen Sie, was Sie möchten.')).toBeInTheDocument()
  })

  it("shows the user's text with its attachments, and each answer with its trail, text and cards", async () => {
    serve({
      'GET /ghostwriter/api/chats/c1': detail([
        message({
          id: 'm1',
          role: 'user',
          content: 'Summe der Spalte B?',
          attachments: [
            {
              id: 'a1',
              file_id: 'F0',
              name: 'sales.xlsx',
              size: 4096,
              mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
              created_at: '2026-11-02T09:00:00Z',
              expired: false,
              kind: 'document',
              status: 'ready',
            },
          ],
        }),
        message({
          id: 'm2',
          role: 'assistant',
          content: 'Die **Summe** ist 42.',
          steps: [step({ id: 's1', tool: 'read_document', detail: 'sales.xlsx' })],
          results: [result({ id: 'r1', kind: 'file', file_id: 'F1', name: 'summe.docx', size: 1536 })],
        }),
        message({ id: 'm3', role: 'user', content: 'Und Spalte C?' }),
        message({ id: 'm4', role: 'assistant', content: 'Spalte C ist leer.' }),
      ]),
    })
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)

    const question = (await screen.findByText('Summe der Spalte B?')).closest('[data-role="user"]') as HTMLElement
    expect(within(question).getByText('sales.xlsx')).toBeInTheDocument()

    const answer = screen.getByText('Summe', { selector: 'strong' }).closest('[data-role="assistant"]') as HTMLElement
    expect(within(answer).getByRole('button', { name: '1 step' })).toHaveAttribute('aria-expanded', 'false')
    expect(within(answer).getByText('summe.docx')).toBeInTheDocument()
    expect(within(answer).getByRole('button', { name: 'Download' })).toBeInTheDocument()
    // Only the last answer can be regenerated.
    expect(within(answer).queryByRole('button', { name: 'Regenerate' })).not.toBeInTheDocument()
    const last = screen.getByText('Spalte C ist leer.').closest('[data-role="assistant"]') as HTMLElement
    expect(within(last).getByRole('button', { name: 'Regenerate' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Regenerate' })).toHaveLength(1)
  })

  it('says when the model answered without the pictures', async () => {
    const sse = sseStream()
    serve({
      'GET /ghostwriter/api/chats/c1': detail([]),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages': () => sse.response(),
    })
    renderWithClient(
      <StreamedConversation
        chatId="c1"
        config={VISION_CONFIG}
        reasoning={false}
        composer={(stream) => (
          <button type="button" onClick={() => void stream.send('Was zeigt das Bild?', [], false)}>
            Ask
          </button>
        )}
      />,
    )
    await screen.findByText('Ask anything, or attach a document.')
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    sse.push('notice', { code: 'images_dropped' })
    sse.push('token', { text: 'Ich sehe nichts.' })

    const line = await screen.findByText('The model did not take the pictures; it answered without them.')
    // Not an error: a quiet status line, muted, below the answer being written.
    expect(line).toHaveAttribute('role', 'status')
    expect(line).toHaveClass('text-sm', 'text-muted-foreground')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByText('Ich sehe nichts.').compareDocumentPosition(line)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  it('names a refusal while files are prepared', async () => {
    const refused = () =>
      new Response(
        JSON.stringify({ detail: 'An attached file is still being prepared.', code: 'attachment_processing' }),
        { status: 409 },
      )
    serve({ 'GET /ghostwriter/api/chats/c1': detail([]), 'POST /ghostwriter/api/chats/c1/messages': refused })
    const send = (stream: ChatStream) => (
      <button type="button" onClick={() => void stream.send('Was zeigt das Bild?', [], false)}>
        Ask
      </button>
    )
    const first = renderWithClient(<StreamedConversation chatId="c1" config={VISION_CONFIG} reasoning={false} composer={send} />)
    await screen.findByText('Ask anything, or attach a document.')
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Wait until the attached files are ready.')
    // The refusal stored nothing: no bubble for the message.
    expect(screen.queryByText('Was zeigt das Bild?')).not.toBeInTheDocument()
    first.unmount()

    renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={VISION_CONFIG} reasoning={false} composer={send} />
      </LanguageContext>,
    )
    await screen.findByText('Fragen Sie, was Sie möchten, oder hängen Sie ein Dokument an.')
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Warten Sie, bis die angehängten Dateien bereit sind.')
  })

  it('copies a finished answer as its Markdown; a question has no copy button', async () => {
    const markdown = 'Die **Summe** ist 42.\n\n| Monat | Betrag |\n|---|---|\n| Jan | 42 |'
    serve({
      'GET /ghostwriter/api/chats/c1': detail([
        message({ id: 'm1', role: 'user', content: 'Summe der Spalte B?' }),
        message({ id: 'm2', role: 'assistant', content: markdown }),
      ]),
    })
    const writeText = stubClipboard()
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    const answer = (await screen.findByText('Summe', { selector: 'strong' })).closest('[data-role="assistant"]') as HTMLElement
    const question = screen.getByText('Summe der Spalte B?').closest('[data-role="user"]') as HTMLElement
    expect(within(question).queryByRole('button', { name: 'Copy answer' })).not.toBeInTheDocument()
    fireEvent.click(within(answer).getByRole('button', { name: 'Copy answer' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(markdown))
    expect(await within(answer).findByRole('button', { name: 'Copied' })).toBeInTheDocument()
  })
})

describe('Conversation pictures', () => {
  const CHAT_URL = 'GET /ghostwriter/api/chats/c1'
  const preview = (id: string) => `/ghostwriter/api/chats/c1/attachments/${id}/preview?page=1`
  const original = (id: string) => `/ghostwriter/api/chats/c1/files/F-${id}`
  const jpeg = () => new Response(new Blob(['jpeg'], { type: 'image/jpeg' }), { status: 200 })
  // Every object URL made, and every one revoked, in order.
  let made: string[]
  let revoked: string[]

  beforeEach(() => {
    made = []
    revoked = []
    vi.spyOn(URL, 'createObjectURL').mockImplementation(() => {
      const url = `blob:picture-${made.length + 1}`
      made.push(url)
      return url
    })
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation((url) => {
      revoked.push(url)
    })
  })

  afterEach(() => {
    clearAllPurposes()
    sessionStorage.clear()
    vi.restoreAllMocks()
  })

  function picture(id: string, name: string, fields: Partial<AttachmentOut> = {}): AttachmentOut {
    return {
      id,
      file_id: `F-${id}`,
      name,
      size: 2048,
      mime: 'image/jpeg',
      created_at: '2026-11-02T09:00:00Z',
      expired: false,
      kind: 'image',
      status: 'ready',
      ...fields,
    }
  }

  const SCAN: AttachmentOut = {
    ...picture('a3', 'quay-scan.pdf'),
    mime: 'application/pdf',
    kind: 'document',
  }

  const question = (attachments: AttachmentOut[]) =>
    message({ id: 'm1', role: 'user', content: 'Was zeigt das Bild?', attachments })

  async function bubble(): Promise<HTMLElement> {
    return (await screen.findByText('Was zeigt das Bild?')).closest('[data-role="user"]') as HTMLElement
  }

  it("shows a message's pictures as a grid, fetched with the purpose, and opens one", async () => {
    setPurpose('ghostwriter', { reason: 'review' })
    const calls = serve({
      [CHAT_URL]: detail([question([picture('a1', 'harbour-photo.jpg'), picture('a2', 'quay-dock.png'), SCAN])]),
      [`GET ${preview('a1')}`]: jpeg,
      [`GET ${preview('a2')}`]: jpeg,
    })
    const user = userEvent.setup()
    const { unmount } = renderWithClient(<StreamedConversation chatId="c1" config={VISION_CONFIG} reasoning={false} />)

    const inBubble = within(await bubble())
    const face = await inBubble.findByRole('button', { name: 'harbour-photo.jpg' })
    const other = await inBubble.findByRole('button', { name: 'quay-dock.png' })
    await waitFor(() => expect(face.querySelector('img')).not.toBeNull())
    await waitFor(() => expect(other.querySelector('img')).not.toBeNull())
    // Each picture its own object URL, made from the rendition fetched with the purpose.
    const src = face.querySelector('img')?.getAttribute('src') ?? ''
    expect(made).toContain(src)
    expect(other.querySelector('img')?.getAttribute('src')).not.toBe(src)
    for (const id of ['a1', 'a2']) {
      const fetched = callsTo(calls, 'GET', preview(id))
      expect(fetched).toHaveLength(1)
      expect(fetched[0].headers[PURPOSE_HEADER]).toBe('reason=review')
    }
    // A grid of the pictures; a document keeps its chip beside it.
    const grid = face.closest('ul') as HTMLElement
    expect(grid).toHaveClass('flex', 'flex-wrap', 'gap-2')
    expect(other.closest('ul')).toBe(grid)
    expect(within(grid).queryByText('quay-scan.pdf')).not.toBeInTheDocument()
    expect(inBubble.getByText('quay-scan.pdf')).toBeInTheDocument()

    await user.click(face)
    const dialog = screen.getByRole('dialog', { name: 'harbour-photo.jpg' })
    expect(within(dialog).getByRole('img', { name: 'harbour-photo.jpg' })).toHaveAttribute('src', src)
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus()
    expect(within(dialog).getByRole('button', { name: 'Download' })).toBeInTheDocument()
    // Opening it fetches nothing more.
    expect(callsTo(calls, 'GET', preview('a1'))).toHaveLength(1)

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(face).toHaveFocus()
    expect(revoked).toEqual([])

    unmount()
    expect(revoked.slice().sort()).toEqual(made.slice().sort())
    expect(made).toHaveLength(2)
  })

  it('saves the original from the lightbox, not the rendition', async () => {
    setPurpose('ghostwriter', { reason: 'review' })
    const saved: Array<{ href: string; download: string }> = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ href: this.href, download: this.download })
    })
    const calls = serve({
      [CHAT_URL]: detail([question([picture('a1', 'harbour-photo.jpg')])]),
      [`GET ${preview('a1')}`]: jpeg,
      [`GET ${original('a1')}`]: () => new Response(new Blob(['original'], { type: 'image/jpeg' }), { status: 200 }),
    })
    const user = userEvent.setup()
    renderWithClient(<StreamedConversation chatId="c1" config={VISION_CONFIG} reasoning={false} />)

    const face = await within(await bubble()).findByRole('button', { name: 'harbour-photo.jpg' })
    await waitFor(() => expect(face.querySelector('img')).not.toBeNull())
    await user.click(face)
    expect(callsTo(calls, 'GET', original('a1'))).toEqual([])
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Download' }))

    await waitFor(() => expect(saved).toHaveLength(1))
    expect(saved[0].download).toBe('harbour-photo.jpg')
    const [fetched] = callsTo(calls, 'GET', original('a1'))
    expect(fetched.headers[PURPOSE_HEADER]).toBe('reason=review')
    // The download's own URL is revoked once the click is through.
    await waitFor(() => expect(revoked).toContain(saved[0].href))
  })

  it('fetches the original once while its download is under way', async () => {
    const saved: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      saved.push(this.download)
    })
    let release!: () => void
    const held = new Promise<void>((done) => (release = done))
    const calls = serve({
      [CHAT_URL]: detail([question([picture('a1', 'harbour-photo.jpg')])]),
      [`GET ${preview('a1')}`]: jpeg,
      [`GET ${original('a1')}`]: async () => {
        await held
        return new Response(new Blob(['original'], { type: 'image/jpeg' }), { status: 200 })
      },
    })
    const user = userEvent.setup()
    renderWithClient(<StreamedConversation chatId="c1" config={VISION_CONFIG} reasoning={false} />)

    const face = await within(await bubble()).findByRole('button', { name: 'harbour-photo.jpg' })
    await waitFor(() => expect(face.querySelector('img')).not.toBeNull())
    await user.click(face)
    const download = within(screen.getByRole('dialog')).getByRole('button', { name: 'Download' })
    await user.click(download)
    await user.click(download)
    await user.click(download)
    expect(callsTo(calls, 'GET', original('a1'))).toHaveLength(1)

    release()
    await waitFor(() => expect(saved).toEqual(['harbour-photo.jpg']))
    // Once it is through, the next click downloads again.
    await user.click(download)
    await waitFor(() => expect(saved).toEqual(['harbour-photo.jpg', 'harbour-photo.jpg']))
    expect(callsTo(calls, 'GET', original('a1'))).toHaveLength(2)
  })

  it('turns a picture expired when its original is gone, and says so when the download fails', async () => {
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    serve({
      [CHAT_URL]: detail([question([picture('a1', 'harbour-photo.jpg')])]),
      [`GET ${preview('a1')}`]: jpeg,
      [`GET ${original('a1')}`]: () => json({ detail: 'gone' }, 410),
    })
    const user = userEvent.setup()
    renderWithClient(<StreamedConversation chatId="c1" config={VISION_CONFIG} reasoning={false} />)

    const inBubble = within(await bubble())
    const face = await inBubble.findByRole('button', { name: 'harbour-photo.jpg' })
    await waitFor(() => expect(face.querySelector('img')).not.toBeNull())
    await user.click(face)
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Download' }))

    // Gone: the dialog closes and the tile says what it is, no longer something to open.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await inBubble.findByText('Expired')).toBeInTheDocument()
    expect(inBubble.queryByRole('button', { name: 'harbour-photo.jpg' })).not.toBeInTheDocument()
    expect(inBubble.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('closes the lightbox and names the error when the original cannot be fetched', async () => {
    serve({
      [CHAT_URL]: detail([question([picture('a1', 'harbour-photo.jpg')])]),
      [`GET ${preview('a1')}`]: jpeg,
      [`GET ${original('a1')}`]: () => json({ detail: 'boom' }, 502),
    })
    const user = userEvent.setup()
    renderWithClient(<StreamedConversation chatId="c1" config={VISION_CONFIG} reasoning={false} />)

    const inBubble = within(await bubble())
    const face = await inBubble.findByRole('button', { name: 'harbour-photo.jpg' })
    await waitFor(() => expect(face.querySelector('img')).not.toBeNull())
    await user.click(face)
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Download' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // In the UI's words, never the server's; the picture can still be opened and tried again.
    expect(await inBubble.findByRole('alert')).toHaveTextContent('Something went wrong (502).')
    expect(inBubble.getByRole('button', { name: 'harbour-photo.jpg' })).toBeInTheDocument()
  })

  it('marks an expired picture without fetching it', async () => {
    const calls = serve({
      [CHAT_URL]: detail([question([picture('a1', 'harbour-photo.jpg', { expired: true })])]),
      [`GET ${preview('a1')}`]: jpeg,
    })
    renderWithClient(<StreamedConversation chatId="c1" config={VISION_CONFIG} reasoning={false} />)

    const inBubble = within(await bubble())
    const marker = await inBubble.findByText('Expired')
    // A tile of the grid, not a chip: the picture's square keeps its place.
    expect(marker.closest('ul')).toHaveClass('flex', 'flex-wrap', 'gap-2')
    // Nothing to open, and nothing asked of the server.
    expect(inBubble.queryByRole('button')).not.toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(calls.filter((call) => call.url.includes('/preview'))).toEqual([])
    expect(made).toEqual([])
  })

  it('marks a picture that could not be read, and offers nothing to open', async () => {
    const calls = serve({
      [CHAT_URL]: detail([question([picture('a1', 'harbour-photo.jpg', { status: 'failed' })])]),
      [`GET ${preview('a1')}`]: jpeg,
    })
    renderWithClient(<StreamedConversation chatId="c1" config={VISION_CONFIG} reasoning={false} />)

    const inBubble = within(await bubble())
    expect(await inBubble.findByRole('img', { name: 'harbour-photo.jpg could not be read; the model will be told.' })).toBeInTheDocument()
    expect(inBubble.queryByRole('button')).not.toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(calls.filter((call) => call.url.includes('/preview'))).toEqual([])
  })

  it('shows the picture of a message still being sent', async () => {
    setPurpose('ghostwriter', { reason: 'review' })
    const sse = sseStream()
    const calls = serve({
      [CHAT_URL]: detail([]),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages': () => sse.response(),
      [`GET ${preview('a1')}`]: jpeg,
    })
    const sent = picture('a1', 'harbour-photo.jpg')
    renderWithClient(
      <StreamedConversation
        chatId="c1"
        config={VISION_CONFIG}
        reasoning={false}
        composer={(stream) => (
          <button type="button" onClick={() => void stream.send('Was zeigt das Bild?', [sent], false)}>
            Ask
          </button>
        )}
      />,
    )
    await screen.findByText('Ask anything, or attach a document.')
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))

    // Its own bubble, before the chat is read again: the picture comes from the server's
    // rendition, not from anything the composer held.
    const inBubble = within(await bubble())
    const face = await inBubble.findByRole('button', { name: 'harbour-photo.jpg' })
    await waitFor(() => expect(face.querySelector('img')).not.toBeNull())
    expect(made).toContain(face.querySelector('img')?.getAttribute('src'))
    const [fetched] = callsTo(calls, 'GET', preview('a1'))
    expect(fetched.headers[PURPOSE_HEADER]).toBe('reason=review')
    expect(face.closest('ul')).toHaveClass('flex', 'flex-wrap', 'gap-2')
  })
})
