import { useEffect, useRef, useState } from 'react'
import type { AdminApi } from '../../shared/api'
import { ApiError } from '../../shared/api'
import type { EventRepairPreview, EventRepairRequest, EventReviewDetail, EventWorkspaceState, NewsEvent } from '../../shared/types'
import { formatDate } from '../../shared/utils'
import { Modal } from '../../shared/ui/Modal'

const defaultWorkspace: EventWorkspaceState = { read: false, followed: false, ignored: false, digest_choice: 'auto' }

export function EventRepairDialog({ api, request, onDone, onClose, onUnauthorized }: {
  api: AdminApi; request: EventRepairRequest; onDone: () => void; onClose: () => void; onUnauthorized: () => void
}) {
  const [preview, setPreview] = useState<EventRepairPreview | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const modal = useRef<HTMLDialogElement>(null)
  useEffect(() => { modal.current?.showModal() }, [])
  const run = async (apply: boolean) => {
    setBusy(true); setError('')
    try {
      if (apply && preview) {
        await api.applyEventRepair({ ...request, expected_revision: preview.revision, reason })
        onDone()
      } else setPreview(await api.previewEventRepair(request))
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
      else setError(failure instanceof Error ? failure.message : '操作失败')
      if (failure instanceof ApiError && failure.status === 409) setPreview(null)
    } finally { setBusy(false) }
  }
  return <Modal ref={modal} labelledBy="event-repair-title" onRequestClose={() => { if (!busy) onClose() }}><section className="modal-card"><div className="modal-body">
    <h3 id="event-repair-title">{request.action === 'merge' ? '合并事件' : '拆分报道'}</h3>
    <p>操作保留原始文章、旧日报与已发通知。先核对证据，再提交原因；不会重新发送通知。</p>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {preview && <>
      <p>{preview.warning}</p>
      <div className="event-repair-evidence"><ul>{preview.reports.map(report => <li key={report.observation_id}>{report.source_tier === 'primary' ? '一手' : '二手/社交'} · {report.publisher || report.source_id} · {report.title}{request.observation_ids?.includes(report.observation_id) ? '（移至新事件）' : ''}</li>)}</ul></div>
      {preview.comparisons.some(item => item.score < 0.57) && <p className="inline-error">部分报道的算法匹配分较低。请确认确实属于同一事件，或确实需要拆开。</p>}
      <label>调整原因<textarea aria-label="调整原因" maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} /></label>
    </>}
    <div className="page-actions">
      <button className="button subtle" disabled={busy} onClick={onClose}>取消</button>
      <button className="button" disabled={busy || (Boolean(preview) && !reason.trim())} onClick={() => { void run(Boolean(preview)) }}>{busy ? '处理中…' : preview ? '确认调整' : '预览影响'}</button>
    </div>
  </div></section></Modal>
}

