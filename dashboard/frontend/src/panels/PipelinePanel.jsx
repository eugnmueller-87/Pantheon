// Vertical pipeline of the 7 agents + Apollo + system seniority block.
// Extracted from App.jsx col-1.
import React from 'react'
import { Panel, PanelHeader } from '../components/ui/Panel'
import { AGENTS } from '../lib/constants'

export function PipelinePanel({ activeStage, killStage, agentMap, status, style }) {
  return (
    <Panel style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', ...style }}>
      <PanelHeader>PIPELINE</PanelHeader>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
        {AGENTS.map((ag, i) => {
          const isActive = activeStage === ag.id
          const isKill   = killStage === ag.id
          const health   = agentMap[ag.id]
          const border = isKill ? 'var(--red)' : isActive ? 'var(--blue)' : health === 'healthy' ? '#bce3cb' : health === 'failed' ? '#f3c4c4' : 'var(--border)'
          const bg     = isKill ? 'var(--bg-kill)' : isActive ? 'var(--bg-signal)' : 'var(--panel-2)'
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
                  <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: 0.5, color: isActive ? 'var(--blue)' : isKill ? 'var(--red)' : 'var(--text)' }}>{ag.label}</div>
                  <div style={{ fontSize: 7, color: 'var(--text-ghost)', marginTop: 1 }}>{ag.sub}</div>
                </div>
                <div style={{ width: 5, height: 5, borderRadius: '50%', flexShrink: 0, background: health === 'healthy' ? 'var(--green)' : health === 'failed' ? 'var(--red)' : 'var(--text-mute)' }} />
              </div>
              {i < AGENTS.length - 1 && (
                <div style={{ textAlign: 'center', color: 'var(--border)', fontSize: 10, lineHeight: '6px' }}>↓</div>
              )}
            </React.Fragment>
          )
        })}
      </div>

      {/* Apollo — separate research agent */}
      <div style={{ borderTop: '1px dashed var(--border)', marginTop: 8, paddingTop: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '7px 8px', borderRadius: 4, border: '1px solid #ddd0f3', background: '#f3eefc' }}>
          <span style={{ fontSize: 12 }}>📚</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 9, fontWeight: 700, color: 'var(--purple)' }}>APOLLO</div>
            <div style={{ fontSize: 7, color: 'var(--text-ghost)' }}>Research · daily</div>
          </div>
          <div style={{ width: 5, height: 5, borderRadius: '50%', background: agentMap['apollo'] === 'healthy' ? 'var(--green)' : 'var(--text-mute)' }} />
        </div>
      </div>

      {/* System seniority */}
      {status?.seniority && (
        <div style={{ marginTop: 8, padding: '6px 8px', borderRadius: 4, background: 'var(--panel)', border: '1px solid var(--border)' }}>
          <div style={{ fontSize: 7, color: 'var(--text-ghost)', letterSpacing: 1, marginBottom: 4 }}>SYSTEM LEVEL</div>
          <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--purple)' }}>{status.seniority.system_level}</div>
          <div style={{ fontSize: 7, color: 'var(--text-ghost)', marginTop: 2 }}>
            MAX POS: {((status.seniority.max_position_pct || 0) * 100).toFixed(0)}% · LIVE: {status.seniority.live_trading_allowed ? '✓' : '✗'}
          </div>
        </div>
      )}
    </Panel>
  )
}
