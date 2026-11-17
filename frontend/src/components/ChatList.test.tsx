import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ChatList } from './ChatList'
import type { ChatOut } from '../api/types'

afterEach(() => vi.unstubAllGlobals())

function chat(id: string, title: string | null, updated_at: string): ChatOut {
  return { id, title, created_at: '2026-11-01T09:00:00Z', updated_at, expires_at: '2027-01-30T09:00:00Z' }
}

const FIGURES = chat('c1', 'Quarterly figures', '2026-11-03T09:00:00Z')
const LETTER = chat('c2', 'Draft letter', '2026-11-02T09:00:00Z')

type Answer = unknown | (() => Response | Promise<Response>)

/** Stub fetch with one answer per `METHOD path` (a JSON body, null for 204, or a
 *  function for a response of its own); every call is kept. */
function serve(routes: Record<string, Answer>) {
  const calls: Array<{ method: string; url: string; body: unknown }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const url = String(input)
      calls.push({ method, url, body: typeof init?.body === 'string' ? JSON.parse(init.body) : init?.body })
      const key = `${method} ${url}`
      if (!(key in routes)) return new Response(JSON.stringify({ detail: 'not found' }), { status: 404 })
      const answer = routes[key]
      if (typeof answer === 'function') return (answer as () => Response | Promise<Response>)()
      if (answer === null) return new Response(null, { status: 204 })
      return new Response(JSON.stringify(answer), { status: 200, headers: { 'content-type': 'application/json' } })
    }),
  )
  return calls
}

function renderList(selected: string | null = null) {
  const onSelect = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ChatList selected={selected} onSelect={onSelect} />
    </QueryClientProvider>,
  )
  return { onSelect }
}

/** The list, once the chats have arrived. */
async function chatList() {
  const list = await screen.findByRole('list', { name: 'Chats' })
  await waitFor(() => expect(within(list).queryAllByRole('listitem').length).toBeGreaterThan(0))
  return list
}

function rows(list: HTMLElement) {
  return within(list)
    .getAllByRole('listitem')
    .map((row) => row.textContent)
}

function rowOf(list: HTMLElement, title: string) {
  return within(list).getByText(title).closest('li')!
}

