import { useEffect, useState } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import { ACTION_LABEL, dayLabel, naira, pct, PriorityBadge, Spinner, StatusBadge, timeLabel, TYPE_LABEL } from '../util'

function Customer360({ id }: { id: string }) {
  const nav = useNav()
  const [p, setP] = useState<Any>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { api.get(`/customers/${id}`).then(setP).catch((e) => setErr(e.message)) }, [id])
  if (err) return <div className="bad-text">{err}</div>
  if (!p) return <Spinner />
  const flagged = (p.cases || []).filter((c: Any) => !['resolved', 'info'].includes(c.status))
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row"><a href="#/customers" className="small">← All customers</a></div>
      <div className="card card-pad">
        <div className="row wrap" style={{ gap: 18 }}>
          <div className="brand-mark" style={{ width: 48, height: 48, fontSize: 17, borderRadius: 12 }}>{p.name.split(' ').map((x: string) => x[0]).join('')}</div>
          <div>
            <div style={{ fontSize: 21, fontWeight: 720, letterSpacing: -0.4 }}>{p.name}</div>
            <div className="muted small">{p.customer_id} · {p.location} · joined {p.join_date} · {p.preferred_channel.replace('_', ' ')} · prefers {p.preferred_language === 'pcm' ? 'Nigerian Pidgin' : 'English'} · contact {p.contact_masked}</div>
          </div>
          <div className="right" style={{ textAlign: 'right' }}>
            <div className="strong">{p.plan.name}</div>
            <div className="small muted">{naira(p.plan.contribution_amount)} {p.plan.frequency} · {p.plan.payout_logic}</div>
          </div>
        </div>
      </div>
      <div className="grid g4">
        <div className="card kpi tone-ok"><div className="label">Recorded savings (28-day window)</div><div className="value">{naira(p.balance.recorded_savings_in_window)}</div><div className="meta">expected {naira(p.balance.expected_to_date_in_window)}</div></div>
        <div className={'card kpi ' + (p.balance.overdue_outstanding ? 'tone-warn' : 'tone-ok')}><div className="label">Overdue outstanding</div><div className="value">{naira(p.balance.overdue_outstanding)}</div><div className="meta">{p.balance.overdue_contributions} contributions · max {p.balance.max_days_late} days late</div></div>
        <div className="card kpi"><div className="label">On-time rate</div><div className="value">{pct(p.payment_history.on_time_rate)}</div><div className="meta">{p.payment_history.on_time} on time · {p.payment_history.late} late · {p.payment_history.missed_or_short} missed</div></div>
        <div className="card kpi"><div className="label">Upcoming payout</div><div className="value">{p.upcoming_payouts[0] ? naira(p.upcoming_payouts[0].amount) : '—'}</div><div className="meta">{p.upcoming_payouts[0] ? `${dayLabel(p.upcoming_payouts[0].due_date)} · ${p.upcoming_payouts[0].payout_type.replace(/_/g, ' ')}` : 'none in 7 days'}</div></div>
      </div>
      {flagged.length > 0 && (
        <div className="card">
          <div className="card-h"><h3>Why is this customer flagged?</h3><span className="hint">evidence-based cases, not a risk label</span></div>
          <table className="t"><tbody>{flagged.map((c: Any) => (
            <tr key={c.case_id} className="click" onClick={() => nav.openCase(c.case_id)}>
              <td className="mono">{c.case_id}</td><td><div className="strong">{c.title}</div><div className="small muted">{TYPE_LABEL[c.case_type]}{c.reminder_suppressed ? ' · reminder held: payment evidence exists' : ''}</div></td>
              <td><PriorityBadge p={c.priority} /></td><td><StatusBadge s={c.status} /></td><td className="num">{naira(c.amount)}</td>
            </tr>
          ))}</tbody></table>
        </div>
      )}
      <div className="grid g2">
        <div className="card">
          <div className="card-h"><h3>Expected vs received contributions</h3></div>
          <div className="t-wrap" style={{ maxHeight: 380 }}><table className="t"><thead><tr><th>Due</th><th className="num">Expected</th><th className="num">Received</th><th>Status</th></tr></thead>
            <tbody>{p.contributions.map((c: Any) => (
              <tr key={c.contribution_id}><td>{c.due_date}<div className="small muted mono">{c.contribution_id}</div></td><td className="num">{naira(c.expected_amount)}</td><td className="num">{naira(c.paid)}</td>
                <td><span className={'badge ' + (c.status === 'paid' ? 'b-ok' : c.status === 'partial' ? 'b-warn' : 'b-neutral')}>{c.status}</span></td></tr>
            ))}</tbody></table></div>
        </div>
        <div className="card">
          <div className="card-h"><h3>Payment history (ledger)</h3><span className="hint">append-only</span></div>
          <div className="t-wrap" style={{ maxHeight: 380 }}><table className="t"><tbody>{p.ledger.map((l: Any) => (
            <tr key={l.entry_id}><td className="small">{timeLabel(l.entry_time)}</td><td className="small">{l.source_type.replace('_', ' ')}<div className="muted mono">{l.source_ref}</div></td>
              <td className="small">{l.posted_by}{l.approval_id && <div className="mono muted">{l.approval_id}</div>}</td><td className="num strong">{naira(l.amount)}</td></tr>
          ))}</tbody></table></div>
        </div>
      </div>
      <div className="grid g2">
        <div className="card">
          <div className="card-h"><h3>Recent messages</h3><span className="hint">untrusted customer content</span></div>
          <div className="card-pad">{p.messages.length ? p.messages.map((m: Any) => (
            <div key={m.message_id} className="quote" style={{ marginBottom: 8 }}><span className="tag">{m.channel} · {m.message_id} · {timeLabel(m.msg_time)} · intent: {(m.intent || '').replace(/_/g, ' ')}</span>“{m.text}”</div>
          )) : <div className="small muted">No recent messages.</div>}</div>
        </div>
        <div className="card">
          <div className="card-h"><h3>Action history</h3></div>
          <div className="card-pad">
            {p.outbox.map((o: Any) => <div key={o.message_id} className="ev"><div className="ev-icon ok">✉</div><div><div className="ev-title small">{ACTION_LABEL[o.action_type]}</div><div className="ev-sub">{o.content}</div><div className="small faint">{timeLabel(o.created_at)} · {o.automated ? 'automated' : 'approval-triggered'}</div></div></div>)}
            {p.approvals.map((a: Any) => <div key={a.approval_id} className="ev"><div className="ev-icon">✓</div><div><div className="ev-title small">{ACTION_LABEL[a.action_type]} <StatusBadge s={a.status} /></div><div className="ev-sub">{a.approval_id} · {naira(a.amount)} {a.decided_by ? `· ${a.decided_by}` : ''}</div></div></div>)}
            {!p.outbox.length && !p.approvals.length && <div className="small muted">No actions yet.</div>}
          </div>
        </div>
      </div>
    </div>
  )
}

