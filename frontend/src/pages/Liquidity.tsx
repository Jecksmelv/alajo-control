import { useEffect, useState } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import LiquidityChart, { ChartLegend } from '../components/LiquidityChart'
import { dayLabel, naira, num, pct, PriorityBadge, Spinner } from '../util'

export default function Liquidity() {
  const nav = useNav()
  const [p, setP] = useState<Any>(null)
  const [busy, setBusy] = useState(false)
  const load = () => api.get('/liquidity').then(setP)
  useEffect(() => { load() }, [])
  if (!p) return <Spinner />
  const explain = async () => {
    setBusy(true)
    try { const r = await api.post('/liquidity/explain'); if (!r.brief) nav.toast('Liquidity agent did not return a brief: ' + r.stop_reason, true); await load() }
    catch (e) { nav.toast((e as Error).message, true) } finally { setBusy(false) }
  }
  const b = p.brief
  const byType: Record<string, Record<string, number>> = {}
  ;(p.obligations?.by_day_and_type || []).forEach((r: Any) => { byType[r.due_date] = byType[r.due_date] || {}; byType[r.due_date][r.payout_type] = r.amount })
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid g4">
        <div className="card kpi"><div className="label">Cleared payout-account balance</div><div className="value">{naira(p.cleared_balance)}</div><div className="meta">as of {p.as_of.replace('T', ' ')}</div></div>
        <div className="card kpi tone-warn"><div className="label">Restricted (suspense)</div><div className="value">{naira(p.suspense_restricted)}</div><div className="meta">unmatched transfers cannot fund payouts</div></div>
        <div className="card kpi tone-ok"><div className="label">Available today</div><div className="value">{naira(p.available_today)}</div><div className="meta">28-day collection rate {pct(p.collection_rate_28d, 1)}</div></div>
        <div className={'card kpi ' + (p.payout_gap > 0 ? 'tone-bad' : 'tone-ok')}><div className="label">Potential payout gap</div><div className="value">{naira(p.payout_gap)}</div><div className="meta">{p.gap_date ? `${dayLabel(p.gap_date)} · base case` : 'none in horizon'}</div></div>
      </div>

      <div className="grid split">
        <div className="card">
          <div className="card-h"><h3>Projected daily position — next 7 days</h3><span className="hint">deterministic projection</span></div>
          <div className="card-pad"><LiquidityChart days={p.days} height={300} buffer={p.operating_buffer} /><ChartLegend /></div>
        </div>
        <div className="card">
          <div className="card-h"><h3>Liquidity &amp; Payout agent</h3>
            <div className="right"><button className="btn small primary" disabled={busy} onClick={explain}>{busy ? <Spinner /> : '✦'} {b ? 'Refresh explanation' : 'Explain position'}</button></div></div>
          <div className="card-pad">
            {b ? (
              <>
                <div className="strong" style={{ fontSize: 15.5 }}>{b.headline}</div>
                <p>{b.explanation}</p>
                {b.recommended_actions?.length ? <ul style={{ paddingLeft: 18 }}>{b.recommended_actions.map((x: string) => <li key={x} className="small">{x}</li>)}</ul> : null}
                <div className="row wrap small">
                  {(b.key_case_ids || []).map((id: string) => <button key={id} className="link-chip" onClick={() => nav.openCase(id)}>{id}</button>)}
                  <span className={'badge ' + (b.grounding?.grounded ? 'b-ok' : 'b-warn')}>{b.grounding?.grounded ? `all ${b.grounding.amounts_found} figures traced to tools` : 'unverified figure detected'}</span>
                  {b.run_id && <a href={`#/trace/${b.run_id}`}>trace →</a>}
                </div>
                {b.figures_basis_gap != null && b.figures_basis_gap !== p.payout_gap && <div className="small warn-text" style={{ marginTop: 8 }}>Written when the gap was {naira(b.figures_basis_gap)}; approvals since then changed it to {naira(p.payout_gap)}. Click Refresh explanation.</div>}
              </>
            ) : <div className="small muted">The agent explains the projection using only figures returned by the liquidity tools. It never does the arithmetic.</div>}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-h"><h3>Daily build-up</h3><span className="hint">{p.method}</span></div>
        <table className="t">
          <thead><tr><th>Day</th><th className="num">Settlements in transit</th><th className="num">Contributions due</th><th className="num">Forecast inflows</th><th className="num">Scheduled payouts</th><th className="num">Payouts</th><th className="num">Closing (base)</th><th className="num">If unresolved resolve</th></tr></thead>
          <tbody>{p.days.map((d: Any) => (
            <tr key={d.date} style={d.closing_position < 0 ? { background: '#fdf1ef' } : undefined}>
              <td className="strong">{dayLabel(d.date)}</td>
              <td className="num">{naira(d.settlements_in_transit)}</td>
              <td className="num">{naira(d.contributions_due)}</td>
              <td className="num">{naira(d.forecast_contributions)}</td>
              <td className="num">{naira(d.scheduled_payouts)}</td>
              <td className="num">{num(d.payout_count)}</td>
              <td className={'num strong ' + (d.closing_position < 0 ? 'bad-text' : '')}>{naira(d.closing_position)}</td>
              <td className="num ok-text">{naira(d.closing_position_if_unresolved_resolve)}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>

      <div className="grid g2">
        <div className="card">
          <div className="card-h"><h3>Unresolved inflows that can change the forecast</h3><span className="badge b-warn">{naira(p.unresolved_inflows_total)}</span></div>
          <table className="t"><tbody>{p.unresolved_inflows.map((u: Any) => (
            <tr key={u.case_id} className="click" onClick={() => nav.openCase(u.case_id)}>
              <td className="mono">{u.case_id}{(p.minimum_cases_to_close_gap || []).includes(u.case_id) && <div><span className="badge b-ok">closes gap</span></div>}</td>
              <td><div className="strong small">{u.title}</div><div className="small muted">{u.kind}</div></td>
              <td><PriorityBadge p={u.priority} /></td>
              <td className="num strong">{naira(u.amount)}</td>
            </tr>
          ))}</tbody></table>
        </div>
        <div className="card">
          <div className="card-h"><h3>Scheduled payouts by type</h3><span className="hint">{num(p.obligations.total_payouts)} payouts · {naira(p.obligations.total_amount)}</span></div>
          <table className="t"><thead><tr><th>Day</th><th className="num">Cycle completion</th><th className="num">School fees target</th><th className="num">Rent target</th></tr></thead>
            <tbody>{Object.entries(byType).map(([d, v]) => (
              <tr key={d}><td>{dayLabel(d)}</td><td className="num">{naira(v.cycle_completion || 0)}</td><td className="num">{naira(v.school_fees_target || 0)}</td><td className="num">{naira(v.rent_target || 0)}</td></tr>
            ))}</tbody></table>
        </div>
      </div>
    </div>
  )
}
