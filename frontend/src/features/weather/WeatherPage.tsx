import { useEffect, useState, type FormEvent } from 'react'
import { CloudSun, Droplets, LocateFixed, MapPin, Moon, RefreshCw, Search, Sun, Wind } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import type { WeatherPlace, WeatherStatus, WeatherSubscription } from '../../shared/types'
import { formatDate } from '../../shared/utils'
import './weather.css'

interface Props {
  api: AdminApi
  canWrite: boolean
  onUnauthorized: () => void
}

function localClock(timestamp: number | null | undefined, timezone: string): string {
  if (timestamp == null) return '—'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(timestamp * 1000)
}

function localDate(timestamp: number, timezone: string): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(timestamp * 1000)
}

export function WeatherPage({ api, canWrite, onUnauthorized }: Props) {
  const [status, setStatus] = useState<WeatherStatus | null>(null)
  const [draft, setDraft] = useState<WeatherSubscription | null>(null)
  const [query, setQuery] = useState('')
  const [places, setPlaces] = useState<WeatherPlace[]>([])
  const [busy, setBusy] = useState(false)
  const [searching, setSearching] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = async (replaceDraft = false) => {
    try {
      const value = await api.weather()
      setStatus(value)
      if (replaceDraft) setDraft(value.subscription)
      setError('')
    } catch (cause) {
      if (cause instanceof Error && cause.message.includes('登录')) onUnauthorized()
      else setError(cause instanceof Error ? cause.message : '天气数据暂时无法读取')
    }
  }

  useEffect(() => {
    let active = true
    void api.weather().then((value) => {
      if (!active) return
      setStatus(value)
      setDraft(value.subscription)
      setError('')
    }).catch((cause: unknown) => {
      if (!active) return
      if (cause instanceof Error && cause.message.includes('登录')) onUnauthorized()
      else setError(cause instanceof Error ? cause.message : '天气数据暂时无法读取')
    })
    return () => { active = false }
  }, [api, onUnauthorized])

  const search = async () => {
    if (query.trim().length < 2) return
    setSearching(true)
    setError('')
    try {
      const response = await api.weatherPlaces(query)
      setPlaces(response.places)
      if (!response.places.length) setMessage('没有找到匹配地点，可以直接填写坐标。')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '地点搜索失败')
    } finally {
      setSearching(false)
    }
  }

  const usePosition = () => {
    if (!navigator.geolocation) {
      setError('当前浏览器不支持定位，请搜索地点或填写坐标。')
      return
    }
    setBusy(true)
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        setDraft((current) => current && ({ ...current, label: '当前位置',
          latitude: Number(coords.latitude.toFixed(6)), longitude: Number(coords.longitude.toFixed(6)),
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || current.timezone }))
        setBusy(false)
        setError('')
      },
      () => { setBusy(false); setError('定位失败或未获授权，请搜索地点或填写坐标。') },
      { enableHighAccuracy: false, timeout: 12_000, maximumAge: 10 * 60_000 },
    )
  }

  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!draft || !canWrite) return
    setBusy(true)
    setMessage('')
    setError('')
    try {
      const payload = {
        label: draft.label, latitude: draft.latitude, longitude: draft.longitude,
        timezone: draft.timezone, daily_time: draft.daily_time,
        daily_enabled: draft.daily_enabled, alerts_enabled: draft.alerts_enabled,
        revision: draft.revision,
      }
      const response = await api.saveWeather(payload)
      setDraft(response.subscription)
      await load()
      setMessage('天气订阅已更新，下次查询自动使用新设置。')
      setPlaces([])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  const choose = (place: WeatherPlace) => {
    setDraft((current) => current && ({ ...current, ...place }))
    setPlaces([])
    setQuery('')
  }

  if (!draft) return error ? <div className="form-error" role="alert">{error}<button className="button subtle" onClick={() => { void load(true) }}>重试</button></div>
    : <div className="loading-state"><RefreshCw className="spin" size={20} />正在读取天气订阅…</div>

  const latest = status?.latest
  const today = latest?.is_today

  return <div className="weather-page">
    <div className="page-actions"><div><h2>本地天气</h2><p>{status?.subscription.label || draft.label}</p></div>
      <button className="icon-button" aria-label="刷新天气状态" title="刷新天气状态" onClick={() => { void load() }}><RefreshCw size={18} /></button></div>

    <section className="weather-current" aria-label="最新天气预报">
      <div className="weather-current-icon"><CloudSun size={34} /></div>
      <div><span className="eyebrow">{status?.last_success_at ? `${today ? '今日预报' : '历史预报'} · 上次查询 ${formatDate(status.last_success_at)}` : '等待首次查询'}</span>
        <h3>{status?.latest?.condition || '暂无预报'}</h3>
        <p>{latest?.low != null && latest.high != null ? `${Math.round(latest.low)}~${Math.round(latest.high)}℃` : '温度待获取'}
          <span> · </span>{today ? '今日' : '当日'}降水 {latest?.rain_mm ?? '—'} mm<span> · </span>阵风 {latest?.wind_gust_kmh ?? '—'} km/h</p>
      </div>
      <div className="weather-current-side"><Wind size={17} />{status?.rain_expected === true ? '今日预计有雨' : status?.rain_expected === false ? '当前预报无明显降雨' : '雨情等待基线'}</div>
    </section>
    {latest && <section className="weather-metrics" aria-label={today ? '今日天气详情' : '历史天气详情'}>
      <div><Droplets size={16} /><span>湿度</span><strong>{latest.humidity != null ? `${Math.round(latest.humidity)}%` : '—'}</strong></div>
      <div><Wind size={16} /><span>风</span><strong>{latest.wind_speed_kmh != null ? `${Math.round(latest.wind_speed_kmh)} km/h ${latest.wind_direction_name || ''}` : '—'}</strong></div>
      <div><Sun size={16} /><span>日出 / 日落</span><strong>{localClock(latest.sunrise, draft.timezone)} / {localClock(latest.sunset, draft.timezone)}</strong></div>
      <div><span>空气（模型估计）</span><strong>{latest.air_quality?.european_aqi != null ? `欧洲 AQI ${Math.round(latest.air_quality.european_aqi)}` : latest.air_quality?.us_aqi != null ? `美国 AQI ${Math.round(latest.air_quality.us_aqi)}` : '—'}<br />PM2.5 {latest.air_quality?.pm2_5 ?? '—'} · PM10 {latest.air_quality?.pm10 ?? '—'} μg/m³</strong></div>
      <div><Moon size={16} /><span>月升 / 月落</span><strong>{localClock(latest.astronomy?.moonrise, draft.timezone)} / {localClock(latest.astronomy?.moonset, draft.timezone)}</strong></div>
      <div><Moon size={16} /><span>月相</span><strong>{latest.astronomy?.moon_phase || '—'}{latest.astronomy?.moon_illumination != null ? ` · 照明 ${latest.astronomy.moon_illumination}%` : ''}</strong></div>
      <div><Sun size={16} /><span>太阳角度</span><strong>{latest.astronomy?.solar_elevation != null ? `高度 ${latest.astronomy.solar_elevation.toFixed(1)}° · 方位 ${latest.astronomy.solar_azimuth != null ? `${latest.astronomy.solar_azimuth.toFixed(1)}°` : '—'} · ${localClock(latest.astronomy.updated_at, draft.timezone)} 查询` : '—'}</strong></div>
      <div><span>日期</span><strong>{localDate(latest.observed_at, draft.timezone)} · {latest.calendar?.lunar || '—'} {latest.calendar?.festivals || ''}</strong></div>
    </section>}

    {status?.last_error && <div className="partial-error" role="status">天气取数失败 {status.consecutive_failures} 次：{status.last_error}</div>}
    {status?.qweather && <div className="weather-provider-status" aria-label="和风天气状态">{Object.entries(status.qweather).map(([kind, provider]) => <div key={kind}>
      <strong>{kind === 'minutely' ? '临近雨雪' : kind === 'alerts' ? '官方预警' : '日月天文'}</strong><span>{provider.last_success_at ? formatDate(provider.last_success_at) : '等待和风天气查询'}</span>
      {provider.last_error && <span className="form-error">连续失败 {provider.consecutive_failures} 次：{provider.last_error}</span>}
    </div>)}</div>}
    {error && <div className="form-error" role="alert">{error}</div>}
    {message && <div className="weather-feedback" role="status">{message}</div>}

    <form className="weather-settings" onSubmit={(event) => { void save(event) }}>
      <div className="weather-section-heading"><MapPin size={19} /><h3>订阅地点</h3></div>
      <div className="weather-location-tools"><div className="weather-search"><input aria-label="搜索城市或地区" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void search() } }} placeholder="搜索城市或地区" />
        <button className="icon-button" type="button" aria-label="搜索地点" title="搜索地点" disabled={searching || query.trim().length < 2} onClick={() => { void search() }}>{searching ? <RefreshCw className="spin" size={18} /> : <Search size={18} />}</button></div>
        <button className="button subtle" type="button" onClick={usePosition} disabled={busy || !canWrite}><LocateFixed size={17} />浏览器定位</button></div>
      {!!places.length && <div className="weather-place-results" role="listbox" aria-label="地点搜索结果">{places.map((place) => <button disabled={!canWrite} type="button" role="option" aria-selected="false" key={`${place.latitude}-${place.longitude}`} onClick={() => choose(place)}>{place.label}<small>{place.latitude.toFixed(3)}, {place.longitude.toFixed(3)}</small></button>)}</div>}
      <div className="form-grid two"><label><span>地点名称</span><input value={draft.label} maxLength={100} required disabled={!canWrite} onChange={(event) => setDraft({ ...draft, label: event.target.value })} /></label>
        <label><span>时区</span><input value={draft.timezone} required disabled={!canWrite} onChange={(event) => setDraft({ ...draft, timezone: event.target.value })} /></label></div>
      <div className="form-grid two"><label><span>纬度</span><input type="number" step="any" min="-90" max="90" value={draft.latitude} required disabled={!canWrite} onChange={(event) => setDraft({ ...draft, latitude: Number(event.target.value) })} /></label>
        <label><span>经度</span><input type="number" step="any" min="-180" max="180" value={draft.longitude} required disabled={!canWrite} onChange={(event) => setDraft({ ...draft, longitude: Number(event.target.value) })} /></label></div>
      <div className="weather-section-heading"><CloudSun size={19} /><h3>通知</h3></div>
      <div className="form-grid two"><label><span>每天推送时间</span><input type="time" value={draft.daily_time} disabled={!canWrite} required onChange={(event) => setDraft({ ...draft, daily_time: event.target.value })} /></label>
        <div className="weather-toggles"><label className="switch-field"><input type="checkbox" checked={draft.daily_enabled} disabled={!canWrite} onChange={(event) => setDraft({ ...draft, daily_enabled: event.target.checked })} /><span><strong>每日天气</strong></span></label>
          <label className="switch-field"><input type="checkbox" checked={draft.alerts_enabled} disabled={!canWrite} onChange={(event) => setDraft({ ...draft, alerts_enabled: event.target.checked })} /><span><strong>天气变化提醒</strong></span></label></div></div>
      <div className="weather-footer"><span>预报：<a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo</a>（CC BY 4.0），每小时更新。临近雨雪、官方预警转发：<a href="https://developer.qweather.com/attribution.html" target="_blank" rel="noopener noreferrer">QWeather</a>。模型预报不是官方预警；预警可能延迟，请以发布机构为准。</span><button className="button primary" type="submit" disabled={busy || !canWrite}>{busy ? <RefreshCw className="spin" size={17} /> : <CloudSun size={17} />}保存天气订阅</button></div>
    </form>
  </div>
}
