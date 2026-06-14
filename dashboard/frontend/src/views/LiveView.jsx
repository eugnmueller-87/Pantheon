// The original 4-column live grid, now composed from extracted panels.
import { PipelinePanel } from '../panels/PipelinePanel'
import {
  EquityPanel, SignalAnalysisPanel, PortfolioPanel,
  LiveFeedPanel, ZeusReasoningPanel, PerformancePanel,
} from '../panels/LivePanels'

export function LiveView({ socket, metrics }) {
  const {
    status, activeStage, killStage, agentMap, displayed,
    chartData, signalTypeCounts, killStageCounts,
    signalEvents, tradeEvents, killEvents, visibleEvents,
    latestSignal, latestTrade, latestKill,
  } = socket
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
