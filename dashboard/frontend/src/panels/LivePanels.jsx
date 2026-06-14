// The five data panels of the original live grid: Signal Analysis, Portfolio,
// Live Feed, ZEUS Reasoning, Performance. Extracted verbatim from App.jsx.
import { useMemo } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  Cell, PieChart, Pie,
} from 'recharts'
import { Panel, PanelHeader } from '../components/ui/Panel'
import { KpiRing } from '../components/ui/KpiRing'
import { StatRow } from '../components/ui/StatRow'
import { EquityChart } from '../components/charts/EquityChart'
import { AGENTS, ALL_AGENTS, ALLOC_COLORS, TYPE_CFG, fmt } from '../lib/constants'

export function EquityPanel({ chartData, style }) {
  return (
    <Panel style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', ...style }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8 }}>
        <PanelHeader>EQUITY CURVE · MA10</PanelHeader>
        <div style={{ display: 'flex', gap: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <div style={{ width: 16, height: 2, background: 'var(--blue)' }} />
            <span style={{ fontSize: 8, color: 'var(--text-ghost)' }}>EQUITY</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <div style={{ width: 16, height: 2, background: 'var(--amber)', borderTop: '1px dashed var(--amber)' }} />
            <span style={{ fontSize: 8, color: 'var(--text-ghost)' }}>MA10</span>
          </div>
        </div>
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>
        <EquityChart data={chartData} />
      </div>
    </Panel>
  )
}

export function SignalAnalysisPanel({ signalTypeCounts, killStageCounts, killEvents, tradeEvents, drawdown, ddColor, approvalPct, killPct, style }) {
  return (
    <Panel style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 10, ...style }}>
      <PanelHeader>SIGNAL ANALYSIS</PanelHeader>

      <div style={{ flex: 1, minHeight: 0 }}>
        <div style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 6 }}>BY CATEGORY</div>
        {signalTypeCounts.length > 0 ? (
          <ResponsiveContainer width="100%" height={90}>
            <BarChart data={signalTypeCounts} layout="vertical" margin={{ top: 0, right: 8, bottom: 0, left: 0 }}>
              <XAxis type="number" tick={{ fontSize: 7, fill: 'var(--text-ghost)' }} axisLine={false} tickLine={false} />
              <YAxis type="category" dataKey="name" tick={{ fontSize: 8, fill: 'var(--text-faint)' }} axisLine={false} tickLine={false} width={60} />
              <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', fontSize: 9 }} />
              <Bar dataKey="value" radius={[0, 3, 3, 0]}>
                {signalTypeCounts.map((_, i) => <Cell key={i} fill={ALLOC_COLORS[i % ALLOC_COLORS.length]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ) : <div style={{ color: 'var(--text-mute)', fontSize: 10, padding: '10px 0' }}>No signal data yet</div>}
      </div>

      <div>
        <div style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 6 }}>KILL STAGE BREAKDOWN</div>
        {killStageCounts.length > 0 ? killStageCounts.map(({ name, value }) => (
          <StatRow key={name} label={name.toUpperCase()} value={value}
            pct={killEvents.length > 0 ? value / killEvents.length * 100 : 0} color="var(--red)" />
        )) : <div style={{ color: 'var(--text-mute)', fontSize: 10 }}>No kills yet</div>}
      </div>

      <div style={{ display: 'flex', gap: 16, justifyContent: 'center', paddingTop: 4, borderTop: '1px solid var(--border)' }}>
        <KpiRing label="APPROVAL" value={`${tradeEvents.length}`} pct={approvalPct} color="var(--green)" />
        <KpiRing label="KILL RATE" value={`${killEvents.length}`} pct={killPct} color="var(--red)" />
        <KpiRing label="DRAWDOWN" value={`${drawdown.toFixed(1)}%`} pct={drawdown / 8 * 100} color={ddColor} />
      </div>
    </Panel>
  )
}