describe('ChatList', () => {
  it('lists chats and starts a new one', async () => {
    const calls = serve({
      'GET /ghostwriter/api/chats': [FIGURES, LETTER],
      'POST /ghostwriter/api/chats': () =>
        new Response(JSON.stringify(chat('c3', null, '2026-11-04T09:00:00Z')), {
          status: 201,
          headers: { 'content-type': 'application/json' },
        }),
    })
    const { onSelect } = renderList()
    const user = userEvent.setup()
    const list = await chatList()
    expect(rows(list)).toEqual(['Quarterly figures', 'Draft letter'])

    await user.click(screen.getByRole('button', { name: 'New chat' }))

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('c3'))
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.url)).toEqual(['/ghostwriter/api/chats'])
    expect(rows(list)).toEqual(['New chat', 'Quarterly figures', 'Draft letter'])
  })

  it('opens a chat when its row is chosen, and marks the open one', async () => {
    serve({ 'GET /ghostwriter/api/chats': [FIGURES, LETTER] })
    const { onSelect } = renderList('c1')
    const user = userEvent.setup()
    const list = await chatList()

    expect(within(list).getByRole('button', { name: 'Quarterly figures' })).toHaveAttribute('aria-current', 'true')
    await user.click(within(list).getByRole('button', { name: 'Draft letter' }))
    expect(onSelect).toHaveBeenCalledWith('c2')
  })

  it('renames a chat in place', async () => {
    const calls = serve({
      'GET /ghostwriter/api/chats': [FIGURES, LETTER],
      'PATCH /ghostwriter/api/chats/c2': chat('c2', 'Q3 Zahlen', '2026-11-04T09:00:00Z'),
    })
    renderList()
    const user = userEvent.setup()
    const list = await chatList()

    await user.click(within(rowOf(list, 'Draft letter')).getByRole('button', { name: 'Chat options' }))
    await user.click(screen.getByRole('menuitem', { name: 'Rename' }))
    const field = within(list).getByRole('textbox', { name: 'Chat title' })
    expect(field).toHaveFocus()
    expect(field).toHaveValue('Draft letter')
    await user.clear(field)
    await user.type(field, 'Q3 Zahlen{Enter}')

    await waitFor(() => expect(within(list).queryByRole('textbox')).not.toBeInTheDocument())
    const patches = calls.filter((c) => c.method === 'PATCH')
    expect(patches).toEqual([{ method: 'PATCH', url: '/ghostwriter/api/chats/c2', body: { title: 'Q3 Zahlen' } }])
    // A rename is activity: the chat moves to the top, as the server orders it.
    expect(rows(list)).toEqual(['Q3 Zahlen', 'Quarterly figures'])
    expect(within(list).getByRole('button', { name: 'Q3 Zahlen' })).toHaveFocus()
  })

  it('cancels a rename with Escape', async () => {
    const calls = serve({ 'GET /ghostwriter/api/chats': [FIGURES, LETTER] })
    renderList()
    const user = userEvent.setup()
    const list = await chatList()

    await user.click(within(rowOf(list, 'Quarterly figures')).getByRole('button', { name: 'Chat options' }))
    await user.click(screen.getByRole('menuitem', { name: 'Rename' }))
    await user.type(within(list).getByRole('textbox', { name: 'Chat title' }), ' draft{Escape}')

    expect(within(list).queryByRole('textbox')).not.toBeInTheDocument()
    expect(rows(list)).toEqual(['Quarterly figures', 'Draft letter'])
    expect(within(list).getByRole('button', { name: 'Quarterly figures' })).toHaveFocus()
    expect(calls.filter((c) => c.method === 'PATCH')).toEqual([])
  })

  it('deletes a chat after confirming', async () => {
    let answer!: () => void
    const calls = serve({
      'GET /ghostwriter/api/chats': [FIGURES, LETTER],
      'DELETE /ghostwriter/api/chats/c1': () =>
        new Promise<Response>((resolve) => {
          answer = () => resolve(new Response(null, { status: 204 }))
        }),
    })
    const { onSelect } = renderList('c1')
    const user = userEvent.setup()
    const list = await chatList()

    await user.click(within(rowOf(list, 'Quarterly figures')).getByRole('button', { name: 'Chat options' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }))
    expect(screen.getByText('Delete this chat and its files?')).toBeInTheDocument()
    expect(calls.filter((c) => c.method === 'DELETE')).toEqual([])

    const confirm = screen.getByRole('button', { name: 'Delete' })
    await user.click(confirm)
    // Disabled from the confirm until the delete settles: a second click sends nothing.
    expect(confirm).toBeDisabled()
    await user.click(confirm)
    expect(calls.filter((c) => c.method === 'DELETE').map((c) => c.url)).toEqual(['/ghostwriter/api/chats/c1'])

    answer()
    await waitFor(() => expect(rows(list)).toEqual(['Draft letter']))
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('keeps the open chat when another one is deleted', async () => {
    serve({ 'GET /ghostwriter/api/chats': [FIGURES, LETTER], 'DELETE /ghostwriter/api/chats/c2': null })
    const { onSelect } = renderList('c1')
    const user = userEvent.setup()
    const list = await chatList()

    await user.click(within(rowOf(list, 'Draft letter')).getByRole('button', { name: 'Chat options' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(rows(list)).toEqual(['Quarterly figures']))
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('says why a delete did not happen, and keeps the chat', async () => {
    serve({
      'GET /ghostwriter/api/chats': [FIGURES, LETTER],
      'DELETE /ghostwriter/api/chats/c1': () =>
        new Response(JSON.stringify({ detail: 'Stop the answer before deleting the chat.' }), { status: 409 }),
    })
    renderList('c1')
    const user = userEvent.setup()
    const list = await chatList()

    await user.click(within(rowOf(list, 'Quarterly figures')).getByRole('button', { name: 'Chat options' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Something went wrong (409).')
    expect(rows(list)[0]).toContain('Quarterly figures')
  })

  it('shows an untitled chat as "New chat"', async () => {
    serve({ 'GET /ghostwriter/api/chats': [chat('c4', null, '2026-11-03T09:00:00Z'), LETTER] })
    renderList()
    const list = await chatList()
    expect(rows(list)).toEqual(['New chat', 'Draft letter'])
  })

  it('says so while there is no chat yet', async () => {
    serve({ 'GET /ghostwriter/api/chats': [] })
    renderList()
    expect(await screen.findByText('No chats yet.')).toBeInTheDocument()
  })
})
