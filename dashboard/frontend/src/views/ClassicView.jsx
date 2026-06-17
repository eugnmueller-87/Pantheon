// CLASSIC — a light, card-based trading dashboard modeled on
// SH-Stark/trading-dashboard, but wired to real ZEUS data (€ equity, real
// symbols/positions/trades) instead of the crypto/USDT mocks. Has its own left
// rail with Dashboard · Order History · Agents (the extra agent-stats button).
import { useMemo, useState } from 'react'
import {
  AreaChart, Area, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, Tooltip, ResponsiveContainer,
} from 'recharts'
import '../theme/classic-light.css'
import { Card, KpiTile, Sidebar } from '../components/classic/ClassicUI'
import { AgentsView } from './AgentsView'
import { LogsView } from './LogsView'
import { useTrades, useEquitySeries, usePositions } from '../hooks/useSupabaseData'

const eur = (n, d = 2) =>
  `€${Number(n || 0).toLocaleString('de-DE', { minimumFractionDigits: d, maximumFractionDigits: d })}`

const PIE = i => `var(--c-pie-${i % 12})`

// ── Derived data from real Supabase rows ──────────────────────────────────────
// Shared metrics (equity, profit, unrealized, win-rate, estimated flag) come
// from the single useMetrics() source so this tab CANNOT disagree with the
// others. Only view-specific shapes (equity area, pie, performers, feed,
// most-traded) are computed locally here.
function useClassicData(metrics) {
  const trades = useTrades()
  const equity = useEquitySeries()
  const dbPositions = usePositions()

  return useMemo(() => {
    // Defensive: never let a transient undefined metrics blank the whole app.
    const m = metrics || {}
    const eqVal = m.equity ?? 0
    const positions = m.openPositions || []

    // Equity area + per-snapshot delta bars (Balance line / Income bars in ref).
    const series = (equity || []).slice(-60).map(r => ({
      t: new Date(r.recorded_at).toLocaleDateString('de-DE', { day: '2-digit', month: 'short' }),
      balance: Number(r.total_equity ?? r.equity ?? 0),
    }))
    const withIncome = series.map((p, i) => ({
      ...p,
      income: i === 0 ? 0 : +(p.balance - series[i - 1].balance).toFixed(2),
    }))

    // Shared realized-€ figures (one formula, one source — see useMetrics).
    const profitToday = m.realizedToday ?? 0
    const profit7d = m.realized7d ?? 0
    const pct7d = eqVal ? (profit7d / eqVal) * 100 : 0
    const unrealized = m.unrealized ?? 0
    const tradeEur = m.tradeEur || (() => 0)
    const closedTrades = m.closed || []

    // Most traded symbol today (view-specific).
    const today = new Date().toISOString().slice(0, 10)
    const todays = (trades || []).filter(t => (t.recorded_at || '').slice(0, 10) === today)
    const symCount = {}
    todays.forEach(t => { symCount[t.symbol] = (symCount[t.symbol] || 0) + 1 })
    const mostTraded = Object.entries(symCount).sort((a, b) => b[1] - a[1])[0]?.[0] || '—'

    // Positions repartition (pie) by notional weight.
    const totalNotional = positions.reduce((s, p) => s + Math.abs(Number(p.qty ?? 0) * Number(p.current_price ?? p.avg_cost ?? 0)), 0)
    const pie = positions
      .map(p => ({
        name: p.symbol,
        value: Math.abs(Number(p.qty ?? 0) * Number(p.current_price ?? p.avg_cost ?? 0)),
      }))
      .filter(p => p.value > 0)
      .sort((a, b) => b.value - a.value)

    // Top performers — same closed set + same € formula (tradeEur) as the KPIs.
    const bySym = {}
    closedTrades.forEach(t => { bySym[t.symbol] = (bySym[t.symbol] || 0) + tradeEur(t) })
    const performers = Object.entries(bySym)
      .map(([name, pnl]) => ({ name, pnl: +pnl.toFixed(2) }))
      .sort((a, b) => b.pnl - a.pnl)
      .slice(0, 14)

    // Last trades feed (opened/closed events).
    const last = (trades || []).slice(0, 8).map(t => ({
      id: t.trade_id || t.order_id,
      label: `${t.hit != null ? 'Closed' : 'Opened'} ${(t.side || '').toUpperCase()} ${t.symbol}`,
      when: t.recorded_at ? new Date(t.recorded_at).toLocaleString('de-DE', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '',
      open: t.hit == null,
    }))

    const profitIsEstimated = m.profitIsEstimated

    return {
      eqVal, withIncome, profitToday, profit7d, pct7d, unrealized,
      mostTraded, tradesToday: todays.length, pie, totalNotional, performers, last,
      totalTrades: m.totalTrades ?? (trades || []).length,
      profitIsEstimated,
    }
  }, [trades, equity, dbPositions, metrics])
}

function ChartTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div style={{ background: '#fff', border: '1px solid var(--c-border)', borderRadius: 8, padding: '8px 10px', boxShadow: 'var(--c-shadow)', fontSize: 12 }}>
      <div style={{ color: 'var(--c-text-faint)', marginBottom: 4 }}>{label}</div>
      {payload.map(p => (
        <div key={p.dataKey} style={{ color: p.color, fontWeight: 600 }}>
          {p.dataKey === 'income' ? 'Income' : 'Balance'}: {eur(p.value)}
        </div>
      ))}
    </div>
  )
}

