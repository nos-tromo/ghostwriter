import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PURPOSE_HEADER, clearAllPurposes, setPurpose } from '@infra/ui'
import { ResultCard } from './ResultCard'
import { LanguageContext } from '../i18n/LanguageContext'
import { callsTo, json, result, serve } from '../test/fixtures'

afterEach(() => {
  clearAllPurposes()
  sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const FILE_URL = '/ghostwriter/api/chats/c1/files/F'

function blobResponse(type: string) {
  return () => new Response(new Blob(['synthetic bytes'], { type }), { status: 200, headers: { 'content-type': type } })
}

function rows(n: number): string[][] {
  return Array.from({ length: n }, (_, i) => [`2026-0${(i % 9) + 1}`, String(10 * (i + 1))])
}

function table(rowCount: number | null, shown: number, truncated = false) {
  return result({
    id: 'r1',
    kind: 'table',
    name: 'summe.csv',
    mime: 'text/csv',
    preview: { columns: ['Monat', 'Betrag'], rows: rows(shown), row_count: rowCount, truncated },
  })
}

describe('ResultCard', () => {
  it('shows an image result inline from a blob fetched with the purpose', async () => {
    setPurpose('ghostwriter', { reason: 'review' })
    const calls = serve({ [`GET ${FILE_URL}`]: blobResponse('image/png') })
    const revoke = vi.spyOn(URL, 'revokeObjectURL')
    const { unmount } = render(
      <ResultCard chatId="c1" result={result({ id: 'r1', kind: 'image', name: 'chart.png', mime: 'image/png' })} />,
    )

    const image = await screen.findByRole('img', { name: 'chart.png' })
    const src = image.getAttribute('src') ?? ''
    expect(src.startsWith('blob:')).toBe(true)
    const [call] = callsTo(calls, 'GET', FILE_URL)
    expect(call.headers[PURPOSE_HEADER]).toBe('reason=review')
    // An image is a file like any other: it can be saved too.
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument()

    unmount()
    expect(revoke).toHaveBeenCalledWith(src)
  })

  it('previews at most five rows and counts the rest', () => {
    const { container, unmount } = render(<ResultCard chatId="c1" result={table(12, 6)} />)
    expect(screen.getByText('summe.csv', { selector: 'caption' })).toBeInTheDocument()
    expect(within(screen.getByRole('table')).getAllByRole('columnheader').map((th) => th.textContent)).toEqual([
      'Monat',
      'Betrag',
    ])
    expect(container.querySelectorAll('tbody tr')).toHaveLength(5)
    expect(screen.getByText('7 more rows')).toBeInTheDocument()
    // The file line follows the preview.
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument()
    unmount()

    const { container: whole, unmount: again } = render(<ResultCard chatId="c1" result={table(3, 3)} />)
    expect(whole.querySelectorAll('tbody tr')).toHaveLength(3)
    expect(screen.queryByText(/more rows?/)).not.toBeInTheDocument()
    again()

    render(<ResultCard chatId="c1" result={table(6, 5)} />)
    expect(screen.getByText('1 more row')).toBeInTheDocument()
  })

  it('says more rows follow when they were not counted', () => {
    const { unmount } = render(<ResultCard chatId="c1" result={table(null, 5, true)} />)
    expect(screen.getByText('More rows in the file')).toBeInTheDocument()
    unmount()

    render(<ResultCard chatId="c1" result={table(null, 2, false)} />)
    expect(screen.queryByText('More rows in the file')).not.toBeInTheDocument()
  })

  it('shows a csv without a preview as a plain file', () => {
    const csv = result({ id: 'r1', kind: 'file', name: 'export.csv', mime: 'text/csv', size: 2048 })
    const { unmount } = render(<ResultCard chatId="c1" result={csv} />)
    expect(screen.getByText('export.csv')).toBeInTheDocument()
    expect(screen.getByText('2 KB')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    unmount()

    // A fresh table may come without a preview: the same file card.
    const { unmount: again } = render(
      <ResultCard chatId="c1" result={{ ...csv, kind: 'table', name: 'summe.xlsx', size: 1536, preview: null }} />,
    )
    expect(screen.getByText('summe.xlsx')).toBeInTheDocument()
    expect(screen.getByText('1.5 KB')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    again()

    render(
      <LanguageContext value="de">
        <ResultCard chatId="c1" result={{ ...csv, size: 1536 }} />
      </LanguageContext>,
    )
    expect(screen.getByText('1,5 KB')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Herunterladen' })).toBeInTheDocument()
  })

  it('offers a download per file and none once expired', async () => {
    const calls = serve({ [`GET ${FILE_URL}`]: blobResponse('text/csv') })
    const create = vi.spyOn(URL, 'createObjectURL')
    const revoke = vi.spyOn(URL, 'revokeObjectURL')
    const saved: Array<{ href: string; download: string }> = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ href: this.href, download: this.download })
    })
    const user = userEvent.setup()
    const csv = result({ id: 'r1', kind: 'file', name: 'export.csv', mime: 'text/csv' })
    const { unmount } = render(<ResultCard chatId="c1" result={csv} />)

    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(callsTo(calls, 'GET', FILE_URL)).toHaveLength(1)
    expect(saved[0].download).toBe('export.csv')
    expect(saved[0].href).toBe(create.mock.results[0].value)
    await waitFor(() => expect(revoke).toHaveBeenCalledWith(saved[0].href))
    unmount()

    // Expired: a muted card that says so, and nothing to fetch.
    const { unmount: again } = render(<ResultCard chatId="c1" result={{ ...csv, expired: true }} />)
    expect(screen.getByText('export.csv')).toBeInTheDocument()
    expect(screen.getByText('Expired')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument()
    again()

    // workbench deleted it meanwhile: the download's 410 turns the card expired.
    serve({ [`GET ${FILE_URL}`]: () => json({ detail: 'The file has expired.' }, 410) })
    render(<ResultCard chatId="c1" result={csv} />)
    await user.click(screen.getByRole('button', { name: 'Download' }))
    expect(await screen.findByText('Expired')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument()
  })

  it('shows an image that is gone as expired, and never fetches an expired one', async () => {
    const calls = serve({ [`GET ${FILE_URL}`]: () => json({ detail: 'not found' }, 404) })
    const image = result({ id: 'r1', kind: 'image', name: 'chart.png', mime: 'image/png' })
    const { unmount } = render(<ResultCard chatId="c1" result={image} />)
    expect(await screen.findByText('Expired')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'chart.png' })).not.toBeInTheDocument()
    unmount()

    render(<ResultCard chatId="c1" result={{ ...image, expired: true }} />)
    expect(screen.getByText('Expired')).toBeInTheDocument()
    expect(callsTo(calls, 'GET', FILE_URL)).toHaveLength(1)
  })
})
