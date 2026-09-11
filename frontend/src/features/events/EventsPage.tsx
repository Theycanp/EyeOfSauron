import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, CheckCircle2, CircleAlert, Clock3, ExternalLink, FileText, Gauge, Newspaper, Plus, RefreshCw, ShieldCheck } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import { ApiError } from '../../shared/api'
import type { AlertDetailResponse, HealthSummary, Incident, IncidentStatus, ManagedSource, ManualEventDraft } from '../../shared/types'
import { formatDate, incidentMeta, knownSources, pageSlice } from '../../shared/utils'
import { Pagination } from '../../shared/ui/Pagination'
import { ManualEventDialog } from './ManualEventDialog'
import { emptyManualEventDraft } from './manualEventModel'

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
  canCreate?: boolean
  onCreated?: () => void
  notify?: (message: string, tone?: 'success' | 'error') => void
}

export function EventsPage({ api, onUnauthorized, initialAlertId, incidents, managedSources, total, health, openCount, canCreate = false, onCreated, notify }: EventsPageProps) {
  const [filter, setFilter] = useState<EventFilter>('all')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<AlertDetailResponse | null>(null)
  const [detailLoading, setDetailLoading] = useState(Boolean(initialAlertId))
  const [detailError, setDetailError] = useState<string | null>(null)
  const [eventDraft, setEventDraft] = useState<ManualEventDraft>(emptyManualEventDraft)
  const [eventBusy, setEventBusy] = useState(false)
  const [eventError, setEventError] = useState<string | null>(null)
  const eventDialog = useRef<HTMLDialogElement>(null)
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

  const openEventDialog = () => {
    setEventDraft({ ...emptyManualEventDraft })
    setEventError(null)
    eventDialog.current?.showModal()
  }

  const closeEventDialog = () => {
    eventDialog.current?.close()
    setEventError(null)
  }

  const createEvent = async () => {
    setEventBusy(true)
    setEventError(null)
    try {
      const detail = await api.createEvent(eventDraft)
      closeEventDialog()
      setSelected(detail)
      window.history.pushState(null, '', `/events/${detail.alert.id}`)
      window.scrollTo({ top: 0, behavior: 'auto' })
      onCreated?.()
      notify?.('事件已创建，正在通过 ntfy 发送')
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
      else setEventError(failure instanceof Error ? failure.message : '无法创建事件')
    } finally {
      setEventBusy(false)
    }
  }

  if (selected) return <EventReader detail={selected} onBack={closeDetail} sourceName={sourceName} />

  return <>
    <div className="page-actions"><div><h2>事件</h2><p>清楚区分一次性重要动态与能够自动恢复的异常。</p></div>{canCreate && <button className="button primary" onClick={openEventDialog}><Plus size={18} />添加事件</button>}</div>
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
    <ManualEventDialog ref={eventDialog} draft={eventDraft} setDraft={setEventDraft} busy={eventBusy} error={eventError} dirty={JSON.stringify(eventDraft) !== JSON.stringify(emptyManualEventDraft)} onClose={closeEventDialog} onSubmit={() => { void createEvent() }} />
  </>
}

