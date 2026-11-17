import { useEffect, useState } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient, refetchRefusedOnPurpose } from './api/queryClient'
import { LanguageProvider } from './i18n/LanguageContext'
import { Shell } from './layout/Shell'

export default function App() {
  useEffect(() => refetchRefusedOnPurpose(queryClient), [])
  const [selected, setSelected] = useState<string | null>(null)
  return (
    <QueryClientProvider client={queryClient}>
      <LanguageProvider>
        <Shell selected={selected} onSelect={setSelected}>
          <div className="flex min-h-full flex-col p-8" />
        </Shell>
      </LanguageProvider>
    </QueryClientProvider>
  )
}
