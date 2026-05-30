import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import {
  AreaChart, Area, BarChart, Bar, ComposedChart, Line,
  XAxis, YAxis, Tooltip, ResponsiveContainer,
  Cell, PieChart, Pie, ReferenceLine,
} from 'recharts'

const WS_URL = import.meta.env.VITE_WS_URL ||
  (window.location.hostname === 'localhost'
    ? 'ws://localhost:8081/ws'
    : 'wss://moremanamoreproblems.de/ws')

const AGENTS = [
  { id: 'icarus',  label: 'ICARUS',  sub: 'Signal Watcher',   icon: '🦅' },
  { id: 'hades',   label: 'HADES',   sub: 'Compliance',       icon: '⚖️' },
  { id: 'artemis', label: 'ARTEMIS', sub: 'Macro Context',    icon: '🌙' },
  { id: 'pythia',  label: 'PYTHIA',  sub: 'Pattern & Size',   icon: '🔮' },
  { id: 'zeus',    label: 'ZEUS',    sub: 'LLM Director',     icon: '⚡' },
  { id: 'ares',    label: 'ARES',    sub: 'Execution',        icon: '⚔️' },
  { id: 'argus',   label: 'ARGUS',   sub: 'Portfolio Guard',  icon: '👁️' },
]

const TYPE_CFG = {
  trade_placed:      { label: 'TRADE',  color: '#48bb78', bg: '#071510' },
  signal_killed:     { label: 'KILL',   color: '#fc8181', bg: '#150505' },
  icarus_signal:     { label: 'SIGNAL', color: '#63b3ed', bg: '#040d18' },
  pipeline_start:    { label: 'START',  color: '#4a5568', bg: '#0d1117' },
  pipeline_complete: { label: 'DONE',   color: '#68d391', bg: '#0d1117' },
  halt:              { label: 'HALT',   color: '#fc8181', bg: '#150505' },
  resume:            { label: 'RESUME', color: '#48bb78', bg: '#071510' },
  error:             { label: 'ERROR',  color: '#fc8181', bg: '#150505' },
}

// Allocation donut colours
const ALLOC_COLORS = ['#63b3ed', '#9f7aea', '#68d391', '#f6ad55', '#fc8181', '#76e4f7']

function fmt(evt) {
  switch (evt.type) {
    case 'trade_placed':      return `${evt.side?.toUpperCase()} ${evt.symbol}  €${evt.fill ?? '—'}  [${((evt.confidence||0)*100).toFixed(0)}% conf]`
    case 'signal_killed':     return `${evt.supplier ?? ''}  killed @ ${evt.stage?.toUpperCase()} — ${evt.reason}`
    case 'icarus_signal':     return `${evt.supplier}  ${evt.category}  ${evt.severity}  ${(evt.headline||'').slice(0,70)}`
    case 'pipeline_complete': return `Done — ${evt.runs} run(s), ${evt.trades} trade(s), ${evt.kills} kill(s)`
    case 'error':             return `Error: ${evt.message}`
    default:                  return evt.message || evt.type
  }
}

// ── Reusable primitives ────────────────────────────────────────────────────────

const P = ({ style, children }) => (
  <div style={{ background: '#0b0f1a', border: '1px solid #1a2540', borderRadius: 6, ...style }}>
    {children}
  </div>
)

const PH = ({ children }) => (
  <div style={{ fontSize: 8, color: '#3a4a6a', letterSpacing: 2, fontWeight: 700, marginBottom: 10 }}>
    {children}
  </div>
)

function KpiRing({ label, value, pct, color }) {
  const r = 26
  const circ = 2 * Math.PI * r
  const dash = circ * Math.min(pct / 100, 1)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
      <svg width={68} height={68} style={{ transform: 'rotate(-90deg)' }}>
        <circle cx={34} cy={34} r={r} fill="none" stroke="#1a2540" strokeWidth={6} />
        <circle cx={34} cy={34} r={r} fill="none" stroke={color} strokeWidth={6}
          strokeDasharray={`${dash} ${circ}`} strokeLinecap="round"
          style={{ transition: 'stroke-dasharray 1s ease' }} />
        <text x={34} y={34} textAnchor="middle" dominantBaseline="central"
          fill={color} fontSize={13} fontWeight={700}
          style={{ transform: 'rotate(90deg)', transformOrigin: '34px 34px' }}>
          {Math.round(pct)}%
        </text>
      </svg>
      <div style={{ fontSize: 9, color: '#4a5568', letterSpacing: 1, textAlign: 'center' }}>{label}</div>
      <div style={{ fontSize: 11, fontWeight: 700, color: '#e2e8f0' }}>{value}</div>
    </div>
  )
}

