import { useEffect, useState } from 'react'
import { RefreshCw, X } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import type { SourceHealth } from '../../shared/types'
import { formatDate } from '../../shared/utils'

type Props = { sourceId: string; api: Pick<AdminApi, 'sourceHealth'>; onClose: () => void }

const percent = (value: number | null) => value === null ? '暂无样本' : `${(value * 100).toFixed(1)}%`
const jobNames: Record<string, string> = { pending: '待抓取', leased: '抓取中', retry: '等待重试', completed: '已完成', dead: '停止重试' }
const runtimeNames: Record<string, string> = { active: '已启动', disabled: '已停用', starting: '启动中', configured: '等待应用', invalid: '配置无效', degraded: '运行异常', stale: '状态过期' }

export function SourceDiagnostics({ sourceId, api, onClose }: Props) {
  const [health, setHealth] = useState<SourceHealth | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    void api.sourceHealth(sourceId, controller.signal).then(({ health: next }) => {
      if (!controller.signal.aborted) { setHealth(next); setError(null); setLoading(false) }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) { setError(failure instanceof Error ? failure.message : '诊断暂时不可用'); setLoading(false) }
    })
    return () => controller.abort()
  }, [sourceId, api, revision])
  return <section className="panel source-diagnostics" aria-label={`${sourceId}来源诊断`}>
    <div className="panel-header"><div><h2>来源诊断 · {sourceId}</h2><p>采集可用性、正文补抓与长期内容质量分别判断。</p></div><div className="icon-action-row"><button type="button" className="icon-button" aria-label="刷新来源诊断" disabled={loading} onClick={() => { setLoading(true); setRevision((value) => value + 1) }}><RefreshCw size={17} className={loading ? 'spin' : ''} /></button><button type="button" className="icon-button" aria-label="关闭来源诊断" onClick={onClose}><X size={17} /></button></div></div>
    {error && <p role="status" className="inline-error">{error}</p>}
    {!health && loading && <p className="empty-compact">正在读取诊断…</p>}
    {health && <div className="source-diagnostics-body">
      <dl className="source-diagnostic-grid">
        <div><dt>采集器状态</dt><dd>{runtimeNames[health.current.runtime_status || ''] || '尚无运行记录'}</dd></div>
        <div><dt>最后成功采集</dt><dd>{health.current.last_success_at ? formatDate(health.current.last_success_at) : '尚无成功记录'}</dd></div>
        <div><dt>当前连续失败</dt><dd>{health.current.consecutive_failures || 0} 次</dd></div>
        <div><dt>累计采集成功率</dt><dd>{percent(health.polling.success_rate)}<small>{health.polling.successes} 成功 / {health.polling.attempts} 轮采集，累计失败 {health.polling.failures}</small></dd></div>
        <div><dt>最近 24 小时入库</dt><dd>{health.evidence.observations_24h} 条</dd></div>
        <div><dt>最近 7 天入库</dt><dd>{health.evidence.observations_7d} 条</dd></div>
        <div><dt>近 7 天入库文章的全文覆盖</dt><dd>{percent(health.evidence.full_text_coverage)}<small>{health.evidence.with_full_text} / {health.evidence.observations_7d} 条已有正文</small></dd></div>
      </dl>
      {health.current.last_error && <p className="inline-error">当前采集错误：{health.current.last_error_kind || '未知类型'} · {health.current.last_error}</p>}
      {health.current.runtime_error && <p className="inline-error">采集器启动或运行错误：{health.current.runtime_error}</p>}
      {health.current.last_warning && <p>最近采集提示：{health.current.last_warning}</p>}
      <p>正文任务：{Object.entries(health.evidence.content_jobs).map(([state, count]) => `${jobNames[state] || state} ${count}`).join('，') || '该窗口内没有补抓任务'}</p>
      {Object.keys(health.evidence.content_failure_kinds).length > 0 && <p>正文失败原因：{Object.entries(health.evidence.content_failure_kinds).map(([kind, count]) => `${kind} ${count}`).join('，')}。正文受限不代表来源停止采集。</p>}
      <p className="preservation-note">累计成功率来自已保存的采集计数，系统尚无逐轮历史，无法计算近 7 天采集成功率。产出按去重后实际入库时间统计，仅包含仍保留的数据；正文覆盖受来源内容策略和首次基线影响，不能当作来源质量分。</p>
      <p className="preservation-note">诊断时间：{formatDate(health.as_of)}。长期权重仍按 90 天证据和人工反馈计算，可在「信源质量」查看。</p>
    </div>}
  </section>
}