function DashboardPage({ d }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* KPI strip */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 14 }}>
        <KpiTile tone="green"  label="Profit Today"      value={eur(d.profitToday)}
          badge={d.profitIsEstimated ? { text: 'EST.', title: 'Includes trades closed at estimated stop_loss prices — not real fills, so Balance is unaffected.' } : null} />
        <KpiTile tone="blue"   label="Profit last 7 days" value={eur(d.profit7d)}
          badge={d.profitIsEstimated ? { text: 'EST.', title: 'Includes trades closed at estimated stop_loss prices — not real fills, so Balance is unaffected.' } : null} />
        <KpiTile tone={d.unrealized >= 0 ? 'green' : 'red'} label="Unrealised PnL" value={eur(d.unrealized)} />
        <KpiTile tone="amber"  label="Most traded today"  value={d.mostTraded} />
        <KpiTile tone="purple" label="Trades today"       value={d.tradesToday} />
        <KpiTile tone="green"  label="Balance"            value={eur(d.eqVal, 0)} />
      </div>

      {/* Performance + positions */}
      <div style={{ display: 'grid', gridTemplateColumns: '1.6fr 1fr', gap: 16 }}>
        <Card title="Performance Overview" sub={`${d.pct7d >= 0 ? '+' : ''}${d.pct7d.toFixed(1)}% last 7 days`} style={{ height: 320 }}>
          {d.withIncome.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={d.withIncome} margin={{ top: 10, right: 8, bottom: 0, left: -8 }}>
                <defs>
                  <linearGradient id="balFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#fbbf24" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#fbbf24" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="t" tick={{ fontSize: 11, fill: 'var(--c-text-faint)' }} axisLine={false} tickLine={false} minTickGap={24} />
                <YAxis tick={{ fontSize: 11, fill: 'var(--c-text-faint)' }} axisLine={false} tickLine={false} width={56} tickFormatter={v => `€${(v / 1000).toFixed(0)}k`} />
                <Tooltip content={<ChartTooltip />} />
                <Area type="monotone" dataKey="balance" stroke="#f59e0b" strokeWidth={2.5} fill="url(#balFill)" />
              </AreaChart>
            </ResponsiveContainer>
          ) : <Empty text="No equity history yet" />}
        </Card>

        <Card title="Positions Repartition" style={{ height: 320 }}>
          {d.pie.length ? (
            <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
              <ResponsiveContainer width="100%" height="70%">
                <PieChart>
                  <Pie data={d.pie} dataKey="value" nameKey="name" innerRadius={40} outerRadius={80} paddingAngle={2}>
                    {d.pie.map((_, i) => <Cell key={i} fill={PIE(i)} />)}
                  </Pie>
                  <Tooltip formatter={(v, n) => [`${eur(v)} (${((v / d.totalNotional) * 100).toFixed(1)}%)`, n]} />
                </PieChart>
              </ResponsiveContainer>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 12px', justifyContent: 'center', overflowY: 'auto' }} className="c-scroll">
                {d.pie.map((p, i) => (
                  <span key={p.name} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color: 'var(--c-text-dim)' }}>
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: PIE(i) }} />{p.name}
                  </span>
                ))}
              </div>
            </div>
          ) : <Empty text="No open positions" />}
        </Card>
      </div>

      {/* Top performers + last trades */}
      <div style={{ display: 'grid', gridTemplateColumns: '1.6fr 1fr', gap: 16 }}>
        <Card title="Top Performers" sub={`${d.profit7d >= 0 ? '+' : ''}${eur(d.profit7d)} last 7 days`} style={{ height: 340 }}>
          {d.performers.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <BarChart layout="vertical" data={d.performers} margin={{ top: 4, right: 16, bottom: 4, left: 8 }}>
                <XAxis type="number" tick={{ fontSize: 11, fill: 'var(--c-text-faint)' }} axisLine={false} tickLine={false} />
                <YAxis type="category" dataKey="name" tick={{ fontSize: 11, fill: 'var(--c-text-dim)' }} axisLine={false} tickLine={false} width={60} />
                <Tooltip formatter={v => eur(v)} cursor={{ fill: '#f4f6fb' }} />
                <Bar dataKey="pnl" radius={[0, 5, 5, 0]} barSize={12}>
                  {d.performers.map((p, i) => <Cell key={i} fill={p.pnl >= 0 ? 'var(--c-green)' : 'var(--c-red)'} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No closed trades yet" />}
        </Card>

        <Card title="Last Trades" style={{ height: 340 }} bodyStyle={{ overflowY: 'auto' }}>
          <div className="c-scroll" style={{ display: 'flex', flexDirection: 'column', gap: 14, height: '100%', overflowY: 'auto', paddingRight: 4 }}>
            {d.last.length ? d.last.map(t => (
              <div key={t.id} style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', marginTop: 3, flexShrink: 0, background: t.open ? 'var(--c-blue)' : 'var(--c-green)' }} />
                <div>
                  <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--c-text)' }}>{t.label}</div>
                  <div style={{ fontSize: 11, color: 'var(--c-text-faint)' }}>{t.when}</div>
                </div>
              </div>
            )) : <Empty text="No trades yet" />}
          </div>
        </Card>
      </div>
    </div>
  )
}

