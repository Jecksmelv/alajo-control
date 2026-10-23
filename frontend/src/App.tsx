import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, type Any } from './api'
import CaseDrawer from './components/CaseDrawer'
import Approvals from './pages/Approvals'
import Ask from './pages/Ask'
import Customers from './pages/Customers'
import Exceptions from './pages/Exceptions'
import Labs from './pages/Labs'
import Liquidity from './pages/Liquidity'
import Outbox from './pages/Outbox'
import Overview from './pages/Overview'
import Reconciliation from './pages/Reconciliation'
import Trace from './pages/Trace'
import { Spinner } from './util'

type NavCtx = {
  go: (path: string) => void
  openCase: (id: string) => void
  openCustomer: (id: string) => void
  toast: (msg: string, bad?: boolean) => void
  refresh: () => void
  version: number
  health: Any
}
const Ctx = createContext<NavCtx>(null as unknown as NavCtx)
export const useNav = () => useContext(Ctx)

function parseHash(): { path: string; parts: string[] } {
  const h = window.location.hash.replace(/^#\/?/, '') || 'overview'
  const parts = h.split('/').filter(Boolean)
  return { path: parts[0] || 'overview', parts }
}

const NAV: { group: string; items: { id: string; label: string; icon: string; count?: string }[] }[] = [
  { group: 'Operate', items: [
    { id: 'overview', label: 'Morning Control Room', icon: '◎' },
    { id: 'reconciliation', label: 'Reconciliation', icon: '⇄', count: 'recon' },
    { id: 'exceptions', label: 'Exceptions', icon: '△', count: 'exceptions' },
    { id: 'liquidity', label: 'Liquidity & Payouts', icon: '▤' },
    { id: 'customers', label: 'Customers', icon: '◉' },
  ] },
  { group: 'Control', items: [
    { id: 'approvals', label: 'Approvals', icon: '✓', count: 'approvals' },
    { id: 'outbox', label: 'Outbox', icon: '✉', count: 'outbox' },
  ] },
  { group: 'AI', items: [
    { id: 'ask', label: 'Ask Alajo Control', icon: '✦' },
    { id: 'trace', label: 'Agent Trace', icon: '⋮' },
    { id: 'labs', label: 'Evaluation · Safety · Performance', icon: '⚗' },
  ] },
]

const TITLES: Record<string, [string, string]> = {
  overview: ['Morning Control Room', 'What happened overnight, what needs you, and whether Friday’s payouts are covered'],
  reconciliation: ['Reconciliation', 'Where is the money? Every unresolved inflow and the evidence behind it'],
  exceptions: ['Exceptions', 'What deserves human attention first'],
  liquidity: ['Liquidity & Payouts', 'Can we meet upcoming customer obligations?'],
  customers: ['Customers', 'Customer 360 — plan, payments, messages, cases and actions'],
  approvals: ['Approvals', 'Material financial actions wait here for a human decision'],
  outbox: ['Outbox', 'Simulated customer communications and internal tasks'],
  ask: ['Ask Alajo Control', 'Questions answered by the Supervisor and its specialists, with evidence'],
  trace: ['Agent Trace', 'Every agent run: handoffs, tool calls, decisions, tokens and cost'],
  labs: ['Evaluation · Safety · Performance', 'Measured results from reproducible test suites'],
}

export default function App() {
  const [route, setRoute] = useState(parseHash())
  const [caseId, setCaseId] = useState<string | null>(null)
  const [toastMsg, setToast] = useState<{ msg: string; bad?: boolean } | null>(null)
  const [version, setVersion] = useState(0)
  const [counts, setCounts] = useState<Any>({})
  const [health, setHealth] = useState<Any>(null)
  const [resetting, setResetting] = useState(false)

  useEffect(() => { setCaseId(null) }, [route.path, route.parts.join('/')])

  useEffect(() => {
    const on = () => setRoute(parseHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  // automation hook (screenshot script / demos): window.alajoOpenCase('RC-1007')
  useEffect(() => { (window as unknown as { alajoOpenCase: (id: string) => void }).alajoOpenCase = (id: string) => setCaseId(id) }, [])

  const toast = useCallback((msg: string, bad?: boolean) => {
    setToast({ msg, bad })
    window.setTimeout(() => setToast(null), 4200)
  }, [])
  const refresh = useCallback(() => setVersion((v) => v + 1), [])

  useEffect(() => {
    api.get('/health').then(setHealth).catch(() => setHealth({ ok: false }))
    api.get('/summary').then((s) => setCounts({
      approvals: s.approvals_pending, exceptions: s.open_exceptions, outbox: s.outbox_total,
      recon: s.unmatched_inflows?.count, review: s.ai_review?.status,
    })).catch(() => undefined)
  }, [version, route.path])

  const nav: NavCtx = {
    go: (p) => { window.location.hash = '#/' + p },
    openCase: (id) => setCaseId(id),
    openCustomer: (id) => { setCaseId(null); window.location.hash = '#/customers/' + id },
    toast, refresh, version, health,
  }

  const reset = async () => {
    if (!window.confirm('Reset Demo rebuilds the deterministic demo day (synthetic data, cases, outbox, approvals). The audit trail is kept and a new demo epoch starts. Continue?')) return
    setResetting(true)
    try {
      const r = await api.post('/reset')
      toast(`Demo day rebuilt in ${r.seconds}s — epoch ${r.epoch}`)
      refresh()
    } catch (e) {
      toast(String((e as Error).message), true)
    } finally {
      setResetting(false)
    }
  }

  let page: ReactNode
  switch (route.path) {
    case 'reconciliation': page = <Reconciliation />; break
    case 'exceptions': page = <Exceptions />; break
    case 'liquidity': page = <Liquidity />; break
    case 'customers': page = <Customers customerId={route.parts[1]} />; break
    case 'approvals': page = <Approvals />; break
    case 'outbox': page = <Outbox />; break
    case 'ask': page = <Ask />; break
    case 'trace': page = <Trace runId={route.parts[1]} />; break
    case 'labs': page = <Labs tab={route.parts[1]} />; break
    default: page = <Overview />
  }
  const [title, sub] = TITLES[route.path] || TITLES.overview

  return (
    <Ctx.Provider value={nav}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark">AC</div>
            <div>
              <div className="brand-name">Alajo Control</div>
              <div className="brand-sub">AI operations control tower<br />for community savings</div>
            </div>
          </div>
          {NAV.map((g) => (
            <div key={g.group}>
              <div className="nav-group">{g.group}</div>
              {g.items.map((it) => {
                const c = it.count ? counts[it.count] : undefined
                return (
                  <a key={it.id} href={'#/' + it.id} className={'nav-item' + (route.path === it.id ? ' active' : '')}>
                    <span className="nav-icon">{it.icon}</span>
                    {it.label}
                    {c ? <span className={'count' + (it.count === 'approvals' ? ' warn' : '')}>{c}</span> : null}
                  </a>
                )
              })}
            </div>
          ))}
          <div className="sidebar-foot">
            Synthetic demo day · Wed 21 Oct 2026<br />
            5,000 savers · no real customer data
          </div>
        </aside>
        <main className="main">
          <div className="topbar">
            <div>
              <h1>{title}</h1>
              <div className="sub">{sub}</div>
            </div>
            <div className="spacer" />
            <span className="pill"><span className={'dot ' + (health?.ai_configured ? 'ok' : 'bad')} />
              {health?.ai_configured ? `AI online · ${health.model}` : 'AI key not found'}</span>
            <span className="pill">Snapshot 07:30 WAT</span>
            <button className="btn small" onClick={reset} disabled={resetting}>{resetting ? <Spinner /> : '↺'} Reset Demo</button>
          </div>
          <div className="content" key={version}>{page}</div>
        </main>
      </div>
      {caseId && <CaseDrawer caseId={caseId} onClose={() => setCaseId(null)} />}
      {toastMsg && <div className={'toast' + (toastMsg.bad ? ' bad' : '')}>{toastMsg.msg}</div>}
    </Ctx.Provider>
  )
}
