import { useQuery } from '@tanstack/react-query'
import { getWhoami } from '../api/chats'

/** The signed-in account, for the header's user slot. Constant for the
 *  session, so it is read once; loading and failure leave it undefined and the
 *  header omits the user rather than showing a wrong one. */
export function useWhoami() {
  return useQuery({
    queryKey: ['whoami'],
    queryFn: getWhoami,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
  })
}
