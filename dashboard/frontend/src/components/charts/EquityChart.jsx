// Equity curve + MA10 overlay (Recharts area). Extracted from App.jsx CandleChart
// and renamed to reflect what it actually is (an area chart, not candlesticks).
// Phase 4 will add a true lightweight-charts version.
import {
  Area, ComposedChart, Line,
  XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine,
} from 'recharts'

export function EquityChart({ data }) {
  if (!data || data.length < 2) {
    return <div style={{ color: 'var(--text-mute)', fontSize: 11, textAlign: 'center', paddingTop: 40 }}>Collecting equity data…</div>
  }
  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 4, right: 12, bottom: 0, left: 0 }}>
        <XAxis dataKey="t" tick={{ fontSize: 8, fill: 'var(--text-ghost)' }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
        <YAxis tick={{ fontSize: 8, fill: 'var(--text-ghost)' }} axisLine={false} tickLine={false}
          tickFormatter={v => `€${(v / 1000).toFixed(1)}k`} width={44} domain={['auto', 'auto']} />
        <Tooltip
          contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 4, fontSize: 10 }}
          formatter={(v, n) => [`€${Number(v).toLocaleString('de-DE')}`, n]}
        />
        <ReferenceLine y={data[0]?.eq} stroke="var(--border)" strokeDasharray="4 4" />
        <defs>
          <linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%"  stopColor="var(--blue)" stopOpacity={0.25} />
            <stop offset="95%" stopColor="var(--blue)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <Area type="monotone" dataKey="eq" stroke="var(--blue)" strokeWidth={2}
          fill="url(#eg)" dot={false} name="Equity" />
        <Line type="monotone" dataKey="ma" stroke="var(--amber)" strokeWidth={1.5}
          dot={false} strokeDasharray="3 3" name="MA10" />
      </ComposedChart>
    </ResponsiveContainer>
  )
}
