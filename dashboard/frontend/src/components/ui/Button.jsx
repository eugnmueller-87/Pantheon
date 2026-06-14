// Control button. Extracted from App.jsx btnStyle().

export function Button({ bg, border, color, onClick, children }) {
  return (
    <button onClick={onClick} style={{
      fontSize: 9, padding: '5px 11px', cursor: 'pointer',
      background: bg, border: `1px solid ${border}`, color,
      borderRadius: 3, letterSpacing: 1, fontFamily: 'inherit', fontWeight: 700,
    }}>
      {children}
    </button>
  )
}
