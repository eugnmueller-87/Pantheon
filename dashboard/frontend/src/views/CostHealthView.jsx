// Cost & system health: LLM spend (today + over time + by model), a budget
// ring, and the 8-agent health board.
import { useMemo } from 'react'
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { Panel, PanelHeader } from '../components/ui/Panel'
import { KpiRing } from '../components/ui/KpiRing'
import { ALL_AGENTS, ALLOC_COLORS } from '../lib/constants'
import { useLlmUsage, useAgentHealthDb } from '../hooks/useSupabaseData'

const DAILY_BUDGET_USD = 25 // matches the Grafana budget gauge

export function CostHealthView() {
  const usage = useLlmUsage()
  const health = useAgentHealthDb()

  const { todayCost, totalCost, byDay, byModel } = useMemo(() => {
    const rows = usage || []
    const today = new Date().toISOString().slice(0, 10)
    let todayCost = 0, totalCost = 0
    const dayMap = {}, modelMap = {}
    for (const r of rows) {
      const c = Number(r.cost_usd || 0)
      totalCost += c
      const day = (r.recorded_at || '').slice(0, 10)
      if (day === today) todayCost += c
      dayMap[day] = (dayMap[day] || 0) + c
      modelMap[r.model || '?'] = (modelMap[r.model || '?'] || 0) + c
    }
    const byDay = Object.entries(dayMap).sort((a, b) => a[0].localeCompare(b[0])).slice(-14)
      .map(([d, c]) => ({ d: d.slice(5), cost: +c.toFixed(4) }))
    const byModel = Object.entries(modelMap).map(([name, c]) => ({ name: name.slice(0, 16), cost: +c.toFixed(4) }))
    return { todayCost, totalCost, byDay, byModel }
  }, [usage])

  const healthMap = {}
  ;(health || []).forEach(h => { healthMap[h.agent_name] = h })

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: 10, display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        {/* LLM spend */}
        <Panel style={{ padding: '12px 14px' }}>
          <PanelHeader>LLM SPEND</PanelHeader>
          <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
            <div>
              <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--text)', fontVariantNumeric: 'tabular-nums' }}>${todayCost.toFixed(4)}</div>
              <div style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 1 }}>TODAY</div>
              <div style={{ fontSize: 9, color: 'var(--text-faint)', marginTop: 6 }}>Total: ${totalCost.toFixed(2)}</div>
            </div>
            <KpiRing label={`BUDGET $${DAILY_BUDGET_USD}/d`} value={`$${(DAILY_BUDGET_USD - todayCost).toFixed(2)} left`}
              pct={Math.min(todayCost / DAILY_BUDGET_USD * 100, 100)}
              color={todayCost > DAILY_BUDGET_USD * 0.8 ? 'var(--red)' : todayCost > DAILY_BUDGET_USD * 0.5 ? 'var(--yellow)' : 'var(--green)'} />
          </div>
          <div style={{ height: 110, marginTop: 8 }}>
            {byDay.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={byDay} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
                  <defs><linearGradient id="cg" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="#9f7aea" stopOpacity={0.3} /><stop offset="95%" stopColor="#9f7aea" stopOpacity={0} /></linearGradient></defs>
                  <XAxis dataKey="d" tick={{ fontSize: 7, fill: 'var(--text-ghost)' }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fontSize: 7, fill: 'var(--text-ghost)' }} axisLine={false} tickLine={false} width={40} tickFormatter={v => `$${v}`} />
                  <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', fontSize: 9 }} />
                  <Area type="monotone" dataKey="cost" stroke="#9f7aea" strokeWidth={2} fill="url(#cg)" />
                </AreaChart>
              </ResponsiveContainer>
            ) : <div style={{ color: 'var(--text-mute)', fontSize: 10, paddingTop: 30, textAlign: 'center' }}>No LLM usage logged yet</div>}
          </div>
        </Panel>

        {/* By model */}
        <Panel style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column' }}>
          <PanelHeader>COST BY MODEL</PanelHeader>
          {byModel.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={byModel} layout="vertical" margin={{ top: 4, right: 12, bottom: 0, left: 10 }}>
                <XAxis type="number" tick={{ fontSize: 7, fill: 'var(--text-ghost)' }} axisLine={false} tickLine={false} tickFormatter={v => `$${v}`} />
                <YAxis type="category" dataKey="name" tick={{ fontSize: 8, fill: 'var(--text-faint)' }} axisLine={false} tickLine={false} width={110} />
                <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', fontSize: 9 }} />
                <Bar dataKey="cost" radius={[0, 3, 3, 0]}>
                  {byModel.map((_, i) => <Cell key={i} fill={ALLOC_COLORS[i % ALLOC_COLORS.length]} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <div style={{ color: 'var(--text-mute)', fontSize: 10, paddingTop: 30, textAlign: 'center' }}>No model cost data yet</div>}
        </Panel>
      </div>

      {/* Agent health board */}
      <Panel style={{ padding: '12px 14px' }}>
        <PanelHeader>AGENT HEALTH</PanelHeader>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 8 }}>
          {ALL_AGENTS.map(ag => {
            const h = healthMap[ag.id]
            const status = h?.status || 'unknown'
            const color = status === 'healthy' ? 'var(--green)' : status === 'failed' ? 'var(--red)' : status === 'degraded' ? 'var(--yellow)' : 'var(--text-mute)'
            return (
              <div key={ag.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px', borderRadius: 6, background: 'var(--panel-2)', border: `1px solid ${color}44` }}>
                <span style={{ fontSize: 16 }}>{ag.icon}</span>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 9, fontWeight: 700, color: 'var(--text-dim)' }}>{ag.label}</div>
                  <div style={{ fontSize: 8, color }}>{status}{h?.error_count ? ` · ${h.error_count} err` : ''}</div>
                </div>
                <div style={{ width: 6, height: 6, borderRadius: '50%', background: color }} />
              </div>
            )
          })}
        </div>
      </Panel>
    </div>
  )
}