export function PortfolioPanel({ status, equity, pnlPct, pnlColor, drawdown, ddColor, style }) {
  const allocData = useMemo(() => {
    const pos = status?.open_positions ?? 0
    if (pos === 0) return [{ name: 'Cash', value: 100 }]
    const posSize = Math.min(pos * 3, 80)
    return [{ name: 'Cash', value: 100 - posSize }, { name: 'Positions', value: posSize }]
  }, [status])

  return (
    <Panel style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 10, ...style }}>
      <PanelHeader>PORTFOLIO</PanelHeader>

      <div style={{ textAlign: 'center', padding: '4px 0' }}>
        <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--text)', fontVariantNumeric: 'tabular-nums' }}>€{equity.toLocaleString('de-DE')}</div>
        <div style={{ fontSize: 10, color: pnlColor, marginTop: 2 }}>{pnlPct >= 0 ? '▲' : '▼'} {Math.abs(pnlPct).toFixed(3)}% vs start</div>
      </div>

      <div style={{ display: 'flex', justifyContent: 'center' }}>
        <div style={{ position: 'relative', width: 110, height: 110 }}>
          <PieChart width={110} height={110}>
            <Pie data={allocData} cx={55} cy={55} innerRadius={30} outerRadius={50} dataKey="value" stroke="none" startAngle={90} endAngle={-270}>
              {allocData.map((_, i) => <Cell key={i} fill={ALLOC_COLORS[i]} />)}
            </Pie>
          </PieChart>
          <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-50%)', textAlign: 'center' }}>
            <div style={{ fontSize: 9, fontWeight: 700, color: 'var(--text)' }}>{status?.open_positions ?? 0}</div>
            <div style={{ fontSize: 7, color: 'var(--text-ghost)' }}>POS</div>
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 5, paddingLeft: 4 }}>
          {allocData.map((d, i) => (
            <div key={d.name} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
              <div style={{ width: 8, height: 8, borderRadius: 2, background: ALLOC_COLORS[i], flexShrink: 0 }} />
              <span style={{ fontSize: 9, color: 'var(--text-faint)' }}>{d.name}</span>
              <span style={{ fontSize: 9, color: 'var(--text)', fontWeight: 700 }}>{d.value.toFixed(0)}%</span>
            </div>
          ))}
        </div>
      </div>

      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
          <span style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 1 }}>DRAWDOWN</span>
          <span style={{ fontSize: 9, color: ddColor, fontWeight: 700 }}>{drawdown.toFixed(2)}% / 8%</span>
        </div>
        <div style={{ height: 6, background: 'var(--border)', borderRadius: 3, overflow: 'hidden', position: 'relative' }}>
          <div style={{ height: '100%', width: `${Math.min(drawdown / 8, 1) * 100}%`, background: ddColor, borderRadius: 3, transition: 'width 0.8s ease' }} />
        </div>
      </div>

      <div>
        <div style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 5 }}>CIRCUIT BREAKERS</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 10px' }}>
          {status?.circuit_breakers && Object.keys(status.circuit_breakers).length > 0
            ? Object.entries(status.circuit_breakers).map(([ag, st]) => (
                <div key={ag} style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                  <div style={{ width: 5, height: 5, borderRadius: '50%', background: st === 'closed' ? 'var(--green)' : st === 'open' ? 'var(--red)' : 'var(--yellow)' }} />
                  <span style={{ fontSize: 8, color: 'var(--text-faint)' }}>{ag}</span>
                </div>
              ))
            : <span style={{ fontSize: 9, color: 'var(--text-mute)' }}>All clear</span>}
        </div>
      </div>
    </Panel>
  )
}

export function LiveFeedPanel({ visibleEvents, style }) {
  return (
    <Panel style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', overflow: 'hidden', ...style }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8 }}>
        <PanelHeader>LIVE FEED</PanelHeader>
        <span style={{ fontSize: 8, color: 'var(--text-ghost)' }}>{visibleEvents.length} events</span>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 2 }}>
        {visibleEvents.length === 0 && (
          <div style={{ color: 'var(--text-mute)', fontSize: 11, textAlign: 'center', padding: 20 }}>Waiting for signals… Hit ▶ RUN</div>
        )}
        {visibleEvents.map(ev => {
          const cfg  = TYPE_CFG[ev.type]
          const time = new Date(ev.timestamp).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
          return (
            <div key={ev.id} style={{
              display: 'flex', alignItems: 'baseline', gap: 6, padding: '4px 7px', borderRadius: 3,
              border: `1px solid ${cfg.color}20`, background: cfg.bg, fontSize: 10, flexShrink: 0,
            }}>
              <span style={{ color: 'var(--text-ghost)', flexShrink: 0, fontSize: 8, fontVariantNumeric: 'tabular-nums', minWidth: 52 }}>{time}</span>
              <span style={{ flexShrink: 0, fontSize: 7, border: `1px solid ${cfg.color}44`, borderRadius: 2, padding: '0 3px', color: cfg.color, letterSpacing: 0.5, minWidth: 34, textAlign: 'center' }}>{cfg.label}</span>
              <span style={{ color: 'var(--text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{fmt(ev)}</span>
            </div>
          )
        })}
      </div>
    </Panel>
  )
}

