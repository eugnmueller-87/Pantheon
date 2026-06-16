// AGENTS — light-themed agent stats reusing the Pantheon seniority/exp/health
// data in the SH-Stark card style. One card per agent: rank, level, EXP bar,
// win/loss, streak, live-trading clearance, and health dot. Rendered inside the
// CLASSIC view's "Agents" page (and also reachable as its own top-level tab).
import { useMemo } from 'react'
import '../theme/classic-light.css'
import { ALL_AGENTS, SENIOR_TIER_INT } from '../lib/constants'
import { deriveExp } from '../lib/exp'
import { useExp, useSeniority, useAgentHealthDb } from '../hooks/useSupabaseData'

// Map dark-terminal rank colors → light palette so badges read on white.
const RANK_LIGHT = {
  0: { label: 'TRAINEE',      fg: '#6b7280', bg: '#f1f3f7' },
  1: { label: 'JUNIOR',       fg: '#2563eb', bg: 'var(--c-blue-soft)' },
  2: { label: 'INTERMEDIATE', fg: '#7c3aed', bg: 'var(--c-purple-soft)' },
  3: { label: 'SENIOR',       fg: '#16a34a', bg: 'var(--c-green-soft)' },
}

function byAgent(rows) {
  const m = {}
  ;(rows || []).forEach(r => { if (r?.agent_name) m[r.agent_name] = r })
  return m
}

function ExpBarLight({ pct, color }) {
  return (
    <div style={{ height: 7, borderRadius: 4, background: '#eef0f6', overflow: 'hidden' }}>
      <div style={{ height: '100%', width: `${Math.round(pct * 100)}%`, background: color, borderRadius: 4, transition: 'width .4s ease' }} />
    </div>
  )
}

const HEALTH = {
  ok:       { fg: 'var(--c-green)', label: 'healthy' },
  healthy:  { fg: 'var(--c-green)', label: 'healthy' },
  degraded: { fg: 'var(--c-amber)', label: 'degraded' },
  down:     { fg: 'var(--c-red)',   label: 'down' },
  error:    { fg: 'var(--c-red)',   label: 'error' },
}

function AgentCardLight({ agent, expRow, seniorityRow, health }) {
  const exp = deriveExp({ ...expRow, seniority_level_int: seniorityRow?.level_int ?? expRow?.seniority_level_int })
  const rank = RANK_LIGHT[exp.tierInt] || RANK_LIGHT[0]
  const tierLevel = seniorityRow?.tier_level ?? null
  const live = seniorityRow?.live_trading_allowed === true
  const h = HEALTH[health?.status] || { fg: 'var(--c-text-faint)', label: health?.status || 'unknown' }
  const total = exp.wins + exp.losses
  const winPct = total ? Math.round((exp.wins / total) * 100) : 0

  return (
    <div className="c-card" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
        <div style={{ width: 42, height: 42, borderRadius: '50%', background: rank.bg, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 21, flexShrink: 0 }}>{agent.icon}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--c-text)' }}>{agent.label}</div>
          <div style={{ fontSize: 11, color: 'var(--c-text-faint)' }}>{agent.sub}</div>
        </div>
        <span title={`health: ${h.label}`} style={{ width: 9, height: 9, borderRadius: '50%', background: h.fg, flexShrink: 0 }} />
      </div>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: rank.fg, background: rank.bg, padding: '3px 9px', borderRadius: 20 }}>
          {rank.label}{tierLevel ? ` · L${tierLevel}` : ''}
        </span>
        {live
          ? <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--c-green)' }}>🔓 live</span>
          : <span style={{ fontSize: 11, color: 'var(--c-text-faint)' }}>📝 paper</span>}
      </div>

      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--c-text-faint)', marginBottom: 4 }}>
          <span>Level {exp.level}</span>
          <span>{exp.xpInto}/{exp.xpToNext} XP</span>
        </div>
        <ExpBarLight pct={exp.fill} color={rank.fg} />
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--c-text-dim)', borderTop: '1px solid var(--c-border)', paddingTop: 10 }}>
        <span><b style={{ color: 'var(--c-green)' }}>{exp.wins}</b>W · <b style={{ color: 'var(--c-red)' }}>{exp.losses}</b>L</span>
        <span>{winPct}% win</span>
        <span>🔥 {exp.streak}</span>
      </div>
    </div>
  )
}

function SystemSummary({ seniorityRows }) {
  const rows = seniorityRows || []
  const tiers = rows.map(r => r.level_int ?? 0)
  const systemInt = tiers.length ? Math.min(...tiers) : 0
  const rank = RANK_LIGHT[systemInt] || RANK_LIGHT[0]
  const liveAll = rows.length > 0 && rows.every(r => r.live_trading_allowed === true)
  const unlocked = systemInt >= SENIOR_TIER_INT
  const badge = liveAll
    ? { text: '🔓 LIVE — REAL MONEY', fg: 'var(--c-green)', bg: 'var(--c-green-soft)' }
    : unlocked
      ? { text: '⚠️ REAL UNLOCKED · DISARMED', fg: 'var(--c-amber)', bg: 'var(--c-amber-soft)' }
      : { text: '📝 PAPER ONLY', fg: 'var(--c-text-dim)', bg: '#f1f3f7' }
  return (
    <div className="c-card" style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
      <div>
        <div style={{ fontSize: 11, color: 'var(--c-text-faint)', fontWeight: 600, letterSpacing: 0.5 }}>SYSTEM RANK</div>
        <div style={{ fontSize: 22, fontWeight: 700, color: rank.fg }}>{rank.label}</div>
        <div style={{ fontSize: 11, color: 'var(--c-text-faint)', marginTop: 2 }}>floor across all {rows.length || 8} agents</div>
      </div>
      <span style={{ fontSize: 13, fontWeight: 700, color: badge.fg, background: badge.bg, padding: '8px 14px', borderRadius: 22 }}>{badge.text}</span>
    </div>
  )
}

export function AgentsView() {
  const expRows = useExp()
  const seniorityRows = useSeniority()
  const healthRows = useAgentHealthDb()
  const expMap = useMemo(() => byAgent(expRows), [expRows])
  const senMap = useMemo(() => byAgent(seniorityRows), [seniorityRows])
  const healthMap = useMemo(() => byAgent(healthRows), [healthRows])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <SystemSummary seniorityRows={seniorityRows} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 14 }}>
        {ALL_AGENTS.map(a => (
          <AgentCardLight
            key={a.id}
            agent={a}
            expRow={expMap[a.id]}
            seniorityRow={senMap[a.id]}
            health={healthMap[a.id]}
          />
        ))}
      </div>
    </div>
  )
}
