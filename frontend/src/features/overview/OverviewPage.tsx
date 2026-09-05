import { Activity, AlarmClock, CheckCircle2, ChevronRight, Clock3, Newspaper, Radar } from 'lucide-react'
import type { HealthSummary, Incident, ManagedSource, Reminder, SourceState, Tone } from '../../shared/types'
import { formatDate, incidentMeta, knownSources, relativeTime } from '../../shared/utils'

interface OverviewPageProps {
  health: HealthSummary
  pending: number
  observations: number
  sourceStates: SourceState[]
  managedSources: ManagedSource[]
  incidents: Incident[]
  reminders: Reminder[]
  go: (page: string) => void
  onCreateReminder: () => void
}

function stateTone(source: SourceState, stale: boolean): Tone {
  if (source.outage_alerted) return 'negative'
  if (Number(source.consecutive_failures || 0) > 0 || stale) return 'warning'
  return 'positive'
}

export function OverviewPage({ health, pending, observations, sourceStates, managedSources, incidents, reminders, go, onCreateReminder }: OverviewPageProps) {
  const activeReminders = reminders.filter((item) => item.enabled && item.next_run_at)
  const nextReminder = activeReminders[0]
  const sourceName = (id: string) => managedSources.find((source) => source.id === id)?.publisher || knownSources[id] || id
  const healthySources = sourceStates.filter((source) => stateTone(source, health.staleSourceIds.includes(source.source_id)) === 'positive').length
  return <>
    <section className="hero-grid">
      <article className={`health-hero ${health.level}`}>
        <div>
          <span className="eyebrow">系统态势</span>
          <h2>{health.headline}</h2>
          <p>{health.detail}</p>
          <div className="hero-signal-row">
            <span><span className={`health-dot ${health.engineState === 'online' ? 'ok' : health.engineState === 'offline' ? 'warning' : 'unknown'}`} />Argus {health.engineState === 'online' ? '在线' : health.engineState === 'offline' ? '离线' : '心跳待接入'}</span>
            {health.heartbeatAt && <span><Clock3 size={14} />{relativeTime(health.heartbeatAt)}</span>}
          </div>
        </div>
        <div className="hero-icon"><Activity size={31} /></div>
      </article>
      <article className="next-card">
        <div className="card-title-row"><span className="eyebrow">下一条提醒</span><AlarmClock size={19} /></div>
        {nextReminder ? <>
          <h3>{nextReminder.title}</h3>
          <p>{nextReminder.message}</p>
          <button className="time-link" onClick={() => go('reminders')}>{formatDate(nextReminder.next_run_at)}<ChevronRight size={16} /></button>
        </> : <>
          <h3>还没有安排提醒</h3>
          <p>可以设定未来某天、多久以后或每天固定时间。</p>
          <button className="time-link" onClick={onCreateReminder}>现在创建<ChevronRight size={16} /></button>
        </>}
      </article>
    </section>
    <section className="stats-grid">
      <article className="stat-card"><span>启用的提醒</span><strong>{activeReminders.length}</strong><small>按计划持久化执行</small></article>
      <article className="stat-card"><span>正常监测源</span><strong>{healthySources} / {sourceStates.length}</strong><small>{health.staleSourceIds.length ? `${health.staleSourceIds.length} 个数据已过期` : '按最近采集状态计算'}</small></article>
      <article className="stat-card"><span>已收集消息</span><strong>{observations.toLocaleString('zh-CN')}</strong><small>按保留策略自动清理</small></article>
      <article className="stat-card"><span>待发送</span><strong>{pending}</strong><small>{pending ? '正在可靠投递' : '队列为空'}</small></article>
    </section>
    <section className="content-grid">
      <article className="panel">
        <div className="panel-header"><div><h2>监测源状态</h2><p>最近一次采集结果</p></div><button className="text-button" onClick={() => go('sources')}>查看全部<ChevronRight size={15} /></button></div>
        <div className="list-stack">{sourceStates.length ? sourceStates.slice(0, 5).map((source) => {
          const stale = health.staleSourceIds.includes(source.source_id)
          const tone = stateTone(source, stale)
          return <div key={source.source_id} className="source-row">
            <div className="source-icon"><Newspaper size={18} /></div>
            <div className="row-main"><strong>{sourceName(source.source_id)}</strong><span>{source.last_success_at ? `${relativeTime(source.last_success_at)}成功` : '等待首次检查'}</span></div>
            <span className={`status-pill ${tone}`}>{tone === 'negative' ? '异常' : tone === 'warning' ? stale ? '数据过期' : '重试中' : '正常'}</span>
          </div>
        }) : <div className="empty-compact"><Radar size={22} /><span>服务启动后会在这里显示监测状态</span></div>}</div>
      </article>
      <article className="panel">
        <div className="panel-header"><div><h2>最近事件</h2><p>重要动态、异常与恢复</p></div><button className="text-button" onClick={() => go('events')}>全部事件<ChevronRight size={15} /></button></div>
        {incidents.length ? <div className="timeline-list">{incidents.slice(0, 4).map((incident) => {
          const meta = incidentMeta(incident)
          const title = incident.latest_title || `${(incident.source_ids || []).map(sourceName).join('、') || '系统'}重要动态`
          return <div key={incident.id} className="timeline-row"><div className={`timeline-icon ${meta.tone}`}><Newspaper size={17} /></div><div><strong>{title}</strong><span>{meta.label} · {formatDate(incident.last_seen_at)}</span></div></div>
        })}</div> : <div className="empty-compact positive"><CheckCircle2 size={22} /><span>目前没有重要事件记录</span></div>}
      </article>
    </section>
  </>
}