export function ZeusReasoningPanel({ latestSignal, displayed, latestTrade, latestKill, style }) {
  return (
    <Panel style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 8, overflow: 'hidden', ...style }}>
      <PanelHeader>⚡ ZEUS DIRECTOR · REASONING</PanelHeader>

      {latestSignal && (
        <div style={{ background: 'var(--bg-signal)', border: '1px solid var(--border)', borderRadius: 4, padding: '7px 9px', flexShrink: 0 }}>
          <div style={{ fontSize: 7, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 3 }}>LATEST SIGNAL</div>
          <div style={{ fontSize: 9, color: 'var(--blue)', marginBottom: 3 }}>{latestSignal.supplier} · {latestSignal.category} · sev {latestSignal.severity}</div>
          <div style={{ fontSize: 9, color: 'var(--text-dim)', lineHeight: 1.5 }}>{(latestSignal.headline || '').slice(0, 100)}</div>
          {latestSignal.tickers?.length > 0 && (
            <div style={{ display: 'flex', gap: 4, marginTop: 5, flexWrap: 'wrap' }}>
              {latestSignal.tickers.map(t => (
                <span key={t} style={{ fontSize: 9, padding: '1px 5px', borderRadius: 3, background: '#071525', border: '1px solid #1a3a5a', color: 'var(--blue)' }}>{t}</span>
              ))}
            </div>
          )}
        </div>
      )}

      <div style={{ flex: 1, overflowY: 'auto', background: 'var(--bg-signal)', border: '1px solid var(--border)', borderRadius: 4, padding: '7px 9px' }}>
        <div style={{ fontSize: 7, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 4 }}>DECISION REASONING</div>
        {displayed
          ? <div style={{ fontSize: 9, color: 'var(--green-bright)', lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>{displayed}<span style={{ animation: 'blink 1s step-end infinite' }}>▋</span></div>
          : <div style={{ fontSize: 10, color: 'var(--text-mute)' }}>No reasoning yet — run a pipeline cycle.</div>}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, flexShrink: 0 }}>
        {latestTrade && (
          <div style={{ padding: '5px 8px', borderRadius: 4, border: '1px solid #48bb7833', background: 'var(--bg-trade)', fontSize: 9 }}>
            <span style={{ color: 'var(--green)', fontWeight: 700 }}>✓ APPROVED  </span>
            <span style={{ color: 'var(--text-dim)' }}>{latestTrade.side?.toUpperCase()} {latestTrade.symbol} @ €{latestTrade.fill ?? '?'} · {((latestTrade.confidence || 0) * 100).toFixed(0)}% conf</span>
          </div>
        )}
        {latestKill && (
          <div style={{ padding: '5px 8px', borderRadius: 4, border: '1px solid #fc818133', background: 'var(--bg-kill)', fontSize: 9 }}>
            <span style={{ color: 'var(--red)', fontWeight: 700 }}>✗ KILLED  </span>
            <span style={{ color: 'var(--text-dim)' }}>{latestKill.stage?.toUpperCase()} — {(latestKill.reason || '').slice(0, 80)}</span>
          </div>
        )}
      </div>
    </Panel>
  )
}

export function PerformancePanel({ signalEvents, tradeEvents, killEvents, approvalPct, agentMap, latestTrade, style }) {
  return (
    <Panel style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 8, overflowY: 'auto', ...style }}>
      <PanelHeader>PERFORMANCE</PanelHeader>

      <div>
        <div style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 6 }}>SESSION STATS</div>
        {[
          { label: 'Signals processed', value: signalEvents.length, color: 'var(--purple)' },
          { label: 'Trades placed', value: tradeEvents.length, color: 'var(--green)' },
          { label: 'Signals killed', value: killEvents.length, color: 'var(--red)' },
          { label: 'Approval rate', value: `${approvalPct.toFixed(0)}%`, color: 'var(--green-bright)' },
        ].map(({ label, value, color }) => (
          <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid var(--border)' }}>
            <span style={{ fontSize: 9, color: 'var(--text-faint)' }}>{label}</span>
            <span style={{ fontSize: 9, fontWeight: 700, color }}>{value}</span>
          </div>
        ))}
      </div>

      <div>
        <div style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 6 }}>AGENT HEALTH</div>
        {ALL_AGENTS.map(ag => {
          const h = agentMap[ag.id]
          const color = h === 'healthy' ? 'var(--green)' : h === 'failed' ? 'var(--red)' : 'var(--text-mute)'
          return (
            <div key={ag.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '3px 0', borderBottom: '1px solid var(--border-soft)' }}>
              <span style={{ fontSize: 9, color: 'var(--text-faint)' }}>{ag.label}</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <div style={{ width: 5, height: 5, borderRadius: '50%', background: color }} />
                <span style={{ fontSize: 8, color }}>{h || 'unknown'}</span>
              </div>
            </div>
          )
        })}
      </div>

      {latestTrade && (
        <div style={{ padding: '8px', background: 'var(--bg-trade)', border: '1px solid #1a3a28', borderRadius: 4 }}>
          <div style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 5 }}>LAST TRADE</div>
          <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--green)' }}>{latestTrade.side?.toUpperCase()} {latestTrade.symbol}</div>
          <div style={{ fontSize: 9, color: 'var(--text-faint)', marginTop: 2 }}>Fill: €{latestTrade.fill ?? '—'} · Conf: {((latestTrade.confidence || 0) * 100).toFixed(0)}%</div>
          <div style={{ fontSize: 8, color: 'var(--text-ghost)', marginTop: 2 }}>{new Date(latestTrade.timestamp).toLocaleString('de-DE')}</div>
        </div>
      )}
    </Panel>
  )
}

// Re-export so the live view can pull agents list if needed
export { AGENTS }
