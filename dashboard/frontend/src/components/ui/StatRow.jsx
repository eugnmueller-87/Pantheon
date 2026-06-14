// Labeled horizontal progress bar. Extracted from App.jsx, unchanged behavior.

export function StatRow({ label, value, pct, color }) {
  return (
    <div style={{ marginBottom: 7 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
        <span style={{ fontSize: 9, color: 'var(--text-faint)' }}>{label}</span>
        <span style={{ fontSize: 9, color, fontWeight: 700 }}>{value}</span>
      </div>
      <div style={{ height: 4, background: 'var(--border)', borderRadius: 2 }}>
        <div style={{ height: '100%', width: `${Math.min(pct, 100)}%`, background: color, borderRadius: 2, transition: 'width 1s ease' }} />
      </div>
    </div>
  )
}
