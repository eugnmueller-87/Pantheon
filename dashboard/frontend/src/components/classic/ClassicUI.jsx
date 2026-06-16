// Light-theme UI primitives for the CLASSIC dashboard (SH-Stark look):
// pastel KPI tiles, white cards, and the left rail. Kept dependency-free so the
// dark terminal is untouched.

export function Card({ title, sub, children, style, bodyStyle, action }) {
  return (
    <div className="c-card" style={{ padding: 18, display: 'flex', flexDirection: 'column', ...style }}>
      {(title || action) && (
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 12 }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--c-text)' }}>{title}</div>
            {sub && <div style={{ fontSize: 12, color: 'var(--c-green)', marginTop: 2, fontWeight: 500 }}>{sub}</div>}
          </div>
          {action}
        </div>
      )}
      <div style={{ flex: 1, minHeight: 0, ...bodyStyle }}>{children}</div>
    </div>
  )
}

// Pastel KPI tile — the colored stat cards along the top of the reference.
const TONES = {
  green:  { bg: 'var(--c-green-soft)',  fg: 'var(--c-green)' },
  blue:   { bg: 'var(--c-blue-soft)',   fg: 'var(--c-blue)' },
  red:    { bg: 'var(--c-red-soft)',    fg: 'var(--c-red)' },
  amber:  { bg: 'var(--c-amber-soft)',  fg: 'var(--c-amber)' },
  purple: { bg: 'var(--c-purple-soft)', fg: 'var(--c-purple)' },
  plain:  { bg: 'var(--c-card)',        fg: 'var(--c-text)' },
}

export function KpiTile({ label, value, tone = 'plain' }) {
  const t = TONES[tone] || TONES.plain
  return (
    <div className="c-card" style={{ background: t.bg, border: 'none', padding: '16px 18px', minWidth: 0 }}>
      <div className="c-kpi-value" style={{ color: t.fg }}>{value}</div>
      <div className="c-kpi-label" style={{ color: t.fg, opacity: 0.85, marginTop: 2 }}>{label}</div>
    </div>
  )
}

export function Sidebar({ profile, items, active, onSelect }) {
  return (
    <div style={{
      width: 200, flexShrink: 0, background: 'var(--c-card)', borderRight: '1px solid var(--c-border)',
      display: 'flex', flexDirection: 'column', padding: '18px 0',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '0 18px 18px' }}>
        <div style={{
          width: 38, height: 38, borderRadius: '50%', background: 'var(--c-blue-soft)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 20, flexShrink: 0,
        }}>⚡</div>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--c-text)', whiteSpace: 'nowrap' }}>{profile.name}</div>
          <div style={{ fontSize: 11, color: 'var(--c-text-faint)' }}>{profile.role}</div>
        </div>
      </div>

      <nav style={{ display: 'flex', flexDirection: 'column', gap: 2, padding: '0 10px' }}>
        {items.map(it => {
          const on = it.id === active
          return (
            <button key={it.id} onClick={() => onSelect(it.id)} style={{
              display: 'flex', alignItems: 'center', gap: 10, width: '100%', textAlign: 'left',
              padding: '10px 12px', borderRadius: 9, cursor: 'pointer', fontFamily: 'inherit',
              fontSize: 13, fontWeight: on ? 600 : 500, border: 'none',
              color: on ? 'var(--c-green)' : 'var(--c-text-dim)',
              background: on ? 'var(--c-green-soft)' : 'transparent',
            }}>
              <span style={{ fontSize: 15 }}>{it.icon}</span> {it.label}
            </button>
          )
        })}
      </nav>

      <div style={{ marginTop: 'auto', padding: '0 18px' }}>
        <div className="c-card" style={{ padding: '12px 14px', background: 'var(--c-bg)', border: 'none' }}>
          <div style={{ fontSize: 11, color: 'var(--c-text-faint)' }}>Total balance</div>
          <div style={{ fontSize: 17, fontWeight: 700, color: 'var(--c-text)', fontVariantNumeric: 'tabular-nums' }}>{profile.balance}</div>
        </div>
      </div>
    </div>
  )
}
