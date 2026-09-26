import { forwardRef, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import { BellRing, CalendarClock, Check, Pencil, Plus, RefreshCw, Send, Trash2, X } from 'lucide-react'
import type { Reminder, ReminderOccurrence } from '../../shared/types'
import type { AdminApi } from '../../shared/api'
import { formatDate, pageSlice, relativeTime, reminderBucket } from '../../shared/utils'
import { Modal } from '../../shared/ui/Modal'
import { Pagination } from '../../shared/ui/Pagination'
import type { ReminderDraft, ReminderFilter } from './reminderModel'


interface RemindersPageProps {
  reminders: Reminder[]
  total?: number
  busy: boolean
  onOpen: (item?: Reminder) => void
  onToggle: (item: Reminder) => void
  onDelete: (item: Reminder) => void
  api: AdminApi
  acknowledgementId?: number | null
  onAcknowledge?: (occurrenceId: number) => void
  acknowledgementBusy?: boolean
}

export function RemindersPage({ reminders, total, busy, onOpen, onToggle, onDelete, api, acknowledgementId, onAcknowledge, acknowledgementBusy }: RemindersPageProps) {
  const [filter, setFilter] = useState<ReminderFilter>('active')
  const [page, setPage] = useState(1)
  const pageSize = 12
  const filtered = useMemo(() => reminders.filter((item) => filter === 'all' || reminderBucket(item) === filter), [filter, reminders])
  const visible = pageSlice(filtered, page, pageSize)
  return <>
    <div className="page-actions"><div><h2>定时提醒</h2><p>保存后立即生效，不需要重启服务。</p></div><button className="button primary" onClick={() => onOpen()}><Plus size={18} />新建提醒</button></div>
    {acknowledgementId && <ReminderOccurrenceNotice key={acknowledgementId} api={api} id={acknowledgementId} busy={acknowledgementBusy} onAcknowledge={onAcknowledge} />}
    <div className="notice-card"><Send size={19} /><div><strong>通知渠道：ntfy / eos</strong><span>所有已经订阅 eos 主题的客户端都会收到。</span></div></div>
    <div className="filter-row"><div className="segmented" role="group" aria-label="提醒状态筛选">
      {([['active', '运行中'], ['paused', '已暂停'], ['completed', '已完成'], ['all', '全部']] as const).map(([value, label]) => <button key={value} aria-pressed={filter === value} className={filter === value ? 'active' : ''} onClick={() => { setFilter(value); setPage(1) }}>{label}</button>)}
    </div><span className="result-count">{filtered.length} 条</span></div>
    {visible.length ? <section className="panel reminder-list">{visible.map((item) => <article key={item.id} className="reminder-row">
      <div className="date-tile"><span>{item.schedule_kind === 'daily' ? '每天' : new Intl.DateTimeFormat('zh-CN', { month: 'short' }).format(new Date((item.next_run_at || item.run_at || 0) * 1000))}</span><strong>{item.schedule_kind === 'daily' ? item.daily_time?.slice(0, 2) : new Intl.DateTimeFormat('zh-CN', { day: '2-digit' }).format(new Date((item.next_run_at || item.run_at || 0) * 1000))}</strong></div>
      <div className="reminder-copy"><div className="row-title"><h3>{item.title}</h3><span className={`status-pill ${reminderBucket(item) === 'active' ? 'positive' : 'neutral'}`}>{reminderBucket(item) === 'active' ? item.next_run_at ? `下次 ${relativeTime(item.next_run_at)}` : '运行中' : reminderBucket(item) === 'paused' ? '已暂停' : '已完成'}</span></div><p>{item.message}</p><div className="meta-line"><CalendarClock size={15} />{item.schedule_kind === 'daily' ? `每天 ${item.daily_time} · ${item.timezone}` : formatDate(item.run_at)}<span>·</span>优先级 {item.priority}{item.ack_enabled ? <><span>·</span>确认后停止重复 · {item.repeat_max_attempts ? `最多 ${item.repeat_max_attempts} 次` : '不限次数'}</> : null}</div></div>
      <div className="row-actions"><button className="icon-button" aria-label={`编辑提醒 ${item.title}`} onClick={() => onOpen(item)}><Pencil size={17} /></button><button className="button subtle" disabled={busy || reminderBucket(item) === 'completed'} onClick={() => onToggle(item)}>{item.enabled ? '暂停' : '启用'}</button><button className="icon-button danger" aria-label={`删除提醒 ${item.title}`} disabled={busy} onClick={() => onDelete(item)}><Trash2 size={17} /></button></div>
    </article>)}</section> : <section className="empty-state"><div className="empty-icon"><BellRing size={29} /></div><h3>这个范围内没有提醒</h3><p>需要记住的事情交给 EyeOfSauron，到时间会通过 ntfy 告诉你。</p><button className="button primary" onClick={() => onOpen()}><Plus size={18} />新建提醒</button></section>}
    <Pagination page={page} pageSize={pageSize} loaded={filtered.length} total={filter === 'all' ? total : undefined} onPage={setPage} noun="条提醒" />
  </>
}

function ReminderOccurrenceNotice({ api, id, busy, onAcknowledge }: { api: AdminApi; id: number; busy?: boolean; onAcknowledge?: (id: number) => void }) {
  const [occurrence, setOccurrence] = useState<ReminderOccurrence | null>(null)
  const [occurrenceError, setOccurrenceError] = useState('')
  useEffect(() => {
    let active = true
    api.reminderOccurrence(id).then((response) => {
      if (active) { setOccurrence(response.occurrence); setOccurrenceError('') }
    }).catch((error: unknown) => {
      if (active) { setOccurrence(null); setOccurrenceError(error instanceof Error ? error.message : '无法读取提醒') }
    })
    return () => { active = false }
  }, [id, api])
  return <section className="notice-card"><BellRing size={19} /><div><strong>{occurrence?.title || '正在读取提醒'}</strong><span>{occurrence ? `${occurrence.message} · 计划时间 ${formatDate(occurrence.scheduled_for)}` : occurrenceError || '请稍候'}</span>{occurrence?.acknowledged_at && <small>已于 {formatDate(occurrence.acknowledged_at)} 确认</small>}</div>{occurrence?.ack_enabled && !occurrence.acknowledged_at && <button className="button primary" disabled={busy} onClick={() => onAcknowledge?.(id)}><Check size={16} />已收到</button>}</section>
}

interface ReminderDialogProps {
  draft: ReminderDraft
  setDraft: Dispatch<SetStateAction<ReminderDraft>>
  busy: boolean
  dirty: boolean
  onClose: () => void
  onSubmit: () => void
}

const change = (setDraft: Dispatch<SetStateAction<ReminderDraft>>, key: keyof ReminderDraft) => (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
  const value = event.target instanceof HTMLInputElement && event.target.type === 'checkbox' ? event.target.checked : event.target.value
  setDraft((current) => ({ ...current, [key]: value }))
}

export const ReminderDialog = forwardRef<HTMLDialogElement, ReminderDialogProps>(function ReminderDialog({ draft, setDraft, busy, dirty, onClose, onSubmit }, ref) {
  const requestClose = () => {
    if (!dirty || window.confirm('放弃尚未保存的提醒修改？')) onClose()
  }
  return <Modal ref={ref} labelledBy="reminder-dialog-title" onRequestClose={requestClose}>
    <form className="modal-card" onSubmit={(event) => { event.preventDefault(); onSubmit() }}>
      <header className="modal-header"><div><span className="eyebrow">{draft.id ? '编辑提醒' : '新建提醒'}</span><h2 id="reminder-dialog-title">{draft.id ? '更新提醒' : '安排一条提醒'}</h2></div><button className="icon-button" type="button" aria-label="关闭提醒编辑" onClick={requestClose}><X size={19} /></button></header>
      <div className="modal-body"><div className="form-grid two"><label><span>标题</span><input value={draft.title} onChange={change(setDraft, 'title')} maxLength={128} required /></label><label><span>发送方式</span><select value={draft.scheduleKind} onChange={change(setDraft, 'scheduleKind')}><option value="once">指定日期时间</option><option value="after" disabled={Boolean(draft.id)}>多久以后</option><option value="daily">每天固定时间</option></select></label></div>
        {draft.scheduleKind === 'once' && <label><span>发送时间</span><input value={draft.runAtLocal} onChange={change(setDraft, 'runAtLocal')} type="datetime-local" required /><small>按当前浏览器所在时区解释。</small></label>}
        {draft.scheduleKind === 'after' && <div className="form-grid two"><label><span>等待时长</span><input value={draft.delayValue} onChange={(event) => setDraft((current) => ({ ...current, delayValue: Number(event.target.value) }))} type="number" min="1" required /></label><label><span>单位</span><select value={draft.delayUnit} onChange={(event) => setDraft((current) => ({ ...current, delayUnit: Number(event.target.value) }))}><option value="60">分钟</option><option value="3600">小时</option><option value="86400">天</option></select></label></div>}
        {draft.scheduleKind === 'daily' && <div className="form-grid two"><label><span>每天时间</span><input value={draft.dailyTime} onChange={change(setDraft, 'dailyTime')} type="time" required /></label><label><span>时区</span><input value={draft.timezone} onChange={change(setDraft, 'timezone')} list="timezone-list" required /><small>会正确处理夏令时。</small></label></div>}
        <label><span>提醒内容</span><textarea value={draft.message} onChange={change(setDraft, 'message')} rows={4} maxLength={4096} required placeholder="届时要发送给 ntfy 的消息" /></label>
        <div className="form-grid two"><label><span>通知优先级</span><select value={draft.priority} onChange={(event) => setDraft((current) => ({ ...current, priority: Number(event.target.value) }))}><option value="2">低</option><option value="3">普通</option><option value="4">高</option><option value="5">最高</option></select></label><label className="switch-field"><input checked={draft.enabled} onChange={change(setDraft, 'enabled')} type="checkbox" /><span><strong>保存后启用</strong><small>暂停后仍保留配置</small></span></label></div>
        <section className="form-section"><label className="switch-field"><input checked={draft.ackEnabled} onChange={change(setDraft, 'ackEnabled')} type="checkbox" /><span><strong>需要确认收到</strong><small>通知会链接到后台，确认后停止重复发送。</small></span></label>{draft.ackEnabled && <div className="form-grid two"><label><span>重复间隔（分钟）</span><input type="number" min="1" max="43200" value={Math.round(draft.repeatIntervalSeconds / 60)} onChange={(event) => setDraft((current) => ({ ...current, repeatIntervalSeconds: Math.max(60, Number(event.target.value) * 60) }))} required /></label><label><span>重复次数</span><input type="number" min="0" max="100" value={draft.repeatMaxAttempts} onChange={(event) => setDraft((current) => ({ ...current, repeatMaxAttempts: Math.max(0, Number(event.target.value)) }))} /><small>0 表示一直重复，直到确认。</small></label></div>}</section>
      </div>
      <footer className="dialog-actions"><button className="button subtle" type="button" onClick={requestClose}>取消</button><button className="button primary" type="submit" disabled={busy}>{busy ? <RefreshCw className="spin" size={17} /> : <Check size={17} />}{draft.id ? '保存修改' : '创建提醒'}</button></footer>
    </form>
  </Modal>
})
