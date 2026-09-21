import { useCallback, useEffect, useRef, useState } from 'react'
import { CircleAlert, FileText, RefreshCw } from 'lucide-react'
import { ApiError, type AdminApi } from '../../shared/api'
import type { DigestGenerationAttempt, DigestRun } from '../../shared/types'
import { formatDate } from '../../shared/utils'

const states: Record<DigestRun['state'], { label: string; tone: string }> = {
  ai_published: { label: 'AI 版已发布', tone: 'positive' },
  generating: { label: '正在生成', tone: 'info' },
  retry_exhausted: { label: '重试已结束', tone: 'warning' },
  ai_retrying: { label: '等待 AI 重试', tone: 'warning' },
  algorithm_published: { label: '算法版已发布', tone: 'neutral' },
  unpublished: { label: '尚未发布', tone: 'neutral' },
}
const attemptLabels: Record<string, string> = {
  running: '进行中', succeeded: '成功', failed: '失败', interrupted: '已中断',
}

export function DigestRunsPanel({ api, onUnauthorized, canRetry }: {
  api: AdminApi; onUnauthorized: () => void; canRetry: boolean
}) {
  const [runs, setRuns] = useState<DigestRun[]>([])
  const [loading, setLoading] = useState(true)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const inFlight = useRef(false)
  const requestIds = useRef(new Map<string, string>())
  const generation = useRef(0)

  const reportError = useCallback((failure: unknown) => {
    if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
    else setError(failure instanceof Error ? failure.message : '无法读取日报运行记录')
  }, [onUnauthorized])

  const load = useCallback(async () => {
    const current = ++generation.current
    setLoading(true); setError(null)
    try {
      const response = await api.digestRuns()
      if (current === generation.current) setRuns(response.runs)
    } catch (failure) {
      if (current === generation.current) reportError(failure)
    } finally {
      if (current === generation.current) setLoading(false)
    }
  }, [api, reportError])

  useEffect(() => {
    const generationAtMount = generation.current
    const timer = window.setTimeout(() => { void load() }, 0)
    return () => {
      window.clearTimeout(timer)
      generation.current = generationAtMount + 1
    }
  }, [load])

  const retry = async (run: DigestRun) => {
    if (inFlight.current || loading || !canRetry || !retryEligible(run)) return
    inFlight.current = true
    setBusyKey(run.digest_key); setError(null); setNotice(null)
    const requestId = requestIds.current.get(run.digest_key) || crypto.randomUUID()
    requestIds.current.set(run.digest_key, requestId)
    try {
      await api.retryDigestNow(run.digest_key, requestId)
      requestIds.current.delete(run.digest_key)
      setRuns((current) => current.map((item) => item.digest_key === run.digest_key
        ? { ...item, can_retry_now: false } : item))
      setNotice(`${run.digest_key} 已安排提前重试；现有次数与截止时间不变。请稍后刷新查看生成结果。`)
      // A refresh failure must not undo the accepted scheduling request or
      // discard the already visible history.
      try {
        const response = await api.digestRun(run.digest_key)
        setRuns((current) => current.map((item) => item.digest_key === run.digest_key ? response.run : item))
      } catch (failure) { reportError(failure) }
    } catch (failure) {
      // Keep the same identity when the network result is uncertain. The next
      // click can safely recover a previously accepted request.
      reportError(failure)
    } finally {
      inFlight.current = false
      setBusyKey(null)
    }
  }

  return <section aria-label="日报运行记录">
    <div className="page-actions"><div><h2>日报运行记录</h2><p>最近 30 期，包含尚未发布的任务。预留次数按一次生成流程计数，不等于 HTTP 请求次数。</p></div><button className="icon-button" aria-label="刷新运行记录" disabled={loading || busyKey !== null} onClick={() => { void load() }}><RefreshCw size={18} className={loading ? 'spin' : ''} /></button></div>
    <p className="security-note">提前重试只调整已排定的下一次时间；每期最多 5 次，原有 5 小时窗口不会重置。</p>
    {error && <div className="inline-error" role="alert"><CircleAlert size={17} />{error}{runs.length > 0 && '（已保留上次成功读取的记录）'}</div>}
    {notice && <p className="security-note" role="status">{notice}</p>}
    {loading && !runs.length ? <div className="loading-state"><RefreshCw className="spin" size={24} />正在读取运行记录…</div>
      : runs.length ? <div className="digest-cards digest-run-list">{runs.map((run) => {
        const state = states[run.state] || states.unpublished
        const errors = run.attempts.filter((attempt) => attempt.error)
        const latestError = errors[0]?.error || run.retry?.last_error
        const firstError = errors.at(-1)?.error
        return <article className="panel event-facts digest-run-card" key={run.digest_key} aria-label={`运行记录 ${run.digest_key}`}>
          <div className="panel-header"><div><h3>{run.digest_key}</h3><span className={`status-pill ${state.tone}`}>{state.label}</span></div></div>
          <dl>
            <div><dt>当前发布</dt><dd>{run.published_version !== null ? `v${run.published_version} · ${run.generation_kind === 'api' ? 'AI 版' : '算法版'}` : '尚未发布'}</dd></div>
            <div><dt>发布时间</dt><dd>{run.published_at ? formatDate(run.published_at) : '—'}</dd></div>
            <div><dt>预留次数</dt><dd>{run.reserved_attempts} / 5 次逻辑生成</dd></div>
            <div><dt>下一次</dt><dd>{run.retry?.status === 'pending' && run.retry.next_attempt_at ? formatDate(run.retry.next_attempt_at) : '无已排定重试'}</dd></div>
            <div><dt>窗口截止</dt><dd>{run.retry ? formatDate(run.retry.retry_deadline_at) : '未建立重试窗口'}</dd></div>
            {firstError && <div><dt>最早已记录错误</dt><dd>{firstError}</dd></div>}
            {latestError && <div><dt>最近错误</dt><dd>{latestError}</dd></div>}
          </dl>
          {!run.attempt_history_available ? <p className="digest-run-note">这期没有逐次运行历史。可能生成于记录功能上线前，无法还原当时的模型、Prompt 或错误；已有发布与预算信息仍可查看。</p>
            : <details className="digest-run-attempts"><summary>展开尝试记录（{run.attempts.length}）</summary><div>{run.attempts.map((attempt) => <AttemptDetail key={attempt.id} attempt={attempt} />)}</div></details>}
          {canRetry && <button className="button subtle" disabled={loading || busyKey !== null || !retryEligible(run)} onClick={() => { void retry(run) }} title={retryEligible(run) ? '提前下一次尝试，不增加次数或延长截止时间' : '仅等待中且仍有额度和有效窗口的任务可提前'}><RefreshCw size={15} className={busyKey === run.digest_key ? 'spin' : ''} />{busyKey === run.digest_key ? '正在安排…' : '提前重试'}</button>}
        </article>
      })}</div> : !error && <div className="empty-state"><div className="empty-icon"><FileText size={25} /></div><h3>暂无日报运行记录</h3><p>生成流程开始后，即使尚未发布，也能在这里查看进度。</p></div>}
  </section>
}

