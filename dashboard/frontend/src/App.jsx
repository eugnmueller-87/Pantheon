import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'

const WS_URL = import.meta.env.VITE_WS_URL ||
  (window.location.hostname === 'localhost'
    ? 'ws://localhost:8081/ws'
    : 'wss://moremanamoreproblems.de/ws')

const AGENTS = [
  { id: 'icarus',  label: 'ICARUS',  sub: 'Signal Watcher',    icon: '🦅' },
  { id: 'hades',   label: 'HADES',   sub: 'Compliance Filter',  icon: '⚖️' },
  { id: 'artemis', label: 'ARTEMIS', sub: 'Macro Context',      icon: '🌙' },
  { id: 'pythia',  label: 'PYTHIA',  sub: 'Pattern & Sizing',   icon: '🔮' },
  { id: 'zeus',    label: 'ZEUS',    sub: 'LLM Reasoning',      icon: '⚡' },
  { id: 'ares',    label: 'ARES',    sub: 'Trade Execution',    icon: '⚔️' },
  { id: 'argus',   label: 'ARGUS',   sub: 'Portfolio Monitor',  icon: '👁️' },
]

const TYPE_CFG = {
  trade_placed:      { label: 'TRADE',  color: '#48bb78', bg: '#0d2818' },
  signal_killed:     { label: 'KILL',   color: '#fc8181', bg: '#1a0808' },
  icarus_signal:     { label: 'SIGNAL', color: '#63b3ed', bg: '#071525' },
  pipeline_start:    { label: 'START',  color: '#4a5568', bg: '#0d1117' },
  pipeline_complete: { label: 'DONE',   color: '#68d391', bg: '#0d1117' },
  halt:              { label: 'HALT',   color: '#fc8181', bg: '#1a0808' },
  resume:            { label: 'RESUME', color: '#48bb78', bg: '#0d2818' },
  error:             { label: 'ERROR',  color: '#fc8181', bg: '#1a0808' },
}

function fmt(evt) {
  switch (evt.type) {
    case 'trade_placed':    return `${evt.side?.toUpperCase()} ${evt.symbol}  €${evt.fill ?? '—'}  [${((evt.confidence||0)*100).toFixed(0)}% conf]`
    case 'signal_killed':   return `${evt.supplier ?? ''}  killed at ${evt.stage?.toUpperCase()} — ${evt.reason}`
    case 'icarus_signal':   return `${evt.supplier}  ${evt.category}  ${evt.severity}  ${(evt.headline||'').slice(0,80)}`
    case 'pipeline_complete': return `Done — ${evt.runs} run(s), ${evt.trades} trade(s), ${evt.kills} kill(s)`
    case 'error':           return `Error: ${evt.message}`
    default:                return evt.message || evt.type
  }
}