function StatRow({ label, value, pct, color }) {
  return (
    <div style={{ marginBottom: 7 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
        <span style={{ fontSize: 9, color: '#4a5568' }}>{label}</span>
        <span style={{ fontSize: 9, color, fontWeight: 700 }}>{value}</span>
      </div>
      <div style={{ height: 4, background: '#1a2540', borderRadius: 2 }}>
        <div style={{ height: '100%', width: `${Math.min(pct, 100)}%`, background: color, borderRadius: 2, transition: 'width 1s ease' }} />
      </div>
    </div>
  )
}

// ── Candlestick via Recharts ComposedChart ─────────────────────────────────────
// We simulate OHLC from equity stream — good enough for demo / paper trading
function CandleChart({ data }) {
  if (!data || data.length < 2) {
    return <div style={{ color: '#2d3748', fontSize: 11, textAlign: 'center', paddingTop: 40 }}>Collecting equity data…</div>
  }
  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 4, right: 12, bottom: 0, left: 0 }}>
        <XAxis dataKey="t" tick={{ fontSize: 8, fill: '#3a4a6a' }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
        <YAxis tick={{ fontSize: 8, fill: '#3a4a6a' }} axisLine={false} tickLine={false}
          tickFormatter={v => `€${(v / 1000).toFixed(1)}k`} width={44} domain={['auto', 'auto']} />
        <Tooltip
          contentStyle={{ background: '#0b0f1a', border: '1px solid #1a2540', borderRadius: 4, fontSize: 10 }}
          formatter={(v, n) => [`€${Number(v).toLocaleString('de-DE')}`, n]}
        />
        <ReferenceLine y={data[0]?.eq} stroke="#1a2540" strokeDasharray="4 4" />
        <defs>
          <linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%"  stopColor="#63b3ed" stopOpacity={0.25} />
            <stop offset="95%" stopColor="#63b3ed" stopOpacity={0} />
          </linearGradient>
        </defs>
        <Area type="monotone" dataKey="eq" stroke="#63b3ed" strokeWidth={2}
          fill="url(#eg)" dot={false} name="Equity" />
        <Line type="monotone" dataKey="ma" stroke="#f6ad55" strokeWidth={1.5}
          dot={false} strokeDasharray="3 3" name="MA10" />
      </ComposedChart>
    </ResponsiveContainer>
  )
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [events, setEvents]       = useState([])
  const [status, setStatus]       = useState(null)
  const [agents, setAgents]       = useState([])
  const [connected, setConnected] = useState(false)
  const [activeStage, setActive]  = useState(null)
  const [killStage, setKill]      = useState(null)
  const [reasoning, setReasoning] = useState('')
  const [displayed, setDisplayed] = useState('')
  const [charIdx, setCharIdx]     = useState(0)
  const ws      = useRef(null)
  const reconnect = useRef(null)

  const connect = useCallback(() => {
    if (ws.current?.readyState === WebSocket.OPEN) return
    const sock = new WebSocket(WS_URL)
    ws.current = sock
    sock.onopen  = () => { setConnected(true); clearTimeout(reconnect.current) }
    sock.onclose = () => { setConnected(false); reconnect.current = setTimeout(connect, 3000) }
    sock.onerror = () => sock.close()
    sock.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data)
        if (ev.type === 'status_update') { setStatus(ev); return }
        if (ev.type === 'agent_health')  { setAgents(ev.agents || []); return }
        setEvents(prev => [ev, ...prev].slice(0, 500))
        if (ev.type === 'icarus_signal')     { setActive('icarus'); setKill(null) }
        if (ev.type === 'signal_killed')     { setActive(null); setKill(ev.stage?.replace('trend','artemis').replace('pattern','pythia')) }
        if (ev.type === 'trade_placed')      { setActive('ares'); setKill(null); if (ev.reasoning) setReasoning(ev.reasoning) }
        if (ev.type === 'pipeline_complete') setTimeout(() => setActive(null), 2000)
      } catch (_) {}
    }
  }, [])

  useEffect(() => { connect(); return () => ws.current?.close() }, [connect])

  // Typewriter
  useEffect(() => { setDisplayed(''); setCharIdx(0) }, [reasoning])
  useEffect(() => {
    if (charIdx >= reasoning.length) return
    const t = setTimeout(() => { setDisplayed(reasoning.slice(0, charIdx + 1)); setCharIdx(c => c + 1) }, 14)
    return () => clearTimeout(t)
  }, [charIdx, reasoning])

  const send = (action) => ws.current?.readyState === WebSocket.OPEN && ws.current.send(JSON.stringify({ action }))

  // Derived data
  const agentMap = {}
  agents.forEach(a => { agentMap[a.name] = a.status })

  const drawdown   = status?.drawdown_pct ?? 0
  const ddColor    = drawdown < 3 ? '#48bb78' : drawdown < 6 ? '#f6e05e' : '#fc8181'
  const pipeStatus = status?.pipeline_status || 'UNKNOWN'
  const equity     = Number(status?.equity ?? 4000)
  const startEquity = 4000

  const tradeEvents   = events.filter(e => e.type === 'trade_placed')
  const killEvents    = events.filter(e => e.type === 'signal_killed')
  const signalEvents  = events.filter(e => e.type === 'icarus_signal')
  const visibleEvents = events.filter(e => TYPE_CFG[e.type])

  const latestSignal = signalEvents[0]
  const latestTrade  = tradeEvents[0]
  const latestKill   = killEvents[0]

  // Equity + MA10 chart data
  const chartData = useMemo(() => {
    const pts = events
      .filter(e => e.type === 'status_update' && e.equity)
      .reverse()
      .slice(-80)
      .map(e => ({ t: new Date(e.timestamp).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }), eq: Number(e.equity) }))
    // compute MA10
    return pts.map((p, i) => {
      const window = pts.slice(Math.max(0, i - 9), i + 1)
      const ma = window.reduce((s, w) => s + w.eq, 0) / window.length
      return { ...p, ma: +ma.toFixed(2) }
    })
  }, [events])

  // Signal type breakdown for bar chart
  const signalTypeCounts = useMemo(() => {
    const counts = {}
    signalEvents.forEach(e => {
      const t = e.category || 'other'
      counts[t] = (counts[t] || 0) + 1
    })
    return Object.entries(counts).map(([name, value]) => ({ name: name.slice(0, 10), value })).slice(0, 6)
  }, [signalEvents])

  // Kill stage breakdown
  const killStageCounts = useMemo(() => {
    const counts = {}
    killEvents.forEach(e => {
      const s = e.stage || 'unknown'
      counts[s] = (counts[s] || 0) + 1
    })
    return Object.entries(counts).map(([name, value]) => ({ name, value }))
  }, [killEvents])

  // Allocation donut — positions from status
  const allocData = useMemo(() => {
    const pos = status?.open_positions ?? 0
    if (pos === 0) return [{ name: 'Cash', value: 100 }]
    const posSize = Math.min(pos * 3, 80)
    return [
      { name: 'Cash',      value: 100 - posSize },
      { name: 'Positions', value: posSize },
    ]
  }, [status])

  // PnL pct
  const pnlPct   = ((equity - startEquity) / startEquity * 100)
  const pnlColor = pnlPct >= 0 ? '#48bb78' : '#fc8181'

  // Signal ratio
  const totalSig    = tradeEvents.length + killEvents.length
  const approvalPct = totalSig > 0 ? (tradeEvents.length / totalSig * 100) : 0
  const killPct     = totalSig > 0 ? (killEvents.length / totalSig * 100) : 0

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', height: '100vh', overflow: 'hidden',
      background: '#060a12', fontFamily: "'Courier New', monospace", color: '#e2e8f0',
    }}>
      <style>{`
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 4px; } ::-webkit-scrollbar-track { background: #0b0f1a; } ::-webkit-scrollbar-thumb { background: #1a2540; border-radius: 2px; }
        @keyframes blink   { 0%,100%{opacity:1} 50%{opacity:0} }
        @keyframes pulse   { 0%,100%{box-shadow:0 0 4px #63b3ed33} 50%{box-shadow:0 0 14px #63b3ed88} }
        @keyframes pulsered{ 0%,100%{box-shadow:0 0 4px #fc818133} 50%{box-shadow:0 0 14px #fc818188} }
        @keyframes scan    { 0%{background-position:0 0} 100%{background-position:0 100%} }
        button:hover { filter: brightness(1.15); }
      `}</style>

      {/* ── TOP BAR ────────────────────────────────────────────────────────── */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '7px 16px', background: '#0b0f1a',
        borderBottom: '1px solid #1a2540', flexShrink: 0, gap: 8,
      }}>
        {/* Logo */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 180 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: '#63b3ed', letterSpacing: 3 }}>⚡ PANTHEON OS</span>
          <span style={{
            fontSize: 9, padding: '2px 6px', borderRadius: 2,
            background: status?.paper_trading ? '#0d1f36' : '#3d0808',
            border: `1px solid ${status?.paper_trading ? '#2d4a6e' : '#7a1515'}`,
            color: status?.paper_trading ? '#63b3ed' : '#fc8181', letterSpacing: 1,
          }}>
            {status?.paper_trading ? 'PAPER' : '⚠ LIVE'}
          </span>
          <span style={{ width: 7, height: 7, borderRadius: '50%', background: connected ? '#48bb78' : '#fc8181', display: 'inline-block', flexShrink: 0 }} />
          <span style={{ fontSize: 9, color: connected ? '#48bb78' : '#fc8181', letterSpacing: 1 }}>{connected ? 'LIVE' : 'RECONNECTING'}</span>
        </div>

        {/* KPIs */}
        <div style={{ display: 'flex', gap: 0, flex: 1, justifyContent: 'center' }}>
          {[
            { label: 'EQUITY',    value: `€${equity.toLocaleString('de-DE')}`,       color: '#e2e8f0' },
            { label: 'P&L',       value: `${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%`, color: pnlColor },
            { label: 'DRAWDOWN',  value: `${drawdown.toFixed(2)}%`,                  color: ddColor },
            { label: 'POSITIONS', value: status?.open_positions ?? '—',              color: '#63b3ed' },
            { label: 'SIGNALS',   value: signalEvents.length,                        color: '#9f7aea' },
            { label: 'TRADES',    value: tradeEvents.length,                         color: '#48bb78' },
            { label: 'KILLS',     value: killEvents.length,                          color: '#fc8181' },
            { label: 'STATUS',    value: pipeStatus,                                 color: pipeStatus === 'RUNNING' ? '#48bb78' : '#fc8181' },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ textAlign: 'center', padding: '0 14px', borderRight: '1px solid #1a2540' }}>
              <div style={{ fontSize: 7, color: '#3a4a6a', letterSpacing: 1, marginBottom: 2 }}>{label}</div>
              <div style={{ fontSize: 13, fontWeight: 700, color, fontVariantNumeric: 'tabular-nums' }}>{value}</div>
            </div>
          ))}
        </div>

        {/* Controls */}
        <div style={{ display: 'flex', gap: 6, minWidth: 180, justifyContent: 'flex-end' }}>
          <button onClick={() => send('run_pipeline')} style={btnStyle('#0d1f36', '#2d4a6e', '#63b3ed')}>▶ RUN</button>
          {pipeStatus === 'RUNNING'
            ? <button onClick={() => send('halt')}   style={btnStyle('#150505', '#7a1515', '#fc8181')}>■ HALT</button>
            : <button onClick={() => send('resume')} style={btnStyle('#071510', '#1a4731', '#48bb78')}>▶ RESUME</button>
          }
        </div>
      </div>

      {/* ── BODY — 4-column grid ───────────────────────────────────────────── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '180px 1fr 1fr 240px',
        gridTemplateRows: '1fr 1fr',
        gap: 6, padding: 6, flex: 1, minHeight: 0, overflow: 'hidden',
      }}>

        {/* COL 1 ROW 1+2 — Pipeline (spans both rows) */}
        <P style={{ gridRow: '1 / 3', padding: '12px 10px', display: 'flex', flexDirection: 'column' }}>
          <PH>PIPELINE</PH>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
            {AGENTS.map((ag, i) => {
              const isActive = activeStage === ag.id
              const isKill   = killStage === ag.id
              const health   = agentMap[ag.id]
              const border   = isKill ? '#fc8181' : isActive ? '#63b3ed' : health === 'healthy' ? '#1a3a28' : health === 'failed' ? '#742a2a' : '#1a2540'
              const bg       = isKill ? '#150505' : isActive ? '#071525' : '#0f1420'
              return (
                <React.Fragment key={ag.id}>
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '7px 8px', borderRadius: 4, border: `1px solid ${border}`,
                    background: bg, transition: 'all 0.3s',
                    animation: isActive ? 'pulse 1.2s infinite' : isKill ? 'pulsered 1.2s infinite' : 'none',
                  }}>
                    <span style={{ fontSize: 12, flexShrink: 0 }}>{ag.icon}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: 0.5, color: isActive ? '#63b3ed' : isKill ? '#fc8181' : '#c0cce0' }}>{ag.label}</div>
                      <div style={{ fontSize: 7, color: '#3a4a6a', marginTop: 1 }}>{ag.sub}</div>
                    </div>
                    <div style={{ width: 5, height: 5, borderRadius: '50%', flexShrink: 0, background: health === 'healthy' ? '#48bb78' : health === 'failed' ? '#fc8181' : '#2d3748' }} />
                  </div>
                  {i < AGENTS.length - 1 && (
                    <div style={{ textAlign: 'center', color: '#1a2540', fontSize: 10, lineHeight: '6px' }}>↓</div>
                  )}
                </React.Fragment>
              )
            })}
          </div>
          {/* Apollo — separate research agent */}
          <div style={{ borderTop: '1px dashed #1a2540', marginTop: 8, paddingTop: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '7px 8px', borderRadius: 4, border: '1px solid #2d1f50', background: '#0d0a1e' }}>
              <span style={{ fontSize: 12 }}>📚</span>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 9, fontWeight: 700, color: '#9f7aea' }}>APOLLO</div>
                <div style={{ fontSize: 7, color: '#3a4a6a' }}>Research · daily</div>
              </div>
              <div style={{ width: 5, height: 5, borderRadius: '50%', background: agentMap['apollo'] === 'healthy' ? '#48bb78' : '#2d3748' }} />
            </div>
          </div>
          {/* Seniority level */}
          {status?.seniority && (
            <div style={{ marginTop: 8, padding: '6px 8px', borderRadius: 4, background: '#0b0f1a', border: '1px solid #1a2540' }}>
              <div style={{ fontSize: 7, color: '#3a4a6a', letterSpacing: 1, marginBottom: 4 }}>SYSTEM LEVEL</div>
              <div style={{ fontSize: 11, fontWeight: 700, color: '#9f7aea' }}>{status.seniority.system_level}</div>
              <div style={{ fontSize: 7, color: '#3a4a6a', marginTop: 2 }}>
                MAX POS: {((status.seniority.max_position_pct || 0) * 100).toFixed(0)}% · LIVE: {status.seniority.live_trading_allowed ? '✓' : '✗'}
              </div>
            </div>
          )}
        </P>

        {/* COL 2 ROW 1 — Candlestick / Equity chart */}
        <P style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8 }}>
            <PH>EQUITY CURVE  ·  MA10</PH>
            <div style={{ display: 'flex', gap: 10 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <div style={{ width: 16, height: 2, background: '#63b3ed' }} />
                <span style={{ fontSize: 8, color: '#3a4a6a' }}>EQUITY</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <div style={{ width: 16, height: 2, background: '#f6ad55', borderTop: '1px dashed #f6ad55' }} />
                <span style={{ fontSize: 8, color: '#3a4a6a' }}>MA10</span>
              </div>
            </div>
          </div>
          <div style={{ flex: 1, minHeight: 0 }}>
            <CandleChart data={chartData} />
          </div>
        </P>

        {/* COL 3 ROW 1 — Signal breakdown + Kill analysis */}
        <P style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          <PH>SIGNAL ANALYSIS</PH>

          {/* Signal type bar chart */}
          <div style={{ flex: 1, minHeight: 0 }}>
            <div style={{ fontSize: 8, color: '#3a4a6a', letterSpacing: 1, marginBottom: 6 }}>BY CATEGORY</div>
            {signalTypeCounts.length > 0 ? (
              <ResponsiveContainer width="100%" height={90}>
                <BarChart data={signalTypeCounts} layout="vertical" margin={{ top: 0, right: 8, bottom: 0, left: 0 }}>
                  <XAxis type="number" tick={{ fontSize: 7, fill: '#3a4a6a' }} axisLine={false} tickLine={false} />
                  <YAxis type="category" dataKey="name" tick={{ fontSize: 8, fill: '#4a5568' }} axisLine={false} tickLine={false} width={60} />
                  <Tooltip contentStyle={{ background: '#0b0f1a', border: '1px solid #1a2540', fontSize: 9 }} />
                  <Bar dataKey="value" radius={[0, 3, 3, 0]}>
                    {signalTypeCounts.map((_, i) => <Cell key={i} fill={ALLOC_COLORS[i % ALLOC_COLORS.length]} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div style={{ color: '#2d3748', fontSize: 10, padding: '10px 0' }}>No signal data yet</div>
            )}
          </div>

          {/* Kill stage breakdown */}
          <div>
            <div style={{ fontSize: 8, color: '#3a4a6a', letterSpacing: 1, marginBottom: 6 }}>KILL STAGE BREAKDOWN</div>
            {killStageCounts.length > 0 ? killStageCounts.map(({ name, value }) => (
              <StatRow key={name} label={name.toUpperCase()} value={value}
                pct={killEvents.length > 0 ? value / killEvents.length * 100 : 0}
                color="#fc8181" />
            )) : (
              <div style={{ color: '#2d3748', fontSize: 10 }}>No kills yet</div>
            )}
          </div>

          {/* Approval rate ring */}
          <div style={{ display: 'flex', gap: 16, justifyContent: 'center', paddingTop: 4, borderTop: '1px solid #1a2540' }}>
            <KpiRing label="APPROVAL" value={`${tradeEvents.length}`} pct={approvalPct} color="#48bb78" />
            <KpiRing label="KILL RATE" value={`${killEvents.length}`} pct={killPct} color="#fc8181" />
            <KpiRing label="DRAWDOWN" value={`${drawdown.toFixed(1)}%`} pct={drawdown / 8 * 100} color={ddColor} />
          </div>
        </P>

        {/* COL 4 ROW 1 — Portfolio + Allocation donut */}
        <P style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          <PH>PORTFOLIO</PH>

          {/* Big equity number */}
          <div style={{ textAlign: 'center', padding: '4px 0' }}>
            <div style={{ fontSize: 22, fontWeight: 700, color: '#e2e8f0', fontVariantNumeric: 'tabular-nums' }}>
              €{equity.toLocaleString('de-DE')}
            </div>
            <div style={{ fontSize: 10, color: pnlColor, marginTop: 2 }}>
              {pnlPct >= 0 ? '▲' : '▼'} {Math.abs(pnlPct).toFixed(3)}%  vs start
            </div>
          </div>

          {/* Allocation donut */}
          <div style={{ display: 'flex', justifyContent: 'center' }}>
            <div style={{ position: 'relative', width: 110, height: 110 }}>
              <PieChart width={110} height={110}>
                <Pie data={allocData} cx={55} cy={55} innerRadius={30} outerRadius={50}
                  dataKey="value" stroke="none" startAngle={90} endAngle={-270}>
                  {allocData.map((_, i) => <Cell key={i} fill={ALLOC_COLORS[i]} />)}
                </Pie>
              </PieChart>
              <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-50%)', textAlign: 'center' }}>
                <div style={{ fontSize: 9, fontWeight: 700, color: '#e2e8f0' }}>{status?.open_positions ?? 0}</div>
                <div style={{ fontSize: 7, color: '#3a4a6a' }}>POS</div>
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 5, paddingLeft: 4 }}>
              {allocData.map((d, i) => (
                <div key={d.name} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                  <div style={{ width: 8, height: 8, borderRadius: 2, background: ALLOC_COLORS[i], flexShrink: 0 }} />
                  <span style={{ fontSize: 9, color: '#4a5568' }}>{d.name}</span>
                  <span style={{ fontSize: 9, color: '#e2e8f0', fontWeight: 700 }}>{d.value.toFixed(0)}%</span>
                </div>
              ))}
            </div>
          </div>

          {/* Drawdown meter */}
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 8, color: '#3a4a6a', letterSpacing: 1 }}>DRAWDOWN</span>
              <span style={{ fontSize: 9, color: ddColor, fontWeight: 700 }}>{drawdown.toFixed(2)}% / 8%</span>
            </div>
            <div style={{ height: 6, background: '#1a2540', borderRadius: 3, overflow: 'hidden', position: 'relative' }}>
              <div style={{ height: '100%', width: `${Math.min(drawdown / 8, 1) * 100}%`, background: ddColor, borderRadius: 3, transition: 'width 0.8s ease' }} />
            </div>
          </div>

          {/* Circuit breakers */}
          <div>
            <div style={{ fontSize: 8, color: '#3a4a6a', letterSpacing: 1, marginBottom: 5 }}>CIRCUIT BREAKERS</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 10px' }}>
              {status?.circuit_breakers && Object.keys(status.circuit_breakers).length > 0
                ? Object.entries(status.circuit_breakers).map(([ag, st]) => (
                    <div key={ag} style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                      <div style={{ width: 5, height: 5, borderRadius: '50%', background: st === 'closed' ? '#48bb78' : st === 'open' ? '#fc8181' : '#f6e05e' }} />
                      <span style={{ fontSize: 8, color: '#4a5568' }}>{ag}</span>
                    </div>
                  ))
                : <span style={{ fontSize: 9, color: '#2d3748' }}>All clear</span>
              }
            </div>
          </div>
        </P>

        {/* COL 2 ROW 2 — Live feed */}
        <P style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8 }}>
            <PH>LIVE FEED</PH>
            <span style={{ fontSize: 8, color: '#3a4a6a' }}>{visibleEvents.length} events</span>
          </div>
          <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 2 }}>
            {visibleEvents.length === 0 && (
              <div style={{ color: '#2d3748', fontSize: 11, textAlign: 'center', padding: 20 }}>
                Waiting for signals… Hit ▶ RUN
              </div>
            )}
            {visibleEvents.map(ev => {
              const cfg  = TYPE_CFG[ev.type]
              const time = new Date(ev.timestamp).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
              return (
                <div key={ev.id} style={{
                  display: 'flex', alignItems: 'baseline', gap: 6,
                  padding: '4px 7px', borderRadius: 3,
                  border: `1px solid ${cfg.color}20`, background: cfg.bg,
                  fontSize: 10, flexShrink: 0,
                }}>
                  <span style={{ color: '#3a4a6a', flexShrink: 0, fontSize: 8, fontVariantNumeric: 'tabular-nums', minWidth: 52 }}>{time}</span>
                  <span style={{ flexShrink: 0, fontSize: 7, border: `1px solid ${cfg.color}44`, borderRadius: 2, padding: '0 3px', color: cfg.color, letterSpacing: 0.5, minWidth: 34, textAlign: 'center' }}>{cfg.label}</span>
                  <span style={{ color: '#8090a8', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{fmt(ev)}</span>
                </div>
              )
            })}
          </div>
        </P>

        {/* COL 3 ROW 2 — Zeus reasoning */}
        <P style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 8, overflow: 'hidden' }}>
          <PH>⚡ ZEUS DIRECTOR · REASONING</PH>

          {/* Latest signal */}
          {latestSignal && (
            <div style={{ background: '#040d18', border: '1px solid #1a2540', borderRadius: 4, padding: '7px 9px', flexShrink: 0 }}>
              <div style={{ fontSize: 7, color: '#3a4a6a', letterSpacing: 1, marginBottom: 3 }}>LATEST SIGNAL</div>
              <div style={{ fontSize: 9, color: '#63b3ed', marginBottom: 3 }}>
                {latestSignal.supplier} · {latestSignal.category} · sev {latestSignal.severity}
              </div>
              <div style={{ fontSize: 9, color: '#8090a8', lineHeight: 1.5 }}>{(latestSignal.headline || '').slice(0, 100)}</div>
              {latestSignal.tickers?.length > 0 && (
                <div style={{ display: 'flex', gap: 4, marginTop: 5, flexWrap: 'wrap' }}>
                  {latestSignal.tickers.map(t => (
                    <span key={t} style={{ fontSize: 9, padding: '1px 5px', borderRadius: 3, background: '#071525', border: '1px solid #1a3a5a', color: '#63b3ed' }}>{t}</span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Reasoning typewriter */}
          <div style={{ flex: 1, overflowY: 'auto', background: '#040d18', border: '1px solid #1a2540', borderRadius: 4, padding: '7px 9px' }}>
            <div style={{ fontSize: 7, color: '#3a4a6a', letterSpacing: 1, marginBottom: 4 }}>DECISION REASONING</div>
            {displayed
              ? <div style={{ fontSize: 9, color: '#68d391', lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>
                  {displayed}<span style={{ animation: 'blink 1s step-end infinite' }}>▋</span>
                </div>
              : <div style={{ fontSize: 10, color: '#2d3748' }}>No reasoning yet — run a pipeline cycle.</div>
            }
          </div>

          {/* Latest decision */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, flexShrink: 0 }}>
            {latestTrade && (
              <div style={{ padding: '5px 8px', borderRadius: 4, border: '1px solid #48bb7833', background: '#071510', fontSize: 9 }}>
                <span style={{ color: '#48bb78', fontWeight: 700 }}>✓ APPROVED  </span>
                <span style={{ color: '#8090a8' }}>{latestTrade.side?.toUpperCase()} {latestTrade.symbol} @ €{latestTrade.fill ?? '?'}  ·  {((latestTrade.confidence || 0) * 100).toFixed(0)}% conf</span>
              </div>
            )}
            {latestKill && (
              <div style={{ padding: '5px 8px', borderRadius: 4, border: '1px solid #fc818133', background: '#150505', fontSize: 9 }}>
                <span style={{ color: '#fc8181', fontWeight: 700 }}>✗ KILLED  </span>
                <span style={{ color: '#8090a8' }}>{latestKill.stage?.toUpperCase()} — {(latestKill.reason || '').slice(0, 80)}</span>
              </div>
            )}
          </div>
        </P>

        {/* COL 4 ROW 2 — Performance table */}
        <P style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 8, overflowY: 'auto' }}>
          <PH>PERFORMANCE</PH>

          {/* Session stats */}
          <div>
            <div style={{ fontSize: 8, color: '#3a4a6a', letterSpacing: 1, marginBottom: 6 }}>SESSION STATS</div>
            {[
              { label: 'Signals processed', value: signalEvents.length, color: '#9f7aea' },
              { label: 'Trades placed',      value: tradeEvents.length,  color: '#48bb78' },
              { label: 'Signals killed',     value: killEvents.length,   color: '#fc8181' },
              { label: 'Approval rate',      value: `${approvalPct.toFixed(0)}%`, color: '#68d391' },
            ].map(({ label, value, color }) => (
              <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #1a2540' }}>
                <span style={{ fontSize: 9, color: '#4a5568' }}>{label}</span>
                <span style={{ fontSize: 9, fontWeight: 700, color }}>{value}</span>
              </div>
            ))}
          </div>

          {/* Agent health table */}
          <div>
            <div style={{ fontSize: 8, color: '#3a4a6a', letterSpacing: 1, marginBottom: 6 }}>AGENT HEALTH</div>
            {[...AGENTS, { id: 'apollo', label: 'APOLLO' }].map(ag => {
              const h = agentMap[ag.id]
              const color = h === 'healthy' ? '#48bb78' : h === 'failed' ? '#fc8181' : '#2d3748'
              return (
                <div key={ag.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '3px 0', borderBottom: '1px solid #0f1420' }}>
                  <span style={{ fontSize: 9, color: '#4a5568' }}>{ag.label}</span>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                    <div style={{ width: 5, height: 5, borderRadius: '50%', background: color }} />
                    <span style={{ fontSize: 8, color }}>{h || 'unknown'}</span>
                  </div>
                </div>
              )
            })}
          </div>

          {/* Last trade card */}
          {latestTrade && (
            <div style={{ padding: '8px', background: '#071510', border: '1px solid #1a3a28', borderRadius: 4 }}>
              <div style={{ fontSize: 8, color: '#3a4a6a', letterSpacing: 1, marginBottom: 5 }}>LAST TRADE</div>
              <div style={{ fontSize: 13, fontWeight: 700, color: '#48bb78' }}>
                {latestTrade.side?.toUpperCase()} {latestTrade.symbol}
              </div>
              <div style={{ fontSize: 9, color: '#4a5568', marginTop: 2 }}>
                Fill: €{latestTrade.fill ?? '—'} · Conf: {((latestTrade.confidence || 0) * 100).toFixed(0)}%
              </div>
              <div style={{ fontSize: 8, color: '#3a4a6a', marginTop: 2 }}>
                {new Date(latestTrade.timestamp).toLocaleString('de-DE')}
              </div>
            </div>
          )}
        </P>

      </div>

      {/* ── FOOTER ─────────────────────────────────────────────────────────── */}
      <div style={{
        textAlign: 'center', fontSize: 7, color: '#1a2540', padding: '3px 0',
        borderTop: '1px solid #0b0f1a', letterSpacing: 2, flexShrink: 0,
      }}>
        PANTHEON OS · ZEUS · ICARUS · ARES · ARGUS · ARTEMIS · PYTHIA · HADES · APOLLO  ·  Claude Haiku · ChromaDB · Kafka · IBKR
      </div>
    </div>
  )
}

const btnStyle = (bg, border, color) => ({
  fontSize: 9, padding: '5px 11px', cursor: 'pointer',
  background: bg, border: `1px solid ${border}`, color,
  borderRadius: 3, letterSpacing: 1, fontFamily: 'inherit', fontWeight: 700,
})