export default function Customers({ customerId }: { customerId?: string }) {
  const [q, setQ] = useState('')
  const [list, setList] = useState<Any[] | null>(null)
  useEffect(() => {
    if (customerId) return
    const t = window.setTimeout(() => api.get('/customers' + (q ? `?q=${encodeURIComponent(q)}` : '')).then(setList), 200)
    return () => window.clearTimeout(t)
  }, [q, customerId])
  if (customerId) return <Customer360 id={customerId} />
  return (
    <div className="stack" style={{ gap: 14 }}>
      <input className="input" style={{ maxWidth: 420 }} placeholder="Search 5,000 savers by name or id (e.g. Aisha Bello, C01842)…" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="card">
        <div className="card-h"><h3>{q ? 'Search results' : 'Customers with open exceptions'}</h3></div>
        <table className="t"><tbody>
          {(list || []).map((c: Any, i: number) => (
            <tr key={c.customer_id + i} className="click" onClick={() => { window.location.hash = '#/customers/' + c.customer_id }}>
              <td className="mono">{c.customer_id}</td><td className="strong">{c.name}</td>
              <td className="small muted">{c.flag || `${c.location} · ${c.plan_id}`}</td>
            </tr>
          ))}
        </tbody></table>
        {!list && <div className="empty"><Spinner /></div>}
      </div>
    </div>
  )
}
