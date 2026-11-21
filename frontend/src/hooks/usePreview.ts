import { useQuery } from '@tanstack/react-query'
import { attachmentPreview } from '../api/chats'
import { isGone } from '../api/client'
import type { AttachmentOut } from '../api/types'
import { useObjectUrl } from './useObjectUrl'

/** How long a rendition stays in memory once no tile shows it: long enough for the
 *  composer's tile to hand it to the sent message's. */
const PREVIEW_GC_MS = 60_000

/**
 * The picture of an attached image, as an object URL of this mount's own: an `<img src>`
 * could not send the stated purpose, so it is fetched as a blob.
 *
 * Fetched once per attachment (every fetch is an audited `disclose`, so the tiles that
 * show it share one blob), never refetched and never retried, and only for a picture that
 * is `ready` and has not expired. `gone` says the server no longer has it (410, or 404),
 * where the tile shows its placeholder; any other failure leaves `url` null.
 *
 * @param chatId - The chat holding the attachment.
 * @param attachment - The attachment.
 * @param enabled - Whether the tile shows it at all.
 * @returns The URL once the picture has arrived, and whether it is gone.
 */
export function usePreview(
  chatId: string,
  attachment: AttachmentOut,
  enabled: boolean,
): { url: string | null; gone: boolean } {
  const { data: blob, error } = useQuery({
    queryKey: ['preview', chatId, attachment.id],
    queryFn: ({ signal }) => attachmentPreview(chatId, attachment.id, 1, signal),
    enabled: enabled && attachment.kind === 'image' && attachment.status === 'ready' && !attachment.expired,
    staleTime: Infinity,
    gcTime: PREVIEW_GC_MS,
    retry: false,
  })
  const url = useObjectUrl(blob)
  return { url, gone: error !== null && isGone(error) }
}
