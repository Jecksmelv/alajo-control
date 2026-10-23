import { useEffect, useState } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import { num, Spinner, StatusBadge, timeLabel, usd } from '../util'

function stepText(s: Any): string {
  const skip = new Set(['t_ms', 'kind', 'agent'])
  const o: Any = {}
  Object.keys(s).forEach((k) => { if (!skip.has(k)) o[k] = s[k] })
  return JSON.stringify(o, null, 1)
}

function stepTitle(s: Any) {
  switch (s.kind) {
    case 'handoff': return <>handoff → <span className={'agent-tag ' + (s.to || '')}>{s.to}</span>{s.case_id ? <span className="mono small"> {s.case_id}</span> : null}</>
    case 'tool_call': return <>tool <span className="mono">{s.tool}</span> <StatusBadge s={s.status} /> {s.ms != null && <span className="small faint">{s.ms} ms</span>}</>
    case 'agent_start': return <>agent started</>
    case 'agent_end': return <>agent finished · {s.model_requests} model calls · {num(s.input_tokens)} in ({num(s.cached_input_tokens)} cached) / {num(s.output_tokens)} out · {usd(s.cost_usd, 5)} · {s.latency_ms} ms · <span className="small">{s.stop_reason}</span></>
    case 'decision': return <>decision{s.decision ? `: ${String(s.decision).replace(/_/g, ' ')}` : ''}{s.confidence != null ? ` (${(s.confidence * 100).toFixed(0)}%)` : ''}</>
    case 'safety_block': return <span className="bad-text">blocked by control <b>{s.control}</b></span>
    case 'guardrail': return <>output guardrails · grounding {s.grounding?.grounded ? 'passed' : 'flagged'}{s.redactions?.length ? ` · redacted ${s.redactions.join(', ')}` : ''}</>
    case 'cache_hit': return <>reused stored decision (no model call)</>
    case 'limit': return <span className="warn-text">limit reached</span>
    default: return <>{s.kind}</>
  }
}

export default function Trace({ runId }: { runId?: string }) {
  const nav = useNav()
  const [runs, setRuns] = useState<Any[] | null>(null)
  const [run, setRun] = useState<Any>(null)
  useEffect(() => { api.get('/runs?limit=80').then(setRuns) }, [])
  useEffect(() => { if (runId) { setRun(null); api.get('/runs/' + runId).then(setRun) } }, [runId])
  useEffect(() => { if (!runId && runs?.length) nav.go('trace/' + runs[0].run_id) }, [runs, runId]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="grid" style={{ gridTemplateColumns: 'minmax(280px, 360px) 1fr' }}>
      <div className="card" style={{ maxHeight: 'calc(100vh - 140px)', overflowY: 'auto' }}>
        <div className="card-h"><h3>Runs</h3><span className="hint">{runs?.length ?? 0}</span></div>
        {!runs ? <div className="empty"><Spinner /></div> : runs.map((r) => (
          <div key={r.run_id} onClick={() => nav.go('trace/' + r.run_id)} style={{ padding: '10px 14px', borderBottom: '1px solid var(--line)', cursor: 'pointer', background: r.run_id === runId ? '#f1ece1' : undefined }}>
            <div className="row"><span className="small strong">{r.kind.replace(/_/g, ' ')}</span><span className="badge b-neutral">{r.mode}</span><span className="right"><StatusBadge s={r.status} /></span></div>
            <div className="small" style={{ marginTop: 3 }}>{r.query.slice(0, 90)}</div>
            <div className="small faint">{timeLabel(r.started_at)} · {r.model_requests} calls · {usd(r.cost_usd)}</div>
          </div>
        ))}
      </div>
      <div className="card">
        {!run ? <div className="empty">{runId ? <Spinner /> : 'Select a run'}</div> : (
          <>
            <div className="card-h"><h3>{run.kind.replace(/_/g, ' ')}</h3><span className="mono small muted">{run.run_id}</span><div className="right"><StatusBadge s={run.status} /></div></div>
            <div className="card-pad">
              <div className="kv" style={{ marginBottom: 14 }}>
                <div className="k">Request</div><div>{run.query}</div>
                <div className="k">Requested by</div><div>{run.requested_by} · {timeLabel(run.started_at)}</div>
                <div className="k">Stop reason</div><div>{run.stop_reason}</div>
                <div className="k">Total latency</div><div>{((run.latency_ms || 0) / 1000).toFixed(1)} s</div>
                <div className="k">Model requests</div><div>{run.model_requests}</div>
                <div className="k">Tokens</div><div>{num(run.input_tokens)} input ({num(run.cached_input_tokens)} cached) · {num(run.output_tokens)} output</div>
                <div className="k">Cost (list price)</div><div>{usd(run.cost_usd, 5)}</div>
                <div className="k">Tool calls</div><div>{run.tool_calls}</div>
              </div>
              <div className="small muted" style={{ marginBottom: 10 }}>Concise decision summaries and compact tool arguments/results are shown. Hidden chain-of-thought, prompts and keys are never recorded.</div>
              <div className="timeline">
                {(run.steps || []).map((s: Any, i: number) => (
                  <div key={i} className={'tl-item k-' + s.kind}>
                    <div className="tl-head"><span className="small faint mono">{(s.t_ms / 1000).toFixed(2)}s</span><span className={'agent-tag ' + s.agent}>{s.agent}</span>{stepTitle(s)}</div>
                    {['tool_call', 'decision', 'safety_block', 'agent_end', 'guardrail', 'limit', 'error'].includes(s.kind) && <div className="tl-body">{stepText(s)}</div>}
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
