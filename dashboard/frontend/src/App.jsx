// Thin shell: providers → socket → computed metrics → TopBar/TabBar → animated
// tab views. The 663-line monolith was decomposed into lib/hooks/components/
// panels/views (Phase 1 of the dashboard plan).
import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import './theme/tokens.css'
import { usePipelineSocket } from './hooks/usePipelineSocket'
import { useSupabaseRealtime } from './hooks/useSupabaseRealtime'
import { useMetrics } from './hooks/useMetrics'
import { TopBar, TabBar } from './panels/TopBar'
import { LiveView } from './views/LiveView'
import { PantheonView } from './views/PantheonView'
import { PortfolioView } from './views/PortfolioView'
import { CostHealthView } from './views/CostHealthView'
import { ClassicView } from './views/ClassicView'
import { AgentsView } from './views/AgentsView'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false } },
})

const TABS = [
  { id: 'live',      label: 'LIVE',      icon: '📡' },
  { id: 'classic',   label: 'CLASSIC',   icon: '📊' },
  { id: 'pantheon',  label: 'PANTHEON',  icon: '🏛️' },
  { id: 'agents',    label: 'AGENTS',    icon: '🤖' },
  { id: 'portfolio', label: 'PORTFOLIO', icon: '📈' },
  { id: 'cost',      label: 'COST & HEALTH', icon: '⚙️' },
]

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>
  )
}

function Dashboard() {
  const [tab, setTab] = useState('classic')
  const socket = usePipelineSocket()
  // Wake the Supabase realtime subscriptions (feeds the query cache used by
  // history/EXP panels). Must run inside QueryClientProvider.
  useSupabaseRealtime()
  const { status, connected, send } = socket

  // Single source of truth — every tab reads these SAME numbers (from Supabase,
  // with the live WS status as the freshest equity/drawdown override). Replaces
  // the old WS-session-only derivation that disagreed with the Supabase tabs.
  const metrics = useMetrics(status)

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
          {tab === 'classic'   && <ClassicView status={status} metrics={metrics} />}
          {tab === 'pantheon'  && <PantheonView />}
          {tab === 'agents'    && (
            <div className="classic-light c-scroll" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '18px 22px' }}>
              <AgentsView />
            </div>
          )}
          {tab === 'portfolio' && <PortfolioView metrics={metrics} />}
          {tab === 'cost'      && <CostHealthView />}
        </motion.div>
      </AnimatePresence>

      <div style={{
        textAlign: 'center', fontSize: 7, color: 'var(--text-ghost)', padding: '3px 0',
        borderTop: '1px solid var(--border)', letterSpacing: 2, flexShrink: 0,
      }}>
        PANTHEON OS · ZEUS · ICARUS · ARES · ARGUS · ARTEMIS · PYTHIA · HADES · APOLLO  ·  Claude Haiku · ChromaDB · IBKR
      </div>
    </div>
  )
}
