// Circular progress ring KPI. Extracted from App.jsx, unchanged behavior.

export function KpiRing({ label, value, pct, color }) {
  const r = 26
  const circ = 2 * Math.PI * r
  const dash = circ * Math.min(pct / 100, 1)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
      <svg width={68} height={68} style={{ transform: 'rotate(-90deg)' }}>
        <circle cx={34} cy={34} r={r} fill="none" stroke="var(--border)" strokeWidth={6} />
        <circle cx={34} cy={34} r={r} fill="none" stroke={color} strokeWidth={6}
          strokeDasharray={`${dash} ${circ}`} strokeLinecap="round"
          style={{ transition: 'stroke-dasharray 1s ease' }} />
        <text x={34} y={34} textAnchor="middle" dominantBaseline="central"
          fill={color} fontSize={13} fontWeight={700}
          style={{ transform: 'rotate(90deg)', transformOrigin: '34px 34px' }}>
          {Math.round(pct)}%
        </text>
      </svg>
      <div style={{ fontSize: 9, color: 'var(--text-faint)', letterSpacing: 1, textAlign: 'center' }}>{label}</div>
      <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text)' }}>{value}</div>
    </div>
  )
}
