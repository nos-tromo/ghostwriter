import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { clearAllPurposes } from '@infra/ui'
import App from './App'
import indexHtml from '../index.html?raw'
import { queryClient } from './api/queryClient'
import type { AppConfig } from './api/types'
import { CHAT, callsTo, detail, json, message, serve as serveRoutes } from './test/fixtures'

const CONFIG: AppConfig = {
  ui_language: 'en',
  require_purpose: [],
  speech_input: true,
  attachments: true,
  attachment_types: ['.csv', '.pdf', '.xlsx'],
  image_types: [],
  media_types: [],
  media: false,
  max_images: 8,
  vision: false,
  max_attachment_bytes: 50 * 1024 * 1024,
  max_recording_s: 120,
  max_recording_bytes: 10 * 1024 * 1024,
  chat_retention_days: 90,
  file_retention_days: 7,
  prompt_tokens: 34_000,
  reasoning_prompt_tokens: 26_000,
  version: '0.1.0',
}

/** Answer the SPA's three bootstrap reads; this deployment's config is `config`. */
function serve(config: AppConfig) {
  const bodies: Record<string, unknown> = {
    '/ghostwriter/api/config': config,
    '/ghostwriter/api/whoami': { username: 'user-a', display_name: null },
    '/ghostwriter/api/chats': [],
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const body = bodies[String(input)]
      return body === undefined
        ? new Response(JSON.stringify({ detail: 'not found' }), { status: 404 })
        : new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
    }),
  )
}

beforeEach(() => serve(CONFIG))

afterEach(() => {
  queryClient.clear()
  clearAllPurposes()
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('shows the app title in its one header row', async () => {
    render(<App />)
    const [header] = await screen.findAllByRole('banner')
    expect(screen.getAllByRole('banner')).toHaveLength(1)
    expect(within(header).getByText('Ghostwriter')).toBeInTheDocument()
  })

  it('keeps a file dropped beside the conversation from opening in the tab', async () => {
    render(<App />)
    await screen.findAllByRole('banner')
    const drop = createEvent.drop(document.body, { dataTransfer: { files: [], types: ['Files'] } })
    fireEvent(document.body, drop)
    expect(drop.defaultPrevented).toBe(true)
  })

  it('names the app alone in the page title, as its sibling apps do', async () => {
    // What the browser shows before the SPA has loaded.
    expect(/<title>(.*)<\/title>/.exec(indexHtml)?.[1]).toBe('Ghostwriter')
    render(<App />)
    await screen.findAllByRole('banner')
    expect(document.title).toBe('Ghostwriter')
  })

  it('shows no purpose bar where the deployment requires no purpose', async () => {
    render(<App />)
    const [header] = await screen.findAllByRole('banner')
    expect(await within(header).findByText('v0.1.0')).toBeInTheDocument()
    expect(within(header).queryByRole('button', { name: /purpose/i })).not.toBeInTheDocument()
    expect(within(header).queryByText('No purpose stated')).not.toBeInTheDocument()
  })

  it("speaks the deployment's language, with the purpose bar, the chats and the periods in place", async () => {
    serve({ ...CONFIG, ui_language: 'de', require_purpose: ['collect'] })
    render(<App />)
    const [header] = await screen.findAllByRole('banner')

    expect(document.documentElement.lang).toBe('de')
    expect(document.title).toBe('Ghostwriter')
    expect(within(header).getByRole('button', { name: 'Zweck erforderlich' })).toBeInTheDocument()
    expect(within(header).getByText('v0.1.0')).toBeInTheDocument()
    expect(await within(header).findByRole('button', { name: /user-a/ })).toBeInTheDocument()
    const sidebar = screen.getByRole('complementary')
    expect(within(sidebar).getByRole('button', { name: 'Neuer Chat' })).toBeInTheDocument()
    expect(await within(sidebar).findByText('Noch keine Chats.')).toBeInTheDocument()
    const main = screen.getByRole('main')
    expect(within(main).getByText(/Chats werden 90 Tage nach ihrem Beginn gelöscht\./)).toBeInTheDocument()
  })

  it('starts a new chat from the note of a chat that is nearly full, and opens it', async () => {
    const other = { ...CHAT, id: 'c2', title: null, updated_at: '2026-11-03T09:00:00Z' }
    const full = detail([
      message({ id: 'm1', role: 'user', content: 'Frage zum Hafen' }),
      message({ id: 'm2', role: 'assistant', content: 'Antwort zum Hafen', context_estimate: 30_000 }),
    ])
    const calls = serveRoutes({
      'GET /ghostwriter/api/config': CONFIG,
      'GET /ghostwriter/api/whoami': { username: 'user-a', display_name: null },
      'GET /ghostwriter/api/chats': [CHAT],
      'GET /ghostwriter/api/chats/c1': full,
      'POST /ghostwriter/api/chats': () => json(other, 201),
      'GET /ghostwriter/api/chats/c2': detail([], other),
    })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Quarterly figures' }))
    const main = screen.getByRole('main')
    await within(main).findByText('Antwort zum Hafen')
    await within(main).findByText(/^This chat is nearly as long/)

    // The list's own New chat is in the sidebar; this one is the note's, in the main column.
    await user.click(within(main).getByRole('button', { name: 'New chat' }))
    await waitFor(() => expect(callsTo(calls, 'POST', '/ghostwriter/api/chats')).toHaveLength(1))
    // The new chat is open, empty, with no ring and no note.
    expect(await within(main).findByText('Ask anything, or attach a document.')).toBeInTheDocument()
    expect(within(main).queryByText(/^This chat is nearly as long/)).not.toBeInTheDocument()
    expect(within(main).queryByRole('meter')).not.toBeInTheDocument()
    expect(callsTo(calls, 'GET', '/ghostwriter/api/chats/c2')).toHaveLength(1)
  })
})
