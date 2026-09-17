import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronUp, CircleAlert, Clock3, ExternalLink, Newspaper, RefreshCw, Signal } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import { ApiError } from '../../shared/api'
import type { NewsEvent, NewsEventReport, NewsEventResponse, NewsEventSort } from '../../shared/types'
import { formatDate } from '../../shared/utils'

const PAGE_SIZE = 50
const DEFAULT_HOURS = 28

interface DailyEventsPageProps {
  api: AdminApi
  onUnauthorized: () => void
}

const tierLabels: Record<NewsEventReport['source_tier'], string> = {
  primary: '一手',
  secondary: '二手',
  social: '社交/热度',
}

const relationLabels: Record<string, string> = {
  primary: '主要报道',
  corroborates: '交叉印证',
  updates: '后续更新',
  contradicts: '存在冲突',
  context: '背景信息',
  social: '社交动态',
}

function normalizedEvents(response: NewsEventResponse): NewsEvent[] {
  return Array.isArray(response.events) ? response.events : []
}

function appendUnique(current: NewsEvent[], incoming: NewsEvent[]): NewsEvent[] {
  const seen = new Set(current.map((event) => event.event_key))
  return [...current, ...incoming.filter((event) => !seen.has(event.event_key))]
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '无法读取日常事件'
}

export function DailyEventsPage({ api, onUnauthorized }: DailyEventsPageProps) {
  const [hours, setHours] = useState(DEFAULT_HOURS)
  const [hoursDraft, setHoursDraft] = useState(String(DEFAULT_HOURS))
  const [sort, setSort] = useState<NewsEventSort>('newest')
  const [events, setEvents] = useState<NewsEvent[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const requestSequence = useRef(0)
  const activeRequest = useRef<AbortController | null>(null)

  const invalidateRequests = useCallback(() => {
    requestSequence.current += 1
    activeRequest.current?.abort()
  }, [])

  useEffect(() => {
    let controller: AbortController | null = null
    const timer = window.setTimeout(() => {
      const sequence = ++requestSequence.current
      activeRequest.current?.abort()
      controller = new AbortController()
      activeRequest.current = controller
      setLoading(true)
      setLoadingMore(false)
      setError(null)
      setEvents([])
      setNextCursor(null)
      setExpanded(new Set())

      void api.newsEvents({ hours, sort, limit: PAGE_SIZE, signal: controller.signal })
        .then((response) => {
          if (sequence !== requestSequence.current) return
          setEvents(normalizedEvents(response))
          setNextCursor(response.pagination?.has_more === false ? null : String(response.pagination?.next_cursor || '') || null)
        })
        .catch((failure: unknown) => {
          if (sequence !== requestSequence.current) return
          if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
          else if (!(failure instanceof ApiError && failure.code === 'request_cancelled')) setError(errorMessage(failure))
        })
        .finally(() => {
          if (sequence === requestSequence.current) setLoading(false)
        })
    }, 0)

    return () => {
      window.clearTimeout(timer)
      controller?.abort()
    }
  }, [api, hours, onUnauthorized, refreshKey, sort])

  const loadMore = useCallback(async () => {
    if (!nextCursor || loading || loadingMore) return
    const cursor = nextCursor
    const sequence = ++requestSequence.current
    activeRequest.current?.abort()
    const controller = new AbortController()
    activeRequest.current = controller
    setLoadingMore(true)
    setError(null)
    try {
      const response = await api.newsEvents({ hours, sort, limit: PAGE_SIZE, cursor, signal: controller.signal })
      if (sequence !== requestSequence.current) return
      setEvents((current) => appendUnique(current, normalizedEvents(response)))
      setNextCursor(response.pagination?.has_more === false ? null : String(response.pagination?.next_cursor || '') || null)
    } catch (failure) {
      if (sequence !== requestSequence.current) return
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
      else if (!(failure instanceof ApiError && failure.code === 'request_cancelled')) setError(errorMessage(failure))
    } finally {
      if (sequence === requestSequence.current) setLoadingMore(false)
    }
  }, [api, hours, loading, loadingMore, nextCursor, onUnauthorized, sort])

  const applyHours = () => {
    const value = Number(hoursDraft)
    if (Number.isInteger(value) && value >= 1 && value <= 720 && value !== hours) {
      invalidateRequests()
      setHours(value)
    }
  }

  const changeSort = (value: NewsEventSort) => {
    if (value === sort) return
    invalidateRequests()
    setSort(value)
  }

  const refresh = () => {
    invalidateRequests()
    setRefreshKey((value) => value + 1)
  }

  const toggleExpanded = (eventKey: string) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(eventKey)) next.delete(eventKey)
      else next.add(eventKey)
      return next
    })
  }

  const validHours = Number.isInteger(Number(hoursDraft)) && Number(hoursDraft) >= 1 && Number(hoursDraft) <= 720

  return <>
    <div className="page-actions daily-events-heading">
      <div><h2>日常事件</h2><p>在一个时间窗口里查看 Argus 聚合后的新闻事件和原始报道。</p></div>
      <button className="icon-button" aria-label="刷新日常事件" disabled={loading || loadingMore} onClick={refresh}><RefreshCw className={loading ? 'spin' : ''} size={18} /></button>
    </div>

    <section className="daily-events-toolbar" aria-label="日常事件筛选">
      <form className="daily-window-control" onSubmit={(event) => { event.preventDefault(); applyHours() }}>
        <label htmlFor="daily-events-hours"><Clock3 size={16} />时间窗</label>
        <input id="daily-events-hours" aria-label="时间窗（小时）" type="number" min="1" max="720" step="1" inputMode="numeric" value={hoursDraft} onChange={(event) => setHoursDraft(event.target.value)} />
        <span>小时</span>
        <button className="button subtle" type="submit" disabled={!validHours || Number(hoursDraft) === hours}>应用</button>
      </form>
      <div className="segmented" aria-label="排序方式">
        <button type="button" className={sort === 'newest' ? 'active' : ''} aria-pressed={sort === 'newest'} onClick={() => changeSort('newest')}>最新</button>
        <button type="button" className={sort === 'importance' ? 'active' : ''} aria-pressed={sort === 'importance'} onClick={() => changeSort('importance')}>重要</button>
      </div>
      <span className="result-count">过去 {hours} 小时 · 已显示 {events.length} 个事件</span>
    </section>

    {error && <div className="inline-error" role="alert"><CircleAlert size={17} /><span>{error}</span>{!events.length && <button className="text-button" onClick={refresh}>重试</button>}</div>}

    {loading && !events.length ? <div className="loading-state"><RefreshCw className="spin" size={24} />正在聚合日常事件…</div> : events.length ? <section className="daily-event-list" aria-live="polite">
      {events.map((item) => <DailyEventCard key={item.event_key} event={item} open={expanded.has(item.event_key)} onToggle={() => toggleExpanded(item.event_key)} />)}
    </section> : !error && <section className="empty-state"><div className="empty-icon"><Newspaper size={29} /></div><h3>这个时间窗内还没有事件</h3><p>可以扩大时间窗，或稍后等 Argus 收集到新的报道。</p></section>}

    {nextCursor && events.length > 0 && <div className="daily-events-more"><button className="button subtle" disabled={loadingMore} onClick={() => { void loadMore() }}>{loadingMore ? <RefreshCw className="spin" size={16} /> : <ChevronDown size={16} />}{loadingMore ? '正在加载…' : '加载更多'}</button></div>}
  </>
}

