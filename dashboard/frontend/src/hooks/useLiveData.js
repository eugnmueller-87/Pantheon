// LIVE-tab data, derived from Supabase (the same source the CLASSIC/AGENTS/COST
// tabs use) instead of the dashboard's in-process WebSocket bus.
//
// Why: the WS bus is fed only by the dashboard's OWN lazily-created ZeusOrchestrator
// (via the RUN button / its pollers), which is a DIFFERENT process than the live
// auto-scheduling pipeline. So the LIVE tab sat empty and reported a false
// "ICARUS failed" while Supabase showed every agent healthy. This hook produces
// the exact `socket`-shaped object the existing Live panels expect, but every
// field comes from real persisted data.
import { useMemo } from 'react'
import { useTraces, useTrades, useEquitySeries, useAgentHealthDb } from './useSupabaseData'

const EQ_START = 4000

export function useLiveData(status) {
  const traces = useTraces()
  const trades = useTrades()
  const equity = useEquitySeries()
  const health = useAgentHealthDb()

  return useMemo(() => {
    const tr = traces || []

    // Agent health straight from the agent_health table (latest row per agent).
    const agentMap = {}
    ;(health || []).forEach(h => {
      if (h?.agent_name && !(h.agent_name in agentMap)) agentMap[h.agent_name] = h.status
    })

    // Signals by category (from decision traces — every signal that entered).
    const catCount = {}
    tr.forEach(t => { const c = t.category || 'other'; catCount[c] = (catCount[c] || 0) + 1 })
    const signalTypeCounts = Object.entries(catCount)
      .map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value)

    // Kills by stage.
    const killed = tr.filter(t => t.killed_at_stage)
    const stageCount = {}
    killed.forEach(t => { const s = t.killed_at_stage; stageCount[s] = (stageCount[s] || 0) + 1 })
    const killStageCounts = Object.entries(stageCount)
      .map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value)

    // Event arrays the panels count (length only, mostly).
    const signalEvents = tr.map(t => ({ id: t.trace_id, supplier: t.supplier }))
    const killEvents = killed.map(t => ({ id: t.trace_id, stage: t.killed_at_stage, reason: t.kill_reason }))
    const tradeEvents = tr.filter(t => t.trade_placed).map(t => ({ id: t.trace_id, symbol: t.symbol }))

    // Approval / kill rate over the trace set.
    const total = tr.length
    const approvalPct = total ? (tradeEvents.length / total) * 100 : 0
    const killPct = total ? (killEvents.length / total) * 100 : 0

    // Latest signal / trade / kill + the latest reasoning text (typewriter slot).
    const latest = tr[0] || null
    const latestSignal = latest ? {
      supplier: latest.supplier, category: latest.category, severity: latest.severity,
      headline: latest.headline, tickers: latest.symbol ? [latest.symbol] : [],
    } : null
    const lastTradeRow = tr.find(t => t.trade_placed)
    const latestTrade = lastTradeRow ? {
      symbol: lastTradeRow.symbol, side: lastTradeRow.side, fill: lastTradeRow.fill_price,
      confidence: lastTradeRow.pattern_confidence, timestamp: lastTradeRow.created_at,
    } : null
    const lastKillRow = killed[0]
    const latestKill = lastKillRow ? { stage: lastKillRow.killed_at_stage, reason: lastKillRow.kill_reason } : null
    const displayed = latest?.zeus_reasoning || ''

    // Live feed: recent traces rendered as signal/kill/trade events.
    const visibleEvents = tr.slice(0, 60).map(t => ({
      id: t.trace_id,
      type: t.trade_placed ? 'trade_placed' : t.killed_at_stage ? 'signal_killed' : 'icarus_signal',
      timestamp: t.created_at,
      supplier: t.supplier, headline: t.headline, symbol: t.symbol,
      stage: t.killed_at_stage, reason: t.kill_reason,
    }))

    // Equity curve for the chart — EquityChart expects { t, eq, ma } (10-point
    // moving average), matching the shape the socket used to produce.
    const points = (equity || []).slice(-120).map(r => ({
      t: new Date(r.recorded_at).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }),
      eq: Number(r.total_equity ?? r.equity ?? 0),
    }))
    const chartData = points.map((p, i) => {
      const window = points.slice(Math.max(0, i - 9), i + 1)
      const ma = window.reduce((s, w) => s + w.eq, 0) / window.length
      return { ...p, ma: +ma.toFixed(2) }
    })

    // PnL vs start + drawdown from live status (falls back to equity series).
    const eq = Number(status?.equity ?? chartData[chartData.length - 1]?.equity ?? EQ_START)
    const pnlPct = EQ_START ? ((eq - EQ_START) / EQ_START) * 100 : 0
    const drawdown = Number(status?.drawdown_pct ?? 0)

    return {
      status, agentMap, displayed, chartData,
      signalTypeCounts, killStageCounts,
      signalEvents, tradeEvents, killEvents, visibleEvents,
      latestSignal, latestTrade, latestKill,
      activeStage: null, killStage: latestKill?.stage || null,
      equity: eq, pnlPct, drawdown, approvalPct, killPct,
    }
  }, [traces, trades, equity, health, status])
}
