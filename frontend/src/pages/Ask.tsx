import { useEffect, useRef, useState } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import { Markdown, num, Spinner, usd } from '../util'

const SUGGESTIONS = [
  'What needs my attention today?',
  "Why is Aisha Bello's payment unresolved?",
  'Who is genuinely overdue?',
  'Which customers should not receive reminders yet?',
  "What is driving Friday's payout risk?",
  'Show me the three largest unresolved discrepancies.',
  'Draft follow-up messages for the routine overdue cases.',
  'How much is unresolved with field agents?',
]

type Msg = { role: 'user' | 'bot'; text: string; meta?: Any }

const KEY = 'alajo-ask-thread'

export default function Ask() {
  const nav = useNav()
  const [msgs, setMsgs] = useState<Msg[]>(() => { try { return JSON.parse(sessionStorage.getItem(KEY) || '[]') } catch { return [] } })
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [lang, setLang] = useState<'en' | 'pcm' | 'auto'>('auto')
  const [session] = useState(() => sessionStorage.getItem(KEY + '-sid') || (() => { const s = 'S' + Math.random().toString(36).slice(2, 10); sessionStorage.setItem(KEY + '-sid', s); return s })())
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { sessionStorage.setItem(KEY, JSON.stringify(msgs)); end.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs])

  const send = async (text: string) => {
    if (!text.trim() || busy) return
    setMsgs((m) => [...m, { role: 'user', text }])
    setInput('')
    setBusy(true)
    try {
      const r = await api.post('/ask', { message: text, session_id: session, language: lang === 'auto' ? undefined : lang })
      setMsgs((m) => [...m, { role: 'bot', text: r.answer, meta: r }])
    } catch (e) {
      setMsgs((m) => [...m, { role: 'bot', text: 'Request failed: ' + (e as Error).message }])
    } finally { setBusy(false) }
  }

  return (
    <div className="chat">
      <div className="card" style={{ display: 'flex', flexDirection: 'column' }}>
        <div className="msgs">
          {msgs.length === 0 && (
            <div className="empty" style={{ textAlign: 'left' }}>
              <div className="strong" style={{ fontSize: 16, color: 'var(--ink)' }}>Ask about today’s operations</div>
              <p className="small">The Supervisor delegates to Money Match, Exception &amp; Risk, Liquidity &amp; Payout and Customer Action. Every naira figure comes from a tool and is checked before you see it. The chat cannot change balances — material actions go to the Approvals queue.</p>
            </div>
          )}
          {msgs.map((m, i) => m.role === 'user' ? <div key={i} className="msg user">{m.text}</div> : (
            <div key={i} className="msg bot">
              <Markdown text={m.text} onCase={nav.openCase} onCustomer={nav.openCustomer} />
              {m.meta && (
                <div className="msg-foot">
                  {(m.meta.specialists_consulted || []).map((s: string) => <span key={s} className={'agent-tag ' + s}>{s.replace('_', ' ')}</span>)}
                  {(m.meta.referenced_case_ids || []).map((c: string) => <button key={c} className="link-chip" onClick={() => nav.openCase(c)}>{c}</button>)}
                  {(m.meta.referenced_customer_ids || []).map((c: string) => <button key={c} className="link-chip cust" onClick={() => nav.openCustomer(c)}>{c}</button>)}
                  <span className={'badge ' + (m.meta.grounding?.grounded ? 'b-ok' : 'b-warn')}>{m.meta.grounding?.grounded ? `✓ ${m.meta.grounding.amounts_found} figures grounded in tools` : 'unverified figure flagged'}</span>
                  <span className="right">{num(m.meta.totals?.model_requests)} model calls · {num(m.meta.totals?.input_tokens)} in / {num(m.meta.totals?.output_tokens)} out tokens · {usd(m.meta.totals?.cost_usd)} · {(m.meta.latency_ms / 1000).toFixed(1)}s</span>
                  <a href={`#/trace/${m.meta.run_id}`}>trace →</a>
                </div>
              )}
            </div>
          ))}
          {busy && <div className="msg bot row muted"><Spinner /> Supervisor is delegating to specialists…</div>}
          <div ref={end} />
        </div>
        <div className="composer">
          <input className="input" placeholder="Ask Alajo Control…" value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') send(input) }} />
          <button className="btn primary" disabled={busy || !input.trim()} onClick={() => send(input)}>Ask</button>
        </div>
      </div>
      <div className="stack">
        <div className="card card-pad">
          <div className="section-title" style={{ marginTop: 0 }}>Try</div>
          <div className="suggest">{SUGGESTIONS.map((s) => <button key={s} onClick={() => send(s)} disabled={busy}>{s}</button>)}</div>
        </div>
        <div className="card card-pad">
          <div className="section-title" style={{ marginTop: 0 }}>Message style for drafts</div>
          <div className="chips">{(['auto', 'en', 'pcm'] as const).map((l) => <button key={l} className={'chip' + (lang === l ? ' active' : '')} onClick={() => setLang(l)}>{l === 'auto' ? 'Customer preference' : l === 'en' ? 'English' : 'Nigerian Pidgin'}</button>)}</div>
          <p className="small muted">Context kept: last 3 exchanges (optimized mode).</p>
          <button className="btn small" onClick={() => { setMsgs([]); sessionStorage.removeItem(KEY) }}>Clear conversation</button>
        </div>
      </div>
    </div>
  )
}
