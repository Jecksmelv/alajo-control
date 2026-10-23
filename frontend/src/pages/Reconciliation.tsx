import { useEffect, useMemo, useState } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import { naira, num, PriorityBadge, Spinner, StatusBadge, TYPE_LABEL } from '../util'

const FILTERS: { id: string; label: string; test: (c: Any) => boolean }[] = [
  { id: 'exceptions', label: 'All reconciliation exceptions', test: (c) => c.case_type !== 'OVERDUE_FOLLOWUP' },
  { id: 'unmatched', label: 'Unmatched transfers', test: (c) => ['PROBABLE_MATCH', 'AMBIGUOUS_INFLOW', 'UNIDENTIFIED_INFLOW'].includes(c.case_type) },
  { id: 'agents', label: 'Field-agent cash', test: (c) => ['AGENT_SHORTFALL', 'DUPLICATE_RECEIPT', 'CUSTOMER_DISPUTE'].includes(c.case_type) },
  { id: 'duplicates', label: 'Duplicates', test: (c) => c.case_type.startsWith('DUPLICATE') },
  { id: 'settlements', label: 'Provider settlements', test: (c) => c.case_type.startsWith('PROVIDER') },
  { id: 'partial', label: 'Partial payments', test: (c) => c.case_type === 'PARTIAL_PAYMENT' },
  { id: 'overdue', label: 'Verified overdue', test: (c) => c.case_type === 'OVERDUE_FOLLOWUP' && !c.reminder_suppressed },
  { id: 'held', label: 'Reminders held (payment evidence)', test: (c) => c.case_type === 'OVERDUE_FOLLOWUP' && !!c.reminder_suppressed },
]

const ACTIVE = new Set(['open', 'awaiting_approval', 'under_investigation', 'awaiting_customer', 'info', 'reminded'])

export default function Reconciliation() {
  const nav = useNav()
  const initial = window.location.hash.split('/')[2] || 'exceptions'
  const [filter, setFilter] = useState(FILTERS.some((f) => f.id === initial) ? initial : 'exceptions')
  const [cases, setCases] = useState<Any[] | null>(null)
  const [showResolved, setShowResolved] = useState(false)
  const [q, setQ] = useState('')
  const [sending, setSending] = useState(false)
  useEffect(() => { api.get('/cases?status=all&limit=1000').then(setCases) }, [])

  const visible = useMemo(() => (cases || []).filter((c) => showResolved || ACTIVE.has(c.status)), [cases, showResolved])
  const f = FILTERS.find((x) => x.id === filter)!
  const list = visible.filter(f.test).filter((c) => !q || `${c.case_id} ${c.title} ${c.customer_name || ''} ${c.customer_id || ''}`.toLowerCase().includes(q.toLowerCase()))
  if (!cases) return <Spinner />

  const sendAll = async () => {
    setSending(true)
    try {
      const r = await api.post('/reminders/run', { style: 'auto' })
      nav.toast(`${r.reminders_queued} verified reminders queued · ${r.reminders_held_for_evidence} held for payment evidence`)
      nav.refresh()
    } catch (e) { nav.toast((e as Error).message, true) } finally { setSending(false) }
  }

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="row wrap">
        <div className="chips">
          {FILTERS.map((x) => (
            <button key={x.id} className={'chip' + (filter === x.id ? ' active' : '')} onClick={() => setFilter(x.id)}>
              {x.label}<span className="n">{visible.filter(x.test).length}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="row">
        <input className="input" style={{ maxWidth: 340 }} placeholder="Filter by case, customer or title…" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="small muted row" style={{ gap: 6 }}><input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} /> show resolved</label>
        {filter === 'overdue' && <button className="btn primary right" disabled={sending} onClick={sendAll}>{sending ? <Spinner /> : '✉'} Queue all verified reminders</button>}
        {filter === 'held' && <span className="small warn-text right">These customers look overdue in the ledger, but evidence says they may have paid. The reminder gate refuses them.</span>}
      </div>
      <div className="card">
        <div className="t-wrap">
          <table className="t">
            <thead><tr>
              <th>Case</th><th>Customer / subject</th><th className="num">Expected</th><th className="num">Candidate received</th>
              <th>Source</th><th>Confidence / status</th><th>Priority</th><th>Recommended action</th>
            </tr></thead>
            <tbody>
              {list.slice(0, 400).map((c) => (
                <tr key={c.case_id} className="click" onClick={() => nav.openCase(c.case_id)}>
                  <td><div className="mono strong">{c.case_id}</div><div className="small muted">{TYPE_LABEL[c.case_type]}</div></td>
                  <td><div className="strong">{c.customer_name || c.agent_id || '—'}</div><div className="small muted" style={{ maxWidth: 330 }}>{c.title}</div></td>
                  <td className="num">{naira(c.expected_amount)}</td>
                  <td className="num">{naira(c.received_amount)}</td>
                  <td className="small">{c.source}</td>
                  <td>
                    <StatusBadge s={c.status} />
                    {c.ai_decision && <div className="small" style={{ marginTop: 4 }}>{c.ai_decision.decision.replace(/_/g, ' ')} · {(c.ai_decision.confidence * 100).toFixed(0)}%</div>}
                    {c.needs_judgment && !c.ai_decision ? <div className="small muted" style={{ marginTop: 4 }}>awaiting Money Match</div> : null}
                  </td>
                  <td><PriorityBadge p={c.priority} /></td>
                  <td className="small" style={{ maxWidth: 260 }}>{c.recommended_action}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!list.length && <div className="empty">No cases in this view.</div>}
          {list.length > 400 && <div className="empty small">Showing 400 of {num(list.length)}.</div>}
        </div>
      </div>
    </div>
  )
}
