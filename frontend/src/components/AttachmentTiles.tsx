import { useState } from 'react'
import { AttachmentTile, ImageLightbox, type AttachmentTileStatus } from '@infra/ui'
import type { AttachmentOut } from '../api/types'
import { isClip, type PendingError, type PendingFile } from '../hooks/useAttachments'
import { usePreview } from '../hooks/usePreview'
import type { Strings } from '../i18n'
import { useLang, useT } from '../i18n/LanguageContext'
import { formatSize } from '../lib/format'

const ERROR_KEYS: Record<PendingError, keyof Strings> = {
  type: 'composer.error_type',
  size: 'composer.error_size',
  upload: 'composer.error_upload',
  storage_full: 'composer.error_storage_full',
  too_many_pending: 'composer.error_too_many_pending',
  purpose: 'composer.error_purpose',
  clip: 'composer.error_clip',
}

/** Where a file stands, as its tile says it: refused or unreadable files are `failed`; one being
 *  taken back spins, as work on it does. */
function tileStatus(file: PendingFile): AttachmentTileStatus {
  if (file.removing) return 'processing'
  if (file.status === 'uploading') return 'uploading'
  if (file.status === 'processing') return 'processing'
  if (file.status === 'error' || file.attachment?.status === 'failed') return 'failed'
  if (file.attachment?.expired) return 'expired'
  return 'ready'
}

/** Whether a file is a picture that would go to the model with the next message. */
function goesToModel(file: PendingFile): boolean {
  return (
    file.kind === 'image' &&
    file.status !== 'error' &&
    file.attachment?.status !== 'failed' &&
    file.attachment?.expired !== true
  )
}

interface TileProps {
  chatId: string
  file: PendingFile
  onRemove: (key: string) => void
}

/** One file as a tile, with its picture (when it has one) opening in the lightbox. A clip shows its kind's
 *  icon only: it has no picture to draw, and nothing to open. */
function Tile({ file, picture, onRemove }: Omit<TileProps, 'chatId'> & { picture: string | undefined }) {
  const t = useT()
  const lang = useLang()
  const [open, setOpen] = useState(false)
  const status = tileStatus(file)
  const statusLabel = {
    uploading: t('composer.uploading', { name: file.name }),
    processing: file.removing
      ? t('composer.removing', { name: file.name })
      : t(isClip(file) ? 'composer.transcribing' : 'composer.processing', { name: file.name }),
    failed: file.error
      ? t(ERROR_KEYS[file.error], { name: file.name })
      : t('composer.unreadable', { name: file.name }),
    expired: t('result.expired'),
    ready: undefined,
  }[status]
  const viewable = file.kind === 'image' && picture !== undefined && status !== 'expired'
  return (
    <>
      <AttachmentTile
        size="sm"
        name={file.name}
        kind={file.kind}
        status={status}
        thumbnailUrl={picture}
        detail={formatSize(file.size, lang)}
        warning={file.attachment?.length?.too_long ? t('composer.too_long') : undefined}
        statusLabel={statusLabel}
        onOpen={viewable ? () => setOpen(true) : undefined}
        onRemove={file.removing ? undefined : () => onRemove(file.key)}
        removeLabel={t('composer.remove', { name: file.name })}
      />
      {viewable && (
        <ImageLightbox
          open={open}
          src={picture}
          alt={file.name}
          onClose={() => setOpen(false)}
          closeLabel={t('lightbox.close')}
        />
      )}
    </>
  )
}

/** A picture the browser could not draw from the file in hand (a HEIC photo, one the chat lists
 *  after a reload): its rendition is fetched once it is ready, shared with the message that sends it. */
function FetchedTile({ chatId, attachment, ...props }: TileProps & { attachment: AttachmentOut }) {
  const { url } = usePreview(chatId, attachment, true)
  return <Tile {...props} picture={url ?? undefined} />
}

/**
 * The files the next message sends, one tile each: a picture shows itself (from the file in hand
 * at once, else from the server's rendition) and opens full size; every other file, a video or audio
 * clip included, shows its kind, name and size. A tile says where its file stands: being attached, being
 * prepared (a clip, being transcribed), refused or unreadable, expired, being taken back. Any file can be
 * taken back before it is sent: the X on its tile. One whose taking back failed says so below the row, and stays.
 *
 * The row sits above the composer's pill, never inside it. The refusals are said below it, each by
 * the reason the server gave; so is the cap on pictures, once more wait than go to the model.
 */
export function AttachmentTiles({
  chatId,
  pending,
  maxImages,
  onRemove,
}: {
  chatId: string
  pending: PendingFile[]
  maxImages: number
  onRemove: (key: string) => void
}) {
  const t = useT()
  if (pending.length === 0) return null
  // Said below the row: why a file was refused, and which ones the model will be told it could not read.
  const refused = pending.flatMap((f) => (f.status === 'error' && f.error ? [{ file: f, error: f.error }] : []))
  const unreadable = pending.filter((f) => f.status !== 'error' && f.attachment?.status === 'failed')
  return (
    <>
      <ul className="flex flex-wrap gap-2 pt-2">
        {pending.map((file) => (
          <li key={file.key} data-status={file.status} className="max-w-full">
            {file.attachment && file.kind === 'image' && file.localUrl === undefined ? (
              <FetchedTile chatId={chatId} file={file} attachment={file.attachment} onRemove={onRemove} />
            ) : (
              <Tile file={file} picture={file.localUrl} onRemove={onRemove} />
            )}
          </li>
        ))}
      </ul>
      {pending
        .filter((f) => f.removeFailed)
        .map((file) => (
          <p key={`${file.key}-remove`} role="alert" className="text-xs text-danger">
            {t('composer.error_remove', { name: file.name })}
          </p>
        ))}
      {refused.map(({ file, error }) => (
        <p key={file.key} role="alert" className="text-xs text-danger">
          {t(ERROR_KEYS[error], { name: file.name })}
        </p>
      ))}
      {unreadable.map((file) => (
        <p key={file.key} role="status" className="text-xs text-danger">
          {t('composer.unreadable', { name: file.name })}
        </p>
      ))}
      {pending.filter(goesToModel).length > maxImages && (
        <p className="text-xs text-muted-foreground">{t('composer.images_cap', { max: maxImages })}</p>
      )}
    </>
  )
}
