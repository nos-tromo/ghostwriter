import { PURPOSE_REQUIRED_STATUS, withPurpose } from '@infra/ui'

/** API base derived from Vite's base path so the SPA works under its
 *  canonical sub-path (/ghostwriter/) and at root (dev) alike. */
export function apiBase(base: string = import.meta.env.BASE_URL): string {
  return `${base.replace(/\/+$/, '')}/api`
}

export const API_BASE = apiBase()

/** The purpose store's scope, ghostwriter's compose project name. The
 *  header's PurposeBar and every request below use the same string. */
export const PURPOSE_SCOPE = 'ghostwriter'

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown
  /** The backend's reason for a refusal, where it gives one (`too_large`,
   *  `storage_full`); `null` otherwise. */
  readonly code: string | null
  constructor(status: number, detail: unknown, code: string | null = null) {
    super(`API ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.code = code
  }
}

/** Whether an error is the backend refusing a request for want of a stated
 *  purpose: what is left of a 428 once the user declined to state one. */
export function isPurposeRefusal(error: unknown): boolean {
  return error instanceof ApiError && error.status === PURPOSE_REQUIRED_STATUS
}

/** Whether a file is no longer there: expired (410), or not held by the chat any more (404). */
export function isGone(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 410 || error.status === 404)
}

/** The `ApiError` a non-2xx response stands for. The body is read once, then
 *  parsed, so a non-JSON error page keeps its status. */
export async function apiErrorOf(res: Response): Promise<ApiError> {
  const text = await res.text()
  let body: unknown = text
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    /* keep raw text */
  }
  if (body && typeof body === 'object') {
    const { detail, code } = body as { detail?: unknown; code?: unknown }
    return new ApiError(res.status, 'detail' in body ? detail : body, typeof code === 'string' ? code : null)
  }
  return new ApiError(res.status, body)
}

/** A response's JSON body, or `undefined` for an empty one (204); a non-2xx
 *  response is thrown as its `ApiError`. */
export async function apiResult<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorOf(res)
  const text = await res.text()
  return (text ? JSON.parse(text) : undefined) as T
}

/** Request options for {@link apiFetch}: `headers` as a plain object, so the purpose can be merged in. */
export type ApiRequestInit = Omit<RequestInit, 'headers'> & { headers?: Record<string, string> }

export interface RequestOptions {
  signal?: AbortSignal
  /** Abort an attempt whose response has not arrived within this many ms.
   *  The limit is per attempt: the time a 428 waits for the user to state a
   *  purpose counts against neither the first attempt nor the resend. */
  timeoutMs?: number
}

/** The one place a request leaves the SPA. It carries the purpose stated in
 *  the header's PurposeBar (X-Audit-Purpose); a 428 opens the bar's dialog
 *  and sends the request once more with the purpose saved there, or hands
 *  the 428 back when the user cancels. The body may therefore go twice, so it
 *  must be re-sendable: a string or `FormData`, never a stream. */
export function apiFetch(path: string, init: ApiRequestInit = {}, timeoutMs?: number): Promise<Response> {
  const target = `${API_BASE}${path}`
  return withPurpose(PURPOSE_SCOPE, (purpose) =>
    attempt(target, { ...init, headers: { ...init.headers, ...purpose } }, timeoutMs),
  )
}

/** One attempt of a request, with its own time limit when one is given.
 *  The caller's signal still aborts it. */
async function attempt(url: string, init: ApiRequestInit, timeoutMs?: number): Promise<Response> {
  if (timeoutMs === undefined) return fetch(url, init)
  const controller = new AbortController()
  const abort = () => controller.abort()
  const outer = init.signal
  if (outer?.aborted) abort()
  else outer?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(abort, timeoutMs)
  try {
    return await fetch(url, { ...init, signal: controller.signal })
  } finally {
    clearTimeout(timer)
    outer?.removeEventListener('abort', abort)
  }
}

/** A request with a JSON body, or with none when `json` is undefined. */
function jsonInit(method: string, json: unknown, signal?: AbortSignal): ApiRequestInit {
  if (json === undefined) return { method, signal }
  return { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(json), signal }
}

export async function apiGet<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return apiResult<T>(await apiFetch(path, { signal: options.signal }, options.timeoutMs))
}

export async function apiPost<T>(path: string, json?: unknown, options: RequestOptions = {}): Promise<T> {
  return apiResult<T>(await apiFetch(path, jsonInit('POST', json, options.signal), options.timeoutMs))
}

export async function apiPatch<T>(path: string, json: unknown, options: RequestOptions = {}): Promise<T> {
  return apiResult<T>(await apiFetch(path, jsonInit('PATCH', json, options.signal), options.timeoutMs))
}

export async function apiDelete<T = void>(path: string, options: RequestOptions = {}): Promise<T> {
  return apiResult<T>(await apiFetch(path, jsonInit('DELETE', undefined, options.signal), options.timeoutMs))
}
