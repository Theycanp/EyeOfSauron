import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { AdminApi, ApiError } from '../../shared/api'
import type { EventFactDiagnostics } from '../../shared/types'
import { formatDate } from '../../shared/utils'

const statuses: Record<string, string> = { pending: '待处理', leased: '处理中', retry: '等待重试', completed: '已完成', dead: '失败终止' }

export function EventFactDiagnosticsPanel({ api, canWrite, onUnauthorized }: { api: AdminApi; canWrite: boolean; onUnauthorized: () => void }) {
  const [data, setData] = useState<EventFactDiagnostics | null>(null)
  const [version, setVersion] = useState('')
  const [limit, setLimit] = useState(100)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const failure = useCallback((cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) onUnauthorized()
    else setError(cause instanceof Error ? cause.message : '无法读取事实任务')
  }, [onUnauthorized])
  useEffect(() => {
    let active = true
    void api.eventFactDiagnostics().then(result => { if (active) setData(result) }).catch(cause => { if (active) failure(cause) })
    return () => { active = false }
  }, [api, failure])
  const versions = [...new Set([...(data?.jobs.map(job => job.extractor_version) ?? []), ...(data?.versions?.map(item => item.extractor_version) ?? [])])].sort((a, b) => b - a)
  const dead = data?.jobs.find(job => job.extractor_version === Number(version) && job.status === 'dead')?.count ?? 0
  const processable = Boolean(version) && (data?.active_worker_version === undefined || Number(version) === data.active_worker_version)
  const retry = async () => {
    if (!canWrite || !processable || !dead || !Number.isInteger(limit) || limit < 1 || limit > 500) return
    setBusy(true); setError(''); setMessage('')
    try {
      const result = await api.retryEventFactJobs(Number(version), limit)
      setMessage(`已将 ${result.count} 个失败任务重新排队`)
      setData(await api.eventFactDiagnostics())
    } catch (cause) { failure(cause) } finally { setBusy(false) }
  }
  const backfill = async () => {
    if (!canWrite || !processable || !Number.isInteger(limit) || limit < 1 || limit > 500) return
    setBusy(true); setError(''); setMessage('')
    try {
      const result = await api.backfillEventFactJobs(Number(version), limit)
      setMessage(`已将 ${result.count} 个历史任务加入回填`)
      setData(await api.eventFactDiagnostics())
    } catch (cause) { failure(cause) } finally { setBusy(false) }
  }
  return <section className="fact-diagnostics" aria-label="事实任务诊断">
    <h3>事实任务诊断</h3>
    {error && <p className="form-error" role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {data && <>
      <p>已完成但未提取到事实：{data.completed_without_claim} 个任务。未命中仅表示材料不符合当前提取规则，不代表报道没有价值。</p>
      {data.completed_count != null && <p>{data.extractor_version === null ? '所有版本' : `版本 ${data.extractor_version}`}完成任务 {data.completed_count} 个，其中提取到事实 {data.completed_with_claim ?? 0} 个。</p>}
      <div className="fact-diagnostic-grid">{data.jobs.map(job => <div key={`${job.extractor_version}:${job.status}`}><strong>版本 {job.extractor_version} · {statuses[job.status] || job.status}</strong><span>{job.count} 个任务</span>{job.newest_updated_at != null && <small>最近更新 {formatDate(job.newest_updated_at)}</small>}</div>)}</div>
      {!data.jobs.length && <p>尚无事实任务</p>}
      {data.worker_batch_limit != null && <p>每批最多 {data.worker_batch_limit} 个任务，批次间隔 {data.worker_active_pause_seconds ?? '—'} 秒。</p>}
      {data.active_worker_version != null && <p>当前处理版本 {data.active_worker_version}；历史版本仅供查看。</p>}
      <form className="fact-retry-controls" onSubmit={event => { event.preventDefault(); void retry() }}>
        <label>提取器版本<select aria-label="重试提取器版本" value={version} onChange={event => setVersion(event.target.value)} disabled={!canWrite || busy}><option value="">选择版本</option>{versions.map(item => <option key={item} value={item}>版本 {item}</option>)}</select></label>
        <label>重试上限<input aria-label="事实任务重试上限" type="number" min="1" max="500" step="1" value={limit} onChange={event => setLimit(Number(event.target.value))} disabled={!canWrite || busy} /></label>
        <button className="button subtle" type="submit" disabled={!canWrite || busy || !processable || !dead || !Number.isInteger(limit) || limit < 1 || limit > 500}><RefreshCw size={16} className={busy ? 'spin' : ''} />重试失败任务（{dead}）</button>
        <button className="button subtle" type="button" disabled={!canWrite || busy || !processable || !Number.isInteger(limit) || limit < 1 || limit > 500} onClick={() => { void backfill() }}>回填历史任务</button>
      </form>
    </>}
  </section>
}
