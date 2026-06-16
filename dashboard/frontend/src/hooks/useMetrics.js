// SINGLE SOURCE OF TRUTH for portfolio metrics across every tab.
//
// The dashboard had two metrics engines that disagreed: the WebSocket "status"
// (equity/drawdown/positions, resets on disconnect, fed by the dashboard's own
// ZEUS instance) and Supabase (trades/positions/equity, persistent, reload-safe
// but ~30s behind). Different tabs preferred different sources and computed P&L
// / counts differently, so the same concept showed different numbers per tab.
//
// This hook computes EVERY shared metric ONCE, from Supabase, and uses the live
// WS status only as the freshest override for equity/drawdown (with a Supabase
// fallback). Every consumer reads these fields — so a number is the same number
// everywhere.
import { useMemo } from 'react'
import { useTrades, useTraces, useEquitySeries, usePositions, usePerformanceStats } from './useSupabaseData'

const START_EQUITY = 4000

// Realized € for one closed trade = pnl_pct × position notional (qty × entry),
// NOT pnl_pct × whole equity. Falls back to position_pct × equity when qty/fill
// are missing. (Same formula as the corrected Classic view.)
function tradeEur(t, eq) {
  if (t.pnl_pct == null) return 0
  const qty = Number(t.qty ?? 0), fill = Number(t.fill_price ?? 0)
  if (qty && fill) return t.pnl_pct * qty * fill
  const posPct = Number(t.position_pct ?? 0)
  return posPct ? t.pnl_pct * posPct * eq : 0
}

export function useMetrics(status) {
  const trades = useTrades()
  const traces = useTraces()
  const equitySeries = useEquitySeries()
  const positions = usePositions()
  const perf = usePerformanceStats()

  return useMemo(() => {
    const tr = trades || []
    const eqRows = equitySeries || []
    const lastEqRow = eqRows.length ? eqRows[eqRows.length - 1] : null
    const eqFromSeries = lastEqRow ? Number(lastEqRow.total_equity ?? lastEqRow.equity ?? START_EQUITY) : START_EQUITY

    // Equity: prefer the live WS snapshot, fall back to the persisted series.
    const equity = Number(status?.equity ?? eqFromSeries)
    const drawdown = Number(status?.drawdown_pct ?? lastEqRow?.drawdown_pct ?? 0)

    // Open positions: portfolio_positions (authoritative) — the WS positions[]
    // array is usually empty, so DON'T prefer it for the count.
    const openPositions = positions || []
    const openCount = openPositions.length
    const unrealized = openPositions.reduce((s, p) => s + Number(p.unrealized_pnl ?? 0), 0)

    // Closed trades (real outcomes) — the basis for realized P&L and win-rate.
    const closed = tr.filter(t => t.hit != null && t.pnl_pct != null)
    const wins = closed.filter(t => t.hit === true || t.hit === 1).length
    const winRate = closed.length ? (wins / closed.length) * 100 : 0

    // Realized P&L windows — ONE formula (tradeEur), used everywhere.
    const todayStr = new Date().toISOString().slice(0, 10)
    const weekAgo = new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10)
    const realizedToday = closed
      .filter(t => (t.closed_at || t.recorded_at || '').slice(0, 10) === todayStr)
      .reduce((s, t) => s + tradeEur(t, equity), 0)
    const realized7d = closed
      .filter(t => (t.closed_at || t.recorded_at || '').slice(0, 10) >= weekAgo)
      .reduce((s, t) => s + tradeEur(t, equity), 0)

    // Total P&L vs the starting bank (equity move; reflects open + realized).
    const pnlPct = START_EQUITY ? ((equity - START_EQUITY) / START_EQUITY) * 100 : 0

    // Trade / signal / kill counts — from PERSISTED tables (reload-safe), not WS
    // session events. perf RPC is preferred for the lifetime total when present.
    const trc = traces || []
    const killCount = trc.filter(t => t.killed_at_stage).length
    const placedCount = trc.filter(t => t.trade_placed).length
    const signalCount = trc.length
    const totalTrades = perf?.total_trades ?? tr.length
    const approvalPct = signalCount ? (placedCount / signalCount) * 100 : 0
    const killPct = signalCount ? (killCount / signalCount) * 100 : 0

    // "Estimated" flag: realized P&L exists but the equity curve never moved —
    // the backlog closed at estimated stop_loss prices. Surfaced as an "EST."
    // badge so real Balance vs estimated profit doesn't read as a contradiction.
    const balances = eqRows.map(r => Number(r.total_equity ?? r.equity ?? 0)).filter(Boolean)
    const equityFlat = balances.length > 1 && Math.abs(balances[balances.length - 1] - balances[0]) < 0.005
    const profitIsEstimated = closed.length > 0 && Math.abs(realized7d) > 0.005 && equityFlat

    const ddColor = drawdown < 3 ? 'var(--green)' : drawdown < 6 ? 'var(--yellow)' : 'var(--red)'
    const pnlColor = pnlPct >= 0 ? 'var(--green)' : 'var(--red)'

    return {
      equity, drawdown, ddColor, pnlPct, pnlColor,
      openCount, openPositions, unrealized,
      closed, wins, winRate,
      realizedToday, realized7d, profitIsEstimated,
      signalCount, killCount, placedCount, totalTrades,
      approvalPct, killPct,
      pipeStatus: status?.pipeline_status || 'UNKNOWN',
      tradeEur: (t) => tradeEur(t, equity),
      START_EQUITY,
    }
  }, [trades, traces, equitySeries, positions, perf, status])
}
