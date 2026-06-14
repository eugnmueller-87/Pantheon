// The gamified hero tab: 8 agent EXP cards, system-level banner with
// live-trading clearance, promotion timeline, and a level-up toast triggered
// by Supabase realtime seniority updates.
import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { AgentCard } from '../components/AgentCard'
import { Panel, PanelHeader } from '../components/ui/Panel'
import { ALL_AGENTS, RANK_META } from '../lib/constants'
import { useExp, useSeniority } from '../hooks/useSupabaseData'

function byAgent(rows) {
  const m = {}
  ;(rows || []).forEach(r => { if (r?.agent_name) m[r.agent_name] = r })
  return m
}

function SystemBanner({ seniorityRows }) {
  const levels = (seniorityRows || []).map(r => r.level_int ?? 0)
  const systemInt = levels.length ? Math.min(...levels) : 0
  const rank = RANK_META[systemInt] || RANK_META[0]
  const liveAllowed = systemInt >= 1 // PRINCIPAL+ — mirrors seniority.live_trading_allowed
  const maxPos = systemInt >= 1 ? 5 : 3
  return (
    <Panel style={{ padding: '14px 18px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderColor: `${rank.color}55`, boxShadow: `0 0 16px ${rank.color}22` }}>
      <div>
        <div style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 2 }}>SYSTEM LEVEL</div>
        <div style={{ fontSize: 20, fontWeight: 700, color: rank.color, letterSpacing: 1 }}>{rank.label}</div>
        <div style={{ fontSize: 8, color: 'var(--text-faint)', marginTop: 2 }}>floor across all 8 agents · max position {maxPos}%</div>
      </div>
      <motion.div
        animate={liveAllowed ? { boxShadow: ['0 0 0 #48bb7800', '0 0 18px #48bb78', '0 0 6px #48bb7855'] } : {}}
        transition={{ duration: 1.5, repeat: liveAllowed ? Infinity : 0, repeatType: 'reverse' }}
        style={{
          padding: '8px 14px', borderRadius: 6, fontSize: 11, fontWeight: 700, letterSpacing: 1,
          color: liveAllowed ? 'var(--green)' : 'var(--red)',
          background: liveAllowed ? '#071510' : '#150505',
          border: `1px solid ${liveAllowed ? '#1a4731' : '#7a1515'}`,
        }}>
        {liveAllowed ? '🔓 LIVE ENABLED' : '🔒 PAPER ONLY'}
      </motion.div>
    </Panel>
  )
}

function PromotionToast({ event, onDone }) {
  useEffect(() => {
    if (!event) return
    const t = setTimeout(onDone, 5000)
    return () => clearTimeout(t)
  }, [event, onDone])
  return (
    <AnimatePresence>
      {event && (
        <motion.div
          initial={{ opacity: 0, scale: 0.8, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.8, y: 20 }}
          style={{
            position: 'fixed', bottom: 30, left: '50%', transform: 'translateX(-50%)', zIndex: 50,
            padding: '14px 26px', borderRadius: 10, textAlign: 'center',
            background: 'var(--panel)', border: `2px solid ${event.color}`, boxShadow: `0 0 30px ${event.color}`,
          }}>
          <div style={{ fontSize: 9, color: 'var(--text-ghost)', letterSpacing: 3 }}>🏛️ PANTHEON PROMOTION</div>
          <div style={{ fontSize: 15, fontWeight: 700, color: event.color, marginTop: 4 }}>
            {event.agent.toUpperCase()} → {event.rank}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

export function PantheonView() {
  const expRows = useExp()
  const seniorityRows = useSeniority()
  const expMap = byAgent(expRows)
  const senMap = byAgent(seniorityRows)

  // Detect promotions from seniority realtime updates → fire toast + card glow.
  const prevLevels = useRef({})
  const [toast, setToast] = useState(null)
  const [promotedAgent, setPromotedAgent] = useState(null)
  useEffect(() => {
    const prev = prevLevels.current
    for (const r of seniorityRows || []) {
      const old = prev[r.agent_name]
      if (old !== undefined && (r.level_int ?? 0) > old) {
        const rank = RANK_META[r.level_int] || RANK_META[0]
        setToast({ agent: r.agent_name, rank: rank.label, color: rank.color })
        setPromotedAgent(r.agent_name)
        setTimeout(() => setPromotedAgent(null), 4000)
      }
      prev[r.agent_name] = r.level_int ?? 0
    }
  }, [seniorityRows])

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: 10, display: 'flex', flexDirection: 'column', gap: 10 }}>
      <SystemBanner seniorityRows={seniorityRows} />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10 }}>
        {ALL_AGENTS.map(agent => (
          <AgentCard
            key={agent.id}
            agent={agent}
            expRow={expMap[agent.id]}
            seniorityRow={senMap[agent.id]}
            justPromoted={promotedAgent === agent.id}
          />
        ))}
      </div>

      <Panel style={{ padding: '12px 14px' }}>
        <PanelHeader>PROMOTION TIMELINE</PanelHeader>
        <PromotionTimeline seniorityRows={seniorityRows} />
      </Panel>

      <PromotionToast event={toast} onDone={() => setToast(null)} />
    </div>
  )
}

function PromotionTimeline({ seniorityRows }) {
  // Best-effort from current seniority (full history would come from
  // get_promotion_history; this shows current standings as a fallback).
  const sorted = [...(seniorityRows || [])].sort((a, b) => (b.level_int ?? 0) - (a.level_int ?? 0))
  if (sorted.length === 0) {
    return <div style={{ fontSize: 10, color: 'var(--text-mute)' }}>No seniority data yet.</div>
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {sorted.map(r => {
        const rank = RANK_META[r.level_int] || RANK_META[0]
        return (
          <div key={r.agent_name} style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 10 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: rank.color, flexShrink: 0 }} />
            <span style={{ color: 'var(--text-dim)', minWidth: 70 }}>{r.agent_name?.toUpperCase()}</span>
            <span style={{ color: rank.color, fontWeight: 700 }}>{rank.label}</span>
            {r.cleared && <span style={{ fontSize: 8, color: 'var(--green)' }}>cleared</span>}
          </div>
        )
      })}
    </div>
  )
}
