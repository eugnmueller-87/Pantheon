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

export const TYPE_CFG = {
  trade_placed:      { label: 'TRADE',  color: '#48bb78', bg: '#071510' },
  signal_killed:     { label: 'KILL',   color: '#fc8181', bg: '#150505' },
  icarus_signal:     { label: 'SIGNAL', color: '#63b3ed', bg: '#040d18' },
  pipeline_start:    { label: 'START',  color: '#4a5568', bg: '#0d1117' },
  pipeline_complete: { label: 'DONE',   color: '#68d391', bg: '#0d1117' },
  halt:              { label: 'HALT',   color: '#fc8181', bg: '#150505' },
  resume:            { label: 'RESUME', color: '#48bb78', bg: '#071510' },
  error:             { label: 'ERROR',  color: '#fc8181', bg: '#150505' },
}

// Allocation / category palette.
export const ALLOC_COLORS = ['#63b3ed', '#9f7aea', '#68d391', '#f6ad55', '#fc8181', '#76e4f7']

// Seniority rank → display + tier color (mirrors core/seniority.py Level).
export const RANK_META = {
  0: { label: 'SENIOR',           color: '#48bb78' },
  1: { label: 'PRINCIPAL',        color: '#63b3ed' },
  2: { label: 'MANAGING DIRECTOR', color: '#9f7aea' },
  3: { label: 'DIRECTOR',         color: '#f6ad55' },
}

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
