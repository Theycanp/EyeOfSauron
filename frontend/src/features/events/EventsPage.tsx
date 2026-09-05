import { useMemo, useState } from 'react'
import { CheckCircle2, CircleAlert, Clock3, Newspaper } from 'lucide-react'
import type { HealthSummary, Incident, IncidentStatus, ManagedSource } from '../../shared/types'
import { formatDate, incidentMeta, knownSources, pageSlice } from '../../shared/utils'
import { Pagination } from '../../shared/ui/Pagination'

type EventFilter = 'all' | IncidentStatus

interface EventsPageProps {
  incidents: Incident[]
  managedSources: ManagedSource[]
  total?: number
  health: HealthSummary
  openCount: number
}

export function EventsPage({ incidents, managedSources, total, health, openCount }: EventsPageProps) {
  const [filter, setFilter] = useState<EventFilter>('all')
  const [page, setPage] = useState(1)
  const pageSize = 15
  const filtered = useMemo(() => incidents.filter((incident) => filter === 'all' || incident.status === filter), [filter, incidents])
  const visible = pageSlice(filtered, page, pageSize)
  const sourceName = (id: string) => managedSources.find((source) => source.id === id)?.publisher || knownSources[id] || id
  const title = (incident: Incident) => incident.latest_title || `${(incident.source_ids || []).map(sourceName).join('、') || '系统'}${incident.status === 'recovered' ? '已恢复' : incident.status === 'recorded' ? '重要动态' : '需要留意'}`
  return <>
    <div className="page-actions"><div><h2>事件</h2><p>清楚区分一次性重要动态与能够自动恢复的异常。</p></div></div>
    <section className={`event-summary ${health.level === 'healthy' ? 'positive' : health.level === 'unknown' ? 'neutral' : 'warning'}`}>
      {health.level === 'healthy' ? <CheckCircle2 size={25} /> : <CircleAlert size={25} />}
      <div><strong>{openCount ? `${openCount} 个异常仍在进行` : health.level === 'unknown' ? '事件为空，但引擎实时状态尚未验证' : '目前没有进行中的异常'}</strong><span>普通重要动态会保留为“已记录”，不会被算作未恢复异常。</span></div>
    </section>
    <div className="filter-row"><div className="segmented" role="group" aria-label="事件状态筛选">{([['all', '全部'], ['open', '进行中'], ['recovered', '已恢复'], ['recorded', '已记录']] as const).map(([value, label]) => <button key={value} aria-pressed={filter === value} className={filter === value ? 'active' : ''} onClick={() => { setFilter(value); setPage(1) }}>{label}</button>)}</div><span className="result-count">{filtered.length} 条</span></div>
    {visible.length ? <section className="panel event-list">{visible.map((incident) => {
      const meta = incidentMeta(incident)
      return <article key={incident.id} className="event-row"><div className={`event-icon ${meta.tone}`}><Newspaper size={20} /></div><div className="event-copy"><div className="row-title"><h3>{incident.latest_click_url ? <a href={incident.latest_click_url} target="_blank" rel="noreferrer">{title(incident)}<span className="sr-only">（在新窗口打开）</span></a> : title(incident)}</h3><span className={`status-pill ${meta.tone}`}>{meta.label}</span></div><p>{incident.latest_message || (incident.evidence || []).slice(0, 2).join(' · ') || `累计 ${incident.observation_count || 0} 条相关记录`}</p><div className="meta-line"><Clock3 size={15} />{formatDate(incident.last_seen_at)}<span>·</span>置信度 {Math.round(Number(incident.confidence || 0) * 100)}%</div></div></article>
    })}</section> : <section className="empty-state"><div className="empty-icon positive"><CheckCircle2 size={29} /></div><h3>这个范围内没有事件</h3><p>EyeOfSauron 会记录重要动态；只有可恢复异常才会出现“进行中”和“已恢复”。</p></section>}
    <Pagination page={page} pageSize={pageSize} loaded={filtered.length} total={filter === 'all' ? total : undefined} onPage={setPage} noun="条事件" />
  </>
}
