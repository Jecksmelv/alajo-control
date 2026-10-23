import { Fragment, type ReactNode } from 'react'

export const naira = (n: number | null | undefined, opts: { sign?: boolean } = {}) => {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  const v = Math.round(n)
  const s = '₦' + Math.abs(v).toLocaleString('en-NG')
  if (v < 0) return '−' + s
  return opts.sign && v > 0 ? '+' + s : s
}

export const compactNaira = (n: number | null | undefined) => {
  if (n === null || n === undefined) return '—'
  const a = Math.abs(n)
  const sign = n < 0 ? '−' : ''
  if (a >= 1_000_000) return `${sign}₦${(a / 1_000_000).toFixed(a >= 10_000_000 ? 1 : 2)}m`
  if (a >= 10_000) return `${sign}₦${Math.round(a / 1000)}k`
  return sign + '₦' + a.toLocaleString('en-NG')
}

export const num = (n: number | null | undefined) => (n === null || n === undefined ? '—' : n.toLocaleString('en-US'))
export const usd = (n: number | null | undefined, digits = 4) =>
  n === null || n === undefined ? '—' : '$' + n.toFixed(digits)
export const pct = (n: number | null | undefined, digits = 0) =>
  n === null || n === undefined ? '—' : (n * 100).toFixed(digits) + '%'

