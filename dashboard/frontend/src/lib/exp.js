// Frontend mirror of core/exp.py's level math, so the PANTHEON tab can derive
// level/fill from total_xp even before the agent_exp rollup columns are filled.
// Keep in sync with core/exp.py.

// Four paper tiers × 10 levels = 40 levels on the paper climb.
export const MAX_LEVEL = 40

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

// tier int → [minLevel, maxLevel]; each tier owns a 10-level block of 1..40.
const RANK_BANDS = { 0: [1, 10], 1: [11, 20], 2: [21, 30], 3: [31, 40] }

export function capLevelToRank(rawLevel, tierInt) {
  const [lo, hi] = RANK_BANDS[tierInt] || [1, MAX_LEVEL]
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

// Given a raw agent_exp row (or partial), return display fields for BOTH bars:
// the paper climb (band-capped into the tier) and the separate real-money bar.
export function deriveExp(row) {
  const totalXp = Number(row?.total_xp ?? 0)
  // seniority_level_int column carries the TIER ordinal (0..3) — caps the paper level.
  const tierInt = Number(row?.seniority_level_int ?? 0)
  // Prefer stored exp_level; else derive + cap.
  const displayed = row?.exp_level ?? capLevelToRank(expLevelUncapped(totalXp), tierInt)
  const base = xpForLevel(displayed)
  const next = xpForLevel(displayed + 1)

  // Separate real-money track — uncapped, only meaningful once Senior.
  const realXp = Number(row?.real_money_xp ?? 0)
  const realLevel = row?.real_money_level ?? expLevelUncapped(realXp)

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
    tierInt,
    seniorityInt: tierInt,   // back-compat alias
    // Real-money bar (separate track)
    realMoney: {
      xp: realXp,
      level: realLevel,
      fill: barFill(realXp, realLevel),
      wins: Number(row?.real_money_wins ?? 0),
      losses: Number(row?.real_money_losses ?? 0),
      active: realXp > 0,
    },
  }
}
