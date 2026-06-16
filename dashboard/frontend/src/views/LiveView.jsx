// The original 4-column live grid, now composed from extracted panels.
// Data comes from Supabase (useLiveData) — the same persisted source the
// CLASSIC/AGENTS/COST tabs read — NOT the dashboard's in-process WS bus, which
// only fills via the RUN button and reported a false "ICARUS failed". The live
// WS `status` (equity/positions/circuit breakers) is still used as the freshest
// portfolio snapshot.
import { PipelinePanel } from '../panels/PipelinePanel'
import {
  EquityPanel, SignalAnalysisPanel, PortfolioPanel,
  LiveFeedPanel, ZeusReasoningPanel, PerformancePanel,
} from '../panels/LivePanels'
import { useLiveData } from '../hooks/useLiveData'

export function LiveView({ socket, metrics }) {
  const live = useLiveData(socket?.status)
  const {
    status, activeStage, killStage, agentMap, displayed,
    chartData, signalTypeCounts, killStageCounts,
    signalEvents, tradeEvents, killEvents, visibleEvents,
    latestSignal, latestTrade, latestKill,
  } = live
  // Shared portfolio numbers come from useMetrics (single source of truth) so the
  // LIVE tab shows the SAME equity/P&L/drawdown/rates as every other tab. The
  // signal/kill/trade FEED stays from useLiveData (LIVE-tab-specific detail).
  const { equity, pnlPct, pnlColor, drawdown, ddColor, approvalPct, killPct } = metrics

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '180px 1fr 1fr 240px',
      gridTemplateRows: '1fr 1fr',
      gap: 6, padding: 6, flex: 1, minHeight: 0, overflow: 'hidden',
    }}>
      <PipelinePanel style={{ gridRow: '1 / 3' }} activeStage={activeStage} killStage={killStage} agentMap={agentMap} status={status} />
      <EquityPanel chartData={chartData} />
      <SignalAnalysisPanel signalTypeCounts={signalTypeCounts} killStageCounts={killStageCounts}
        killEvents={killEvents} tradeEvents={tradeEvents} drawdown={drawdown} ddColor={ddColor}
        approvalPct={approvalPct} killPct={killPct} />
      <PortfolioPanel status={status} equity={equity} pnlPct={pnlPct} pnlColor={pnlColor} drawdown={drawdown} ddColor={ddColor} />
      <LiveFeedPanel visibleEvents={visibleEvents} />
      <ZeusReasoningPanel latestSignal={latestSignal} displayed={displayed} latestTrade={latestTrade} latestKill={latestKill} />
      <PerformancePanel signalEvents={signalEvents} tradeEvents={tradeEvents} killEvents={killEvents}
        approvalPct={approvalPct} agentMap={agentMap} latestTrade={latestTrade} />
    </div>
  )
}
