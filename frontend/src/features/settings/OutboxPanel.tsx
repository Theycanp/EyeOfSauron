import { Ban, CircleAlert, Inbox, RefreshCw, RotateCcw, Send, Trash2 } from 'lucide-react'
import type { OutboxAlert } from '../../shared/types'
import { formatDate } from '../../shared/utils'

type QueueView = 'dead' | 'pending'

interface OutboxPanelProps {
  status: QueueView
  alerts: OutboxAlert[]
  loading: boolean
  error: string | null
  nextCursor: number | null
  busy: boolean
  onStatus: (status: QueueView) => void
  onReload: () => void
  onLoadMore: () => void
  onRetry: (alert: OutboxAlert) => void
  onCancel: (alert: OutboxAlert) => void
  onDelete: (alert: OutboxAlert) => void
}

export function OutboxPanel({ status, alerts, loading, error, nextCursor, busy, onStatus, onReload, onLoadMore, onRetry, onCancel, onDelete }: OutboxPanelProps) {
  return <section className="panel outbox-panel">
    <div className="panel-header"><div><h2>通知投递队列</h2><p>处理无法送达的消息，或取消尚未开始发送的通知。</p></div><button className="icon-button" type="button" aria-label="刷新通知队列" disabled={loading} onClick={onReload}><RefreshCw className={loading ? 'spin' : ''} size={17} /></button></div>
    <div className="outbox-toolbar"><div className="segmented" aria-label="通知队列状态"><button type="button" className={status === 'dead' ? 'active' : ''} aria-pressed={status === 'dead'} onClick={() => onStatus('dead')}>死信</button><button type="button" className={status === 'pending' ? 'active' : ''} aria-pressed={status === 'pending'} onClick={() => onStatus('pending')}>待发送</button></div><span className="result-count">已载入 {alerts.length} 条</span></div>
    {error && <div className="inline-error" role="status"><CircleAlert size={17} /><span>{error}</span></div>}
    {alerts.length ? <div className="outbox-list">{alerts.map((alert) => <article className="outbox-row" key={alert.id}>
      <div className={`outbox-icon ${status === 'dead' ? 'negative' : 'info'}`}>{status === 'dead' ? <CircleAlert size={18} /> : <Send size={18} />}</div>
      <div className="outbox-copy"><div className="row-title"><h3>{alert.title || `通知 #${alert.id}`}</h3><span className={`status-pill ${status === 'dead' ? 'negative' : 'info'}`}>{status === 'dead' ? '投递失败' : '等待发送'}</span></div><p>{alert.message || '没有消息正文'}</p>{alert.last_error && <div className="outbox-error"><strong>{alert.failure_kind || '发送错误'}</strong><span>{alert.last_error}</span></div>}<div className="meta-line"><span>#{alert.id}</span><span>·</span><span>{alert.topic}</span><span>·</span><span>尝试 {Number(alert.attempts || 0)} 次</span><span>·</span><span>{formatDate(alert.dead_at || alert.created_at)}</span></div></div>
      <div className="outbox-actions">{status === 'dead' ? <><button className="button subtle" type="button" disabled={busy} onClick={() => onRetry(alert)}><RotateCcw size={15} />重试</button><button className="icon-button danger" type="button" aria-label={`删除通知 ${alert.title || alert.id}`} disabled={busy} onClick={() => onDelete(alert)}><Trash2 size={15} /></button></> : <button className="button subtle danger-text" type="button" disabled={busy} onClick={() => onCancel(alert)}><Ban size={15} />取消发送</button>}</div>
    </article>)}</div> : !loading && !error && <div className="empty-compact positive"><Inbox size={21} /><span>{status === 'dead' ? '没有需要人工处理的死信' : '当前没有等待发送的通知'}</span></div>}
    {loading && !alerts.length && <div className="empty-compact"><RefreshCw className="spin" size={20} /><span>正在读取通知队列…</span></div>}
    {nextCursor && <div className="load-more"><button className="button subtle" type="button" disabled={loading} onClick={onLoadMore}>{loading ? <RefreshCw className="spin" size={16} /> : null}载入更早记录</button></div>}
  </section>
}
