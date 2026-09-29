import { useCallback, useEffect, useRef, useState } from 'react'
import { BellOff, ExternalLink, Plus, RefreshCw, Save, Send, Trash2 } from 'lucide-react'
import { AdminApi, ApiError } from '../../shared/api'
import type { NotificationCategory, NotificationPolicy, NotificationSettings } from '../../shared/types'
import { formatDate } from '../../shared/utils'
import './notifications.css'

const categories: Array<[NotificationCategory, string]> = [
  ['news', '新闻与日报'], ['weather', '天气'], ['reminders', '提醒'], ['system', '系统运行'],
]
const deliveryLabels: Record<string, string> = { pending: '待发送', sending: '发送中', delivered: '已投递', dead: '投递失败', cancelled: '已取消' }

export function NotificationTopicsPanel({ api, canWrite, onUnauthorized, onSaved }: {
  api: AdminApi; canWrite: boolean; onUnauthorized: () => void; onSaved: () => void
}) {
  const [snapshot, setSnapshot] = useState<NotificationSettings | null>(null)
  const [draft, setDraft] = useState<NotificationPolicy | null>(null)
  const [revision, setRevision] = useState(0)
  const [dirty, setDirty] = useState(false)
  const dirtyRef = useRef(false)
  const readSequence = useRef(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [testId, setTestId] = useState<number | null>(null)
  const [newTopic, setNewTopic] = useState('')
  const failure = useCallback((cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) onUnauthorized()
    else setError(cause instanceof Error ? cause.message : '无法读取通知主题')
  }, [onUnauthorized])

  useEffect(() => {
    let active = true
    const refresh = async () => {
      const sequence = ++readSequence.current
      try {
        const result = await api.notifications()
        if (!active || sequence !== readSequence.current) return
        setSnapshot(result)
        if (!dirtyRef.current) { setDraft(result.policy); setRevision(result.revision) }
      } catch (cause) { if (active && sequence === readSequence.current) failure(cause) }
    }
    void refresh()
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void refresh() }, 15_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [api, failure])

  const edit = (policy: NotificationPolicy) => {
    dirtyRef.current = true; setDirty(true); setDraft(policy); setMessage('')
  }
  const reload = async () => {
    if (dirty && !window.confirm('放弃尚未保存的通知设置并重新读取？')) return
    setBusy(true); setError('')
    const sequence = ++readSequence.current
    try {
      const result = await api.notifications()
      if (sequence !== readSequence.current) return
      setSnapshot(result); setDraft(result.policy); setRevision(result.revision)
      dirtyRef.current = false; setDirty(false)
    } catch (cause) { failure(cause) } finally { setBusy(false) }
  }
  const save = async () => {
    if (!draft) return
    setBusy(true); setError(''); setMessage('')
    readSequence.current += 1
    try {
      const result = await api.saveNotifications(draft, revision)
      readSequence.current += 1
      dirtyRef.current = false; setDirty(false)
      setRevision(result.revision ?? revision)
      setMessage('通知设置已保存'); onSaved()
    } catch (cause) { failure(cause) } finally { setBusy(false) }
  }
  const test = async (topic: string) => {
    setBusy(true); setError(''); setMessage('')
    try {
      const result = await api.testNotificationTopic(topic)
      setTestId(result.alert_id); setMessage(`${topic} 测试已排队`)
      const fresh = await api.notifications()
      setSnapshot(fresh)
    } catch (cause) { failure(cause) } finally { setBusy(false) }
  }
  const unused = snapshot?.allowed_topics.filter(topic => !draft?.destinations.some(item => item.topic === topic)) || []
  const selectedNewTopic = unused.includes(newTopic) ? newTopic : unused[0] || ''
  const disabled = !canWrite || busy
  const subscriptionLink = (topic: string) => {
    if (!snapshot?.base_url) return null
    try {
      const url = new URL(snapshot.base_url)
      if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) return null
      return `${snapshot.base_url.replace(/\/$/, '')}/${encodeURIComponent(topic)}`
    } catch { return null }
  }

  return <section className="panel settings-panel notification-topics" aria-label="通知主题与路由">
    <div className="panel-header"><div><h2><Send size={17} />通知主题与路由</h2></div>
      <button className="icon-button" title="重新读取通知设置" aria-label="重新读取通知设置" disabled={busy} onClick={() => { void reload() }}><RefreshCw size={17} /></button>
    </div>
    <div className="notification-body">
    {error && <p className="form-error" role="alert">{error}</p>}
    {message && <p role="status">{message}{testId && <a className="notification-test-link" href={`/events/${testId}`}>查看测试消息 <ExternalLink size={13} /></a>}</p>}
    {!draft ? <p>{error ? '通知设置暂不可用' : '正在读取通知主题…'}</p> : <>
      <div className="notification-route-grid">{categories.map(([category, label]) => <label key={category}>
        <span>{label}{draft.routes[category] === null && <BellOff size={14} />}</span>
        <select aria-label={`${label}目的地`} value={draft.routes[category] ?? ''} disabled={disabled}
          onChange={event => edit({ ...draft, routes: { ...draft.routes, [category]: event.target.value || null } })}>
          <option value="">不推送</option>{draft.destinations.map(item => <option key={item.id} value={item.id}>{item.label} · {item.topic}</option>)}
        </select>
      </label>)}</div>
      <label className="notification-fallback"><span>未分类消息目的地</span><select aria-label="未分类消息目的地" value={draft.fallback} disabled={disabled} onChange={event => edit({ ...draft, fallback: event.target.value })}>{draft.destinations.map(item => <option key={item.id} value={item.id}>{item.label} · {item.topic}</option>)}</select></label>
      <div className="notification-destinations">{draft.destinations.map(item => {
        const status = snapshot?.topics.find(entry => entry.topic === item.topic)
        const referenced = draft.fallback === item.id || Object.values(draft.routes).includes(item.id)
        const link = subscriptionLink(item.topic)
        return <article className="notification-destination" key={item.id}>
          <div className="notification-topic-heading"><strong>{item.topic}</strong><span className={`status-pill ${status?.latest?.status === 'dead' ? 'negative' : status?.latest?.status === 'delivered' ? 'positive' : 'neutral'}`}>{status?.latest ? deliveryLabels[status.latest.status] || status.latest.status : '尚无投递记录'}</span></div>
          <div className="form-grid two"><label><span>名称</span><input aria-label={`${item.topic}名称`} maxLength={80} value={item.label} disabled={disabled} onChange={event => edit({ ...draft, destinations: draft.destinations.map(entry => entry.id === item.id ? { ...entry, label: event.target.value } : entry) })} /></label>
            <label><span>备注</span><input aria-label={`${item.topic}备注`} maxLength={300} value={item.description} disabled={disabled} onChange={event => edit({ ...draft, destinations: draft.destinations.map(entry => entry.id === item.id ? { ...entry, description: event.target.value } : entry) })} /></label></div>
          <div className="notification-topic-footer"><div><span>待发 {(status?.counts.pending || 0) + (status?.counts.sending || 0)} · 失败 {status?.counts.dead || 0}</span>{status?.latest && <small>最近消息 {formatDate(status.latest.created_at)}</small>}{status?.latest?.last_error && <small className="form-error">{status.latest.last_error}</small>}</div>
            <div className="row-actions">{link && <a className="button subtle" href={link} target="_blank" rel="noreferrer"><ExternalLink size={15} />订阅</a>}
              <button className="icon-button" title={`发送 ${item.topic} 测试`} aria-label={`发送 ${item.topic} 测试`} disabled={disabled} onClick={() => { void test(item.topic) }}><Send size={16} /></button>
              <button className="icon-button danger" title={referenced ? '先调整引用此主题的路由' : `删除 ${item.topic} 目的地`} aria-label={`删除 ${item.topic} 目的地`} disabled={disabled || referenced || draft.destinations.length === 1} onClick={() => edit({ ...draft, destinations: draft.destinations.filter(entry => entry.id !== item.id) })}><Trash2 size={16} /></button>
            </div>
          </div>
        </article>
      })}</div>
      {unused.length > 0 && <div className="notification-add"><select aria-label="新增目的地主题" value={selectedNewTopic} disabled={disabled} onChange={event => setNewTopic(event.target.value)}>{unused.map(topic => <option key={topic}>{topic}</option>)}</select><button className="button subtle" disabled={disabled || !selectedNewTopic} onClick={() => edit({ ...draft, destinations: [...draft.destinations, { id: `dest-${selectedNewTopic}`.slice(0, 64), topic: selectedNewTopic, label: selectedNewTopic, description: '' }] })}><Plus size={16} />新增目的地</button></div>}
      <div className="dialog-actions"><button className="button primary" disabled={disabled || !dirty || draft.destinations.some(item => !item.label.trim())} onClick={() => { void save() }}><Save size={16} />保存通知设置</button></div>
    </>}
    </div>
  </section>
}
