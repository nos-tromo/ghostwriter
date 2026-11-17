import { Banner } from '@infra/ui'
import { useT } from '../i18n/LanguageContext'
import type { AppConfig } from '../api/types'

/** How long chats and files are kept here. The chat period is left out while
 *  chat retention is off, and the file period while nothing can be attached,
 *  so a chat-only deployment reads as one; with neither, there is no banner. */
export function RetentionBanner({ config }: { config: AppConfig }) {
  const t = useT()
  const chatDays = config.chat_retention_days
  if (chatDays === null && !config.attachments) return null
  return (
    <Banner variant="info">
      {chatDays !== null && <>{t('retention.chats', { days: chatDays })} </>}
      {config.attachments && t('retention.files', { days: config.file_retention_days })}
    </Banner>
  )
}