export default function App() {
  const [events, setEvents]       = useState([])
  const [status, setStatus]       = useState(null)
  const [agents, setAgents]       = useState([])
  const [connected, setConnected] = useState(false)
  const [activeStage, setActive]  = useState(null)
  const [killStage, setKill]      = useState(null)
  const [reasoning, setReasoning] = useState('')
  const [displayed, setDisplayed] = useState('')
  const [charIdx, setCharIdx]     = useState(0)
  const ws = useRef(null)
  const reconnect = useRef(null)

  // WebSocket
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
        setEvents(prev => [ev, ...prev].slice(0, 200))
        if (ev.type === 'icarus_signal')    { setActive('icarus'); setKill(null) }
        if (ev.type === 'signal_killed')    { setActive(null); setKill(ev.stage?.replace('trend','artemis').replace('pattern','pythia')) }
        if (ev.type === 'trade_placed')     { setActive('ares'); setKill(null); if (ev.reasoning) setReasoning(ev.reasoning) }
        if (ev.type === 'pipeline_complete') setTimeout(() => setActive(null), 2000)
      } catch (_) {}
    }
  }, [])

  useEffect(() => { connect(); return () => ws.current?.close() }, [connect])

  // Typewriter
  useEffect(() => { setDisplayed(''); setCharIdx(0) }, [reasoning])
  useEffect(() => {
    if (charIdx >= reasoning.length) return
    const t = setTimeout(() => { setDisplayed(reasoning.slice(0, charIdx+1)); setCharIdx(c=>c+1) }, 18)
    return () => clearTimeout(t)
  }, [charIdx, reasoning])

  const send = (action) => ws.current?.readyState === WebSocket.OPEN && ws.current.send(JSON.stringify({ action }))

  // Equity chart data
  const chartData = useMemo(() => events.filter(e => e.type==='status_update' && e.equity).reverse().slice(-60).map(e=>({
    t: new Date(e.timestamp).toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit'}),
    eq: Number(e.equity)
  })), [events])

  const agentMap = {}
  agents.forEach(a => { agentMap[a.name] = a.status })

  const drawdown = status?.drawdown_pct ?? 0
  const ddColor  = drawdown < 3 ? '#48bb78' : drawdown < 6 ? '#f6e05e' : '#fc8181'
  const pipeStatus = status?.pipeline_status || 'UNKNOWN'

  const visibleEvents = events.filter(e => TYPE_CFG[e.type])
  const latestSignal  = events.find(e => e.type === 'icarus_signal')
  const latestTrade   = events.find(e => e.type === 'trade_placed')
  const latestKill    = events.find(e => e.type === 'signal_killed')

  return (
    <div style={{display:'flex',flexDirection:'column',height:'100vh',overflow:'hidden',background:'#080c16',fontFamily:"'Courier New',monospace",color:'#e2e8f0'}}>
      <style>{`@keyframes blink{0%,100%{opacity:1}50%{opacity:0}} @keyframes pulse{0%,100%{box-shadow:0 0 4px #63b3ed44}50%{box-shadow:0 0 12px #63b3ed99}}`}</style>

      {/* TOP BAR */}
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',padding:'8px 16px',background:'#0d1117',borderBottom:'1px solid #1e2d40',flexShrink:0}}>
        <div style={{display:'flex',alignItems:'center',gap:12}}>
          <span style={{fontSize:16,fontWeight:700,color:'#63b3ed',letterSpacing:2}}>⚡ PANTHEON OS</span>
          <span style={{fontSize:10,padding:'2px 7px',borderRadius:3,background:status?.paper_trading?'#1a2236':'#3d0f0f',border:'1px solid #2d3748',color:'#a0aec0'}}>
            {status?.paper_trading ? 'PAPER' : 'LIVE'}
          </span>
          <span style={{width:7,height:7,borderRadius:'50%',background:connected?'#48bb78':'#fc8181',display:'inline-block'}}/>
          <span style={{fontSize:10,color:connected?'#48bb78':'#fc8181'}}>{connected?'CONNECTED':'RECONNECTING…'}</span>
        </div>
        <div style={{display:'flex',gap:24}}>
          {[
            ['EQUITY', `€${Number(status?.equity??0).toLocaleString('de-DE')}`, '#e2e8f0'],
            ['DRAWDOWN', `${drawdown.toFixed(2)}%`, ddColor],
            ['OPEN POS', status?.open_positions??'—', '#63b3ed'],
            ['STATUS', pipeStatus, pipeStatus==='RUNNING'?'#48bb78':'#fc8181'],
          ].map(([label,val,col]) => (
            <div key={label} style={{textAlign:'center'}}>
              <div style={{fontSize:8,color:'#4a5568',letterSpacing:1,marginBottom:2}}>{label}</div>
              <div style={{fontSize:14,fontWeight:700,color:col,fontVariantNumeric:'tabular-nums'}}>{val}</div>
            </div>
          ))}
        </div>
        <div style={{display:'flex',gap:6}}>
          <button onClick={()=>send('run_pipeline')} style={btn('#1a2236','#2d4a6e','#63b3ed')}>▶ RUN CYCLE</button>
          {pipeStatus==='RUNNING'
            ? <button onClick={()=>send('halt')}   style={btn('#1a0808','#fc8181','#fc8181')}>■ HALT</button>
            : <button onClick={()=>send('resume')} style={btn('#0d2818','#48bb78','#48bb78')}>▶ RESUME</button>}
        </div>
      </div>

      {/* BODY */}
      <div style={{display:'grid',gridTemplateColumns:'200px 1fr 300px',gap:8,padding:8,flex:1,minHeight:0,overflow:'hidden'}}>

        {/* LEFT — Pipeline */}
        <div style={panel}>
          <div style={panelTitle}>PIPELINE</div>
          {AGENTS.map((ag,i) => {
            const isActive = activeStage===ag.id
            const isKill   = killStage===ag.id
            const health   = agentMap[ag.id]
            const border   = isKill?'#fc8181':isActive?'#63b3ed':health==='healthy'?'#1a3a28':health==='failed'?'#742a2a':'#1e2d40'
            const bg       = isKill?'#1a0808':isActive?'#0d1f36':'#111827'
            const shadow   = isActive?'0 0 8px #63b3ed55':isKill?'0 0 8px #fc818155':'none'
            return (
              <React.Fragment key={ag.id}>
                <div style={{display:'flex',alignItems:'center',gap:7,padding:'6px 8px',borderRadius:4,border:`1px solid ${border}`,background:bg,boxShadow:shadow,transition:'all 0.3s',animation:isActive?'pulse 1s infinite':'none'}}>
                  <span style={{fontSize:13,flexShrink:0}}>{ag.icon}</span>
                  <div style={{flex:1,minWidth:0}}>
                    <div style={{fontSize:10,fontWeight:700,letterSpacing:0.5}}>{ag.label}</div>
                    <div style={{fontSize:8,color:'#4a5568',marginTop:1}}>{ag.sub}</div>
                  </div>
                  <div style={{width:6,height:6,borderRadius:'50%',flexShrink:0,background:health==='healthy'?'#48bb78':health==='failed'?'#fc8181':'#2d3748'}}/>
                </div>
                {i<AGENTS.length-1 && <div style={{textAlign:'center',color:'#2d3748',fontSize:11,lineHeight:'8px',margin:'1px 0'}}>↓</div>}
              </React.Fragment>
            )
          })}
          <div style={{borderTop:'1px dashed #1e2d40',margin:'8px 0 4px'}}/>
          <div style={{display:'flex',alignItems:'center',gap:7,padding:'6px 8px',borderRadius:4,border:'1px solid #2d1f50',background:'#0f0a1e'}}>
            <span style={{fontSize:13}}>📚</span>
            <div style={{flex:1}}>
              <div style={{fontSize:10,fontWeight:700}}>APOLLO</div>
              <div style={{fontSize:8,color:'#4a5568'}}>Research (daily)</div>
            </div>
            <div style={{width:6,height:6,borderRadius:'50%',background:agentMap['apollo']==='healthy'?'#48bb78':'#2d3748'}}/>
          </div>
        </div>

        {/* CENTER — Chart + Feed */}
        <div style={{display:'flex',flexDirection:'column',gap:8,minHeight:0,overflow:'hidden'}}>

          {/* Equity chart */}
          <div style={{...panel,flexShrink:0}}>
            <div style={panelTitle}>EQUITY CURVE</div>
            {chartData.length > 1
              ? <ResponsiveContainer width="100%" height={100}>
                  <AreaChart data={chartData} margin={{top:2,right:4,bottom:0,left:0}}>
                    <defs>
                      <linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%"  stopColor="#63b3ed" stopOpacity={0.3}/>
                        <stop offset="95%" stopColor="#63b3ed" stopOpacity={0}/>
                      </linearGradient>
                    </defs>
                    <XAxis dataKey="t" tick={{fontSize:8,fill:'#4a5568'}} axisLine={false} tickLine={false}/>
                    <YAxis tick={{fontSize:8,fill:'#4a5568'}} axisLine={false} tickLine={false} tickFormatter={v=>`€${(v/1000).toFixed(0)}k`} width={38}/>
                    <Tooltip contentStyle={{background:'#0d1117',border:'1px solid #1e2d40',fontSize:10,borderRadius:4}} formatter={v=>[`€${Number(v).toLocaleString('de-DE')}`,'Equity']}/>
                    <Area type="monotone" dataKey="eq" stroke="#63b3ed" strokeWidth={2} fill="url(#eg)" dot={false}/>
                  </AreaChart>
                </ResponsiveContainer>
              : <div style={{color:'#2d3748',fontSize:11,textAlign:'center',padding:24}}>Collecting data… (updates every 5s)</div>
            }
          </div>

          {/* Live feed */}
          <div style={{...panel,flex:1,minHeight:0,overflow:'hidden',display:'flex',flexDirection:'column'}}>
            <div style={panelTitle}>LIVE FEED</div>
            <div style={{flex:1,overflowY:'auto',display:'flex',flexDirection:'column',gap:3}}>
              {visibleEvents.length===0 && <div style={{color:'#2d3748',fontSize:11,textAlign:'center',padding:20}}>Waiting for signals… Hit ▶ RUN CYCLE</div>}
              {visibleEvents.map(ev => {
                const cfg = TYPE_CFG[ev.type]
                const time = new Date(ev.timestamp).toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit',second:'2-digit'})
                return (
                  <div key={ev.id} style={{display:'flex',alignItems:'baseline',gap:6,padding:'4px 7px',borderRadius:3,border:`1px solid ${cfg.color}22`,background:cfg.bg,fontSize:11}}>
                    <span style={{color:'#4a5568',flexShrink:0,fontSize:9,fontVariantNumeric:'tabular-nums'}}>{time}</span>
                    <span style={{flexShrink:0,fontSize:8,border:`1px solid ${cfg.color}55`,borderRadius:2,padding:'0 3px',color:cfg.color,letterSpacing:0.5}}>{cfg.label}</span>
                    <span style={{color:'#a0aec0',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{fmt(ev)}</span>
                  </div>
                )
              })}
            </div>
          </div>
        </div>

        {/* RIGHT — Portfolio + Reasoning */}
        <div style={{display:'flex',flexDirection:'column',gap:8,overflowY:'auto'}}>

          {/* Portfolio */}
          <div style={panel}>
            <div style={panelTitle}>PORTFOLIO</div>
            <div style={{display:'flex',gap:16,marginBottom:12}}>
              {[
                ['EQUITY',    `€${Number(status?.equity??0).toLocaleString('de-DE')}`, '#e2e8f0'],
                ['OPEN POS',  status?.open_positions??'—', '#63b3ed'],
                ['TRADES',    events.filter(e=>e.type==='trade_placed').length, '#68d391'],
              ].map(([l,v,c])=>(
                <div key={l}>
                  <div style={{fontSize:8,color:'#4a5568',letterSpacing:1,marginBottom:2}}>{l}</div>
                  <div style={{fontSize:16,fontWeight:700,color:c}}>{v}</div>
                </div>
              ))}
            </div>
            <div style={{marginBottom:4,display:'flex',justifyContent:'space-between',fontSize:9}}>
              <span style={{color:'#4a5568',letterSpacing:1}}>DRAWDOWN</span>
              <span style={{color:ddColor,fontWeight:700}}>{drawdown.toFixed(2)}% / 8%</span>
            </div>
            <div style={{height:8,background:'#1a2236',borderRadius:4,overflow:'hidden',position:'relative'}}>
              <div style={{height:'100%',width:`${Math.min(drawdown/8,1)*100}%`,background:ddColor,borderRadius:4,transition:'width 0.8s ease'}}/>
            </div>
            {status?.circuit_breakers && (
              <div style={{marginTop:10}}>
                <div style={{fontSize:8,color:'#4a5568',letterSpacing:1,marginBottom:5}}>CIRCUIT BREAKERS</div>
                <div style={{display:'flex',flexWrap:'wrap',gap:'4px 12px'}}>
                  {Object.entries(status.circuit_breakers).map(([ag,st])=>(
                    <div key={ag} style={{display:'flex',alignItems:'center',gap:4}}>
                      <div style={{width:5,height:5,borderRadius:'50%',background:st==='closed'?'#48bb78':st==='open'?'#fc8181':'#f6e05e'}}/>
                      <span style={{fontSize:9,color:'#718096'}}>{ag}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Reasoning */}
          <div style={panel}>
            <div style={panelTitle}>⚡ ZEUS REASONING</div>
            {latestSignal && (
              <div style={{background:'#0d1420',border:'1px solid #1e2d40',borderRadius:4,padding:'7px 9px',marginBottom:8}}>
                <div style={{fontSize:8,color:'#4a5568',letterSpacing:1,marginBottom:3}}>LATEST SIGNAL</div>
                <div style={{fontSize:9,color:'#63b3ed',marginBottom:3}}>{latestSignal.supplier}  ·  {latestSignal.category}  ·  {latestSignal.severity}</div>
                <div style={{fontSize:10,color:'#a0aec0',lineHeight:1.5}}>{latestSignal.headline}</div>
                {latestSignal.tickers?.length > 0 && (
                  <div style={{display:'flex',gap:4,marginTop:5,flexWrap:'wrap'}}>
                    {latestSignal.tickers.map(t=>(
                      <span key={t} style={{fontSize:9,padding:'1px 5px',borderRadius:3,background:'#0d1f36',border:'1px solid #2d4a6e',color:'#63b3ed'}}>{t}</span>
                    ))}
                  </div>
                )}
              </div>
            )}
            <div style={{background:'#0d1420',border:'1px solid #1e2d40',borderRadius:4,padding:'7px 9px',minHeight:60,marginBottom:8}}>
              <div style={{fontSize:8,color:'#4a5568',letterSpacing:1,marginBottom:4}}>DECISION REASONING</div>
              {displayed
                ? <div style={{fontSize:10,color:'#68d391',lineHeight:1.7}}>{displayed}<span style={{animation:'blink 1s step-end infinite'}}>▋</span></div>
                : <div style={{fontSize:10,color:'#2d3748'}}>No reasoning yet. Run a pipeline cycle.</div>
              }
            </div>
            {latestTrade && (
              <div style={{padding:'5px 8px',borderRadius:4,border:'1px solid #48bb7844',background:'#0d2818',fontSize:10}}>
                <span style={{color:'#48bb78',fontWeight:700}}>✓ APPROVED  </span>
                <span style={{color:'#a0aec0'}}>{latestTrade.side?.toUpperCase()} {latestTrade.symbol} @ €{latestTrade.fill??'?'}</span>
              </div>
            )}
            {latestKill && (
              <div style={{padding:'5px 8px',borderRadius:4,border:'1px solid #fc818144',background:'#1a0808',fontSize:10,marginTop:4}}>
                <span style={{color:'#fc8181',fontWeight:700}}>✗ KILLED  </span>
                <span style={{color:'#a0aec0'}}>{latestKill.stage?.toUpperCase()} — {latestKill.reason}</span>
              </div>
            )}
          </div>
        </div>
      </div>

      <div style={{textAlign:'center',fontSize:8,color:'#1e2d40',padding:'4px 0',borderTop:'1px solid #0d1117',letterSpacing:2,flexShrink:0}}>
        PANTHEON OS  ·  Claude Haiku · ChromaDB · IBKR · n8n
      </div>
    </div>
  )
}

const panel     = { background:'#0d1117', border:'1px solid #1e2d40', borderRadius:6, padding:'12px 10px' }
const panelTitle = { fontSize:8, color:'#4a5568', letterSpacing:2, marginBottom:10, fontWeight:700 }
const btn = (bg, border, color) => ({
  fontSize:10, padding:'5px 12px', cursor:'pointer',
  background:bg, border:`1px solid ${border}`, color,
  borderRadius:3, letterSpacing:1, fontFamily:'inherit',
})
