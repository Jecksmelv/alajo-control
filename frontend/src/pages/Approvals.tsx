import { useEffect, useState } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import { ACTION_LABEL, naira, Spinner, StatusBadge, timeLabel, TYPE_LABEL } from '../util'

function ApprovalCard({ a, onDone }: { a: Any; onDone: () => void }) {
  const nav = useNav()
  const [busy, setBusy] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const d = a.ai_decision
  const decide = async (decision: 'approve' | 'reject') => {
    setBusy(decision)
    try {
      const r = await api.post(`/approvals/${a.approval_id}/decision`, { decision, note: note || undefined, decided_by: 'ops.manager' })
      nav.toast(decision === 'approve' ? `Approved ${a.approval_id}${r.result?.posted ? ` — ${naira(r.result.ledger_amount)} posted to ledger; acknowledgment queued` : ' — executed'}` : `Rejected ${a.approval_id} — logged`)
      onDone()
    } catch (e) { nav.toast((e as Error).message, true) } finally { setBusy(null) }
  }
  const ev = a.evidence?.deterministic_evidence || {}
  return (
    <div className="card">
      <div className="card-h">
        <span className="badge b-info">Human approval required</span>
        <h3>{ACTION_LABEL[a.action_type] || a.action_type}</h3>
        <span className="mono small muted">{a.approval_id}</span>
        <div className="right"><StatusBadge s={a.status} /></div>
      </div>
      <div className="card-pad stack" style={{ gap: 12 }}>
        <div className="kv">
          <div className="k">Case</div><div><button className="link-chip" onClick={() => nav.openCase(a.case_id)}>{a.case_id}</button> {TYPE_LABEL[a.case_type]} — {a.case_title}</div>
          <div className="k">Customer</div><div>{a.customer_id ? <><button className="link-chip cust" onClick={() => nav.openCustomer(a.customer_id)}>{a.customer_id}</button> {a.customer_name}</> : '—'}</div>
          <div className="k">Financial amount</div><div className="strong">{naira(a.amount)}</div>
          <div className="k">Requested by</div><div>{a.requested_by} · {timeLabel(a.created_at)}</div>
          <div className="k">Why approval is required</div><div className="warn-text">{a.reason_required}</div>
        </div>
        <div className="judgment">
          <div className="row"><b>Agent recommendation</b>{a.confidence != null && <span className="right small muted">confidence {(a.confidence * 100).toFixed(0)}%</span>}</div>
          {a.confidence != null && <div className="conf"><span style={{ width: `${a.confidence * 100}%` }} /></div>}
          <p style={{ margin: '8px 0 0' }}>{a.recommendation}</p>
          {d?.evidence_used?.length ? <ul className="small" style={{ margin: '6px 0 0', paddingLeft: 18 }}>{d.evidence_used.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul> : null}
        </div>
        <div className="small"><b>Deterministic evidence:</b> {a.evidence?.case_summary}</div>
        {a.payload?.bank_txn_id && <div className="small muted">Proposed posting: transfer <span className="mono">{a.payload.bank_txn_id}</span> → contribution <span className="mono">{a.payload.contribution_id}</span> · amount re-verified against SQL at execution{ev.bank_txn ? '' : ''}</div>}
        {a.status === 'pending' ? (
          <div className="row">
            <input className="input" placeholder="Decision note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
            <button className="btn approve" disabled={!!busy} onClick={() => decide('approve')}>{busy === 'approve' ? <Spinner /> : '✓'} Approve</button>
            <button className="btn reject" disabled={!!busy} onClick={() => decide('reject')}>{busy === 'reject' ? <Spinner /> : '✕'} Reject</button>
          </div>
        ) : (
          <div className="control-box ok small">Decided by <b>{a.decided_by}</b> at {timeLabel(a.decided_at)}{a.decision_note ? ` — “${a.decision_note}”` : ''}. Result: <span className="mono">{JSON.stringify(a.execution_result)}</span></div>
        )}
      </div>
    </div>
  )
}

export default function Approvals() {
  const [status, setStatus] = useState('pending')
  const [list, setList] = useState<Any[] | null>(null)
  const load = () => api.get(`/approvals?status=${status}`).then(setList)
  useEffect(() => { setList(null); load() }, [status]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="row">
        <div className="chips">{['pending', 'approved', 'rejected', 'all'].map((s) => <button key={s} className={'chip' + (status === s ? ' active' : '')} onClick={() => setStatus(s)}>{s}</button>)}</div>
        <span className="small muted right">Approval is enforced in code: agents can only request; this queue is the only path that executes material actions.</span>
      </div>
      {!list ? <Spinner /> : list.length === 0 ? <div className="card empty">No {status} approvals. Run the morning AI review on the Control Room to generate recommendations.</div> :
        <div className="grid g2">{list.map((a) => <ApprovalCard key={a.approval_id} a={a} onDone={load} />)}</div>}
    </div>
  )
}
