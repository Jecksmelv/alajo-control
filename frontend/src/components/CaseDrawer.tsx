import { useEffect, useState, type ReactNode } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import { ACTION_LABEL, naira, PriorityBadge, Spinner, StatusBadge, timeLabel, TypeBadge } from '../util'

function Ev({ tone, icon, title, sub, children }: { tone?: string; icon: string; title: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="ev">
      <div className={'ev-icon ' + (tone || '')}>{icon}</div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="ev-title">{title}</div>
        {sub && <div className="ev-sub">{sub}</div>}
        {children}
      </div>
    </div>
  )
}

function Quote({ m }: { m: Any }) {
  return (
    <div className="quote">
      <span className="tag">Untrusted customer content · {m.channel || 'message'} · {m.message_id} · {timeLabel(m.msg_time)}</span>
      “{m.text}”
      {m.instruction_like_content_removed && <div className="small bad-text" style={{ marginTop: 4 }}>Instruction-like text was neutralized by the safety filter before any agent saw it.</div>}
    </div>
  )
}

function EvidenceFor({ c }: { c: Any }) {
  const ev = c.evidence || {}
  const cands = c.candidates?.candidates || []
  const top = cands[0]
  switch (c.case_type) {
    case 'PROBABLE_MATCH':
    case 'AMBIGUOUS_INFLOW':
    case 'UNIDENTIFIED_INFLOW': {
      const t = c.candidates?.bank_txn || ev.bank_txn || {}
      const supported = cands.filter((x: Any) => x.signal_score >= 30)
      return (
        <>
          {top && c.case_type !== 'UNIDENTIFIED_INFLOW' && (
            <Ev icon="≡" title={`Ledger: ${top.customer_name} owes ${naira(top.outstanding)} (due ${top.due_date})`}
              sub={`Expected contribution ${top.contribution_id} is unpaid in the internal ledger.`} />
          )}
          <Ev tone="ok" icon="₦" title={`Bank transfer ${t.bank_txn_id}: ${naira(t.amount)} from ${t.sender_name}`}
            sub={<>Reference “{t.reference}” · {timeLabel(t.txn_time)} · import {t.import_batch} · no customer reference, so not auto-posted</>} />
          {supported.map((s: Any) => (
            <Ev key={s.contribution_id} tone={s.message_names_sender || s.sender_name_signal === 'full_name' ? 'ok' : 'warn'} icon="✓"
              title={`${s.customer_name} (${s.customer_id}) — signal score ${s.signal_score}`}
              sub={<>Sender-name signal: <b>{s.sender_name_signal.replace(/_/g, ' ')}</b> · message names sender: <b>{s.message_names_sender ? 'yes' : 'no'}</b> · transfer {s.days_between_due_and_transfer} day(s) after due date</>}>
              {(s.messages || []).slice(0, 2).map((m: Any) => <Quote key={m.message_id} m={m} />)}
            </Ev>
          ))}
          <Ev tone={supported.length > 1 ? 'bad' : 'ok'} icon={supported.length > 1 ? '!' : '○'}
            title={supported.length > 1 ? `Conflict: ${supported.length} customers are equally supported` : supported.length === 1 ? 'No conflicting evidence' : 'No supported candidate'}
            sub={`${c.candidates?.candidate_count_same_amount ?? 0} open contributions have the same outstanding amount; ${supported.length} ${supported.length === 1 ? 'has' : 'have'} a name or message link. Deterministic assessment: ${String(c.candidates?.deterministic_assessment || '').replace(/_/g, ' ')}.`} />
        </>
      )
    }
    case 'AGENT_SHORTFALL':
      return (
        <>
          <Ev icon="≡" title={`${ev.agent_id} (${ev.agent_name}) reported ${(ev.collections || []).length} cash collections = ${naira(ev.reported_total)}`} sub={`Collection day ${ev.day}`}>
            <table className="t" style={{ marginTop: 8 }}><thead><tr><th>Collection</th><th>Customer</th><th>Receipt</th><th className="num">Amount</th></tr></thead>
              <tbody>{(ev.collections || []).map((x: Any) => <tr key={x.collection_id}><td className="mono">{x.collection_id}</td><td>{x.customer_name}</td><td className="mono">{x.receipt_no}</td><td className="num">{naira(x.amount_reported)}</td></tr>)}</tbody></table>
          </Ev>
          <Ev tone="bad" icon="₦" title={`Remittance ${ev.remittance?.bank_txn_id}: ${naira(ev.remittance?.amount)} (“${ev.remittance?.reference}”)`} sub={`Shortfall ${naira(ev.shortfall)} — collections held, not credited; affected customers are not chased.`} />
          {(ev.collections || []).filter((x: Any) => x.agent_note).map((x: Any) => (
            <Ev key={x.collection_id} tone="warn" icon="✎" title={`Field-agent note on ${x.collection_id}`}>
              <div className="quote"><span className="tag">Untrusted field-agent note</span>“{x.agent_note.text}”
                {x.agent_note.instruction_like_content_removed && <div className="small bad-text" style={{ marginTop: 4 }}>Embedded instruction to the AI was removed; it cannot close this case.</div>}</div>
            </Ev>
          ))}
        </>
      )
    case 'DUPLICATE_RECEIPT':
    case 'DUPLICATE_IMPORT':
      return (
        <Ev tone="warn" icon="⧉" title={`${(ev.records || []).length} identical records — 1 kept, ${(ev.quarantined_records || ev.excluded_records || []).length} excluded`} sub="The customer is credited once.">
          <table className="t" style={{ marginTop: 8 }}><tbody>{(ev.records || []).map((r: Any) => {
            const id = r.bank_txn_id || r.collection_id
            const kept = id === ev.kept_record
            return <tr key={id}><td className="mono">{id}</td><td>{r.import_batch || r.receipt_no}</td><td>{timeLabel(r.txn_time || r.collection_time)}</td><td className="num">{naira(r.amount || r.amount_reported)}</td><td>{kept ? <span className="badge b-ok">kept</span> : <span className="badge b-bad">excluded</span>}</td></tr>
          })}</tbody></table>
        </Ev>
      )
    case 'PROVIDER_SETTLEMENT_DELAYED':
    case 'PROVIDER_SETTLEMENT_PENDING':
      return (
        <>
          <Ev tone={c.case_type === 'PROVIDER_SETTLEMENT_DELAYED' ? 'bad' : 'ok'} icon="⏱" title={`${ev.batch_id}: ${ev.payments} payments, ${naira(ev.total)} — ${ev.customers_affected} customers`} sub={`${ev.provider} · expected settlement ${ev.expected_settlement_date}`} />
          <Ev icon="≡" title="Sample of provider-confirmed payments">
            <table className="t" style={{ marginTop: 8 }}><tbody>{(ev.sample || []).map((s: Any) => <tr key={s.settlement_id}><td className="mono">{s.settlement_id}</td><td className="mono">{s.customer_id}</td><td>{timeLabel(s.payment_time)}</td><td className="num">{naira(s.amount)}</td></tr>)}</tbody></table>
          </Ev>
        </>
      )
    case 'PARTIAL_PAYMENT':
      return (
        <>
          <Ev tone="ok" icon="₦" title={`Transfer ${ev.bank_txn?.bank_txn_id}: ${naira(ev.bank_txn?.amount)} with exact reference “${ev.bank_txn?.reference}”`} sub="Posted automatically by rule R1 (exact reference)." />
          {(ev.allocation || []).map((a: Any) => <Ev key={a.contribution_id} icon="≡" title={`Allocated ${naira(a.amount)} to ${a.contribution_id} (due ${a.due_date})`} sub={`Expected ${naira(a.expected_amount)} → outstanding ${naira(a.outstanding_after)} (deterministic)`} />)}
        </>
      )
    case 'CUSTOMER_DISPUTE':
      return (
        <>
          <Ev tone="warn" icon="✉" title="Customer reports a cash payment to the field agent"><Quote m={ev.message} /></Ev>
          {(ev.disputed_contributions || []).map((d: Any) => <Ev key={d.contribution_id} icon="≡" title={`Ledger: ${d.contribution_id} due ${d.due_date} — ${naira(d.outstanding)} unpaid`} />)}
          <Ev tone="bad" icon="!" title={`Conflict: agent ${c.agent_id} has no collection for this customer on the disputed day`}
            sub={`Agent records in the last 3 days: ${(ev.agent_daily_records || []).map((r: Any) => `${r.day}: ${r.n} collections`).join(' · ')}. Customer's own agent records: ${(ev.customer_agent_records_last_3_days || []).map((r: Any) => r.collection_time.slice(0, 10)).join(', ') || 'none'}.`} />
        </>
      )
    case 'MESSAGE_SECURITY_FLAG':
    case 'PRIVACY_REQUEST':
      return (
        <>
          <Ev tone="bad" icon="⚠" title={c.case_type === 'PRIVACY_REQUEST' ? 'Request for another customer’s information' : 'Message attempted to instruct the system'}><Quote m={ev.message} /></Ev>
          <Ev tone="ok" icon="🔒" title={c.case_type === 'PRIVACY_REQUEST' ? 'Policy: decline; never disclose another customer’s data' : 'No record was changed'}
            sub={c.case_type === 'PRIVACY_REQUEST' ? 'Agents never receive contact details; the reply is a fixed policy template.' : 'Customer text is data, not instructions. Ledger changes are only possible through the human approval path.'} />
        </>
      )
    case 'OVERDUE_FOLLOWUP': {
      const ch = ev.checks || {}
      return (
        <>
          {(ev.overdue_contributions || []).map((o: Any) => <Ev key={o.contribution_id} icon="≡" title={`${o.contribution_id}: ${naira(o.outstanding)} outstanding, ${o.days_late} days late`} sub={`Due ${o.due_date} · expected ${naira(o.expected_amount)}`} />)}
          <Ev tone={ev.suppression_reasons?.length ? 'warn' : 'ok'} icon="✓" title="Deterministic checks before any reminder"
            sub={<>Pending provider settlement: <b>{ch.pending_provider_settlement ? 'yes' : 'no'}</b> · candidate unmatched transfer: <b>{ch.candidate_unmatched_transfer ? 'yes' : 'no'}</b> · agent collection held: <b>{ch.agent_collection_on_record ? 'yes' : 'no'}</b> · open dispute: <b>{ch.open_dispute ? 'yes' : 'no'}</b></>} />
          {ev.suppression_reasons?.length ? (
            <Ev tone="warn" icon="✋" title="Reminder held — the ledger says unpaid, but:">
              <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>{ev.suppression_reasons.map((r: string) => <li key={r}>{r}</li>)}</ul>
            </Ev>
          ) : null}
          {ev.payment_history && <Ev icon="◷" title="Payment history (28 days)" sub={`${ev.payment_history.on_time} on time · ${ev.payment_history.late} late · ${ev.payment_history.missed_or_short} missed/short of ${ev.payment_history.contributions_due} due. Operational context only, not a credit score.`} />}
        </>
      )
    }
    default:
      return <pre className="json">{JSON.stringify(ev, null, 2)}</pre>
  }
}

