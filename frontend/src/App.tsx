import { AppShell } from '@infra/ui'
import { useT } from './i18n/LanguageContext'

export default function App() {
  const t = useT()
  return (
    <AppShell
      title="Ghostwriter"
      homeLabel={t('appheader.home')}
      themeLabels={{
        system: t('appheader.theme_system'),
        light: t('appheader.theme_light'),
        dark: t('appheader.theme_dark'),
      }}
      signOutLabel={t('appheader.sign_out')}
    >
      <div className="flex min-h-full flex-col p-8" />
    </AppShell>
  )
}
