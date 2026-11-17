import { describeError } from '../api/errorMessage'
import { useT } from '../i18n/LanguageContext'

/** Why a request failed, in the UI's words (never the server's `detail`). */
export function RequestError({ error, className }: { error: unknown; className?: string }) {
  const t = useT()
  const { key, vars } = describeError(error)
  return (
    <p role="alert" className={className ?? 'text-xs text-danger'}>
      {t(key, vars)}
    </p>
  )
}