export default function CaseDrawer({ caseId, onClose }: { caseId: string; onClose: () => void }) {
  const nav = useNav()
  const [c, setC] = useState<Any>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const load = () => api.get(`/cases/${caseId}`).then(setC).catch((e) => setErr(e.message))
  useEffect(() => { setC(null); load() }, [caseId]) // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (label: string, fn: () => Promise<Any>, okMsg: (r: Any) => string) => {
    setBusy(label)
    try {
      const r = await fn()
      nav.toast(okMsg(r))
      await load()
      nav.refresh()
    } catch (e) {
      nav.toast((e as Error).message, true)
    } finally {
      setBusy(null)
    }
  }

  const d = c?.ai_decision
  const pending = (c?.approvals || []).filter((a: Any) => a.status === 'pending')
  const top = c?.candidates?.candidates?.[0]
  const expected = c?.expected_amount ?? top?.outstanding
  const received = c?.received_amount
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer">
        <div className="drawer-h">
          <span className="mono strong">{caseId}</span>
          {c && <TypeBadge t={c.case_type} />}
          {c && <PriorityBadge p={c.priority} />}
          {c && <StatusBadge s={c.status} />}
          <button className="btn small right" onClick={onClose}>Close ✕</button>
        </div>
        {!c ? <div className="drawer-b">{err ? <span className="bad-text">{err}</span> : <Spinner />}</div> : (
          <div className="drawer-b">
            <div>
              <h2 style={{ margin: '0 0 4px', fontSize: 19, letterSpacing: -0.3 }}>{c.title}</h2>
              <div className="muted">{c.summary}</div>
              {c.customer_id && <div style={{ marginTop: 8 }}><button className="link-chip cust" onClick={() => nav.openCustomer(c.customer_id)}>{c.customer_id}</button> <span className="small muted">{c.customer_name} · open Customer 360</span></div>}
            </div>
            <div className="money">
              <div><div className="label">Expected</div><div className="v">{naira(expected)}</div></div>
              <div><div className="label">{c.case_type.includes('INFLOW') || c.case_type === 'PROBABLE_MATCH' ? 'Candidate received' : 'Received / recorded'}</div><div className="v">{naira(received)}</div></div>
              <div><div className="label">{c.case_type === 'AGENT_SHORTFALL' ? 'Shortfall' : 'Case amount'}</div><div className="v">{naira(c.amount)}</div></div>
            </div>

            <div className="card card-pad">
              <div className="section-title" style={{ marginTop: 0 }}>Why? — evidence</div>
              <EvidenceFor c={c} />
            </div>

            {(c.case_type === 'PROBABLE_MATCH' || c.case_type === 'AMBIGUOUS_INFLOW') && (
              <div className="judgment">
                <div className="row"><b>Money Match agent judgment</b>
                  {!d && <button className="btn small primary right" disabled={!!busy} onClick={() => act('review', () => api.post(`/cases/${caseId}/review`), (r) => r.error ? `Review failed: ${r.error}` : `Money Match: ${r.decision?.decision?.replace(/_/g, ' ')}`)}>{busy === 'review' ? <Spinner /> : '✦'} Run Money Match</button>}
                </div>
                {d ? (
                  <div style={{ marginTop: 8 }}>
                    <div className="row"><span className={'badge ' + (d.decision === 'recommend_match' ? 'b-ok' : 'b-warn')}>{d.decision.replace(/_/g, ' ')}</span>
                      {d.customer_id && <span className="small">→ <b>{d.customer_id}</b> · {d.expected_contribution_id}</span>}
                      <span className="right small muted">confidence {(d.confidence * 100).toFixed(0)}%</span></div>
                    <div className="conf"><span style={{ width: `${d.confidence * 100}%` }} /></div>
                    <p style={{ margin: '10px 0 6px' }}>{d.reasoning_summary}</p>
                    <div className="small"><b>Evidence used:</b><ul style={{ margin: '4px 0', paddingLeft: 18 }}>{(d.evidence_used || []).map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></div>
                    <div className="small"><b>Conflicts:</b> {(d.conflicts || []).join('; ') || 'none'}</div>
                    {d.matched_amount ? <div className="small" style={{ marginTop: 4 }}><b>Matched amount (from SQL):</b> {naira(d.matched_amount)}</div> : null}
                    {d.validator_note && <div className="small bad-text" style={{ marginTop: 4 }}>{d.validator_note}</div>}
                  </div>
                ) : <div className="small muted" style={{ marginTop: 6 }}>Not yet reviewed. Money Match only sees compact evidence for this one case.</div>}
              </div>
            )}

            <div className={'control-box' + (pending.length ? '' : ' ok')}>
              <div className="row"><b>Control</b><span className="small muted">{c.recommended_action}</span></div>
              {pending.map((a: Any) => (
                <div key={a.approval_id} style={{ marginTop: 10 }}>
                  <div className="small"><span className="badge b-info">Human approval required</span> <b>{ACTION_LABEL[a.action_type] || a.action_type}</b> · <span className="mono">{a.approval_id}</span></div>
                  <div className="row" style={{ marginTop: 8 }}>
                    <button className="btn approve" disabled={!!busy} onClick={() => act('approve', () => api.post(`/approvals/${a.approval_id}/decision`, { decision: 'approve', decided_by: 'ops.manager' }), (r) => `Approved — ${r.result?.posted ? `ledger posted ${naira(r.result.ledger_amount)}` : 'action executed'}`)}>{busy === 'approve' ? <Spinner /> : '✓'} Approve</button>
                    <button className="btn reject" disabled={!!busy} onClick={() => act('reject', () => api.post(`/approvals/${a.approval_id}/decision`, { decision: 'reject', decided_by: 'ops.manager', note: 'Rejected from case view' }), () => 'Rejected — logged to audit trail')}>Reject</button>
                  </div>
                </div>
              ))}
              {c.case_type === 'OVERDUE_FOLLOWUP' && c.status === 'open' && (
                <div className="row" style={{ marginTop: 10 }}>
                  <button className="btn primary" disabled={!!busy} onClick={() => act('remind', () => api.post(`/cases/${caseId}/remind`, { style: 'auto' }), (r) => r.status === 'already_queued' ? 'Reminder already queued' : `Reminder queued to Outbox (${r.language})`)}>{busy === 'remind' ? <Spinner /> : '✉'} Send safe reminder</button>
                  {c.reminder_suppressed ? <span className="small warn-text">The code-level gate will refuse this reminder — try it.</span> : <span className="small ok-text">Verified overdue: automation allowed.</span>}
                </div>
              )}
              {(c.approvals || []).filter((a: Any) => a.status !== 'pending').map((a: Any) => (
                <div key={a.approval_id} className="small" style={{ marginTop: 8 }}><StatusBadge s={a.status} /> {ACTION_LABEL[a.action_type] || a.action_type} by <b>{a.decided_by}</b> · {timeLabel(a.decided_at)}</div>
              ))}
            </div>

            {(c.outbox?.length || c.tasks?.length) ? (
              <div className="card card-pad">
                <div className="section-title" style={{ marginTop: 0 }}>Actions on this case</div>
                {(c.outbox || []).map((o: Any) => <div key={o.message_id} className="small" style={{ padding: '4px 0' }}>✉ <span className="mono">{o.message_id}</span> {ACTION_LABEL[o.action_type]} → {o.recipient} · {o.automated ? 'automated' : 'approval-triggered'}</div>)}
                {(c.tasks || []).map((t: Any) => <div key={t.task_id} className="small" style={{ padding: '4px 0' }}>☐ <span className="mono">{t.task_id}</span> {t.task_type} → {t.assignee}</div>)}
              </div>
            ) : null}
          </div>
        )}
      </div>
    </>
  )
}
