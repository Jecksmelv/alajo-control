import { compactNaira, dayLabel } from '../util'
import type { Any } from '../api'

// Daily closing position (base vs. if unresolved inflows resolve) with payouts / inflows bars.
export default function LiquidityChart({ days, height = 260, bars = true, buffer }: { days: Any[]; height?: number; bars?: boolean; buffer?: number }) {
  if (!days?.length) return null
  const W = 760
  const H = height
  const padL = 64, padR = 16, padT = 16, padB = 34
  const base = days.map((d) => d.closing_position as number)
  const res = days.map((d) => d.closing_position_if_unresolved_resolve as number)
  const pay = days.map((d) => (d.scheduled_payouts ?? 0) as number)
  const inf = days.map((d) => ((d.forecast_contributions ?? 0) + (d.settlements_in_transit ?? 0)) as number)
  const vals = [...base, ...res, 0, ...(bars ? [...pay, ...inf] : []), ...(buffer ? [buffer] : [])]
  let max = Math.max(...vals), min = Math.min(...vals)
  const span = max - min || 1
  max += span * 0.08; min -= span * 0.08
  const y = (v: number) => padT + (H - padT - padB) * (1 - (v - min) / (max - min))
  const step = (W - padL - padR) / days.length
  const x = (i: number) => padL + step * i + step / 2
  const path = (arr: number[]) => arr.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const ticks = 4
  const tickVals = Array.from({ length: ticks + 1 }, (_, i) => min + ((max - min) * i) / ticks)
  const bw = Math.min(18, step / 5)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Seven-day liquidity projection">
      {tickVals.map((t, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={y(t)} y2={y(t)} stroke="#ece5d8" />
          <text x={padL - 8} y={y(t) + 4} textAnchor="end" fontSize="10.5" fill="#8a909c">{compactNaira(t)}</text>
        </g>
      ))}
      <line x1={padL} x2={W - padR} y1={y(0)} y2={y(0)} stroke="#b3261e" strokeWidth="1.2" strokeDasharray="4 3" />
      <text x={W - padR} y={y(0) - 5} textAnchor="end" fontSize="10.5" fill="#b3261e">₦0</text>
      {buffer ? <><line x1={padL} x2={W - padR} y1={y(buffer)} y2={y(buffer)} stroke="#c9a24b" strokeDasharray="2 4" />
        <text x={padL + 4} y={y(buffer) - 4} fontSize="10" fill="#a8822f">operating buffer</text></> : null}
      {bars && days.map((_, i) => (
        <g key={'b' + i}>
          <rect x={x(i) - bw - 1} y={Math.min(y(inf[i]), y(0))} width={bw} height={Math.abs(y(0) - y(inf[i]))} fill="#0f6f78" opacity="0.28" rx="2" />
          <rect x={x(i) + 1} y={Math.min(y(pay[i]), y(0))} width={bw} height={Math.abs(y(0) - y(pay[i]))} fill="#15274a" opacity="0.32" rx="2" />
        </g>
      ))}
      <path d={path(res)} fill="none" stroke="#0b7a5f" strokeWidth="2" strokeDasharray="6 4" />
      <path d={path(base)} fill="none" stroke="#15274a" strokeWidth="2.6" />
      {base.map((v, i) => (
        <g key={'p' + i}>
          <circle cx={x(i)} cy={y(v)} r={v < 0 ? 5.5 : 3.8} fill={v < 0 ? '#b3261e' : '#15274a'} stroke="#fff" strokeWidth="1.5" />
          {v < 0 && <text x={x(i)} y={y(v) + 18} textAnchor="middle" fontSize="11" fontWeight="700" fill="#b3261e">{compactNaira(v)}</text>}
        </g>
      ))}
      {days.map((d, i) => <text key={'l' + i} x={x(i)} y={H - 12} textAnchor="middle" fontSize="11" fill="#5d6574">{dayLabel(d.date)}</text>)}
    </svg>
  )
}

export function ChartLegend({ bars = true }: { bars?: boolean }) {
  return (
    <div className="chart-legend">
      <span><span className="sw" style={{ background: '#15274a' }} />Closing position (base case)</span>
      <span><span className="sw" style={{ background: '#0b7a5f' }} />If unresolved inflows resolve</span>
      {bars && <><span><span className="sw" style={{ background: 'rgba(15,111,120,0.35)' }} />Expected inflows</span>
        <span><span className="sw" style={{ background: 'rgba(21,39,74,0.35)' }} />Scheduled payouts</span></>}
    </div>
  )
}
