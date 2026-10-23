import { useEffect, useState } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import { naira, PriorityBadge, Spinner, StatusBadge, TYPE_LABEL } from '../util'

export default function Exceptions() {
  const nav = useNav()
  const [cases, setCases] = useState<Any[] | null>(null)
  const [s, setS] = useState<Any>(null)
  useEffect(() => {
    api.get('/cases?status=unresolved&limit=1000').then((x: Any[]) => setCases(x.filter((c) => c.case_type !== 'OVERDUE_FOLLOWUP')))
    api.get('/summary').then(setS)
  }, [])
  const [triage, setTriage] = useState<Any>(null)
  useEffect(() => { api.get('/state/triage_report').then(setTriage).catch(() => setTriage(null)) }, [])
  if (!cases || !s) return <Spinner />
  const byType: Record<string, { n: number; amt: number }> = {}
  cases.forEach((c) => { byType[c.case_type] = byType[c.case_type] || { n: 0, amt: 0 }; byType[c.case_type].n++; byType[c.case_type].amt += c.amount || 0 })
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid g4">
        {Object.entries(byType).sort((a, b) => b[1].amt - a[1].amt).slice(0, 8).map(([t, v]) => (
          <div key={t} className="card kpi"><div className="label">{TYPE_LABEL[t] || t}</div><div className="value" style={{ fontSize: 21 }}>{naira(v.amt)}</div><div className="meta">{v.n} open case{v.n > 1 ? 's' : ''}</div></div>
        ))}
      </div>
      <div className="grid split">
        <div className="card">
          <div className="card-h"><h3>Exception queue</h3><span className="hint">ranked by deterministic priority (type, amount, payout sensitivity, age) — not a credit score</span></div>
          <table className="t">
            <thead><tr><th>Score</th><th>Case</th><th>What happened</th><th className="num">Amount</th><th>Status</th></tr></thead>
            <tbody>{cases.map((c) => (
              <tr key={c.case_id} className="click" onClick={() => nav.openCase(c.case_id)}>
                <td><div className="strong">{c.priority_score}</div><PriorityBadge p={c.priority} /></td>
                <td className="mono">{c.case_id}<div className="small muted" style={{ fontFamily: 'var(--sans)' }}>{TYPE_LABEL[c.case_type]}</div></td>
                <td><div className="strong">{c.title}</div><div className="small muted">{c.recommended_action}</div></td>
                <td className="num">{naira(c.amount)}</td>
                <td><StatusBadge s={c.status} /></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        <div className="card">
          <div className="card-h"><h3>Exception &amp; Risk agent triage</h3></div>
          <div className="card-pad">
            {triage?.top_items ? (
              <>
                <p style={{ marginTop: 0 }}>{triage.summary}</p>
                {triage.top_items.map((t: Any, i: number) => (
                  <div key={t.case_id + i} className="ev">
                    <div className="ev-icon warn">{i + 1}</div>
                    <div><button className="link-chip" onClick={() => nav.openCase(t.case_id)}>{t.case_id}</button>
                      <div className="small" style={{ marginTop: 4 }}>{t.why_it_matters}</div>
                      <div className="small muted" style={{ marginTop: 2 }}>Next: {t.next_step}</div></div>
                  </div>
                ))}
                <a className="small" href={`#/trace/${triage.run_id}`}>View the run that produced this →</a>
              </>
            ) : <div className="muted small">Run the morning AI review on the Control Room to get the agent’s triage narrative. The queue on the left is already ranked deterministically.</div>}
          </div>
        </div>
      </div>
      <div className="small muted">Under investigation: <StatusBadge s="open" /> {naira(s.under_investigation.amount)} across {s.under_investigation.cases} cases.</div>
    </div>
  )
}
