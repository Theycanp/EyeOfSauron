import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, BookOpen, CalendarDays, CircleAlert, ExternalLink, FileText, RefreshCw, ShieldCheck } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import { ApiError } from '../../shared/api'
import type { DigestDetail, DigestSummary } from '../../shared/types'
import { formatDate } from '../../shared/utils'

interface DigestsPageProps { api: AdminApi; onUnauthorized: () => void; initialKey?: string }

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
    <header className="digest-reader-header"><button className="button subtle" onClick={onBack}><ArrowLeft size={16} />返回日报</button><div><span className="eyebrow">{digest.digest_key} · v{digest.version}</span><h2>{digest.title}</h2><p>{digest.summary}</p><div className="digest-meta"><span>{formatDate(digest.period_end)}</span><span>{digest.item_count} 条重点</span><span>{digest.source_count} 个来源</span></div></div></header>
    <section className="digest-items">{(digest.items || []).map((item) => <article className="digest-item" key={item.cluster_key}><div className="digest-score"><strong>{item.score.toFixed(1)}</strong><span>综合分</span></div><div><div className="digest-item-title"><h3>{item.title}</h3><span className={`status-pill ${item.importance >= 4 ? 'warning' : 'neutral'}`}>重要度 {item.importance}</span></div><p>{item.summary}</p><div className="digest-tags">{[...(item.regions || []), ...(item.topics || [])].map((tag) => <span key={tag}>{tag}</span>)}</div>{(item.links || []).length > 0 && <div className="digest-links">{(item.links || []).map((link, index) => <a href={link} key={link} target="_blank" rel="noreferrer"><ExternalLink size={13} />来源 {index + 1}</a>)}</div>}</div></article>)}</section>
    {coverage.length > 0 && <details className="coverage-panel"><summary><ShieldCheck size={16} />来源覆盖：{healthySources}/{coverage.length} 正常</summary><div>{coverage.map((source) => <span key={source.source_id}><strong>{source.source_id}</strong><small>{source.status} · {source.observation_count} 条</small></span>)}</div></details>}
  </article>
}
