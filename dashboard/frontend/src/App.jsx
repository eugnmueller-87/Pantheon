// Thin shell: providers → socket → computed metrics → TopBar/TabBar → animated
// tab views. The 663-line monolith was decomposed into lib/hooks/components/
// panels/views (Phase 1 of the dashboard plan).
import { useState, useMemo } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import './theme/tokens.css'
import { usePipelineSocket } from './hooks/usePipelineSocket'
import { useSupabaseRealtime } from './hooks/useSupabaseRealtime'
import { TopBar, TabBar } from './panels/TopBar'
import { LiveView } from './views/LiveView'
import { PantheonView } from './views/PantheonView'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false } },
})

const START_EQUITY = 4000 // matches settings.json starting_equity

const TABS = [
  { id: 'live',      label: 'LIVE',      icon: '📡' },
  { id: 'pantheon',  label: 'PANTHEON',  icon: '🏛️' },
  { id: 'portfolio', label: 'PORTFOLIO', icon: '📈' },
  { id: 'cost',      label: 'COST & HEALTH', icon: '⚙️' },
]

function ComingSoon({ label }) {
  return (
    <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-mute)', fontSize: 13, letterSpacing: 2 }}>
      {label} — coming in a later phase
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>
  )
}

function Dashboard() {
  const [tab, setTab] = useState('live')
  const socket = usePipelineSocket()
  // Wake the Supabase realtime subscriptions (feeds the query cache used by
  // history/EXP panels). Must run inside QueryClientProvider.
  useSupabaseRealtime()
  const { status, connected, send, signalEvents, tradeEvents, killEvents } = socket

  const metrics = useMemo(() => {
    const drawdown = status?.drawdown_pct ?? 0
    const ddColor = drawdown < 3 ? 'var(--green)' : drawdown < 6 ? 'var(--yellow)' : 'var(--red)'
    const equity = Number(status?.equity ?? START_EQUITY)
    const pnlPct = (equity - START_EQUITY) / START_EQUITY * 100
    const totalSig = tradeEvents.length + killEvents.length
    return {
      drawdown, ddColor, equity,
      pnlPct, pnlColor: pnlPct >= 0 ? 'var(--green)' : 'var(--red)',
      pipeStatus: status?.pipeline_status || 'UNKNOWN',
      signalCount: signalEvents.length,
      tradeCount: tradeEvents.length,
      killCount: killEvents.length,
      approvalPct: totalSig > 0 ? tradeEvents.length / totalSig * 100 : 0,
      killPct: totalSig > 0 ? killEvents.length / totalSig * 100 : 0,
    }
  }, [status, signalEvents, tradeEvents, killEvents])

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', height: '100vh', overflow: 'hidden',
      background: 'var(--bg)', fontFamily: 'var(--font-mono)', color: 'var(--text)',
    }}>
      <TopBar status={status} connected={connected} metrics={metrics} send={send} />
      <TabBar tabs={TABS} activeTab={tab} onTab={setTab} />

      <AnimatePresence mode="wait">
        <motion.div
          key={tab}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -6 }}
          transition={{ duration: 0.18 }}
          style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
        >
          {tab === 'live'      && <LiveView socket={socket} metrics={metrics} />}
          {tab === 'pantheon'  && <PantheonView />}
          {tab === 'portfolio' && <ComingSoon label="📈 PORTFOLIO depth" />}
          {tab === 'cost'      && <ComingSoon label="⚙️ COST & HEALTH" />}
        </motion.div>
      </AnimatePresence>

      <div style={{
        textAlign: 'center', fontSize: 7, color: 'var(--border)', padding: '3px 0',
        borderTop: '1px solid var(--panel)', letterSpacing: 2, flexShrink: 0,
      }}>
        PANTHEON OS · ZEUS · ICARUS · ARES · ARGUS · ARTEMIS · PYTHIA · HADES · APOLLO  ·  Claude Haiku · ChromaDB · IBKR
      </div>
    </div>
  )
}