function retryEligible(run: DigestRun): boolean {
  return run.can_retry_now && run.reserved_attempts < 5 && run.retry?.status === 'pending'
    && run.state !== 'retry_exhausted'
}

function AttemptDetail({ attempt }: { attempt: DigestGenerationAttempt }) {
  return <section className="digest-run-attempt">
    <h4>{attemptLabels[attempt.status] || attempt.status} · {formatDate(attempt.started_at)}</h4>
    <p>{attempt.finished_at ? `总用时 ${Math.max(0, attempt.finished_at - attempt.started_at)} 秒` : '等待本次生成完成'}</p>
    {attempt.error && <p>{attempt.error}</p>}
    {attempt.providers.length ? attempt.providers.map((provider, index) => <dl key={index}>
      <div><dt>提供方主机</dt><dd>{provider.provider || '未记录'}</dd></div>
      <div><dt>模型</dt><dd>{provider.model || '未记录'}</dd></div>
      <div><dt>Prompt</dt><dd>{provider.prompt_id ? `${provider.prompt_id}@${provider.prompt_version ?? '?'}` : '未记录'}</dd></div>
      <div><dt>Prompt 指纹</dt><dd>{provider.prompt_hash || '未记录'}</dd></div>
      <div><dt>模型结果</dt><dd>{attemptLabels[provider.status || ''] || provider.status || '未记录'}{provider.elapsed_ms !== undefined && Number.isFinite(Number(provider.elapsed_ms)) ? ` · ${(Number(provider.elapsed_ms) / 1000).toFixed(1)} 秒` : ''}</dd></div>
      {provider.error && <div><dt>模型错误</dt><dd>{provider.error}</dd></div>}
    </dl>) : <p>未记录模型调用详情；此次流程可能在调用前结束，不能据此推断调用了哪个模型。</p>}
  </section>
}
