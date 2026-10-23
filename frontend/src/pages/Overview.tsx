import { useEffect, useRef, useState } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import LiquidityChart, { ChartLegend } from '../components/LiquidityChart'
import { ACTION_LABEL, dayLabel, Markdown, naira, num, PriorityBadge, Spinner, StatusBadge, timeLabel, TYPE_LABEL, usd } from '../util'

function Kpi({ label, value, meta, tone, onClick }: { label: string; value: string; meta?: string; tone?: string; onClick?: () => void }) {
  return (
    <div className={`card kpi tone-${tone || 'info'}`} onClick={onClick} style={{ cursor: onClick ? 'pointer' : undefined }}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {meta && <div className="meta">{meta}</div>}
    </div>
  )
}

export default function Overview() {
  const nav = useNav()
  const [s, setS] = useState<Any>(null)
  const [starting, setStarting] = useState(false)
  const timer = useRef<number | null>(null)
  const load = () => api.get('/summary').then(setS)
  useEffect(() => { load(); return () => { if (timer.current) window.clearInterval(timer.current) } }, [])
  useEffect(() => {
    if (s?.ai_review?.status === 'running' && !timer.current) {
      timer.current = window.setInterval(() => api.get('/summary').then((x) => {
        setS(x)
        if (x.ai_review?.status !== 'running' && timer.current) {
          window.clearInterval(timer.current); timer.current = null
          nav.toast(x.ai_review?.status === 'complete' ? 'Morning AI review complete' : 'Morning review ended: ' + (x.ai_review?.error || x.ai_review?.status), x.ai_review?.status !== 'complete')
          nav.refresh()
        }
      }), 3000)
    }
  }, [s?.ai_review?.status]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!s) return <Spinner />
  const review = s.ai_review || {}
  const gap = s.potential_payout_gap
  const startReview = async () => {
    setStarting(true)
    try { await api.post('/morning-review'); await load() } catch (e) { nav.toast((e as Error).message, true) } finally { setStarting(false) }
  }
  const auto = (s.automated_actions || []).reduce((a: number, x: Any) => a + x.n, 0)

  return (
    <div className="stack" style={{ gap: 16 }}>
      {gap.amount > 0 && (
        <div className="banner warn">
          <div style={{ fontSize: 22 }}>⚠</div>
          <div>
            <div className="strong">Payout gap of {naira(gap.amount)} projected for {dayLabel(gap.date)}</div>
            <div className="small muted">Scheduled payouts exceed projected available cash on that day. {gap.closes_if_unresolved_resolve ? 'It closes if the unresolved inflows below are resolved — ' : ''}
              {(s.liquidity_top_unresolved || []).map((u: Any) => <button key={u.case_id} className="link-chip" style={{ marginRight: 4 }} onClick={() => nav.openCase(u.case_id)}>{u.case_id} {naira(u.amount)}</button>)}</div>
          </div>
          <button className="btn right" onClick={() => nav.go('liquidity')}>Open liquidity →</button>
        </div>
      )}

      <div className="grid g4">
        <Kpi label="Expected today" value={naira(s.expected_today.amount)} meta={`${num(s.expected_today.contributions)} contributions due Wed 21 Oct`} />
        <Kpi label="Reconciled this morning" tone="ok" value={naira(s.reconciled_this_morning.amount)} meta={`${num(s.reconciled_this_morning.rule_matches)} records auto-matched by exact rules`} onClick={() => nav.go('reconciliation')} />
        <Kpi label="Unmatched inflows (suspense)" tone="warn" value={naira(s.unmatched_inflows.amount)} meta={`${s.unmatched_inflows.count} transfers awaiting a match decision`} onClick={() => nav.go('reconciliation')} />
        <Kpi label="Confirmed overdue" tone="warn" value={naira(s.confirmed_overdue.amount)} meta={`${num(s.confirmed_overdue.customers)} customers verified · ${num(s.reminders_held.customers)} reminders held`} onClick={() => nav.go('reconciliation/overdue')} />
        <Kpi label="Under investigation" tone="bad" value={naira(s.under_investigation.amount)} meta={`${s.under_investigation.cases} cases: agent cash, disputes, duplicates, settlements`} onClick={() => nav.go('exceptions')} />
        <Kpi label="Upcoming 7-day payouts" value={naira(s.upcoming_payouts_7d.amount)} meta={`${num(s.upcoming_payouts_7d.count)} scheduled payouts`} onClick={() => nav.go('liquidity')} />
        <Kpi label="Projected available cash (low point)" tone={s.projected_available_cash.amount < 0 ? 'bad' : 'ok'} value={naira(s.projected_available_cash.amount)} meta={`Lowest closing position · ${dayLabel(s.projected_available_cash.worst_day)} · today ${naira(s.projected_available_cash.available_today)}`} onClick={() => nav.go('liquidity')} />
        <Kpi label="Potential payout gap" tone={gap.amount > 0 ? 'bad' : 'ok'} value={naira(gap.amount)} meta={gap.amount > 0 ? `${dayLabel(gap.date)} · base case` : 'No gap in the 7-day horizon'} onClick={() => nav.go('liquidity')} />
      </div>

      <div className="card">
        <div className="card-h">
          <h3>Morning AI review</h3>
          <span className="hint">Supervisor → Money Match → Exception & Risk → Liquidity → Customer Action</span>
          <div className="right">
            {review.status === 'complete' && <a href={`#/trace/${review.run_id}`} className="small">View trace →</a>}
            <button className="btn primary" disabled={starting || review.status === 'running'} onClick={startReview}>
              {review.status === 'running' || starting ? <><Spinner /> Running…</> : review.status === 'complete' ? '↻ Re-run review' : '✦ Run morning AI review'}
            </button>
          </div>
        </div>
        <div className="card-pad">
          {review.status === 'complete' ? (
            <div className="msg bot" style={{ border: 0, padding: 0 }}><Markdown text={review.brief || ''} onCase={nav.openCase} onCustomer={nav.openCustomer} /></div>
          ) : review.status === 'running' ? (
            <div className="row muted"><Spinner /> Specialists are working — Money Match is judging each ambiguous transfer with compact evidence. This takes about a minute.</div>
          ) : review.status === 'error' ? (
            <div className="bad-text">Review failed: {review.error}</div>
          ) : (
            <div className="muted">The deterministic morning run has already reconciled {num(s.reconciliation_run.auto_reconciled_count)} records and opened every exception below. Run the AI review to get Money Match judgments, prioritised triage, a liquidity explanation and safe automated follow-ups.</div>
          )}
        </div>
      </div>

      <div className="grid split">
        <div className="card">
          <div className="card-h"><h3>Prioritised action queue</h3><span className="hint">deterministic priority score</span>
            <div className="right"><a href="#/exceptions" className="small">All exceptions →</a></div></div>
          <table className="t">
            <thead><tr><th>Case</th><th>Issue</th><th>Priority</th><th className="num">Amount</th><th>Status</th></tr></thead>
            <tbody>
              {(s.action_queue || []).map((c: Any) => (
                <tr key={c.case_id} className="click" onClick={() => nav.openCase(c.case_id)}>
                  <td className="mono">{c.case_id}</td>
                  <td><div className="strong">{c.title}</div><div className="small muted">{TYPE_LABEL[c.case_type]} · {c.recommended_action}</div></td>
                  <td><PriorityBadge p={c.priority} /></td>
                  <td className="num">{naira(c.amount)}</td>
                  <td><StatusBadge s={c.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="stack" style={{ gap: 16 }}>
          <div className="card">
            <div className="card-h"><h3>7-day payout position</h3><div className="right"><a href="#/liquidity" className="small">Details →</a></div></div>
            <div className="card-pad" style={{ paddingTop: 8 }}>
              <LiquidityChart days={s.liquidity_days} height={200} bars={false} />
              <ChartLegend bars={false} />
            </div>
          </div>
          <div className="card">
            <div className="card-h"><h3>Approvals waiting</h3><span className="badge b-info">{s.approvals_pending}</span><div className="right"><a href="#/approvals" className="small">Open queue →</a></div></div>
            <div className="card-pad" style={{ paddingTop: 6, paddingBottom: 8 }}>
              {(s.approvals || []).length === 0 ? <div className="small muted">No approvals pending. Run the morning AI review to generate match recommendations.</div> :
                s.approvals.map((a: Any) => (
                  <div key={a.approval_id} className="row" style={{ padding: '7px 0', borderBottom: '1px dashed var(--line)', cursor: 'pointer' }} onClick={() => nav.openCase(a.case_id)}>
                    <div><div className="strong small">{ACTION_LABEL[a.action_type] || a.action_type}</div><div className="small muted">{a.customer_name || a.case_id} {a.confidence ? `· confidence ${(a.confidence * 100).toFixed(0)}%` : ''}</div></div>
                    <div className="right strong">{naira(a.amount)}</div>
                  </div>
                ))}
            </div>
          </div>
        </div>
      </div>

      <div className="grid g2">
        <div className="card">
          <div className="card-h"><h3>Actions completed automatically</h3><span className="hint">low-risk only · simulated outbox</span></div>
          <div className="card-pad">
            <div className="row" style={{ marginBottom: 10 }}>
              <div><div className="kpi" style={{ padding: 0 }}><div className="value" style={{ fontSize: 22 }}>{num(s.reconciliation_run.auto_reconciled_count)}</div></div><div className="small muted">exact-rule ledger postings</div></div>
              <div style={{ marginLeft: 30 }}><div className="value strong" style={{ fontSize: 22 }}>{num(auto)}</div><div className="small muted">automated outbox messages</div></div>
              <div style={{ marginLeft: 30 }}><div className="value strong" style={{ fontSize: 22 }}>{num(s.reminders_held.customers)}</div><div className="small muted">false reminders prevented</div></div>
            </div>
            {(s.automated_actions || []).map((a: Any) => <div key={a.action_type} className="row small" style={{ padding: '3px 0' }}><span>{ACTION_LABEL[a.action_type] || a.action_type}</span><span className="right strong">{num(a.n)}</span></div>)}
            {!s.automated_actions?.length && <div className="small muted">No automated messages yet — reminders are queued only after payment status is verified.</div>}
          </div>
        </div>
        <div className="card">
          <div className="card-h"><h3>Recent agent activity</h3><div className="right"><a href="#/trace" className="small">Agent Trace →</a></div></div>
          <table className="t"><tbody>
            {(s.recent_runs || []).map((r: Any) => (
              <tr key={r.run_id} className="click" onClick={() => nav.go('trace/' + r.run_id)}>
                <td><div className="strong small">{r.query.slice(0, 70)}</div><div className="small muted">{r.kind.replace('_', ' ')} · {timeLabel(r.started_at)}</div></td>
                <td className="num small">{r.model_requests} calls<br /><span className="muted">{usd(r.cost_usd)}</span></td>
                <td><StatusBadge s={r.status} /></td>
              </tr>
            ))}
            {!s.recent_runs?.length && <tr><td className="muted">No agent runs yet.</td></tr>}
          </tbody></table>
        </div>
      </div>
    </div>
  )
}
