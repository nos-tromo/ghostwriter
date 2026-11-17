import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { clearAllPurposes, setPurpose } from '@infra/ui'
import { refetchRefusedOnPurpose, retryPolicy } from './queryClient'
import { ApiError, PURPOSE_SCOPE } from './client'

describe('retryPolicy', () => {
  it('does not retry a deterministic 4xx', () => {
    expect(retryPolicy(0, new ApiError(401, 'Missing authenticated principal.'))).toBe(false)
    expect(retryPolicy(0, new ApiError(404, 'Chat not found.'))).toBe(false)
  })

  it('retries a transient failure once', () => {
    expect(retryPolicy(0, new ApiError(502, 'boom'))).toBe(true)
    expect(retryPolicy(1, new ApiError(502, 'boom'))).toBe(false)
    expect(retryPolicy(0, new TypeError('Failed to fetch'))).toBe(true)
    expect(retryPolicy(1, new TypeError('Failed to fetch'))).toBe(false)
  })
})

describe('refetchRefusedOnPurpose', () => {
  afterEach(() => {
    clearAllPurposes()
    sessionStorage.clear()
  })

  /** Observe a query, so it counts as active, and count its fetches. */
  function watch(client: QueryClient, key: string, fail: (call: number) => unknown) {
    let calls = 0
    const observer = new QueryObserver(client, {
      queryKey: [key],
      queryFn: async () => {
        calls += 1
        const error = fail(calls)
        if (error) throw error
        return 'ok'
      },
    })
    const stop = observer.subscribe(() => {})
    return { calls: () => calls, stop }
  }

  it('refetches what a declined 428 left in error once a purpose is stated, and nothing else', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const refused = watch(client, 'refused', (call) => (call === 1 ? new ApiError(428, 'purpose') : null))
    const broken = watch(client, 'broken', () => new ApiError(500, 'boom'))
    await vi.waitFor(() => expect(client.getQueryState(['refused'])?.status).toBe('error'))
    await vi.waitFor(() => expect(client.getQueryState(['broken'])?.status).toBe('error'))
    const stop = refetchRefusedOnPurpose(client)

    setPurpose(PURPOSE_SCOPE, { caseRef: 'case-0001' })

    await vi.waitFor(() => expect(client.getQueryData(['refused'])).toBe('ok'))
    expect(refused.calls()).toBe(2)
    expect(broken.calls()).toBe(1)
    stop()
    refused.stop()
    broken.stop()
  })

  it('does nothing when the purpose is cleared, or when another app states one', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const refused = watch(client, 'refused', () => new ApiError(428, 'purpose'))
    await vi.waitFor(() => expect(client.getQueryState(['refused'])?.status).toBe('error'))
    const stop = refetchRefusedOnPurpose(client)

    setPurpose('docint', { caseRef: 'case-0002' })
    clearAllPurposes()

    expect(refused.calls()).toBe(1)
    stop()
    refused.stop()
  })
})
