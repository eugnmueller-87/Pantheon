// Top bar: logo + paper/live + connection, KPI strip, RUN/HALT/RESUME controls,
// and tab navigation. Extracted from App.jsx and extended with tabs.
import { Button } from '../components/ui/Button'

export function TopBar({ status, connected, metrics, send }) {
  const { equity, pnlPct, pnlColor, drawdown, ddColor, pipeStatus, signalCount, tradeCount, killCount } = metrics
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '7px 16px', background: 'var(--panel)',
      borderBottom: '1px solid var(--border)', flexShrink: 0, gap: 8,
    }}>
      {/* Logo + status */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 180 }}>
        <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--blue)', letterSpacing: 3 }}>⚡ PANTHEON OS</span>
        <span style={{
          fontSize: 9, padding: '2px 6px', borderRadius: 2,
          background: status?.paper_trading ? 'var(--bg-signal)' : 'var(--bg-kill)',
          border: `1px solid ${status?.paper_trading ? '#bcd2f7' : '#f3c4c4'}`,
          color: status?.paper_trading ? 'var(--blue)' : 'var(--red)', letterSpacing: 1,
        }}>
          {status?.paper_trading ? 'PAPER' : '⚠ LIVE'}
        </span>
        <span style={{ width: 7, height: 7, borderRadius: '50%', background: connected ? 'var(--green)' : 'var(--red)', display: 'inline-block', flexShrink: 0 }} />
        <span style={{ fontSize: 9, color: connected ? 'var(--green)' : 'var(--red)', letterSpacing: 1 }}>{connected ? 'LIVE' : 'RECONNECTING'}</span>
      </div>

      {/* KPI strip */}
      <div style={{ display: 'flex', gap: 0, flex: 1, justifyContent: 'center' }}>
        {[
          { label: 'EQUITY',    value: `€${equity.toLocaleString('de-DE')}`, color: 'var(--text)' },
          { label: 'P&L',       value: `${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%`, color: pnlColor },
          { label: 'DRAWDOWN',  value: `${drawdown.toFixed(2)}%`, color: ddColor },
          { label: 'POSITIONS', value: status?.open_positions ?? '—', color: 'var(--blue)' },
          { label: 'SIGNALS',   value: signalCount, color: 'var(--purple)' },
          { label: 'TRADES',    value: tradeCount, color: 'var(--green)' },
          { label: 'KILLS',     value: killCount, color: 'var(--red)' },
          { label: 'STATUS',    value: pipeStatus, color: pipeStatus === 'RUNNING' ? 'var(--green)' : 'var(--red)' },
        ].map(({ label, value, color }) => (
          <div key={label} style={{ textAlign: 'center', padding: '0 14px', borderRight: '1px solid var(--border)' }}>
            <div style={{ fontSize: 7, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 2 }}>{label}</div>
            <div style={{ fontSize: 13, fontWeight: 700, color, fontVariantNumeric: 'tabular-nums' }}>{value}</div>
          </div>
        ))}
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', gap: 6, minWidth: 180, justifyContent: 'flex-end' }}>
        <Button bg="var(--bg-signal)" border="#bcd2f7" color="var(--blue)" onClick={() => send('run_pipeline')}>▶ RUN</Button>
        {pipeStatus === 'RUNNING'
          ? <Button bg="var(--bg-kill)" border="#f3c4c4" color="var(--red)" onClick={() => send('halt')}>■ HALT</Button>
          : <Button bg="var(--bg-trade)" border="#bce3cb" color="var(--green)" onClick={() => send('resume')}>▶ RESUME</Button>}
      </div>
    </div>
  )
}

export function TabBar({ tabs, activeTab, onTab }) {
  return (
    <div style={{ display: 'flex', gap: 2, padding: '6px 16px 0', background: 'var(--panel)', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
      {tabs.map(t => {
        const active = t.id === activeTab
        return (
          <button key={t.id} onClick={() => onTab(t.id)} style={{
            fontSize: 10, fontWeight: 700, letterSpacing: 1, cursor: 'pointer',
            padding: '7px 16px', border: 'none', borderBottom: `2px solid ${active ? 'var(--blue)' : 'transparent'}`,
            background: 'transparent', color: active ? 'var(--blue)' : 'var(--text-faint)',
            fontFamily: 'inherit',
          }}>
            {t.icon} {t.label}
          </button>
        )
      })}
    </div>
  )
}
