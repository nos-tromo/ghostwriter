import { Button, WarningIcon } from '@infra/ui'
import { useT } from '../i18n/LanguageContext'

/**
 * What the chat's context says to the user above the composer: the chat is nearly as long as the model can
 * read at once, and, when it has already been trimmed, that earlier parts are no longer sent. Both end in the
 * same offer, a new chat, which is the one remedy. Quiet, like the lines under an answer: the text stays
 * muted and only the icon takes the warning colour.
 *
 * Not a live region: it is a standing state of the chat, read in its place, and the ring beside the send
 * button carries the same number for a screen reader.
 *
 * @param nearlyFull - The ring is at or above its warning level (`nearlyFull(ratio)`, decided on the same
 *   unrounded ratio the ring gets).
 * @param trimmed - The latest answer's prompt left earlier turns out.
 * @param onNewChat - Start a new chat.
 * @returns The note, or nothing while neither holds.
 */
export function ContextNote({
  nearlyFull,
  trimmed,
  onNewChat,
}: {
  nearlyFull: boolean
  trimmed: boolean
  onNewChat: () => void
}) {
  const t = useT()
  if (!nearlyFull && !trimmed) return null
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <WarningIcon aria-hidden="true" className="size-4 shrink-0 text-warning" />
      <div className="min-w-0 flex-1 space-y-0.5 text-sm text-muted-foreground">
        {nearlyFull && <p>{t('context.nearly_full')}</p>}
        {trimmed && <p>{t('context.trimmed')}</p>}
      </div>
      <Button type="button" variant="ghost" size="sm" onClick={onNewChat}>
        {t('chat.new')}
      </Button>
    </div>
  )
}
