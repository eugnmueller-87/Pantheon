// Deep portfolio & P&L: real equity curve (lightweight-charts), performance
// stats, R-multiple histogram, and recent closed trades.
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { Panel, PanelHeader } from '../components/ui/Panel'
import { KpiRing } from '../components/ui/KpiRing'
import { EquityChartLW } from '../components/charts/EquityChartLW'
import { useEquitySeries, useTrades, useRMultiples, usePerformanceStats } from '../hooks/useSupabaseData'

function Stat({ label, value, color }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '5px 0', borderBottom: '1px solid var(--border)' }}>
      <span style={{ fontSize: 9, color: 'var(--text-faint)' }}>{label}</span>
      <span style={{ fontSize: 10, fontWeight: 700, color: color || 'var(--text)', fontVariantNumeric: 'tabular-nums' }}>{value}</span>
    </div>
  )
}

export function PortfolioView() {
  const equity = useEquitySeries()
  const trades = useTrades()
  const rmult  = useRMultiples()
  const perf   = usePerformanceStats()

  const closed = (trades || []).filter(t => t.hit !== null && t.hit !== undefined).slice(0, 30)
  const winRate = perf ? Math.round((perf.win_rate || 0) * 100) : 0
  const pf = perf ? (perf.profit_factor || 0) : 0
  const exp = perf ? (perf.expectancy || 0) : 0

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: 10, display: 'flex', flexDirection: 'column', gap: 10 }}>
      {/* Equity curve */}
      <Panel style={{ padding: '12px 12px', height: 280, display: 'flex', flexDirection: 'column' }}>
        <PanelHeader>EQUITY CURVE · PEAK OVERLAY</PanelHeader>
        <div style={{ flex: 1, minHeight: 0 }}><EquityChartLW data={equity} /></div>
      </Panel>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        {/* Performance stats */}
        <Panel style={{ padding: '12px 14px' }}>
          <PanelHeader>PERFORMANCE</PanelHeader>
          <div style={{ display: 'flex', gap: 16, justifyContent: 'space-around', margin: '6px 0 12px' }}>
            <KpiRing label="WIN RATE" value={`${perf?.total_trades || 0} trades`} pct={winRate} color="var(--green)" />
            <KpiRing label="PROFIT FACTOR" value={pf.toFixed(2)} pct={Math.min(pf / 3 * 100, 100)} color="var(--blue)" />
          </div>
          <Stat label="Expectancy / trade" value={`${(exp * 100).toFixed(2)}%`} color={exp >= 0 ? 'var(--green)' : 'var(--red)'} />
          <Stat label="Avg win" value={`${((perf?.avg_win_pct || 0) * 100).toFixed(2)}%`} color="var(--green)" />
          <Stat label="Avg loss" value={`${((perf?.avg_loss_pct || 0) * 100).toFixed(2)}%`} color="var(--red)" />
          <Stat label="Per-trade Sharpe" value={(perf?.per_trade_sharpe || 0).toFixed(2)} />
        </Panel>

        {/* R-multiple histogram */}
        <Panel style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column' }}>
          <PanelHeader>R-MULTIPLE DISTRIBUTION</PanelHeader>
          {rmult && rmult.length > 0 ? (
            <ResponsiveContainer width="100%" height={170}>
              <BarChart data={rmult} margin={{ top: 8, right: 8, bottom: 0, left: -10 }}>
                <XAxis dataKey="r_bucket" tick={{ fontSize: 7, fill: 'var(--text-ghost)' }} axisLine={false} tickLine={false} interval={0} angle={-25} textAnchor="end" height={42} />
                <YAxis tick={{ fontSize: 8, fill: 'var(--text-ghost)' }} axisLine={false} tickLine={false} allowDecimals={false} />
                <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', fontSize: 9 }} />
                <Bar dataKey="cnt" radius={[3, 3, 0, 0]}>
                  {rmult.map((d, i) => <Cell key={i} fill={d.r_bucket?.includes('-') && !d.r_bucket.includes('..') ? 'var(--red)' : d.r_bucket?.startsWith('-') ? 'var(--red)' : 'var(--green)'} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <div style={{ color: 'var(--text-mute)', fontSize: 10, paddingTop: 30, textAlign: 'center' }}>No closed trades with R data yet</div>}
        </Panel>
      </div>

      {/* Recent closed trades */}
      <Panel style={{ padding: '12px 14px' }}>
        <PanelHeader>RECENT CLOSED TRADES</PanelHeader>
        {closed.length === 0 ? (
          <div style={{ color: 'var(--text-mute)', fontSize: 10 }}>No closed trades yet.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {closed.map(t => {
              const win = (t.pnl_pct ?? 0) > 0
              return (
                <div key={t.trade_id || t.order_id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '4px 0', borderBottom: '1px solid var(--border-soft)', fontSize: 9 }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: win ? 'var(--green)' : 'var(--red)', flexShrink: 0 }} />
                  <span style={{ color: 'var(--text-dim)', minWidth: 60, fontWeight: 700 }}>{t.side} {t.symbol}</span>
                  <span style={{ color: 'var(--text-faint)' }}>@ €{t.fill_price ?? '—'}</span>
                  <span style={{ color: win ? 'var(--green)' : 'var(--red)', marginLeft: 'auto', fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>
                    {win ? '+' : ''}{((t.pnl_pct ?? 0) * 100).toFixed(2)}%
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </Panel>
    </div>
  )
}
