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
export const useLlmUsage      = () => useCached(QK.llm)

// ── Heavy aggregates via RPC (polled, not realtime) ──────────────────────────
const SB_URL = import.meta.env.VITE_SUPABASE_URL
const SB_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY

function useRpc(name, args = {}) {
  return useQuery({
    queryKey: ['rpc', name, args],
    enabled: Boolean(SB_URL && SB_KEY),
    refetchInterval: 60_000,
    queryFn: async () => {
      const { createClient } = await import('@supabase/supabase-js')
      const sb = createClient(SB_URL, SB_KEY)
      const { data, error } = await sb.rpc(name, args)
      if (error) throw error
      return data
    },
    initialData: null,
    staleTime: 30_000,
  }).data
}

export const useRMultiples       = () => useRpc('get_r_multiple_distribution') || []
export const usePerformanceStats = () => {
  const d = useRpc('get_performance_stats')
  return Array.isArray(d) ? d[0] : d || null
}

// Open positions straight from portfolio_positions (the authoritative table) —
// the live WebSocket status.positions is often empty, so the dashboard should
// not depend on it. Polled every 30s.
export const usePositions = () => useQuery({
  queryKey: ['positions', 'open'],
  enabled: Boolean(SB_URL && SB_KEY),
  refetchInterval: 30_000,
  queryFn: async () => {
    const { createClient } = await import('@supabase/supabase-js')
    const sb = createClient(SB_URL, SB_KEY)
    const { data, error } = await sb
      .from('portfolio_positions')
      .select('symbol, side, qty, avg_cost, current_price, unrealized_pnl, unrealized_pnl_pct')
      .is('closed_at', null)
    if (error) throw error
    return data || []
  },
  initialData: [],
  staleTime: 15_000,
}).data || []