function EventReader({ detail, onBack, sourceName }: { detail: AlertDetailResponse; onBack: () => void; sourceName: (id: string) => string }) {
  const { alert, observation, incident, content_fetch: contentFetch } = detail
  const documents = (detail.documents || []).filter((item) => item.body.trim())
  const content = documents.find((item) => ['document', 'full_text'].includes(item.level))
    || documents.find((item) => item.level === 'analysis')
    || documents.find((item) => item.level === 'excerpt')
  const contentMeta = content ? contentLabels(content.level, content.source_method, content.rights_policy) : null
  const isManual = observation?.source_id === 'manual'
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
        <section className="panel event-detail-section"><div className="panel-header content-heading"><div><h2>{isManual ? '事件内容' : contentMeta?.heading || '已采集内容'}</h2><p>{isManual ? '这是后台提交并由 EyeOfSauron 保存的原始内容。' : contentMeta?.description || '该来源目前只提供了通知元数据。'}</p></div>{contentMeta && <span className={`content-level ${contentMeta.tone}`}><FileText size={14} />{contentMeta.label}</span>}</div><div className="event-detail-copy preserve-lines"><p>{content?.body || observation?.summary || alert.message || '该来源没有提供摘要。'}</p></div>{content && <footer className="content-provenance"><span>取得方式：{contentMeta?.method}</span><span>内容策略：{contentMeta?.rights}</span><span>保存时间：{formatDate(content.fetched_at)}</span></footer>}</section>
        {contentFetch && !['completed'].includes(contentFetch.status) && <section className={`content-fetch-state ${contentFetch.status === 'dead' ? 'negative' : 'neutral'}`}><RefreshCw className={['pending', 'leased', 'retry'].includes(contentFetch.status) ? 'spin' : ''} size={16} /><span>{contentFetch.status === 'dead' ? '公开文档正文抓取失败，已保留 Feed 摘要。' : '公开文档正文正在独立抓取，当前先显示 Feed 摘要。'}</span></section>}
        {alert.message && alert.message !== observation?.summary && <section className="panel event-detail-section"><div className="panel-header"><div><h2>通知说明</h2><p>发送到 ntfy 的消息正文。</p></div></div><div className="event-detail-copy preserve-lines"><p>{alert.message}</p></div></section>}
      </div>
      <aside className="panel event-facts"><div className="panel-header"><div><h2>判断依据</h2><p>通知触发时保存的可审计事实。</p></div></div><dl><div><dt>来源</dt><dd>{sourceLabel}</dd></div><div><dt>主题</dt><dd>{observation?.topic || 'general'}</dd></div><div><dt>地区</dt><dd>{observation?.region || 'GLOBAL'}</dd></div><div><dt>通知状态</dt><dd>{alert.status}</dd></div><div><dt>{isManual ? '记录时间' : '抓取时间'}</dt><dd>{formatDate(observation?.fetched_at || alert.created_at)}</dd></div></dl>{(alert.evidence || []).length > 0 && <div className="event-evidence">{(alert.evidence || []).map((item) => <span key={item}>{item}</span>)}</div>}{alert.source_url && <a className="button subtle wide" href={alert.source_url} target="_blank" rel="noreferrer"><ExternalLink size={16} />查看原文</a>}</aside>
    </section>
  </article>
}

function contentLabels(level: string, method: string, rights: string): { heading: string; label: string; method: string; rights: string; description: string; tone: string } {
  type LevelLabel = { heading: string; label: string; description: string; tone: string }
  const fallback: LevelLabel = { heading: '来源元数据', label: '元数据', description: '来源只提供了标题和基本元数据。', tone: 'neutral' }
  const levelLabels: Record<string, LevelLabel> = {
    document: { heading: '公开文档', label: '文档全文', description: '来自公开一手资料，已提取为纯文本保存。', tone: 'positive' },
    full_text: { heading: '已采集正文', label: '全文', description: '来源明确提供或允许保存的正文纯文本。', tone: 'info' },
    analysis: { heading: '分析内容', label: '分析', description: '由已配置的分析流程生成并保存。', tone: 'warning' },
    excerpt: { heading: 'Feed 摘要', label: '摘要', description: '来源 Feed 当时提供的摘要，不依赖再次打开原站。', tone: 'neutral' },
    metadata: fallback,
  }
  const methods: Record<string, string> = {
    rss_description: 'RSS description', rss_content: 'RSS content:encoded', atom_summary: 'Atom summary', atom_content: 'Atom content',
    public_html: '公开网页正文提取', public_pdf: '公开 PDF 文本提取', public_text: '公开文本读取', manual_entry: '后台人工录入', normalized_observation: '标准化采集记录',
  }
  const rightsLabels: Record<string, string> = {
    public_official_document: '公开一手文档', source_authorized_feed: '来源授权的 Feed 全文', source_terms_apply: '遵循来源条款，仅保留 Feed 内容', user_supplied: '用户提供',
  }
  const selected = levelLabels[level] ?? fallback
  return { ...selected, method: methods[method] || method, rights: rightsLabels[rights] || rights }
}
