import { useEffect, useState } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient, refetchRefusedOnPurpose } from './api/queryClient'
import { ChatPane } from './components/ChatPane'
import { useConfig } from './hooks/useConfig'
import { LanguageProvider } from './i18n/LanguageContext'
import { Shell } from './layout/Shell'

/** The open chat. Keyed by its id, so leaving a chat closes its stream and
 *  nothing of it carries over into the next. */
function OpenChat({ chatId }: { chatId: string | null }) {
  const { data: config } = useConfig()
  if (chatId === null || !config) return null
  return <ChatPane key={chatId} chatId={chatId} config={config} />
}

export default function App() {
  useEffect(() => refetchRefusedOnPurpose(queryClient), [])
  const [selected, setSelected] = useState<string | null>(null)
  return (
    <QueryClientProvider client={queryClient}>
      <LanguageProvider>
        <Shell selected={selected} onSelect={setSelected}>
          <OpenChat chatId={selected} />
        </Shell>
      </LanguageProvider>
    </QueryClientProvider>
  )
}
