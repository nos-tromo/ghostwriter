import { useQuery } from '@tanstack/react-query'
import { getConfig } from '../api/chats'

/** The deployment's switches, limits and periods, under the query key
 *  ['config']. The backend reads them from env at start and serves them
 *  without auth, so they are read first, once, and never again. */
export function useConfig() {
  return useQuery({
    queryKey: ['config'],
    queryFn: getConfig,
    staleTime: Infinity,
    gcTime: Infinity,
  })
}