function DailyEventCard({ event, open, onToggle }: { event: NewsEvent; open: boolean; onToggle: () => void }) {
  const reports = event.reports || []
  const tags = [...new Set([...(event.regions || []), ...(event.topics || [])])]
  return <article className={`daily-event-card ${open ? 'open' : ''}`}>
    <button className="daily-event-summary" type="button" aria-expanded={open} onClick={onToggle}>
      <span className={`daily-event-score ${event.importance >= 4 ? 'high' : ''}`}><strong>{event.importance}</strong><small>重要度</small></span>
      <span className="daily-event-copy">
        <span className="daily-event-title"><strong>{event.title}</strong>{event.handling === 'immediate' && <span className="status-pill warning">即时通知</span>}<span className={`status-pill ${event.status === 'active' ? 'info' : 'neutral'}`}>{event.status === 'active' ? '活跃' : event.status === 'quiet' ? '低活跃' : '已结束'}</span></span>
        <span className="daily-event-description">{event.summary || '暂无事件摘要。'}</span>
        <span className="daily-event-meta"><span><Clock3 size={13} />更新于 {formatDate(event.last_seen_at)}</span><span><Signal size={13} />{event.independent_source_count || 0} 个独立来源</span><span>综合分 {Number(event.score || 0).toFixed(1)}</span></span>
        {tags.length > 0 && <span className="daily-event-tags">{tags.map((tag) => <span key={tag}>{tag}</span>)}</span>}
      </span>
      <span className="daily-event-chevron" aria-hidden="true">{open ? <ChevronUp size={18} /> : <ChevronDown size={18} />}</span>
    </button>

    {open && <div className="daily-event-reports">
      <div className="daily-event-report-heading"><strong>原始报道</strong><span>{event.report_count || reports.length} 篇{event.reports_truncated ? `，当前展示 ${reports.length} 篇` : ''}</span></div>
      {reports.length ? reports.map((report) => <article className="daily-event-report" key={report.report_id || `${report.observation_id}-${report.source_id}`}>
        <div className="daily-event-report-meta"><span className="status-pill neutral">{tierLabels[report.source_tier] || report.source_tier}</span><strong>{report.publisher || report.source_id}</strong><span>{relationLabels[report.relation] || report.relation}</span><time>{formatDate(report.published_at)}</time></div>
        <h4>{report.title || event.title}</h4>
        {report.summary && <p>{report.summary}</p>}
        {report.url && <a href={report.url} target="_blank" rel="noreferrer"><ExternalLink size={14} />查看原文</a>}
      </article>) : <p className="daily-event-no-reports">这个事件暂时没有可展示的原始报道。</p>}
    </div>}
  </article>
}
