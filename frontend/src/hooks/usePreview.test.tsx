import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { clearAllPurposes } from '@infra/ui'
import type { AttachmentOut } from '../api/types'
import { callsTo, json, serve } from '../test/fixtures'
import { usePreview } from './usePreview'

const PREVIEW = '/ghostwriter/api/chats/c1/attachments/a1/preview?page=1'

function picture(fields: Partial<AttachmentOut> = {}): AttachmentOut {
  return {
    id: 'a1',
    file_id: 'F-a1',
    name: 'harbour-photo.jpg',
    size: 2048,
    mime: 'image/jpeg',
    created_at: '2026-11-02T09:00:00Z',
    expired: false,
    kind: 'image',
    status: 'ready',
    length: null,
    ...fields,
  }
}

const png = () => new Response('png', { status: 200, headers: { 'content-type': 'image/png' } })

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: true } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

beforeEach(() => {
  vi.spyOn(URL, 'createObjectURL').mockImplementation(() => 'blob:preview')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
})

afterEach(() => {
  clearAllPurposes()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('usePreview', () => {
  it('fetches the picture of a ready image once, and shows it as an object URL, revoked with the mount', async () => {
    const calls = serve({ [`GET ${PREVIEW}`]: png })
    const { result, unmount, rerender } = renderHook(() => usePreview('c1', picture(), true), { wrapper: wrapper() })
    expect(result.current).toEqual({ url: null, gone: false })

    await waitFor(() => expect(result.current.url).toBe('blob:preview'))
    expect(result.current.gone).toBe(false)
    rerender()
    expect(callsTo(calls, 'GET', PREVIEW)).toHaveLength(1)

    unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview')
  })

  it.each([
    ['not asked for', picture(), false],
    ['a document', picture({ kind: 'document' }), true],
    ['a picture still being prepared', picture({ status: 'processing' }), true],
    ['a picture whose preparation failed', picture({ status: 'failed' }), true],
    ['an expired picture', picture({ expired: true }), true],
  ])('fetches nothing for %s', async (_name, attachment, enabled) => {
    const calls = serve({ [`GET ${PREVIEW}`]: png })
    const { result } = renderHook(() => usePreview('c1', attachment, enabled), { wrapper: wrapper() })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(result.current).toEqual({ url: null, gone: false })
    expect(calls).toEqual([])
  })

  it('says a picture is gone on a 410 or a 404, and never asks again', async () => {
    for (const status of [410, 404]) {
      const calls = serve({ [`GET ${PREVIEW}`]: () => json({ detail: 'Gone.' }, status) })
      const { result, unmount } = renderHook(() => usePreview('c1', picture(), true), { wrapper: wrapper() })
      await waitFor(() => expect(result.current.gone).toBe(true))
      expect(result.current.url).toBeNull()
      await new Promise((resolve) => setTimeout(resolve, 20))
      expect(callsTo(calls, 'GET', PREVIEW)).toHaveLength(1)
      unmount()
    }
  })

  it('does not take another failure for a gone picture, and does not retry it', async () => {
    const calls = serve({ [`GET ${PREVIEW}`]: () => json({ detail: 'boom' }, 502) })
    const { result } = renderHook(() => usePreview('c1', picture(), true), { wrapper: wrapper() })
    await waitFor(() => expect(callsTo(calls, 'GET', PREVIEW)).toHaveLength(1))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(callsTo(calls, 'GET', PREVIEW)).toHaveLength(1)
    expect(result.current).toEqual({ url: null, gone: false })
  })
})
