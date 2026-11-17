import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { LanguageContext } from '../i18n/LanguageContext'
import {
  CONFIG,
  StreamedConversation,
  detail,
  message,
  renderWithClient,
  result,
  serve,
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
