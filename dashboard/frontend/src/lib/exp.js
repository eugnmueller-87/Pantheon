// Frontend mirror of core/exp.py's level math, so the PANTHEON tab can derive
// level/fill from total_xp even before the agent_exp rollup columns are filled.
// Keep in sync with core/exp.py.

export const MAX_LEVEL = 50

export function xpForLevel(level) {
  if (level <= 1) return 0
  return Math.round(500 * Math.pow(level, 1.6))
}

export function expLevelUncapped(totalXp) {
  let lvl = 1
  for (let L = 2; L <= MAX_LEVEL; L++) {
    if (totalXp >= xpForLevel(L)) lvl = L
    else break
  }
  return lvl
}

// rank int → [minLevel, maxLevel]
const RANK_BANDS = { 0: [1, 9], 1: [10, 24], 2: [25, 39], 3: [40, 50] }

export function capLevelToRank(rawLevel, seniorityInt) {
  const [lo, hi] = RANK_BANDS[seniorityInt] || [1, MAX_LEVEL]
  return Math.max(lo, Math.min(rawLevel, hi))
}

export function barFill(totalXp, displayedLevel) {
  if (displayedLevel >= MAX_LEVEL) return 1
  const base = xpForLevel(displayedLevel)
  const next = xpForLevel(displayedLevel + 1)
  const span = next - base
  if (span <= 0) return 0
  return Math.max(0, Math.min(1, (totalXp - base) / span))
}

// Given a raw agent_exp row (or partial), return display fields.
export function deriveExp(row) {
  const totalXp = Number(row?.total_xp ?? 0)
  const seniorityInt = Number(row?.seniority_level_int ?? 0)
  // Prefer stored exp_level; else derive + cap.
  const displayed = row?.exp_level ?? capLevelToRank(expLevelUncapped(totalXp), seniorityInt)
  const base = xpForLevel(displayed)
  const next = xpForLevel(displayed + 1)
  return {
    level: displayed,
    totalXp,
    xpInto: row?.xp_into_level ?? Math.max(0, totalXp - base),
    xpToNext: row?.xp_to_next ?? Math.max(0, next - base),
    fill: barFill(totalXp, displayed),
    progressPct: Number(row?.progress_to_next_pct ?? 0),
    wins: Number(row?.lifetime_wins ?? 0),
    losses: Number(row?.lifetime_losses ?? 0),
    streak: Number(row?.current_win_streak ?? 0),
    seniorityInt,
  }
}
