import type { AdminStatus, ConfigResponse, HealthSummary, Incident, Reminder, Tone } from './types'

export const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai'

export const knownSources: Record<string, string> = {
  bloomberg_markets: 'Bloomberg Markets',
  bloomberg_politics: 'Bloomberg Politics',
  bloomberg_technology: 'Bloomberg Technology',
  bloomberg_economics: 'Bloomberg Economics',
}

export function dateTimeValue(epoch?: number | null): string {
  const date = new Date((epoch || Math.floor(Date.now() / 1000) + 3600) * 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function formatDate(epoch?: number | null): string {
  if (!epoch) return '尚无记录'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(epoch * 1000))
}

export function relativeTime(epoch?: number | null, now = Date.now() / 1000): string {
  if (!epoch) return '尚未运行'
  const seconds = Math.round(epoch - now)
  const absolute = Math.abs(seconds)
  if (absolute < 60) return seconds > 0 ? '不到 1 分钟后' : '刚刚'
  if (absolute < 3600) return `${Math.round(absolute / 60)} 分钟${seconds > 0 ? '后' : '前'}`
  if (absolute < 86400) return `${Math.round(absolute / 3600)} 小时${seconds > 0 ? '后' : '前'}`
  return `${Math.round(absolute / 86400)} 天${seconds > 0 ? '后' : '前'}`
}

export function splitWords(value: string): string[] {
  return value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean)
}

export function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function incidentMeta(incident: Incident): { label: string; tone: Tone } {
  if (incident.status === 'recorded') return { label: '已记录', tone: 'info' }
  if (incident.status === 'recovered') return { label: '已恢复', tone: 'positive' }
  return { label: '进行中', tone: 'negative' }
}

export function reminderBucket(item: Reminder): 'active' | 'paused' | 'completed' {
  if (item.completed_at) return 'completed'
  if (!item.enabled) return 'paused'
  return 'active'
}

export function pageSlice<T>(items: T[], page: number, pageSize: number): T[] {
  return items.slice((page - 1) * pageSize, page * pageSize)
}

function runtimeStatus(status: AdminStatus): { heartbeatAt: number | null; appliedRevision: number | null; state: string | null } {
  const runtime = status.runtime || status.engine || {}
  const appliedRevision = runtime.applied_revision ?? status.applied_revision
  return {
    heartbeatAt: Number(runtime.heartbeat_at || runtime.last_heartbeat_at || status.heartbeat_at || 0) || null,
    appliedRevision: appliedRevision === null || appliedRevision === undefined ? null : Number(appliedRevision),
    state: typeof runtime.state === 'string' ? runtime.state : null,
  }
}

export function deriveHealth(config: ConfigResponse | null, now = Date.now() / 1000): HealthSummary {
  const status = config?.status || {}
  const sources = status.sources || []
  const managed = config?.managed.sources || []
  const managedIntervals = new Map(managed.map((source) => [source.id, Number(source.poll_interval_seconds || 300)]))
  const enabledSources = sources.filter((source) => source.runtime_status !== 'disabled' && source.configured_enabled !== false && source.configured_enabled !== 0 && !managed.some((item) => item.id === source.source_id && item.enabled === false))
  const staleSourceIds = enabledSources.filter((source) => {
    if (!source.last_success_at) return Boolean(source.initialized)
    const interval = Number(source.expected_interval_seconds || managedIntervals.get(source.source_id) || 120)
    return now - source.last_success_at > Math.max(interval * 3, 600)
  }).map((source) => source.source_id)
  const failing = enabledSources.filter((source) => Number(source.consecutive_failures || 0) > 0 || source.outage_alerted || ['degraded', 'invalid', 'stale'].includes(source.runtime_status || ''))
  const openIncidents = Number(status.incidents?.open || 0)
  const deadLetters = Number(status.outbox?.dead || 0)
  const oldestPendingAgeSeconds = Number(status.outbox_metrics?.oldest_pending_age_seconds || 0)
  const runtime = runtimeStatus(status)
  const explicitlyOffline = ['offline', 'failed', 'stopping'].includes(runtime.state || '')
  const heartbeatTimeout = Number(status.runtime?.heartbeat_timeout_seconds || status.engine?.heartbeat_timeout_seconds || 60)
  const engineState = runtime.heartbeatAt === null ? explicitlyOffline ? 'offline' : 'unknown' : explicitlyOffline || now - runtime.heartbeatAt > heartbeatTimeout ? 'offline' : 'online'
  const desiredValue = config?.revision?.revision ?? status.desired_revision
  const desiredRevision = desiredValue === null || desiredValue === undefined ? null : Number(desiredValue)
  const appliedRevision = runtime.appliedRevision
  const configPending = desiredRevision === null ? null : appliedRevision === null ? desiredRevision > 0 : desiredRevision !== appliedRevision

  if (engineState === 'offline') {
    return { level: 'attention', headline: 'Argus 引擎可能已离线', detail: `最后心跳在${relativeTime(runtime.heartbeatAt, now)}。`, engineState, heartbeatAt: runtime.heartbeatAt, desiredRevision, appliedRevision, configPending, staleSourceIds, deadLetters, oldestPendingAgeSeconds }
  }
  if (runtime.state === 'degraded' || failing.length || openIncidents || staleSourceIds.length || configPending || deadLetters || oldestPendingAgeSeconds > 900) {
    return { level: 'attention', headline: '有新的情况需要留意', detail: `${failing.length} 个监测任务异常，${staleSourceIds.length} 个任务数据过期，${openIncidents} 个异常仍在进行，${deadLetters} 条通知进入死信。`, engineState, heartbeatAt: runtime.heartbeatAt, desiredRevision, appliedRevision, configPending, staleSourceIds, deadLetters, oldestPendingAgeSeconds }
  }
  if (engineState === 'unknown') {
    return { level: 'unknown', headline: '运行状态尚未验证', detail: '尚未收到 Argus 心跳。', engineState, heartbeatAt: null, desiredRevision, appliedRevision, configPending, staleSourceIds, deadLetters, oldestPendingAgeSeconds }
  }
  if (runtime.state === 'starting' || enabledSources.some((source) => ['starting', 'configured'].includes(source.runtime_status || '') || !source.initialized && !source.last_success_at)) {
    return { level: 'unknown', headline: '监测任务正在启动', detail: '等待首次检查结果。', engineState, heartbeatAt: runtime.heartbeatAt, desiredRevision, appliedRevision, configPending, staleSourceIds, deadLetters, oldestPendingAgeSeconds }
  }
  return { level: 'healthy', headline: '一切运行正常', detail: 'Argus 心跳、监测任务、事件和通知队列均未发现异常。', engineState, heartbeatAt: runtime.heartbeatAt, desiredRevision, appliedRevision, configPending, staleSourceIds, deadLetters, oldestPendingAgeSeconds }
}