function OrderHistoryPage({ trades }) {
  return (
    <Card title="Order History">
      <div style={{ overflowX: 'auto' }} className="c-scroll">
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ color: 'var(--c-text-faint)', textAlign: 'left', fontSize: 11 }}>
              {['Time', 'Symbol', 'Side', 'Fill', 'P&L %', 'Result'].map(h => (
                <th key={h} style={{ padding: '8px 12px', fontWeight: 500, borderBottom: '1px solid var(--c-border)' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(trades || []).slice(0, 60).map(t => {
              const res = t.hit == null ? 'OPEN' : t.hit ? 'WIN' : 'LOSS'
              const resColor = t.hit == null ? 'var(--c-blue)' : t.hit ? 'var(--c-green)' : 'var(--c-red)'
              return (
                <tr key={t.trade_id || t.order_id} style={{ borderBottom: '1px solid var(--c-border)' }}>
                  <td style={{ padding: '9px 12px', color: 'var(--c-text-faint)' }}>{t.recorded_at ? new Date(t.recorded_at).toLocaleString('de-DE', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—'}</td>
                  <td style={{ padding: '9px 12px', fontWeight: 600 }}>{t.symbol}</td>
                  <td style={{ padding: '9px 12px' }}>{(t.side || '').toUpperCase()}</td>
                  <td style={{ padding: '9px 12px', fontVariantNumeric: 'tabular-nums' }}>{t.fill_price != null ? eur(t.fill_price) : '—'}</td>
                  <td style={{ padding: '9px 12px', fontVariantNumeric: 'tabular-nums', color: (t.pnl_pct ?? 0) >= 0 ? 'var(--c-green)' : 'var(--c-red)' }}>{t.pnl_pct != null ? `${(t.pnl_pct * 100).toFixed(2)}%` : '—'}</td>
                  <td style={{ padding: '9px 12px' }}><span style={{ fontSize: 11, fontWeight: 600, color: resColor }}>{res}</span></td>
                </tr>
              )
            })}
            {(!trades || trades.length === 0) && (
              <tr><td colSpan={6} style={{ padding: 24, textAlign: 'center', color: 'var(--c-text-faint)' }}>No orders yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

function Empty({ text }) {
  return <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--c-text-faint)', fontSize: 13 }}>{text}</div>
}

const NAV = [
  { id: 'dashboard', label: 'Dashboard',     icon: '📊' },
  { id: 'orders',    label: 'Order History', icon: '🧾' },
  { id: 'agents',    label: 'Agents',        icon: '🏛️' }, // ← the extra button
  { id: 'logs',      label: 'Logs',          icon: '📜' }, // ← which stock the pipeline tried
]

export function ClassicView({ status, metrics }) {
  const [page, setPage] = useState('dashboard')
  const d = useClassicData(metrics)
  const trades = useTrades()

  const lastUpdate = status?.timestamp
    ? new Date(status.timestamp).toLocaleString('de-DE', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }).toUpperCase()
    : '—'

  return (
    <div className="classic-light" style={{ flex: 1, minHeight: 0, display: 'flex', overflow: 'hidden' }}>
      <Sidebar
        profile={{ name: 'PANTHEON OS', role: status?.paper_trading === false ? 'Live Trader' : 'Paper Trader', balance: eur(d.eqVal, 0) }}
        items={NAV}
        active={page}
        onSelect={setPage}
      />

      <div className="c-scroll" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '18px 22px' }}>
        <div style={{ textAlign: 'center', fontSize: 11, letterSpacing: 1, color: 'var(--c-text-faint)', fontWeight: 600, marginBottom: 16 }}>
          LAST UPDATE: {lastUpdate}
        </div>

        {page === 'dashboard' && <DashboardPage d={d} />}
        {page === 'orders'    && <OrderHistoryPage trades={trades} />}
        {page === 'agents'    && <AgentsView />}
        {page === 'logs'      && <LogsView />}
      </div>
    </div>
  )
}
