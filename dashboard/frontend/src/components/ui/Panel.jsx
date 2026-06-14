// Panel surface + small header label. Extracted from App.jsx (P / PH).

export function Panel({ style, children }) {
  return (
    <div style={{ background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', ...style }}>
      {children}
    </div>
  )
}

export function PanelHeader({ children }) {
  return (
    <div style={{ fontSize: 8, color: 'var(--text-ghost)', letterSpacing: 2, fontWeight: 700, marginBottom: 10 }}>
      {children}
    </div>
  )
}
