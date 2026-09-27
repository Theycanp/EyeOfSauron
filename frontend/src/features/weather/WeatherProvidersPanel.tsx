import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Save } from 'lucide-react'
import { AdminApi, ApiError } from '../../shared/api'
import type { WeatherProviderPolicy } from '../../shared/types'
import { formatDate } from '../../shared/utils'

const channels: Record<string, string> = { forecast: '日预报', air_quality: '空气质量', minutely: '临近雨雪', alerts: '官方预警', astronomy: '天文', hourly: '小时兜底' }
const statuses: Record<string, string> = { disabled: '已关闭', not_configured: '未配置', budget_exhausted: '今日预算已用完', error: '查询失败', awaiting_engine: '等待引擎检查', conditional_standby: '条件性待命', enabled: '已启用' }

export function WeatherProvidersPanel({ api, canWrite, onUnauthorized }: { api: AdminApi; canWrite: boolean; onUnauthorized: () => void }) {
  const [policies, setPolicies] = useState<WeatherProviderPolicy[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const failure = useCallback((cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) onUnauthorized()
    else setError(cause instanceof Error ? cause.message : '无法读取天气来源')
  }, [onUnauthorized])
  useEffect(() => {
    let active = true
    void api.weatherProviderPolicies().then(result => { if (active) setPolicies(result.policies) }).catch(cause => { if (active) failure(cause) })
    return () => { active = false }
  }, [api, failure])
  const edit = (index: number, patch: Partial<WeatherProviderPolicy>) => setPolicies(current => current.map((item, i) => i === index ? { ...item, ...patch } : item))
  const save = async (policy: WeatherProviderPolicy) => {
    const key = `${policy.provider}:${policy.kind}`
    setBusy(key); setError(''); setMessage('')
    try {
      const result = await api.updateWeatherProviderPolicy(policy.provider, policy.kind, {
        enabled: Boolean(policy.enabled), interval_seconds: policy.interval_seconds, daily_budget: policy.daily_budget,
      })
      setPolicies(current => current.map(item => item.provider === policy.provider && item.kind === policy.kind ? result.policy : item))
      setMessage(`${channels[policy.kind] || policy.kind}设置已保存`)
    } catch (cause) { failure(cause) } finally { setBusy(null) }
  }
  return <section className="provider-policies" aria-label="天气来源用量与预算">
    <h3>天气来源用量与预算</h3>
    {error && <p className="form-error" role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    <div className="provider-policy-list">{policies.map((policy, index) => {
      const name = `${policy.provider === 'open_meteo' ? 'Open-Meteo' : 'QWeather'} · ${channels[policy.kind] || policy.kind}`
      const valid = Number.isInteger(policy.interval_seconds) && policy.interval_seconds >= 60 && policy.interval_seconds <= 86400 && Number.isInteger(policy.daily_budget) && policy.daily_budget >= 1 && policy.daily_budget <= 10000
      return <form className="provider-policy-row" aria-label={name} key={`${policy.provider}:${policy.kind}`} onSubmit={event => { event.preventDefault(); void save(policy) }}>
        <div className="provider-policy-state"><strong>{name}</strong><span>今日已用 {policy.requests ?? 0} / {policy.daily_budget}</span>{policy.budget_day && <small>预算日期 {policy.budget_day}（UTC）</small>}<small>{policy.last_request_at ? `最近请求 ${formatDate(policy.last_request_at)}` : '尚无请求'}{policy.status ? ` · ${statuses[policy.status] || policy.status}` : ''}</small>{policy.last_error && <small className="form-error">{policy.last_error}</small>}</div>
        <label className="switch-field"><input aria-label={`${name}启用`} type="checkbox" disabled={!canWrite || busy !== null} checked={Boolean(policy.enabled)} onChange={event => edit(index, { enabled: event.target.checked })} />启用</label>
        <label>间隔（秒）<input aria-label={`${name}间隔（秒）`} type="number" min="60" max="86400" step="1" value={policy.interval_seconds} disabled={!canWrite || busy !== null} onChange={event => edit(index, { interval_seconds: Number(event.target.value) })} /></label>
        <label>每日预算<input aria-label={`${name}每日预算`} type="number" min="1" max="10000" step="1" value={policy.daily_budget} disabled={!canWrite || busy !== null} onChange={event => edit(index, { daily_budget: Number(event.target.value) })} /></label>
        <button type="submit" className="icon-button" aria-label={`保存${name}`} title={`保存${name}`} disabled={!canWrite || busy !== null || !valid}>{busy === `${policy.provider}:${policy.kind}` ? <RefreshCw className="spin" size={17} /> : <Save size={17} />}</button>
      </form>
    })}</div>
  </section>
}
