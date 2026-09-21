import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import type { SourceQualityAudit } from '../../shared/types'
import { formatDate } from '../../shared/utils'

const actions: Record<string, string> = { feedback: '记录反馈', override_set: '设置人工权重', override_cleared: '清除人工权重' }

function reason(row: SourceQualityAudit): string {
  if (typeof row.details.reason === 'string') return row.details.reason
  if (typeof row.details.previous_reason === 'string') return `原原因：${row.details.previous_reason}`
  return '未记录原因'
}

export function SourceQualityAuditPanel({ sourceId, api }: { sourceId: string; api: Pick<AdminApi, 'sourceQualityAudit'> }) {
  const [rows, setRows] = useState<SourceQualityAudit[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    void api.sourceQualityAudit(sourceId, controller.signal).then(({ audit }) => {
      if (!controller.signal.aborted) { setRows(audit); setError(null); setLoading(false) }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) { setError(failure instanceof Error ? failure.message : '无法读取审计记录'); setLoading(false) }
    })
    return () => controller.abort()
  }, [sourceId, api, revision])
  return <div className="source-diagnostics-body" aria-label={`${sourceId}权重审计`}>
    <div className="section-heading"><h3>{sourceId} · 最近 100 条调整</h3><button className="button subtle" disabled={loading} onClick={() => { setLoading(true); setRevision((value) => value + 1) }}><RefreshCw size={15} className={loading ? 'spin' : ''} />刷新记录</button></div>
    {error && <p role="status" className="inline-error">{error}</p>}
    {loading && !rows.length && <p>正在读取记录…</p>}
    {!loading && !error && !rows.length && <p>此来源还没有人工反馈或覆盖记录。</p>}
    <ol className="source-quality-audit">{rows.map((row) => <li key={row.id}>
      <strong>{actions[row.action] || row.action}</strong>
      <span>{formatDate(row.created_at)} · {row.actor}</span>
      <p>{reason(row)}</p>
      <div className="meta-line">
        {row.details.signal !== undefined && <span>反馈：{Number(row.details.signal) > 0 ? '正向' : Number(row.details.signal) < 0 ? '负向' : '中立'}</span>}
        {row.details.weight !== undefined && <span>权重：{Number(row.details.weight).toFixed(2)}</span>}
        {row.details.previous_weight !== undefined && <span>原权重：{Number(row.details.previous_weight).toFixed(2)}</span>}
      </div>
    </li>)}</ol>
  </div>
}