export const dayLabel = (iso: string) => {
  const d = new Date(iso + 'T12:00:00')
  return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })
}
export const timeLabel = (iso?: string | null) => {
  if (!iso) return '—'
  const d = new Date(iso.endsWith('Z') ? iso : iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

export const TYPE_LABEL: Record<string, string> = {
  PROBABLE_MATCH: 'Probable match',
  AMBIGUOUS_INFLOW: 'Ambiguous transfer',
  UNIDENTIFIED_INFLOW: 'Unidentified transfer',
  AGENT_SHORTFALL: 'Agent shortfall',
  DUPLICATE_RECEIPT: 'Duplicate receipt',
  DUPLICATE_IMPORT: 'Duplicate import',
  PROVIDER_SETTLEMENT_DELAYED: 'Settlement delayed',
  PROVIDER_SETTLEMENT_PENDING: 'Settlement in transit',
  PARTIAL_PAYMENT: 'Partial payment',
  OVERPAYMENT: 'Overpayment',
  CUSTOMER_DISPUTE: 'Customer dispute',
  MESSAGE_SECURITY_FLAG: 'Security flag',
  PRIVACY_REQUEST: 'Privacy request',
  OVERDUE_FOLLOWUP: 'Overdue',
}

export const ACTION_LABEL: Record<string, string> = {
  match_payment_to_contribution: 'Match transfer to contribution',
  clarify_ambiguous_payment: 'Hold & request clarification',
  resolve_agent_discrepancy: 'Open agent cash investigation',
  resolve_customer_dispute: 'Investigate customer dispute',
  quarantine_duplicate_record: 'Confirm duplicate quarantine',
  adjust_customer_balance: 'Adjust customer balance',
  mark_contribution_satisfied: 'Mark contribution satisfied',
  routine_overdue_reminder: 'Routine overdue reminder',
  payment_acknowledgment: 'Payment acknowledgment',
  request_transfer_reference: 'Request transfer reference',
  request_documentation: 'Request receipt / documentation',
  policy_reply: 'Privacy policy reply',
  field_agent_info_request: 'Field-agent information request',
}

export function PriorityBadge({ p }: { p?: string }) {
  if (!p) return null
  return <span className={`badge b-${p}`}>{p}</span>
}

export function StatusBadge({ s }: { s?: string }) {
  if (!s) return null
  const tone: Record<string, string> = {
    open: 'b-warn', awaiting_approval: 'b-info', under_investigation: 'b-warn', awaiting_customer: 'b-info',
    reminded: 'b-ok', resolved: 'b-ok', info: 'b-neutral', pending: 'b-info', approved: 'b-ok', rejected: 'b-bad',
    queued: 'b-ok', ok: 'b-ok', running: 'b-info', error: 'b-bad', limited: 'b-warn',
  }
  return <span className={`badge ${tone[s] || 'b-neutral'}`}>{s.replace(/_/g, ' ')}</span>
}

export function TypeBadge({ t }: { t: string }) {
  return <span className="badge b-dark" style={{ background: '#22365c' }}>{TYPE_LABEL[t] || t}</span>
}

// Linkify case ids / customer ids inside text.
const ID_RE = /\b(RC-\d{4}|C\d{5}|RUN-\d{10}-[0-9a-f]{6})\b/g

export function Linkified({ text, onCase, onCustomer }: { text: string; onCase?: (id: string) => void; onCustomer?: (id: string) => void }) {
  const parts: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  ID_RE.lastIndex = 0
  while ((m = ID_RE.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const id = m[1]
    if (id.startsWith('RC-') && onCase) parts.push(<button key={m.index} className="link-chip" onClick={() => onCase(id)}>{id}</button>)
    else if (id.startsWith('C') && onCustomer) parts.push(<button key={m.index} className="link-chip cust" onClick={() => onCustomer(id)}>{id}</button>)
    else parts.push(id)
    last = m.index + id.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return <>{parts}</>
}

function inline(text: string, onCase?: (id: string) => void, onCustomer?: (id: string) => void): ReactNode[] {
  // **bold** and `code`
  const out: ReactNode[] = []
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g
  let last = 0
  let m: RegExpExecArray | null
  let k = 0
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(<Linkified key={k++} text={text.slice(last, m.index)} onCase={onCase} onCustomer={onCustomer} />)
    const tok = m[0]
    if (tok.startsWith('**')) out.push(<strong key={k++}><Linkified text={tok.slice(2, -2)} onCase={onCase} onCustomer={onCustomer} /></strong>)
    else out.push(<code key={k++}>{tok.slice(1, -1)}</code>)
    last = m.index + tok.length
  }
  if (last < text.length) out.push(<Linkified key={k++} text={text.slice(last)} onCase={onCase} onCustomer={onCustomer} />)
  return out
}

export function Markdown({ text, onCase, onCustomer }: { text: string; onCase?: (id: string) => void; onCustomer?: (id: string) => void }) {
  const lines = (text || '').split('\n')
  const blocks: ReactNode[] = []
  let list: ReactNode[] = []
  const flush = () => {
    if (list.length) { blocks.push(<ul key={blocks.length}>{list}</ul>); list = [] }
  }
  lines.forEach((raw, i) => {
    const line = raw.trimEnd()
    if (/^\s*[-*]\s+/.test(line)) { list.push(<li key={i}>{inline(line.replace(/^\s*[-*]\s+/, ''), onCase, onCustomer)}</li>); return }
    if (/^\s*\d+\.\s+/.test(line)) { list.push(<li key={i}>{inline(line.replace(/^\s*\d+\.\s+/, ''), onCase, onCustomer)}</li>); return }
    flush()
    if (!line.trim()) return
    if (line.startsWith('### ')) blocks.push(<h3 key={i}>{inline(line.slice(4), onCase, onCustomer)}</h3>)
    else if (line.startsWith('## ')) blocks.push(<h3 key={i}>{inline(line.slice(3), onCase, onCustomer)}</h3>)
    else if (line.startsWith('# ')) blocks.push(<h3 key={i}>{inline(line.slice(2), onCase, onCustomer)}</h3>)
    else if (line.startsWith('> ')) blocks.push(<blockquote key={i}>{inline(line.slice(2), onCase, onCustomer)}</blockquote>)
    else blocks.push(<p key={i}>{inline(line, onCase, onCustomer)}</p>)
  })
  flush()
  return <Fragment>{blocks}</Fragment>
}

export function Spinner() {
  return <span className="spinner" />
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}
