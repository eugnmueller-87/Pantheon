// Real equity curve via lightweight-charts: area series for total_equity with a
// peak_equity line overlay. Reads the equity_snapshots time-series (Phase 2).
import { useEffect, useRef } from 'react'
import { createChart } from 'lightweight-charts'

export function EquityChartLW({ data }) {
  const ref = useRef(null)
  const chartRef = useRef(null)

  useEffect(() => {
    if (!ref.current) return
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: '#4a5568', fontFamily: "'Courier New', monospace" },
      grid: { vertLines: { color: '#0f1420' }, horzLines: { color: '#0f1420' } },
      rightPriceScale: { borderColor: '#1a2540' },
      timeScale: { borderColor: '#1a2540', timeVisible: true },
      crosshair: { mode: 0 },
    })
    chartRef.current = chart
    const area = chart.addAreaSeries({
      lineColor: '#63b3ed', topColor: '#63b3ed40', bottomColor: '#63b3ed00', lineWidth: 2,
      priceFormat: { type: 'price', precision: 0, minMove: 1 },
    })
    const peak = chart.addLineSeries({ color: '#f6ad55', lineWidth: 1, lineStyle: 2, priceLineVisible: false })
    chart.__area = area
    chart.__peak = peak
    return () => { chart.remove(); chartRef.current = null }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !data) return
    // lightweight-charts wants {time, value} sorted ascending; time as unix secs.
    const toTime = r => Math.floor(new Date(r.recorded_at).getTime() / 1000)
    const seen = new Set()
    const rows = [...data]
      .filter(r => r.recorded_at && r.total_equity != null)
      .map(r => ({ t: toTime(r), eq: Number(r.total_equity), pk: Number(r.peak_equity ?? r.total_equity) }))
      .sort((a, b) => a.t - b.t)
      .filter(r => { if (seen.has(r.t)) return false; seen.add(r.t); return true }) // unique, ascending times
    chart.__area.setData(rows.map(r => ({ time: r.t, value: r.eq })))
    chart.__peak.setData(rows.map(r => ({ time: r.t, value: r.pk })))
    if (rows.length) chart.timeScale().fitContent()
  }, [data])

  if (!data || data.length === 0) {
    return <div style={{ color: 'var(--text-mute)', fontSize: 11, textAlign: 'center', paddingTop: 40 }}>Collecting equity snapshots…</div>
  }
  return <div ref={ref} style={{ width: '100%', height: '100%', minHeight: 220 }} />
}
