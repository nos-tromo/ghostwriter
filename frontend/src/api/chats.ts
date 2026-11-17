import { apiDelete, apiErrorOf, apiFetch, apiGet, apiPatch, apiPost, apiResult } from './client'
import type { AppConfig, AttachmentOut, ChatDetail, ChatOut, TranscriptOut, Whoami } from './types'

const chatPath = (id: string) => `/chats/${encodeURIComponent(id)}`

/** The user's chats, the latest activity first. */
export const listChats = (): Promise<ChatOut[]> => apiGet<ChatOut[]>('/chats')

/** Start an untitled chat. */
export const createChat = (): Promise<ChatOut> => apiPost<ChatOut>('/chats')

export const getChat = (id: string): Promise<ChatDetail> => apiGet<ChatDetail>(chatPath(id))

export const renameChat = (id: string, title: string): Promise<ChatOut> =>
  apiPatch<ChatOut>(chatPath(id), { title })

/** Delete the chat and its files in workbench; 409 while an answer runs in it. */
export const deleteChat = (id: string): Promise<void> => apiDelete(chatPath(id))

/** Ask the chat's running answer to stop. */
export const stopChat = (id: string): Promise<void> => apiPost<void>(`${chatPath(id)}/stop`)

/** Hand a file to the chat; it stays pending until a message sends it. A 413
 *  carries `code: 'too_large'`, a 507 `code: 'storage_full'`. */
export async function uploadAttachment(chatId: string, file: File, signal?: AbortSignal): Promise<AttachmentOut> {
  const body = new FormData()
  body.append('file', file, file.name)
  return apiResult<AttachmentOut>(await apiFetch(`${chatPath(chatId)}/attachments`, { method: 'POST', body, signal }))
}

/** A file of the chat, an attachment or a tool's output, as a blob. */
export async function fileBlob(chatId: string, fileId: string): Promise<Blob> {
  const res = await apiFetch(`${chatPath(chatId)}/files/${encodeURIComponent(fileId)}`)
  if (!res.ok) throw await apiErrorOf(res)
  return res.blob()
}

/** The text of a recording. The server names the file by its type, never by
 *  the name sent here. */
export async function transcribe(blob: Blob, mime: string): Promise<string> {
  const body = new FormData()
  body.append('file', new Blob([blob], { type: mime }), 'recording')
  const out = await apiResult<TranscriptOut>(await apiFetch('/transcribe', { method: 'POST', body }))
  return out.text
}

export const getConfig = (): Promise<AppConfig> => apiGet<AppConfig>('/config')

export const getWhoami = (): Promise<Whoami> => apiGet<Whoami>('/whoami')
