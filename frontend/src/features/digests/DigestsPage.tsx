import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, BookOpen, CalendarDays, CircleAlert, ExternalLink, FileText, RefreshCw, ShieldCheck } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import { ApiError } from '../../shared/api'
import type { DigestDetail, DigestItem, DigestReport, DigestSummary } from '../../shared/types'
import { formatDate } from '../../shared/utils'

interface DigestsPageProps { api: AdminApi; onUnauthorized: () => void; initialKey?: string }

type ReportView = { link: string; tier: 'primary' | 'secondary' | 'social'; relation?: string; sourceId?: string; title?: string; summary?: string }

function reportTier(report: DigestReport): ReportView['tier'] {
  return report.source_tier === 'primary' || report.source_tier === 'social' ? report.source_tier : 'secondary'
}

function representativeReports(item: DigestItem): ReportView[] {
  if (item.reports?.length) {
    return item.reports
      .filter((report) => Boolean(report.url))
      .map((report) => ({
        link: report.url as string,
        tier: reportTier(report),
        relation: report.relation,
        sourceId: report.source_id,
        title: report.title,
        summary: report.summary,
      }))
  }
  const sourceCount = Math.max(1, item.source_ids?.length || 0)
  return (item.links || []).slice(0, sourceCount).map((link, index) => ({
    link,
    tier: item.source_tiers?.[index] || 'secondary',
  }))
}

const tierLabels: Record<ReportView['tier'], string> = { primary: '一手', secondary: '二手', social: '社交/热度' }
const relationLabels: Record<string, string> = {
  corroborates: '交叉印证',
  contradicts: '存在冲突',
  updates: '后续更新',
  context: '背景信息',
}

export function DigestsPage({ api, onUnauthorized, initialKey }: DigestsPageProps) {
  const [digests, setDigests] = useState<DigestSummary[]>([])
  const [selected, setSelected] = useState<DigestDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try { setDigests((await api.digests()).digests || []) }
    catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
      else setError(failure instanceof Error ? failure.message : '无法读取日报')
    } finally { setLoading(false) }
  }, [api, onUnauthorized])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (initialKey) {
        setLoading(true)
        api.digest(initialKey).then((response) => setSelected(response.digest)).catch((failure: unknown) => {
          if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
          else setError(failure instanceof Error ? failure.message : '无法读取日报详情')
        }).finally(() => setLoading(false))
      } else void load()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [api, initialKey, load, onUnauthorized])

  const open = async (item: DigestSummary) => {
    setLoading(true); setError(null)
    try { setSelected((await api.digest(item.digest_key, item.version)).digest) }
    catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
      else setError(failure instanceof Error ? failure.message : '无法读取日报详情')
    } finally { setLoading(false) }
  }

  if (selected) return <DigestReader digest={selected} onBack={() => { setSelected(null); window.history.replaceState(null, '', '/#/digests'); void load() }} />
  return <>
    <div className="page-actions"><div><h2>情报日报</h2><p>每天值得了解、但不需要立即打断你的信息。</p></div><button className="icon-button" aria-label="刷新日报" disabled={loading} onClick={() => { void load() }}><RefreshCw className={loading ? 'spin' : ''} size={18} /></button></div>
    {error && <div className="inline-error"><CircleAlert size={17} />{error}</div>}
    {loading && !digests.length ? <div className="loading-state"><RefreshCw className="spin" size={24} />正在读取日报…</div> : digests.length ? <section className="digest-cards">{digests.map((digest) => <article className="digest-card" key={`${digest.digest_key}@${digest.version}`}><div className="digest-date"><CalendarDays size={17} />{formatDate(digest.period_end)}</div><h3>{digest.title}</h3><p>{digest.summary || '这期日报暂无摘要。'}</p><div className="digest-meta"><span>{digest.item_count} 条重点</span><span>{digest.source_count} 个来源</span><span>{digest.generation_kind}</span></div><button className="button subtle" onClick={() => { void open(digest) }}><BookOpen size={16} />阅读日报</button></article>)}</section> : <section className="empty-state"><div className="empty-icon"><FileText size={29} /></div><h3>还没有已发布日报</h3><p>日报生成器发布第一期内容后会出现在这里。</p></section>}
  </>
}

function DigestReader({ digest, onBack }: { digest: DigestDetail; onBack: () => void }) {
  const coverage = digest.coverage || []
  const healthySources = coverage.filter((item) => item.status === 'ok' || item.status === 'healthy').length
  return <article className="digest-reader">
    <header className="digest-reader-header"><button className="button subtle" onClick={onBack}><ArrowLeft size={16} />返回日报</button><div><span className="eyebrow">{digest.digest_key} · v{digest.version}</span><h2>{digest.title}</h2><p style={{ whiteSpace: 'pre-wrap' }}>{digest.summary}</p><div className="digest-meta"><span>{formatDate(digest.period_end)}</span><span>{digest.item_count} 条重点</span><span>{digest.source_count} 个来源</span></div></div></header>
    <section className="digest-items">{(digest.items || []).map((item, itemIndex) => {
      const reports = representativeReports(item)
      const updateCount = item.observation_ids?.length || 0
      return <article className="digest-item" key={item.event_id || item.event_key || item.cluster_key}><div className="digest-score"><strong>{item.score.toFixed(1)}</strong><span>综合分</span></div><div><div className="digest-item-title"><span className="status-pill neutral">[{itemIndex + 1}]</span><h3>{item.title}</h3>{(item.event_id || item.event_key) && <span className="status-pill neutral">事件</span>}{item.handling === 'immediate' && <span className="status-pill info">即时通知</span>}<span className={`status-pill ${item.importance >= 4 ? 'warning' : 'neutral'}`}>重要度 {item.importance}</span></div><p>{item.summary}</p><div className="digest-tags">{[...(item.regions || []), ...(item.topics || [])].map((tag) => <span key={tag}>{tag}</span>)}{updateCount > 1 && <span>合并 {updateCount} 次更新</span>}</div>{reports.length > 0 && <div className="digest-report-groups">{(['primary', 'secondary', 'social'] as const).map((tier) => { const tierReports = reports.filter((report) => report.tier === tier); if (!tierReports.length) return null; return <div className="digest-report-group" key={tier}><span className="status-pill neutral">{tierLabels[tier]}</span><div className="digest-links">{tierReports.map((report, index) => <span className="digest-report" key={`${report.link}-${index}`}>{report.relation && relationLabels[report.relation] && <small>{relationLabels[report.relation]}</small>}<a href={report.link} target="_blank" rel="noreferrer" title={report.title || undefined}><ExternalLink size={13} /><span>{reports.length === 1 ? '查看原文' : `原文 ${index + 1}`}</span></a></span>)}</div></div> })}</div>}</div></article>
    })}</section>
    {coverage.length > 0 && <details className="coverage-panel"><summary><ShieldCheck size={16} />来源覆盖：{healthySources}/{coverage.length} 正常</summary><div>{coverage.map((source) => <span key={source.source_id}><strong>{source.source_id}</strong><small>{source.status} · {source.observation_count} 条</small></span>)}</div></details>}
  </article>
}