export function EventReviewPanel({ api, event, canWrite, onChange, onUnauthorized }: {
  api: AdminApi; event: NewsEvent; canWrite: boolean; onChange: () => void; onUnauthorized: () => void
}) {
  const [state, setState] = useState<EventWorkspaceState>(event.workspace || defaultWorkspace)
  const [detail, setDetail] = useState<EventReviewDetail | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [reason, setReason] = useState('')
  const [choice, setChoice] = useState(state.digest_choice)
  const [selected, setSelected] = useState<number[]>([])
  const [repair, setRepair] = useState(false)
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError('')
    try { await action() } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
      else setError(failure instanceof Error ? failure.message : '操作失败')
    } finally { setBusy(false) }
  }
  const preference = (key: 'read' | 'followed' | 'ignored') => run(async () => {
    const result = await api.eventPreference(event.event_key, { ...state, [key]: !state[key] })
    setState(result.state)
  })
  return <section className="event-review-panel" aria-label="事件管理">
    <div className="page-actions">
      <button className="button subtle" disabled={busy} aria-pressed={state.read} onClick={() => { void preference('read') }}>{state.read ? '已读' : '标记已读'}</button>
      <button className="button subtle" disabled={busy} aria-pressed={state.followed} onClick={() => { void preference('followed') }}>{state.followed ? '已关注' : '关注事件'}</button>
      <button className="button subtle" disabled={busy} aria-pressed={state.ignored} onClick={() => { void preference('ignored') }}>{state.ignored ? '已忽略（个人）' : '忽略（个人）'}</button>
      <button className="button subtle" disabled={busy} onClick={() => { void run(async () => { setDetail(await api.eventReview(event.event_key)) }) }}>查看事件历史与匹配依据</button>
    </div>
    <p className="hint">阅读标记仅属于当前账户；关注是收藏，忽略不会停用信源或影响通知和全局日报。</p>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {canWrite && <form className="event-editorial" onSubmit={submission => { submission.preventDefault(); void run(async () => { const result = await api.eventDigestChoice(event.event_key, choice, reason); setState(result.state); setReason('') }) }}>
      <label>全局日报选择<select aria-label="全局日报选择" value={choice} onChange={event => setChoice(event.target.value as EventWorkspaceState['digest_choice'])}><option value="auto">自动选择</option><option value="include">优先纳入</option><option value="exclude">排除</option></select></label>
      <label>原因<input aria-label="日报选择原因" maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} /></label>
      <button className="button subtle" disabled={busy || !reason.trim() || choice === state.digest_choice}>保存日报选择</button>
      <p>只影响以后生成的、时间窗内的日报；仍受容量上限约束，旧日报不变。{state.digest_reason && `当前原因：${state.digest_reason}`}</p>
    </form>}
    {detail && <div>
      <h4>事实与证据</h4>
      {detail.claims?.length ? <ul>{detail.claims.map(claim => <li key={claim.claim_key}>
        <strong>{claim.status === 'disputed' ? '有争议' : claim.status === 'superseded' ? '已被更正' : '当前记录'}</strong>：{claim.text}
        {claim.supersedes_claim_key && <span>（更正此前记录）</span>}
        <ul>{detail.claim_evidence?.filter(evidence => evidence.claim_key === claim.claim_key).map(evidence => {
          const report = detail.reports.find(item => item.report_id === evidence.report_id)
          return <li key={`${evidence.report_id}-${evidence.stance}`}>{evidence.stance === 'supports' ? '支持' : evidence.stance === 'refutes' ? '反驳' : '背景'} · {report ? <a href={report.url || undefined} target="_blank" rel="noreferrer">{report.publisher || report.source_id}：{report.title}</a> : `报道 #${evidence.report_id}`}{evidence.note && ` · ${evidence.note}`}</li>
        })}</ul>
      </li>)}</ul> : <p>尚无结构化事实记录，不代表报道中没有事实。</p>}
      <h4>时间线</h4>
      {detail.timeline?.length ? <ol>{detail.timeline.map(item => <li key={item.timeline_id}>{formatDate(item.occurred_at)} · {item.text}</li>)}</ol> : <p>尚无时间线记录。</p>}
      <h4>通知记录</h4>
      {detail.notifications?.length ? <ul>{detail.notifications.map(item => <li key={item.id}>{formatDate(item.created_at)} · <a href={`/events/${item.id}`}>{item.title}</a> · {({ delivered: '已投递', pending: '待发送', sending: '发送中', dead: '投递失败', cancelled: '已取消' } as Record<string, string>)[item.status] || item.status}</li>)}</ul> : <p>此事件尚无关联通知。</p>}
      {detail.history_truncated && Object.values(detail.history_truncated).some(Boolean) && <p role="status">部分历史超过读取上限，此处不是完整历史。</p>}
      <h4>当前规则重放</h4><p>匹配分不是正确概率，也不是历史决策记录。这里比较每篇报道与当前事件代表；原匹配分另行保留。</p>
      {detail.reports_truncated && <p role="status">超过 200 篇，仅展示前 200 篇；此事件需专门批次处理，不能在此拆分。</p>}
      {detail.reports.map(report => <div className="event-match-row" key={report.observation_id}>
        {canWrite && !detail.reports_truncated && <input type="checkbox" aria-label={`拆分选择：${report.title}`} checked={selected.includes(report.observation_id)} onChange={e => setSelected(current => e.target.checked ? [...current, report.observation_id] : current.filter(id => id !== report.observation_id))} />}
        <div><strong>{report.title}</strong><p>原匹配 {report.match_score.toFixed(2)} · 当前重放 {report.evidence.score.toFixed(2)} · 标题相似 {report.evidence.title_similarity.toFixed(2)} · 相隔 {report.evidence.time_distance_hours} 小时{report.evidence.semantic_identity ? ' · 共享明确决议身份' : ''}{report.evidence.generic_title ? ' · 栏目标题，不自动合并' : ''}{report.evidence.possible_numeric_conflict ? ' · 数字可能冲突' : ''}</p><p>共同实体：{report.evidence.shared_entities.join('、') || '未检出'}；共同数字：{report.evidence.shared_numbers.join('、') || '无'}</p></div>
      </div>)}
      {canWrite && <button className="button subtle" disabled={busy || detail.reports_truncated || selected.length === 0 || selected.length >= detail.reports.length} onClick={() => setRepair(true)}>将选中报道拆成新事件</button>}
      <h4>人工调整记录</h4>
      {detail.audit.length ? <ul>{detail.audit.map(item => <li key={item.id}>{formatDate(item.created_at)} · {item.actor} · {item.action}：{item.reason}</li>)}</ul> : <p>尚无人工调整。</p>}
    </div>}
    {repair && <EventRepairDialog api={api} request={{ action: 'split', event_keys: [event.event_key], observation_ids: selected }} onClose={() => setRepair(false)} onDone={() => { setRepair(false); onChange() }} onUnauthorized={onUnauthorized} />}
  </section>
}
