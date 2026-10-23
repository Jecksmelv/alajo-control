import { useEffect, useState } from 'react'
import { api, type Any } from '../api'
import { useNav } from '../App'
import { ACTION_LABEL, num, Spinner, timeLabel } from '../util'

export default function Outbox() {
  const nav = useNav()
  const [tab, setTab] = useState<'messages' | 'tasks'>('messages')
  const [msgs, setMsgs] = useState<Any[] | null>(null)
  const [tasks, setTasks] = useState<Any[] | null>(null)
  const [type, setType] = useState('all')
  const [lang, setLang] = useState('all')
  useEffect(() => { api.get('/outbox').then(setMsgs); api.get('/tasks').then(setTasks) }, [])
  if (!msgs || !tasks) return <Spinner />
  const types = Array.from(new Set(msgs.map((m) => m.action_type)))
  const shown = msgs.filter((m) => (type === 'all' || m.action_type === type) && (lang === 'all' || m.language === lang))
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="banner ok small">✉ This is a simulated outbox: nothing is sent to real WhatsApp/SMS. Every message has a reason, an originating case, and is marked automated or approval-triggered. Amounts in messages are filled from the ledger by code.</div>
      <div className="tabs">
        <button className={'tab' + (tab === 'messages' ? ' active' : '')} onClick={() => setTab('messages')}>Messages ({num(msgs.length)})</button>
        <button className={'tab' + (tab === 'tasks' ? ' active' : '')} onClick={() => setTab('tasks')}>Internal tasks ({tasks.length})</button>
      </div>
      {tab === 'messages' ? (
        <>
          <div className="row wrap">
            <div className="chips">
              <button className={'chip' + (type === 'all' ? ' active' : '')} onClick={() => setType('all')}>All</button>
              {types.map((t) => <button key={t} className={'chip' + (type === t ? ' active' : '')} onClick={() => setType(t)}>{ACTION_LABEL[t] || t}<span className="n">{msgs.filter((m) => m.action_type === t).length}</span></button>)}
            </div>
            <div className="chips right">
              {['all', 'en', 'pcm'].map((l) => <button key={l} className={'chip' + (lang === l ? ' active' : '')} onClick={() => setLang(l)}>{l === 'pcm' ? 'Pidgin' : l === 'en' ? 'English' : 'All languages'}</button>)}
            </div>
          </div>
          <div className="card"><div className="t-wrap" style={{ maxHeight: 700 }}>
            <table className="t">
              <thead><tr><th>Recipient</th><th>Message</th><th>Reason / origin</th><th>Mode</th><th>Time</th></tr></thead>
              <tbody>{shown.slice(0, 300).map((m) => (
                <tr key={m.message_id}>
                  <td style={{ minWidth: 190 }}><div className="strong small">{m.recipient}</div><div className="small muted">{m.channel} · {m.language === 'pcm' ? 'Pidgin' : 'English'}</div></td>
                  <td style={{ maxWidth: 520 }}><div className="small strong">{ACTION_LABEL[m.action_type]}</div><div className="small">{m.content}</div></td>
                  <td className="small">{m.reason}<div><button className="link-chip" onClick={() => nav.openCase(m.case_id)}>{m.case_id}</button></div></td>
                  <td><span className={'badge ' + (m.automated ? 'b-ok' : 'b-info')}>{m.automated ? 'automated' : 'approval-triggered'}</span><div className="small muted" style={{ marginTop: 3 }}>{m.created_by}</div></td>
                  <td className="small muted">{timeLabel(m.created_at)}<div><span className="badge b-neutral">{m.status}</span></div></td>
                </tr>
              ))}</tbody>
            </table>
            {!shown.length && <div className="empty">Outbox is empty. Reminders are queued only after payment status is verified — run the morning AI review or send one from an overdue case.</div>}
          </div></div>
        </>
      ) : (
        <div className="card"><table className="t">
          <thead><tr><th>Task</th><th>Type</th><th>Description</th><th>Assignee</th><th>Created by</th></tr></thead>
          <tbody>{tasks.map((t) => (
            <tr key={t.task_id}><td className="mono">{t.task_id}<div><button className="link-chip" onClick={() => nav.openCase(t.case_id)}>{t.case_id}</button></div></td><td className="small">{t.task_type}</td><td className="small" style={{ maxWidth: 480 }}>{t.description}</td><td className="small">{t.assignee}</td><td className="small">{t.created_by}<div className="muted">{timeLabel(t.created_at)}</div></td></tr>
          ))}</tbody>
        </table>{!tasks.length && <div className="empty">No internal tasks yet.</div>}</div>
      )}
    </div>
  )
}
