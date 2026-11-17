import { useMemo, type ReactNode } from 'react'
import { AppShell, PurposeBar, type PurposeBarLabels } from '@infra/ui'
import { PURPOSE_SCOPE } from '../api/client'
import { ChatList } from '../components/ChatList'
import { RetentionBanner } from '../components/RetentionBanner'
import { useT } from '../i18n/LanguageContext'
import { useConfig } from '../hooks/useConfig'
import { useWhoami } from '../hooks/useWhoami'

/** The purpose bar's strings in the active locale. */
function usePurposeLabels(): PurposeBarLabels {
  const t = useT()
  return useMemo(
    () => ({
      purpose: t('purpose.label'),
      empty: t('purpose.empty'),
      required: t('purpose.required'),
      title: t('purpose.title'),
      description: t('purpose.description'),
      requested: t('purpose.requested'),
      caseRef: t('purpose.case_ref'),
      reason: t('purpose.reason'),
      save: t('purpose.save'),
      cancel: t('purpose.cancel'),
      clear: t('purpose.clear'),
      errorLength: t('purpose.error_length'),
      errorControl: t('purpose.error_control'),
      errorHeader: t('purpose.error_header'),
      errorEmpty: t('purpose.error_empty'),
    }),
    [t],
  )
}

/** The chrome: the purpose bar in the header, the chats beside, and the
 *  retention periods above the conversation, which is `children`. */
export function Shell({
  selected,
  onSelect,
  children,
}: {
  selected: string | null
  onSelect: (id: string | null) => void
  children: ReactNode
}) {
  const t = useT()
  const { data: config } = useConfig()
  const { data: whoami } = useWhoami()
  const purposeLabels = usePurposeLabels()
  return (
    <AppShell
      title="Ghostwriter"
      // The backend's trusted-header principal (X-Auth-User), echoed back by
      // GET /whoami: undefined while loading or on a fetch error, so the
      // header omits the user block rather than showing a wrong identity.
      // The gateway's display name is preferred when present.
      user={whoami?.display_name ?? whoami?.username ?? undefined}
      version={config?.version ? `v${config.version}` : undefined}
      homeLabel={t('appheader.home')}
      themeLabels={{
        system: t('appheader.theme_system'),
        light: t('appheader.theme_light'),
        dark: t('appheader.theme_dark'),
      }}
      signOutLabel={t('appheader.sign_out')}
      sidebarToggleLabel={t('appheader.sidebar_toggle')}
      // Where the purpose every request carries is stated (`apiFetch`), and
      // the dialog a 428 opens. The backend says which actions it refuses
      // without one; any at all make the empty bar a warning.
      headerActions={
        <PurposeBar
          scope={PURPOSE_SCOPE}
          required={(config?.require_purpose?.length ?? 0) > 0}
          labels={purposeLabels}
        />
      }
      sidebar={<ChatList selected={selected} onSelect={onSelect} />}
    >
      <div className="flex h-full min-h-0 flex-col">
        {config && (
          <div className="shrink-0 px-4 pt-4">
            <RetentionBanner config={config} />
          </div>
        )}
        <div className="min-h-0 flex-1">{children}</div>
      </div>
    </AppShell>
  )
}
