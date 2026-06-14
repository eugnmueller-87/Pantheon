// Owns the WebSocket connection to the dashboard backend, the event log, the
// live pipeline stage state, the ZEUS reasoning typewriter, and all derived
// session metrics. Extracted from the old App.jsx monolith verbatim in behavior.
import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { WS_URL, TYPE_CFG } from '../lib/constants'

export function usePipelineSocket() {
  const [events, setEvents]       = useState([])
  const [status, setStatus]       = useState(null)
  const [agents, setAgents]       = useState([])
  const [connected, setConnected] = useState(false)
  const [activeStage, setActive]  = useState(null)
  const [killStage, setKill]      = useState(null)
  const [reasoning, setReasoning] = useState('')
  const [displayed, setDisplayed] = useState('')
  const [charIdx, setCharIdx]     = useState(0)
  const ws        = useRef(null)
  const reconnect = useRef(null)

  const connect = useCallback(() => {
    if (ws.current?.readyState === WebSocket.OPEN) return
    const sock = new WebSocket(WS_URL)
    ws.current = sock
    sock.onopen  = () => { setConnected(true); clearTimeout(reconnect.current) }
    sock.onclose = () => { setConnected(false); reconnect.current = setTimeout(connect, 3000) }
    sock.onerror = () => sock.close()
    sock.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data)
        if (ev.type === 'status_update') { setStatus(ev); return }
        if (ev.type === 'agent_health')  { setAgents(ev.agents || []); return }
        setEvents(prev => [ev, ...prev].slice(0, 500))
        if (ev.type === 'icarus_signal')     { setActive('icarus'); setKill(null) }
        if (ev.type === 'signal_killed')     { setActive(null); setKill(ev.stage?.replace('trend', 'artemis').replace('pattern', 'pythia')) }
        if (ev.type === 'trade_placed')      { setActive('ares'); setKill(null); if (ev.reasoning) setReasoning(ev.reasoning) }
        if (ev.type === 'pipeline_complete') setTimeout(() => setActive(null), 2000)
      } catch { /* ignore malformed frames */ }
    }
  }, [])

  useEffect(() => { connect(); return () => ws.current?.close() }, [connect])

  // Typewriter for ZEUS reasoning
  useEffect(() => { setDisplayed(''); setCharIdx(0) }, [reasoning])
  useEffect(() => {
    if (charIdx >= reasoning.length) return
    const t = setTimeout(() => { setDisplayed(reasoning.slice(0, charIdx + 1)); setCharIdx(c => c + 1) }, 14)
    return () => clearTimeout(t)
  }, [charIdx, reasoning])

  const send = useCallback((action) => {
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify({ action }))
  }, [])

  // ── Derived data ──────────────────────────────────────────────────────────
  const agentMap = useMemo(() => {
    const m = {}
    agents.forEach(a => { m[a.name] = a.status })
    return m
  }, [agents])

  const tradeEvents   = useMemo(() => events.filter(e => e.type === 'trade_placed'), [events])
  const killEvents    = useMemo(() => events.filter(e => e.type === 'signal_killed'), [events])
  const signalEvents  = useMemo(() => events.filter(e => e.type === 'icarus_signal'), [events])
  const visibleEvents = useMemo(() => events.filter(e => TYPE_CFG[e.type]), [events])

  const chartData = useMemo(() => {
    const pts = events
      .filter(e => e.type === 'status_update' && e.equity)
      .reverse()
      .slice(-80)
      .map(e => ({ t: new Date(e.timestamp).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }), eq: Number(e.equity) }))
    return pts.map((p, i) => {
      const window = pts.slice(Math.max(0, i - 9), i + 1)
      const ma = window.reduce((s, w) => s + w.eq, 0) / window.length
      return { ...p, ma: +ma.toFixed(2) }
    })
  }, [events])

  const signalTypeCounts = useMemo(() => {
    const counts = {}
    signalEvents.forEach(e => { const t = e.category || 'other'; counts[t] = (counts[t] || 0) + 1 })
    return Object.entries(counts).map(([name, value]) => ({ name: name.slice(0, 10), value })).slice(0, 6)
  }, [signalEvents])

  const killStageCounts = useMemo(() => {
    const counts = {}
    killEvents.forEach(e => { const s = e.stage || 'unknown'; counts[s] = (counts[s] || 0) + 1 })
    return Object.entries(counts).map(([name, value]) => ({ name, value }))
  }, [killEvents])

  return {
    // raw
    events, status, connected, activeStage, killStage, displayed,
    // actions
    send,
    // derived
    agentMap, tradeEvents, killEvents, signalEvents, visibleEvents,
    chartData, signalTypeCounts, killStageCounts,
    latestSignal: signalEvents[0],
    latestTrade: tradeEvents[0],
    latestKill: killEvents[0],
  }
}
