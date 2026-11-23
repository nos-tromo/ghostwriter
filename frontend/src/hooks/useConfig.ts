import { useQuery } from '@tanstack/react-query'
import { getConfig } from '../api/chats'

/** The query key of the deployment's config: a refused clip invalidates it, so the offer is read again. */
export const CONFIG_KEY = ['config'] as const

/** The deployment's switches, limits and periods, under the query key
 *  ['config']. The backend reads them from env at start and serves them
 *  without auth, so they are read first, once, and never again, but for the clip offer, which follows Nextext's health:
 *  a clip the server refuses has it read again (`useAttachments`). */
export function useConfig() {
  return useQuery({
    queryKey: CONFIG_KEY,
    queryFn: getConfig,
    staleTime: Infinity,
    gcTime: Infinity,
  })
}
