import { useEffect, useState } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient, refetchRefusedOnPurpose } from './api/queryClient'
import { ChatPane } from './components/ChatPane'
import { ChatStreamsProvider } from './hooks/chatStreams'
import { useConfig } from './hooks/useConfig'
import { useStrayDropGuard } from './hooks/useStrayDropGuard'
import { LanguageProvider } from './i18n/LanguageContext'
import { Shell } from './layout/Shell'

/** The open chat. Keyed by its id, so nothing of its view carries over into
 *  the next; its answer keeps being written in the registry above it. */
function OpenChat({ chatId }: { chatId: string | null }) {
  const { data: config } = useConfig()
  if (chatId === null || !config) return null
  return <ChatPane key={chatId} chatId={chatId} config={config} />
}

export default function App() {
  useEffect(() => refetchRefusedOnPurpose(queryClient), [])
  useStrayDropGuard()
  const [selected, setSelected] = useState<string | null>(null)
  return (
    <QueryClientProvider client={queryClient}>
      <LanguageProvider>
        <ChatStreamsProvider>
          <Shell selected={selected} onSelect={setSelected}>
            <OpenChat chatId={selected} />
          </Shell>
        </ChatStreamsProvider>
      </LanguageProvider>
    </QueryClientProvider>
  )
}
