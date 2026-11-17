import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { Conversation } from './Conversation'
import { LanguageContext } from '../i18n/LanguageContext'
import { CONFIG, detail, message, renderWithClient, result, serve, step } from '../test/fixtures'

afterEach(() => vi.unstubAllGlobals())

describe('Conversation', () => {
  it('invites a question, and a document only when attachments are on', async () => {
    serve({ 'GET /ghostwriter/api/chats/c1': detail([]) })
    const first = renderWithClient(<Conversation chatId="c1" config={CONFIG} reasoning={false} />)
    expect(await screen.findByText('Ask anything, or attach a document.')).toBeInTheDocument()
    first.unmount()

    const second = renderWithClient(<Conversation chatId="c1" config={{ ...CONFIG, attachments: false }} reasoning={false} />)
    expect(await screen.findByText('Ask anything.')).toBeInTheDocument()
    expect(screen.queryByText(/attach a document/)).not.toBeInTheDocument()
    second.unmount()

    renderWithClient(
      <LanguageContext value="de">
        <Conversation chatId="c1" config={{ ...CONFIG, attachments: false }} reasoning={false} />
      </LanguageContext>,
    )
    expect(await screen.findByText('Frag, was du willst.')).toBeInTheDocument()
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
    renderWithClient(<Conversation chatId="c1" config={CONFIG} reasoning={false} />)

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
})
