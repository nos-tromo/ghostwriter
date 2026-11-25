import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PURPOSE_HEADER, clearAllPurposes, setPurpose } from '@infra/ui'
import type { AttachmentOut, NoticeOut } from '../api/types'
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

/** The codes of the notice lines under an answer, in reading order. */
function noticeCodes(root: ParentNode = document): string[] {
  return [...root.querySelectorAll<HTMLElement>('[data-notice]')].map((line) => line.dataset.notice ?? '')
}

/** The warnings React gave about two children with one key, from a spy on `console.error`. */
function keyWarnings(spy: { mock: { calls: unknown[][] } }): string[] {
  return spy.mock.calls.map((call) => String(call[0])).filter((text) => text.includes('same key'))
}

const QUESTION = message({ id: 'q1', role: 'user', content: 'Fasse den Bericht zusammen.' })
const PARTIAL = { code: 'document_partial' as const, file_id: 'F1', name: 'long-report.pdf', read: 9, total: 500, unit: 'page' as const }

/** A chat of one question and one answer holding `notices`. */
function serveAnswered(notices: NoticeOut[] | null, content = 'Zusammenfassung') {
  return serve({
    'GET /ghostwriter/api/chats/c1': detail([QUESTION, message({ id: 'a1', role: 'assistant', content, notices })]),
  })
}

