import { useT } from '../i18n/LanguageContext'
import type { AppConfig } from '../api/types'

/** How long chats and files are kept here, as a quiet line under the composer.
 *  The chat period is left out while chat retention is off, and the file period
 *  while nothing can be attached, so a chat-only deployment reads as one; with
 *  neither, there is no line. */
export function RetentionNote({ config }: { config: AppConfig }) {
  const t = useT()
  const chatDays = config.chat_retention_days
  if (chatDays === null && !config.attachments) return null
  return (
    <p className="shrink-0 px-4 pb-3 text-center text-xs text-muted-foreground">
      {chatDays !== null && <>{t('retention.chats', { days: chatDays })} </>}
      {config.attachments && t('retention.files', { days: config.file_retention_days })}
    </p>
  )
}
