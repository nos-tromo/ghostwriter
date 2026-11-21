import { useState } from 'react'
import { AttachmentTile, ImageLightbox, type AttachmentTileStatus } from '@infra/ui'
import { fileBlob } from '../api/chats'
import { isGone } from '../api/client'
import type { AttachmentOut } from '../api/types'
import { saveBlob } from '../hooks/useObjectUrl'
import { usePreview } from '../hooks/usePreview'
import { useT } from '../i18n/LanguageContext'
import { RequestError } from './RequestError'

/**
 * A picture a message bound, as a tile of the bubble's grid that opens full size.
 *
 * The picture is the server's rendition, fetched once through `usePreview` (so the stated
 * purpose goes with it, and the composer's tile and the stored message share one fetch) and
 * held as an object URL of this mount's own, never the composer's local one, which is revoked
 * when the message takes the file. An expired picture is marked and not fetched; so is one
 * that could not be read, or whose file the server no longer has (a 410 or a 404 on the
 * rendition or on the original), and neither opens. The lightbox saves the original, fetched
 * on the click: a failure closes it and is said below the tile, where the bubble's text is.
 */
export function AttachmentImage({ chatId, attachment }: { chatId: string; attachment: AttachmentOut }) {
  const t = useT()
  const { url, gone } = usePreview(chatId, attachment, true)
  const [open, setOpen] = useState(false)
  const [lost, setLost] = useState(false)
  const [failure, setFailure] = useState<unknown>(null)

  let status: AttachmentTileStatus = 'ready'
  if (attachment.expired || gone || lost) status = 'expired'
  else if (attachment.status === 'failed') status = 'failed'
  const viewable = status === 'ready' && url !== null

  const download = async () => {
    setFailure(null)
    try {
      saveBlob(await fileBlob(chatId, attachment.file_id), attachment.name)
    } catch (error) {
      setOpen(false)
      if (isGone(error)) setLost(true)
      else setFailure(error)
    }
  }

  return (
    <>
      <AttachmentTile
        size="md"
        name={attachment.name}
        kind="image"
        status={status}
        thumbnailUrl={url ?? undefined}
        statusLabel={
          status === 'expired'
            ? t('result.expired')
            : status === 'failed'
              ? t('composer.unreadable', { name: attachment.name })
              : undefined
        }
        onOpen={viewable ? () => setOpen(true) : undefined}
      />
      {viewable && (
        <ImageLightbox
          open={open}
          src={url}
          alt={attachment.name}
          onClose={() => setOpen(false)}
          closeLabel={t('lightbox.close')}
          downloadLabel={t('result.download')}
          onDownload={() => void download()}
        />
      )}
      {failure !== null && <RequestError error={failure} className="mt-1 text-xs text-bubble-foreground" />}
    </>
  )
}
