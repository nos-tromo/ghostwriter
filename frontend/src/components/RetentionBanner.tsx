import { Banner } from '@infra/ui'
import { useT } from '../i18n/LanguageContext'
import type { AppConfig } from '../api/types'

/** How long chats and files are kept here. The chat period is left out while
 *  chat retention is off; the file period is workbench's and always applies. */
export function RetentionBanner({ config }: { config: AppConfig }) {
  const t = useT()
  return (
    <Banner variant="info">
      {config.chat_retention_days !== null && <>{t('retention.chats', { days: config.chat_retention_days })} </>}
      {t('retention.files', { days: config.file_retention_days })}
    </Banner>
  )
}
