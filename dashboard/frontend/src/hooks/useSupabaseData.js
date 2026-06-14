// Thin read accessors over the TanStack Query cache that useSupabaseRealtime
// keeps fresh. Components call these instead of touching the client directly.
import { useQuery } from '@tanstack/react-query'
import { QK } from './useSupabaseRealtime'

// The realtime hook seeds + updates these keys; the queryFn is a no-op that
// returns whatever is cached (initialData []), so reads never refetch on their
// own — realtime is the source of truth.
function useCached(key) {
  const { data } = useQuery({
    queryKey: key,
    queryFn: () => [],
    initialData: [],
    staleTime: Infinity,
  })
  return data || []
}

export const useTrades        = () => useCached(QK.trades)
export const useTraces        = () => useCached(QK.traces)
export const useEquitySeries  = () => useCached(QK.equity)
export const useAgentHealthDb = () => useCached(QK.agentHealth)
export const useSeniority     = () => useCached(QK.seniority)
export const useExp           = () => useCached(QK.exp)
