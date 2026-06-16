// LOGS — the pipeline decision trail from decision_traces: every signal the
// system evaluated, which STOCK it was about, how far it got, where it died,
// and ZEUS's reasoning. Answers "which stock are they trying to trade?".
// Light-themed; rendered under the CLASSIC sidebar's "Logs" entry.
import { useMemo, useState } from 'react'
import '../theme/classic-light.css'
import { Card } from '../components/classic/ClassicUI'
import { useTraces } from '../hooks/useSupabaseData'

// Where a signal ended up → label + light tone. null killed_at_stage + trade
// placed = executed; null + not placed = passed but not filled.
const STAGE = {
  hades:         { label: 'Compliance',    fg: 'var(--c-red)',    bg: 'var(--c-red-soft)' },
  trend:         { label: 'Macro',         fg: 'var(--c-amber)',  bg: 'var(--c-amber-soft)' },
  pattern:       { label: 'Pattern',       fg: 'var(--c-amber)',  bg: 'var(--c-amber-soft)' },
  concentration: { label: 'Concentration', fg: 'var(--c-purple)', bg: 'var(--c-purple-soft)' },
  zeus:          { label: 'ZEUS veto',     fg: 'var(--c-red)',    bg: 'var(--c-red-soft)' },
}
const EXECUTED = { label: 'Executed', fg: 'var(--c-green)', bg: 'var(--c-green-soft)' }
const PASSED   = { label: 'Passed',   fg: 'var(--c-blue)',  bg: 'var(--c-blue-soft)' }

function outcome(t) {
  if (t.trade_placed) return EXECUTED
  if (t.killed_at_stage) return STAGE[t.killed_at_stage] || { label: t.killed_at_stage, fg: 'var(--c-text-dim)', bg: '#f1f3f7' }
  return PASSED
}

const FILTERS = ['all', 'executed', 'vetoed', 'concentration']

function LogRow({ t }) {
  const [open, setOpen] = useState(false)
  const o = outcome(t)
  const stock = t.symbol?.trim() || (t.headline ? '—' : '—')
  const when = t.timestamp ? new Date(t.timestamp).toLocaleString('de-DE', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : ''
  const conf = t.pattern_confidence != null ? `${(t.pattern_confidence * 100).toFixed(0)}%` : '—'
  const reason = t.kill_reason || (t.trade_placed ? 'Order placed' : 'Approved')

  return (
    <div style={{ borderBottom: '1px solid var(--c-border)' }}>
      <button onClick={() => setOpen(v => !v)} style={{
        display: 'grid', gridTemplateColumns: '120px 64px 1fr 92px 18px', gap: 10, alignItems: 'center',
        width: '100%', textAlign: 'left', background: 'transparent', border: 'none', cursor: 'pointer',
        fontFamily: 'inherit', padding: '10px 4px', color: 'var(--c-text)',
      }}>
        <span style={{ fontSize: 11, color: 'var(--c-text-faint)', whiteSpace: 'nowrap' }}>{when}</span>
        <span style={{ fontSize: 13, fontWeight: 700 }}>{stock}</span>
        <span style={{ fontSize: 12, color: 'var(--c-text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={t.headline}>
          {t.headline || reason}
        </span>
        <span style={{ fontSize: 11, fontWeight: 600, color: o.fg, background: o.bg, padding: '3px 8px', borderRadius: 20, textAlign: 'center', justifySelf: 'start' }}>{o.label}</span>
        <span style={{ fontSize: 11, color: 'var(--c-text-faint)', justifySelf: 'center' }}>{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div style={{ padding: '4px 8px 14px 130px', display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', fontSize: 11, color: 'var(--c-text-dim)' }}>
            <span>Confidence: <b>{conf}</b></span>
            <span>Category: <b>{t.category || '—'}</b></span>
            <span>Severity: <b>{t.severity || '—'}</b></span>
            <span>Regime: <b>{t.trend_regime || '—'}</b></span>
            <span>VIX: <b>{t.trend_vix != null ? t.trend_vix.toFixed(1) : '—'}</b></span>
            {t.side && <span>Side: <b>{t.side}</b></span>}
            {t.fill_price != null && <span>Fill: <b>€{t.fill_price}</b></span>}
          </div>
          {t.kill_reason && (
            <div style={{ fontSize: 12 }}>
              <span style={{ color: 'var(--c-text-faint)' }}>Killed: </span>
              <span style={{ color: o.fg, fontWeight: 600 }}>{t.kill_reason}</span>
            </div>
          )}
          {t.zeus_reasoning && (
            <div style={{ fontSize: 12, color: 'var(--c-text-dim)', lineHeight: 1.55, background: 'var(--c-bg)', borderRadius: 8, padding: '10px 12px', whiteSpace: 'pre-wrap' }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--c-text-faint)', marginBottom: 5, letterSpacing: 0.5 }}>⚡ ZEUS REASONING</div>
              {t.zeus_reasoning}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function LogsView() {
  const traces = useTraces()
  const [filter, setFilter] = useState('all')

  const rows = useMemo(() => {
    const list = traces || []
    switch (filter) {
      case 'executed':      return list.filter(t => t.trade_placed)
      case 'vetoed':        return list.filter(t => t.killed_at_stage === 'zeus')
      case 'concentration': return list.filter(t => t.killed_at_stage === 'concentration')
      default:              return list
    }
  }, [traces, filter])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card
        title="Decision Logs"
        sub={`${rows.length} signal${rows.length === 1 ? '' : 's'} — which stock the pipeline tried & where it ended`}
        action={
          <div style={{ display: 'flex', gap: 6 }}>
            {FILTERS.map(f => (
              <button key={f} onClick={() => setFilter(f)} style={{
                fontSize: 11, fontWeight: 600, textTransform: 'capitalize', cursor: 'pointer',
                padding: '5px 11px', borderRadius: 20, border: 'none', fontFamily: 'inherit',
                color: filter === f ? 'var(--c-green)' : 'var(--c-text-dim)',
                background: filter === f ? 'var(--c-green-soft)' : '#f1f3f7',
              }}>{f}</button>
            ))}
          </div>
        }
      >
        {rows.length === 0 ? (
          <div style={{ padding: 30, textAlign: 'center', color: 'var(--c-text-faint)', fontSize: 13 }}>
            No decisions logged yet.
          </div>
        ) : (
          <div className="c-scroll" style={{ maxHeight: 640, overflowY: 'auto' }}>
            <div style={{
              display: 'grid', gridTemplateColumns: '120px 64px 1fr 92px 18px', gap: 10,
              fontSize: 10, fontWeight: 600, color: 'var(--c-text-faint)', letterSpacing: 0.5,
              padding: '0 4px 8px', borderBottom: '1px solid var(--c-border)',
            }}>
              <span>TIME</span><span>STOCK</span><span>HEADLINE</span><span>OUTCOME</span><span />
            </div>
            {rows.map(t => <LogRow key={t.trace_id} t={t} />)}
          </div>
        )}
      </Card>
    </div>
  )
}
