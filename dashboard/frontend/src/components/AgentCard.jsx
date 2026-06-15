// One agent's RPG card: rank-glow avatar, rank ribbon, animated EXP bar, and a
// flip to the "what's needed to level up" quest log.
import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ExpBar } from './ui/ExpBar'
import { RANK_META } from '../lib/constants'
import { deriveExp } from '../lib/exp'

function QuestLog({ criteria, notes }) {
  // criteria: object of {name: bool} for the rung currently attempted.
  const entries = criteria && typeof criteria === 'object' ? Object.entries(criteria) : []
  if (entries.length === 0) {
    return (
      <div style={{ fontSize: 9, color: 'var(--text-faint)', lineHeight: 1.6 }}>
        All current-rung criteria met — awaiting promotion eval.
      </div>
    )
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      {entries.map(([name, met]) => (
        <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 9 }}>
          <span style={{ color: met ? 'var(--green)' : 'var(--text-mute)', flexShrink: 0 }}>{met ? '✓' : '✗'}</span>
          <span style={{ color: met ? 'var(--text-dim)' : 'var(--text-faint)' }}>{name.replace(/_/g, ' ')}</span>
        </div>
      ))}
      {Array.isArray(notes) && notes.length > 0 && (
        <div style={{ marginTop: 4, fontSize: 8, color: 'var(--text-ghost)', lineHeight: 1.5 }}>
          {notes.slice(0, 3).join(' · ')}
        </div>
      )}
    </div>
  )
}

export function AgentCard({ agent, expRow, seniorityRow, justPromoted }) {
  const [flipped, setFlipped] = useState(false)
  const exp = deriveExp({ ...expRow, seniority_level_int: seniorityRow?.level_int ?? expRow?.seniority_level_int })
  const rank = RANK_META[exp.tierInt] || RANK_META[0]
  const color = rank.color
  // Within-tier level (1..10) for the ribbon, from the authoritative seniority row.
  const tierLevel = seniorityRow?.tier_level ?? null
  const ribbonLabel = tierLevel ? `${rank.label} L${tierLevel}` : rank.label

  return (
    <motion.div
      onClick={() => setFlipped(f => !f)}
      animate={justPromoted ? { boxShadow: [`0 0 0px ${color}00`, `0 0 26px ${color}`, `0 0 8px ${color}55`] } : {}}
      transition={{ duration: 1.2, repeat: justPromoted ? 2 : 0 }}
      style={{
        position: 'relative', cursor: 'pointer', minHeight: 168,
        background: 'var(--panel)', border: `1px solid ${color}55`, borderRadius: 8,
        padding: 12, display: 'flex', flexDirection: 'column', gap: 9,
        boxShadow: `0 0 8px ${color}22`,
      }}
    >
      {/* rank ribbon */}
      <div style={{
        position: 'absolute', top: 0, right: 0, fontSize: 7, fontWeight: 700, letterSpacing: 1,
        color, background: `${color}1a`, border: `1px solid ${color}44`,
        borderRadius: '0 8px 0 8px', padding: '2px 7px',
      }}>{ribbonLabel}</div>

      {/* avatar + name */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
        <div style={{
          width: 40, height: 40, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 20, background: 'var(--panel-2)', border: `2px solid ${color}`, boxShadow: `0 0 10px ${color}66`, flexShrink: 0,
        }}>{agent.icon}</div>
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text)', letterSpacing: 1 }}>{agent.label}</div>
          <div style={{ fontSize: 8, color: 'var(--text-ghost)' }}>{agent.sub}</div>
        </div>
      </div>

      <AnimatePresence mode="wait">
        {!flipped ? (
          <motion.div key="front" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} style={{ display: 'flex', flexDirection: 'column', gap: 8, flex: 1 }}>
            <ExpBar fillPct={exp.fill} level={exp.level} xpInto={exp.xpInto} xpToNext={exp.xpToNext} color={color} />
            {exp.realMoney.active && (
              <div>
                <div style={{ fontSize: 7, color: '#f6ad55', letterSpacing: 1, marginBottom: 2 }}>💰 REAL MONEY</div>
                <ExpBar fillPct={exp.realMoney.fill} level={exp.realMoney.level} color="#f6ad55" />
              </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 8, color: 'var(--text-faint)', marginTop: 'auto' }}>
              <span>W {exp.wins} · L {exp.losses}</span>
              <span>🔥 {exp.streak}</span>
              <span>{exp.totalXp.toLocaleString('de-DE')} XP</span>
            </div>
          </motion.div>
        ) : (
          <motion.div key="back" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} style={{ flex: 1 }}>
            <div style={{ fontSize: 7, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 6 }}>NEXT RANK — REQUIREMENTS</div>
            <QuestLog criteria={seniorityRow?.criteria} notes={seniorityRow?.notes} />
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
