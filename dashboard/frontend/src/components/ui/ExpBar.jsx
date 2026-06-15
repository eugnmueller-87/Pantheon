// Animated EXP bar — spring-driven fill that "counts up", shimmer sweep, and a
// level badge. The fill is progress to the next seniority rung; the badge shows
// the current (band-capped) exp_level.
import { motion, useSpring, useTransform } from 'framer-motion'
import { useEffect } from 'react'

export function ExpBar({ fillPct, level, xpInto, xpToNext, color }) {
  const target = Math.max(0, Math.min(fillPct, 1))
  const spring = useSpring(0, { stiffness: 90, damping: 18 })
  const width = useTransform(spring, v => `${v * 100}%`)
  useEffect(() => { spring.set(target) }, [target, spring])

  return (
    <div style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 9, fontWeight: 700, color, letterSpacing: 1 }}>LVL {level}</span>
        <span style={{ fontSize: 8, color: 'var(--text-faint)', fontVariantNumeric: 'tabular-nums' }}>
          {xpToNext === undefined ? '' : xpToNext > 0 ? `${xpInto} / ${xpToNext} XP` : 'MAX'}
        </span>
      </div>
      <div style={{ position: 'relative', height: 10, background: 'var(--border)', borderRadius: 5, overflow: 'hidden' }}>
        <motion.div style={{
          height: '100%', width, borderRadius: 5,
          background: `linear-gradient(90deg, ${color}aa, ${color})`,
          boxShadow: `0 0 8px ${color}99`,
        }} />
        {/* shimmer sweep */}
        <div style={{
          position: 'absolute', inset: 0, borderRadius: 5, pointerEvents: 'none',
          background: 'linear-gradient(90deg, transparent, #ffffff22, transparent)',
          backgroundSize: '200% 100%', animation: 'shimmer 2.4s linear infinite',
        }} />
      </div>
    </div>
  )
}
