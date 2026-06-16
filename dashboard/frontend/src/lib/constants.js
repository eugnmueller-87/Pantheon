// Shared constants for the ZEUS dashboard.

export const WS_URL =
  import.meta.env.VITE_WS_URL ||
  (window.location.hostname === 'localhost'
    ? 'ws://localhost:8081/ws'
    : 'wss://moremanamoreproblems.de/ws')

// The 7 sequential pipeline agents (Apollo is rendered separately as research).
export const AGENTS = [
  { id: 'icarus',  label: 'ICARUS',  sub: 'Signal Watcher',  icon: '🦅' },
  { id: 'hades',   label: 'HADES',   sub: 'Compliance',      icon: '⚖️' },
  { id: 'artemis', label: 'ARTEMIS', sub: 'Macro Context',   icon: '🌙' },
  { id: 'pythia',  label: 'PYTHIA',  sub: 'Pattern & Size',  icon: '🔮' },
  { id: 'zeus',    label: 'ZEUS',    sub: 'LLM Director',     icon: '⚡' },
  { id: 'ares',    label: 'ARES',    sub: 'Execution',       icon: '⚔️' },
  { id: 'argus',   label: 'ARGUS',   sub: 'Portfolio Guard', icon: '👁️' },
]

// All 8 agents incl. Apollo — used by EXP/health views.
export const ALL_AGENTS = [
  ...AGENTS,
  { id: 'apollo', label: 'APOLLO', sub: 'Research · daily', icon: '📚' },
]

// Light theme: dark text on soft pastel row backgrounds (was neon-on-near-black).
export const TYPE_CFG = {
  trade_placed:      { label: 'TRADE',  color: '#15803d', bg: '#e7f6ec' },
  signal_killed:     { label: 'KILL',   color: '#dc2626', bg: '#fdeaea' },
  icarus_signal:     { label: 'SIGNAL', color: '#2563eb', bg: '#e8effd' },
  pipeline_start:    { label: 'START',  color: '#6b7280', bg: '#f1f3f7' },
  pipeline_complete: { label: 'DONE',   color: '#15803d', bg: '#eef6f0' },
  halt:              { label: 'HALT',   color: '#dc2626', bg: '#fdeaea' },
  resume:            { label: 'RESUME', color: '#15803d', bg: '#e7f6ec' },
  error:             { label: 'ERROR',  color: '#dc2626', bg: '#fdeaea' },
}

// Allocation / category palette.
export const ALLOC_COLORS = ['#63b3ed', '#9f7aea', '#68d391', '#f6ad55', '#fc8181', '#76e4f7']

// Seniority TIER → display + color (mirrors core/seniority.py Tier).
// Agents earn their way up: Trainee → Junior → Intermediate → Senior.
// Senior unlocks real money (when armed). Keyed by tier_int (0..3).
export const RANK_META = {
  0: { label: 'TRAINEE',      color: '#718096' },
  1: { label: 'JUNIOR',       color: '#63b3ed' },
  2: { label: 'INTERMEDIATE', color: '#9f7aea' },
  3: { label: 'SENIOR',       color: '#48bb78' },
}

// The Senior tier (int 3) is the one that unlocks real-money trading.
export const SENIOR_TIER_INT = 3

export function fmt(evt) {
  switch (evt.type) {
    case 'trade_placed':
      return `${evt.side?.toUpperCase()} ${evt.symbol}  €${evt.fill ?? '—'}  [${((evt.confidence || 0) * 100).toFixed(0)}% conf]`
    case 'signal_killed':
      return `${evt.supplier ?? ''}  killed @ ${evt.stage?.toUpperCase()} — ${evt.reason}`
    case 'icarus_signal':
      return `${evt.supplier}  ${evt.category}  ${evt.severity}  ${(evt.headline || '').slice(0, 70)}`
    case 'pipeline_complete':
      return `Done — ${evt.runs} run(s), ${evt.trades} trade(s), ${evt.kills} kill(s)`
    case 'error':
      return `Error: ${evt.message}`
    default:
      return evt.message || evt.type
  }
}
