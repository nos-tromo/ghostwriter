import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import type { ChatStream } from '../hooks/useChatStream'
import { LanguageContext } from '../i18n/LanguageContext'
import {
  CHAT,
  CONFIG,
  StreamedConversation,
  VISION_CONFIG,
  detail,
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
