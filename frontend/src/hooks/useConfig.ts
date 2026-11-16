import { useQuery } from '@tanstack/react-query'
import type { Lang } from '../i18n'

/** The SPA's bootstrap config, under the query key ['config']. Nothing
 *  fetches it yet (GET /api/config comes later), so the UI stays English. */
export function useConfig() {
  return useQuery<{ ui_language: Lang }>({ queryKey: ['config'], enabled: false })
}