describe('Conversation notices', () => {
  it("shows a stored answer's notices after a reload", async () => {
    serveAnswered([
      { code: 'answer_cut' },
      PARTIAL,
      { code: 'document_unread', file_id: 'F2', name: 'quay-ledger.xlsx' },
      { code: 'images_dropped' },
      { code: 'context_trimmed' },
    ])
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    const answer = (await screen.findByText('Zusammenfassung')).closest('[data-role="assistant"]') as HTMLElement

    expect(within(answer).getByText('The answer was cut at the length limit.')).toBeInTheDocument()
    expect(within(answer).getByText("Of 'long-report.pdf', 9 of 500 pages were read.")).toBeInTheDocument()
    expect(within(answer).getByText("'quay-ledger.xlsx' was not read for this answer.")).toBeInTheDocument()
    expect(within(answer).getByText('The model did not take the pictures; it answered without them.')).toBeInTheDocument()
    // The trimmed context is not an answer-level line.
    expect(noticeCodes(answer)).toEqual(['answer_cut', 'document_partial', 'document_unread', 'images_dropped'])
    // Quiet and stored: muted text, a warning icon, nothing announced.
    const line = within(answer).getByText("Of 'long-report.pdf', 9 of 500 pages were read.")
    expect(line).toHaveClass('text-sm', 'text-muted-foreground')
    expect(line).not.toHaveAttribute('role')
    expect(line.parentElement?.querySelector('svg')).toHaveClass('text-warning')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // The lines follow the answer's text.
    expect(screen.getByText('Zusammenfassung').compareDocumentPosition(line)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  it('names the notices in German, with the unit of the document', async () => {
    serveAnswered([
      { code: 'answer_cut' },
      PARTIAL,
      { code: 'document_partial', file_id: 'F3', name: 'harbour-survey.pptx', read: 1, total: 4, unit: 'slide' },
      { code: 'document_unread', file_id: 'F2', name: 'quay-ledger.xlsx' },
    ])
    renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />
      </LanguageContext>,
    )
    expect(await screen.findByText('Die Antwort wurde an der Längengrenze abgeschnitten.')).toBeInTheDocument()
    expect(screen.getByText('Von ‚long-report.pdf‘ wurden 9 von 500 Seiten gelesen.')).toBeInTheDocument()
    expect(screen.getByText('Von ‚harbour-survey.pptx‘ wurden 1 von 4 Folien gelesen.')).toBeInTheDocument()
    expect(screen.getByText('‚quay-ledger.xlsx‘ wurde für diese Antwort nicht gelesen.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Weiter' })).toBeInTheDocument()
  })

  it('puts the German unit in the dative after "von"', async () => {
    serveAnswered([
      { ...PARTIAL, file_id: 'F1', name: 'harbour-survey.docx', read: 1, total: 4, unit: 'paragraph' },
      { ...PARTIAL, file_id: 'F2', name: 'tide-notes.txt', read: 0, total: 1, unit: 'paragraph' },
    ])
    renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />
      </LanguageContext>,
    )
    expect(await screen.findByText('Von ‚harbour-survey.docx‘ wurden 1 von 4 Absätzen gelesen.')).toBeInTheDocument()
    expect(screen.getByText('Von ‚tide-notes.txt‘ wurden 0 von 1 Absatz gelesen.')).toBeInTheDocument()
  })

  it('says one page', async () => {
    serveAnswered([{ ...PARTIAL, read: 0, total: 1 }])
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    expect(await screen.findByText("Of 'long-report.pdf', 0 of 1 page were read.")).toBeInTheDocument()
  })

  it('counts each unit by its own word', async () => {
    const units = ['page', 'slide', 'paragraph', 'row', 'line'] as const
    serveAnswered(units.map((unit) => ({ ...PARTIAL, file_id: `F-${unit}`, name: `${unit}.txt`, read: 2, total: 3, unit })))
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    for (const unit of units) {
      expect(await screen.findByText(`Of '${unit}.txt', 2 of 3 ${unit}s were read.`)).toBeInTheDocument()
    }
  })

  it('falls back to the generic text once the details expired', async () => {
    serveAnswered([
      { code: 'document_partial' },
      // Half of the details is as good as none.
      { code: 'document_partial', file_id: 'F1', name: 'long-report.pdf', read: null, total: null, unit: null },
      { code: 'document_partial', file_id: 'F3', name: 'tide-notes.txt', read: 3, total: 9, unit: null },
      { code: 'document_unread' },
      { code: 'document_unread', file_id: 'F2', name: null },
    ])
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    await screen.findByText('Zusammenfassung')
    expect(screen.getAllByText('A document was read only in part.')).toHaveLength(3)
    expect(screen.getAllByText('An attachment was not read for this answer.')).toHaveLength(2)
    expect(screen.queryByText(/long-report|tide-notes/)).not.toBeInTheDocument()
  })

  it('says each of two notices of one code once their details expired', async () => {
    // The server serves an expired notice as its code alone, so two files arrive as two equal notices.
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      serveAnswered([{ code: 'document_unread' }, { code: 'document_unread' }, { code: 'document_partial' }, { code: 'document_partial' }])
      renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
      await screen.findByText('Zusammenfassung')
      expect(screen.getAllByText('An attachment was not read for this answer.')).toHaveLength(2)
      expect(screen.getAllByText('A document was read only in part.')).toHaveLength(2)
      expect(noticeCodes()).toEqual(['document_unread', 'document_unread', 'document_partial', 'document_partial'])
      expect(keyWarnings(error)).toEqual([])
    } finally {
      error.mockRestore()
    }
  })

  it('ignores a notice of a code it does not know', async () => {
    serveAnswered([{ code: 'something_new' } as unknown as NoticeOut, { code: 'images_dropped' }])
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    await screen.findByText('Zusammenfassung')
    expect(noticeCodes()).toEqual(['images_dropped'])
  })

  it('renders an answer stored before notices', async () => {
    serveAnswered(null)
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    const answer = (await screen.findByText('Zusammenfassung')).closest('[data-role="assistant"]') as HTMLElement
    expect(noticeCodes()).toEqual([])
    expect(within(answer).queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
    // Still copyable and regenerable as before.
    expect(within(answer).getByRole('button', { name: 'Copy answer' })).toBeInTheDocument()
    expect(within(answer).getByRole('button', { name: 'Regenerate' })).toBeInTheDocument()
  })

  it('offers Continue on the latest answer that was cut, and on no other', async () => {
    serve({
      'GET /ghostwriter/api/chats/c1': detail([
        QUESTION,
        message({ id: 'a1', role: 'assistant', content: 'Erster Anlauf', notices: [{ code: 'answer_cut' }] }),
        message({ id: 'q2', role: 'user', content: 'Und weiter?' }),
        message({ id: 'a2', role: 'assistant', content: 'Zweiter Anlauf', notices: [{ code: 'answer_cut' }] }),
      ]),
    })
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    const second = (await screen.findByText('Zweiter Anlauf')).closest('[data-role="assistant"]') as HTMLElement
    const first = screen.getByText('Erster Anlauf').closest('[data-role="assistant"]') as HTMLElement
    // Both say they were cut; only the latest can be continued.
    expect(noticeCodes(first)).toEqual(['answer_cut'])
    expect(noticeCodes(second)).toEqual(['answer_cut'])
    expect(screen.getAllByRole('button', { name: 'Continue' })).toHaveLength(1)
    expect(within(second).getByRole('button', { name: 'Continue' })).toBeInTheDocument()
  })

  it('offers no Continue on a latest answer that was not cut', async () => {
    serveAnswered([PARTIAL, { code: 'images_dropped' }])
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    await screen.findByText('Zusammenfassung')
    expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
  })

  it('continues a cut answer in place', async () => {
    const sse = sseStream()
    let done = false
    const CUT = message({
      id: 'a2',
      role: 'assistant',
      content: 'Kapitel 1 …',
      notices: [{ code: 'answer_cut' }, PARTIAL],
    })
    const WHOLE = message({
      id: 'a2',
      role: 'assistant',
      content: 'Kapitel 1 … und weiter.',
      notices: [PARTIAL],
    })
    const before = detail([
      QUESTION,
      message({ id: 'a1', role: 'assistant', content: 'Erster Anlauf', notices: [{ code: 'answer_cut' }] }),
      message({ id: 'q2', role: 'user', content: 'Und weiter?' }),
      CUT,
    ])
    const calls = serve({
      'GET /ghostwriter/api/chats/c1': () => json(done ? detail([...before.messages.slice(0, 3), WHOLE]) : before),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages/a2/continue': () => sse.response(),
    })
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    await screen.findByText('Kapitel 1 …')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

    // Nothing but the route and an empty body: the answer is named by the path.
    await waitFor(() => expect(callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/messages/a2/continue')).toHaveLength(1))
    expect(callsTo(calls, 'POST', '/ghostwriter/api/chats/c1/messages/a2/continue')[0].body).toEqual({})
    // At once, one answer holds the text so far: the stored one gives way to the draft, and it is not cut yet.
    const draft = (await screen.findByText('Kapitel 1 …')).closest('[data-role="assistant"]') as HTMLElement
    expect(draft).toHaveAttribute('aria-busy', 'true')
    expect(screen.getAllByText('Kapitel 1 …')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
    // Only the older answer says it was cut; this one is not, until its round ends so.
    expect(screen.getAllByText('The answer was cut at the length limit.')).toHaveLength(1)
    // The notices it already had stay, once, outside the busy element.
    expect(screen.getAllByText("Of 'long-report.pdf', 9 of 500 pages were read.")).toHaveLength(1)
    expect(screen.getByText("Of 'long-report.pdf', 9 of 500 pages were read.").closest('[aria-busy="true"]')).toBeNull()

    sse.push('start', { message_id: 'a2', user_message_id: null })
    sse.push('token', { text: ' und weiter.' })
    // The text goes on in the same answer, never as a second one.
    expect(await screen.findByText('Kapitel 1 … und weiter.')).toBeInTheDocument()
    expect(screen.queryByText('Kapitel 1 …')).not.toBeInTheDocument()
    expect(screen.getAllByText(/Kapitel 1/)).toHaveLength(1)
    expect(document.querySelectorAll('[data-role="assistant"]')).toHaveLength(2)
    expect(screen.getAllByText("Of 'long-report.pdf', 9 of 500 pages were read.")).toHaveLength(1)

    done = true
    sse.push('final', { message_id: 'a2', user_message_id: null, chat: CHAT, stopped: false, context_estimate: null })
    sse.end()
    await waitFor(() => expect(document.querySelector('[aria-busy="true"]')).toBeNull())
    expect(await screen.findByText('Kapitel 1 … und weiter.')).toBeInTheDocument()
    expect(document.querySelectorAll('[data-role="assistant"]')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
    expect(screen.queryByText('The answer was cut at the length limit.', { selector: 'p' })).toBeInTheDocument() // a1's, the older answer
    expect(noticeCodes()).toEqual(['answer_cut', 'document_partial'])
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Regenerate' })).toBeInTheDocument()
  })

  it('carries the expired notices of a cut answer into its continuation', async () => {
    const sse = sseStream()
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      // Two files unread, their details long gone: two notices that are equal.
      const CUT = message({
        id: 'a1',
        role: 'assistant',
        content: 'Kapitel 1',
        notices: [{ code: 'answer_cut' }, { code: 'document_unread' }, { code: 'document_unread' }],
      })
      serve({
        'GET /ghostwriter/api/chats/c1': detail([QUESTION, CUT]),
        'GET /ghostwriter/api/chats': [CHAT],
        'POST /ghostwriter/api/chats/c1/messages/a1/continue': () => sse.response(),
      })
      renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
      await screen.findByText('Kapitel 1')
      expect(screen.getAllByText('An attachment was not read for this answer.')).toHaveLength(2)
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

      // The draft has both, as the stored answer had, and no longer says it was cut.
      await waitFor(() => expect(document.querySelector('[aria-busy="true"]')).not.toBeNull())
      expect(screen.getAllByText('An attachment was not read for this answer.')).toHaveLength(2)
      expect(screen.queryByText('The answer was cut at the length limit.')).not.toBeInTheDocument()
      // A frame names its file, so it is a third notice; sent twice, it is said once.
      sse.push('notice', { code: 'document_unread', file_id: 'F9', name: 'tide-notes.txt' })
      sse.push('notice', { code: 'document_unread', file_id: 'F9', name: 'tide-notes.txt' })
      sse.push('token', { text: ' und 2' })
      expect(await screen.findByText("'tide-notes.txt' was not read for this answer.")).toBeInTheDocument()
      expect(screen.getAllByText('An attachment was not read for this answer.')).toHaveLength(2)
      expect(screen.getAllByText("'tide-notes.txt' was not read for this answer.")).toHaveLength(1)
      expect(keyWarnings(error)).toEqual([])
    } finally {
      error.mockRestore()
    }
  })

  it('offers Continue again when the continuation was cut too', async () => {
    const sse = sseStream()
    let done = false
    const CUT = message({ id: 'a1', role: 'assistant', content: 'Kapitel 1', notices: [{ code: 'answer_cut' }] })
    const CUT_AGAIN = message({ id: 'a1', role: 'assistant', content: 'Kapitel 1 und 2', notices: [{ code: 'answer_cut' }] })
    serve({
      'GET /ghostwriter/api/chats/c1': () => json(detail([QUESTION, done ? CUT_AGAIN : CUT])),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages/a1/continue': () => sse.response(),
    })
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))
    sse.push('start', { message_id: 'a1', user_message_id: null })
    sse.push('token', { text: ' und 2' })
    // Told again as the round ends: live, it is announced and offers no button.
    sse.push('notice', { code: 'answer_cut' })
    const live = await screen.findByText('The answer was cut at the length limit.')
    expect(live).toHaveAttribute('role', 'status')
    expect(live.closest('[aria-busy="true"]')).toBeNull()
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
    done = true
    sse.push('final', { message_id: 'a1', user_message_id: null, chat: CHAT, stopped: false, context_estimate: null })
    sse.end()
    expect(await screen.findByRole('button', { name: 'Continue' })).toBeInTheDocument()
    expect(screen.getByText('Kapitel 1 und 2')).toBeInTheDocument()
    expect(screen.getByText('The answer was cut at the length limit.')).not.toHaveAttribute('role')
  })

  it('names a context refusal', async () => {
    const sse = sseStream()
    serve({
      'GET /ghostwriter/api/chats/c1': detail([]),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages': () => sse.response(),
    })
    const ask = (stream: ChatStream) => (
      <button type="button" onClick={() => void stream.send('Fasse den Bericht zusammen.', [], false)}>
        Ask
      </button>
    )
    const first = renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={ask} />)
    await screen.findByText('Ask anything, or attach a document.')
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    sse.push('error', { code: 'context', message: 'synthetic router: maximum context exceeded' })
    sse.push('final', { message_id: 'a1', user_message_id: 'q1', chat: CHAT, stopped: false, context_estimate: null })
    sse.end()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('This chat is too long for the model. Start a new chat or shorten your message.')
    // The router's own words are never shown.
    expect(alert).not.toHaveTextContent('synthetic router')
    first.unmount()

    const again = sseStream()
    serve({
      'GET /ghostwriter/api/chats/c1': detail([]),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages': () => again.response(),
    })
    renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={false} composer={ask} />
      </LanguageContext>,
    )
    await screen.findByText('Fragen Sie, was Sie möchten, oder hängen Sie ein Dokument an.')
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    again.push('error', { code: 'context', message: 'synthetic router: maximum context exceeded' })
    again.push('final', { message_id: 'a1', user_message_id: 'q1', chat: CHAT, stopped: false, context_estimate: null })
    again.end()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Dieser Chat ist zu lang für das Modell. Beginnen Sie einen neuen Chat oder kürzen Sie Ihre Nachricht.',
    )
  })

  it('names a refused continue', async () => {
    const CUT = message({ id: 'a1', role: 'assistant', content: 'Kapitel 1', notices: [{ code: 'answer_cut' }] })
    const refused = () =>
      new Response(
        JSON.stringify({ detail: 'Only the latest answer cut at the length limit can be continued.', code: 'not_continuable' }),
        { status: 409 },
      )
    serve({
      'GET /ghostwriter/api/chats/c1': detail([QUESTION, CUT]),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages/a1/continue': refused,
    })
    const first = renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Only the latest answer cut at the length limit can be continued.',
    )
    // Nothing was changed: the answer is back in its place, once.
    expect(await screen.findByText('Kapitel 1')).toBeInTheDocument()
    expect(screen.getAllByText('Kapitel 1')).toHaveLength(1)
    first.unmount()

    serve({
      'GET /ghostwriter/api/chats/c1': detail([QUESTION, CUT]),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages/a1/continue': refused,
    })
    renderWithClient(
      <LanguageContext value="de">
        <StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />
      </LanguageContext>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Weiter' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Nur die letzte Antwort, die an der Längengrenze abgeschnitten wurde, kann fortgesetzt werden.',
    )
  })

  it('says a busy chat refused a continue, as it does a regenerate', async () => {
    const CUT = message({ id: 'a1', role: 'assistant', content: 'Kapitel 1', notices: [{ code: 'answer_cut' }] })
    serve({
      'GET /ghostwriter/api/chats/c1': detail([QUESTION, CUT]),
      'GET /ghostwriter/api/chats': [CHAT],
      'POST /ghostwriter/api/chats/c1/messages/a1/continue': () =>
        new Response(JSON.stringify({ detail: 'An answer is still being written in this chat.', code: 'busy' }), {
          status: 409,
        }),
    })
    renderWithClient(<StreamedConversation chatId="c1" config={CONFIG} reasoning={false} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('An answer is still being written in this chat.')
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
