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

/** Take back an attachment not sent yet: its file goes in workbench, then the chat forgets it. A 404 is
 *  one already gone, a 409 with `code: 'attachment_sent'` one a message sent meanwhile. */
export const deleteAttachment = (chatId: string, attachmentId: string): Promise<void> =>
  apiDelete(`${chatPath(chatId)}/attachments/${encodeURIComponent(attachmentId)}`)

/** One attachment, read as it is now. A `processing` one is waited for on the server for
 *  up to `waitS` seconds (at most 20) and answers as soon as its preparation ends. */
export const getAttachment = (
  chatId: string,
  attachmentId: string,
  waitS: number,
  signal?: AbortSignal,
): Promise<AttachmentOut> =>
  apiGet<AttachmentOut>(`${chatPath(chatId)}/attachments/${encodeURIComponent(attachmentId)}?wait=${waitS}`, { signal })

/** The picture of an attachment, or its page, as a blob: a small rendition the server
 *  made, never the original. Errors as {@link fileBlob}'s. */
export async function attachmentPreview(
  chatId: string,
  attachmentId: string,
  page = 1,
  signal?: AbortSignal,
): Promise<Blob> {
  const res = await apiFetch(`${chatPath(chatId)}/attachments/${encodeURIComponent(attachmentId)}/preview?page=${page}`, {
    signal,
  })
  if (!res.ok) throw await apiErrorOf(res)
  return res.blob()
}

/** A file of the chat, an attachment or a tool's output, as a blob. 410 once
 *  it has expired, also when workbench no longer holds it; 404 for one this
 *  chat does not hold. */
export async function fileBlob(chatId: string, fileId: string, signal?: AbortSignal): Promise<Blob> {
  const res = await apiFetch(`${chatPath(chatId)}/files/${encodeURIComponent(fileId)}`, { signal })
  if (!res.ok) throw await apiErrorOf(res)
  return res.blob()
}

/** The text of a recording. The server names the file by its type, never by
 *  the name sent here. */
export async function transcribe(blob: Blob, mime: string, signal?: AbortSignal): Promise<string> {
  const body = new FormData()
  body.append('file', new Blob([blob], { type: mime }), 'recording')
  const out = await apiResult<TranscriptOut>(await apiFetch('/transcribe', { method: 'POST', body, signal }))
  return out.text
}

export const getConfig = (): Promise<AppConfig> => apiGet<AppConfig>('/config')

export const getWhoami = (): Promise<Whoami> => apiGet<Whoami>('/whoami')
