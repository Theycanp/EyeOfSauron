import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowLeft, CheckCircle2, CircleAlert, Clock3, ExternalLink, Gauge, Newspaper, RefreshCw, ShieldCheck } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import { ApiError } from '../../shared/api'
import type { AlertDetailResponse, HealthSummary, Incident, IncidentStatus, ManagedSource } from '../../shared/types'
import { formatDate, incidentMeta, knownSources, pageSlice } from '../../shared/utils'
import { Pagination } from '../../shared/ui/Pagination'

type EventFilter = 'all' | IncidentStatus

interface EventsPageProps {
  api: AdminApi
  onUnauthorized: () => void
  initialAlertId?: string
  incidents: Incident[]
  managedSources: ManagedSource[]
  total?: number
  health: HealthSummary
  openCount: number
}

export function EventsPage({ api, onUnauthorized, initialAlertId, incidents, managedSources, total, health, openCount }: EventsPageProps) {
  const [filter, setFilter] = useState<EventFilter>('all')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<AlertDetailResponse | null>(null)
  const [detailLoading, setDetailLoading] = useState(Boolean(initialAlertId))
  const [detailError, setDetailError] = useState<string | null>(null)
  const pageSize = 15
  const filtered = useMemo(() => incidents.filter((incident) => filter === 'all' || incident.status === filter), [filter, incidents])
  const visible = pageSlice(filtered, page, pageSize)
  const sourceName = (id: string) => managedSources.find((source) => source.id === id)?.publisher || knownSources[id] || id
  const title = (incident: Incident) => incident.latest_title || `${(incident.source_ids || []).map(sourceName).join('、') || '系统'}${incident.status === 'recovered' ? '已恢复' : incident.status === 'recorded' ? '重要动态' : '需要留意'}`

  const openDetail = useCallback(async (alertId: number, updatePath = true) => {
    setDetailLoading(true)
    setDetailError(null)
    try {
      setSelected(await api.alert(alertId))
      if (updatePath) window.history.pushState(null, '', `/events/${alertId}`)
      window.scrollTo({ top: 0, behavior: 'auto' })
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
      else setDetailError(failure instanceof Error ? failure.message : '无法读取消息详情')
    } finally {
      setDetailLoading(false)
    }
  }, [api, onUnauthorized])

  useEffect(() => {
    if (!initialAlertId) return
    const timer = window.setTimeout(() => { void openDetail(Number(initialAlertId), false) }, 0)
    return () => window.clearTimeout(timer)
  }, [initialAlertId, openDetail])

  useEffect(() => {
    const handlePopState = () => {
      const match = /^\/events\/([1-9]\d*)$/.exec(window.location.pathname)
      if (match) void openDetail(Number(match[1]), false)
      else {
        setSelected(null)
        setDetailError(null)
      }
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [openDetail])

  const closeDetail = () => {
    setSelected(null)
    setDetailError(null)
    window.history.replaceState(null, '', '/#/events')
    window.scrollTo({ top: 0, behavior: 'auto' })
  }

  if (selected) return <EventReader detail={selected} onBack={closeDetail} sourceName={sourceName} />

  return <>
    <div className="page-actions"><div><h2>事件</h2><p>清楚区分一次性重要动态与能够自动恢复的异常。</p></div></div>
    {detailError && <div className="inline-error"><CircleAlert size={17} />{detailError}</div>}
    {detailLoading && <div className="loading-state"><RefreshCw className="spin" size={24} />正在读取消息详情…</div>}
    <section className={`event-summary ${health.level === 'healthy' ? 'positive' : health.level === 'unknown' ? 'neutral' : 'warning'}`}>
      {health.level === 'healthy' ? <CheckCircle2 size={25} /> : <CircleAlert size={25} />}
      <div><strong>{openCount ? `${openCount} 个异常仍在进行` : health.level === 'unknown' ? '事件为空，但引擎实时状态尚未验证' : '目前没有进行中的异常'}</strong><span>普通重要动态会保留为“已记录”，不会被算作未恢复异常。</span></div>
    </section>
    <div className="filter-row"><div className="segmented" role="group" aria-label="事件状态筛选">{([['all', '全部'], ['open', '进行中'], ['recovered', '已恢复'], ['recorded', '已记录']] as const).map(([value, label]) => <button key={value} aria-pressed={filter === value} className={filter === value ? 'active' : ''} onClick={() => { setFilter(value); setPage(1) }}>{label}</button>)}</div><span className="result-count">{filtered.length} 条</span></div>
    {visible.length ? <section className="panel event-list">{visible.map((incident) => {
      const meta = incidentMeta(incident)
      return <article key={incident.id} className="event-row"><div className={`event-icon ${meta.tone}`}><Newspaper size={20} /></div><div className="event-copy"><div className="row-title"><h3>{incident.latest_alert_id ? <button className="event-title-button" onClick={() => { void openDetail(Number(incident.latest_alert_id)) }}>{title(incident)}</button> : title(incident)}</h3><span className={`status-pill ${meta.tone}`}>{meta.label}</span></div><p>{incident.latest_message || (incident.evidence || []).slice(0, 2).join(' · ') || `累计 ${incident.observation_count || 0} 条相关记录`}</p><div className="meta-line"><Clock3 size={15} />{formatDate(incident.last_seen_at)}<span>·</span>置信度 {Math.round(Number(incident.confidence || 0) * 100)}%</div></div></article>
    })}</section> : <section className="empty-state"><div className="empty-icon positive"><CheckCircle2 size={29} /></div><h3>这个范围内没有事件</h3><p>EyeOfSauron 会记录重要动态；只有可恢复异常才会出现“进行中”和“已恢复”。</p></section>}
    <Pagination page={page} pageSize={pageSize} loaded={filtered.length} total={filter === 'all' ? total : undefined} onPage={setPage} noun="条事件" />
  </>
}

function EventReader({ detail, onBack, sourceName }: { detail: AlertDetailResponse; onBack: () => void; sourceName: (id: string) => string }) {
  const { alert, observation, incident } = detail
  const displayTitle = observation?.title || alert.title
  const sourceLabel = observation ? sourceName(observation.source_id) : (incident?.source_ids || []).map(sourceName).join('、') || 'EyeOfSauron'
  const rawSection = observation?.attributes?.section
  const section = typeof rawSection === 'string' ? rawSection.trim() : ''
  return <article className="event-reader">
    <header className="event-reader-header">
      <button className="button subtle" onClick={onBack}><ArrowLeft size={16} />返回事件</button>
      <div><span className="eyebrow">NOTIFICATION #{alert.id} · {sourceLabel}{section ? ` · ${section}` : ''}</span><h2>{displayTitle}</h2><div className="event-reader-meta"><span><Clock3 size={14} />发布 {formatDate(observation?.published_at || alert.created_at)}</span><span><ShieldCheck size={14} />置信度 {Math.round(Number(alert.confidence || 0) * 100)}%</span><span><Gauge size={14} />重要度 {observation?.importance || alert.priority}/5</span></div></div>
    </header>
    <section className="event-detail-grid">
      <div className="event-detail-main">
        <section className="panel event-detail-section"><div className="panel-header"><div><h2>已采集内容</h2><p>这是 EyeOfSauron 从来源 Feed 保存的内容，不依赖再次打开原站。</p></div></div><div className="event-detail-copy"><p>{observation?.summary || alert.message || '该来源没有提供摘要。'}</p></div></section>
        {alert.message && alert.message !== observation?.summary && <section className="panel event-detail-section"><div className="panel-header"><div><h2>通知说明</h2><p>发送到 ntfy 的消息正文。</p></div></div><div className="event-detail-copy preserve-lines"><p>{alert.message}</p></div></section>}
      </div>
      <aside className="panel event-facts"><div className="panel-header"><div><h2>判断依据</h2><p>通知触发时保存的可审计事实。</p></div></div><dl><div><dt>来源</dt><dd>{sourceLabel}</dd></div><div><dt>主题</dt><dd>{observation?.topic || 'general'}</dd></div><div><dt>地区</dt><dd>{observation?.region || 'GLOBAL'}</dd></div><div><dt>通知状态</dt><dd>{alert.status}</dd></div><div><dt>抓取时间</dt><dd>{formatDate(observation?.fetched_at || alert.created_at)}</dd></div></dl>{(alert.evidence || []).length > 0 && <div className="event-evidence">{(alert.evidence || []).map((item) => <span key={item}>{item}</span>)}</div>}{alert.source_url && <a className="button subtle wide" href={alert.source_url} target="_blank" rel="noreferrer"><ExternalLink size={16} />查看原文</a>}</aside>
    </section>
  </article>
}
