/**
 * Supabase Realtime hook — subscribes to live DB changes and feeds the
 * TanStack Query cache so persisted history survives reloads.
 *
 * Tables covered (anon SELECT + on the supabase_realtime publication):
 *   trades, decision_traces, equity_snapshots, agent_health,
 *   agent_seniority, agent_exp
 *
 * Returns the connection state; read the data via the query keys below with
 * useQuery (see useSupabaseData). Needs VITE_SUPABASE_URL + VITE_SUPABASE_ANON_KEY.
 */
import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

const SUPABASE_URL      = import.meta.env.VITE_SUPABASE_URL
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY

// Query keys — one per table/RPC.
export const QK = {
  trades:        ['sb', 'trades'],
  traces:        ['sb', 'decision_traces'],
  equity:        ['sb', 'equity_snapshots'],
  agentHealth:   ['sb', 'agent_health'],
  seniority:     ['sb', 'agent_seniority'],
  exp:           ['sb', 'agent_exp'],
  llm:           ['sb', 'llm_usage'],
}

function upsertById(list, row, idKey) {
  const arr = Array.isArray(list) ? list : []
  const without = arr.filter(r => r[idKey] !== row[idKey])
  return [row, ...without]
}

export function useSupabaseRealtime() {
  const qc = useQueryClient()
  const [connected, setConnected] = useState(false)
  const [enabled] = useState(Boolean(SUPABASE_URL && SUPABASE_ANON_KEY))
  const clientRef = useRef(null)

  useEffect(() => {
    if (!enabled) {
      console.warn('[Supabase] VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY not set — realtime disabled')
      return
    }

    let channel
    let cancelled = false

    import('@supabase/supabase-js').then(({ createClient }) => {
      if (cancelled) return
      const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY)
      clientRef.current = supabase

      // ── Initial fetches → seed the query cache ──────────────────────────
      const seed = (key, data) => qc.setQueryData(key, data || [])
      supabase.from('trades').select('*').order('recorded_at', { ascending: false }).limit(100)
        .then(({ data }) => seed(QK.trades, data))
      supabase.from('decision_traces').select('*').order('timestamp', { ascending: false }).limit(100)
        .then(({ data }) => seed(QK.traces, data))
      supabase.from('equity_snapshots').select('*').order('recorded_at', { ascending: true }).limit(500)
        .then(({ data }) => seed(QK.equity, data))
      supabase.from('agent_health').select('*').order('checked_at', { ascending: false }).limit(20)
        .then(({ data }) => seed(QK.agentHealth, data))
      supabase.from('llm_usage').select('*').order('recorded_at', { ascending: false }).limit(500)
        .then(({ data }) => seed(QK.llm, data))
      supabase.from('agent_seniority').select('*')
        .then(({ data }) => seed(QK.seniority, data))
      // agent_exp may not exist until Phase 3 migration is applied — tolerate 404.
      supabase.from('agent_exp').select('*')
        .then(({ data }) => seed(QK.exp, data))
        .catch(() => seed(QK.exp, []))

      // ── Realtime subscriptions ──────────────────────────────────────────
      channel = supabase.channel('pantheon-live')
        .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'trades' },
          ({ new: row }) => qc.setQueryData(QK.trades, prev => [row, ...(prev || [])].slice(0, 300)))
        .on('postgres_changes', { event: 'UPDATE', schema: 'public', table: 'trades' },
          ({ new: row }) => qc.setQueryData(QK.trades, prev => (prev || []).map(t => t.trade_id === row.trade_id ? row : t)))

        .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'decision_traces' },
          ({ new: row }) => qc.setQueryData(QK.traces, prev => [row, ...(prev || [])].slice(0, 300)))
        .on('postgres_changes', { event: 'UPDATE', schema: 'public', table: 'decision_traces' },
          ({ new: row }) => qc.setQueryData(QK.traces, prev => (prev || []).map(t => t.trace_id === row.trace_id ? row : t)))

        .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'equity_snapshots' },
          ({ new: row }) => qc.setQueryData(QK.equity, prev => [...(prev || []), row].slice(-1000)))

        .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'agent_health' },
          ({ new: row }) => qc.setQueryData(QK.agentHealth, prev => upsertById(prev, row, 'agent_name').slice(0, 20)))

        .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'llm_usage' },
          ({ new: row }) => qc.setQueryData(QK.llm, prev => [row, ...(prev || [])].slice(0, 1000)))

        // Seniority promotions — the level-up trigger
        .on('postgres_changes', { event: 'UPDATE', schema: 'public', table: 'agent_seniority' },
          ({ new: row }) => qc.setQueryData(QK.seniority, prev => upsertById(prev, row, 'agent_name')))
        .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'agent_seniority' },
          ({ new: row }) => qc.setQueryData(QK.seniority, prev => upsertById(prev, row, 'agent_name')))

        // EXP pushes (Phase 3)
        .on('postgres_changes', { event: 'UPDATE', schema: 'public', table: 'agent_exp' },
          ({ new: row }) => qc.setQueryData(QK.exp, prev => upsertById(prev, row, 'agent_name')))
        .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'agent_exp' },
          ({ new: row }) => qc.setQueryData(QK.exp, prev => upsertById(prev, row, 'agent_name')))

        .subscribe(status => setConnected(status === 'SUBSCRIBED'))
    })

    return () => { cancelled = true; channel?.unsubscribe() }
  }, [enabled, qc])

  return { connected, enabled }
}
