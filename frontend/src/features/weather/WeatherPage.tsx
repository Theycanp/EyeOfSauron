import { useEffect, useRef, useState, type FormEvent } from 'react'
import L from 'leaflet'
import { CloudSun, Droplets, LocateFixed, MapPin, Moon, RefreshCw, Search, Sun, Wind } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import type { WeatherHour, WeatherPlace, WeatherStatus, WeatherSubscription } from '../../shared/types'
import { formatDate } from '../../shared/utils'
import 'leaflet/dist/leaflet.css'
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

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function hourLabel(at: number, timezone: string): string {
  return localClock(at, timezone).replace(/^0/, '')
}

function precipitationKind(hour: WeatherHour): 'rain' | 'snow' | 'sleet' | 'none' {
  if (hour.precipitation_type) return hour.precipitation_type
  const code = hour.weather_code ?? 0
  if ([71, 73, 75, 77, 85, 86].includes(code)) return 'snow'
  if ([66, 67].includes(code)) return 'sleet'
  if ((hour.precipitation ?? 0) > 0 || [51, 53, 55, 56, 57, 61, 63, 65, 80, 81, 82].includes(code)) return 'rain'
  return 'none'
}

function ForecastChart({ hours, timezone, high, low }: { hours: WeatherHour[]; timezone: string; high: number | null; low: number | null }) {
  const visible = hours.filter((hour) => Number.isFinite(hour.at)).slice(0, 24)
  if (!visible.length) return null
  const temps = visible.map((hour) => hour.temperature).filter((value): value is number => value != null && Number.isFinite(value))
  const precipitation = visible.map((hour) => Math.max(0, Number(hour.precipitation) || 0))
  const maxPrecip = Math.max(1, ...precipitation)
  const minTemp = Math.floor(Math.min(...(temps.length ? temps : [0]), low ?? Infinity) - 1)
  const maxTemp = Math.ceil(Math.max(...(temps.length ? temps : [1]), high ?? -Infinity) + 1)
  const width = 760
  const height = 216
  const left = 34
  const right = 18
  const top = 18
  const bottom = 42
  const chartWidth = width - left - right
  const chartHeight = height - top - bottom
  const x = (index: number) => left + (visible.length === 1 ? chartWidth / 2 : (index / (visible.length - 1)) * chartWidth)
  const yTemp = (temp: number) => top + ((maxTemp - temp) / Math.max(1, maxTemp - minTemp)) * chartHeight
  const yPrecip = (amount: number) => top + chartHeight - (amount / maxPrecip) * chartHeight
  const line = temps.length ? visible.map((hour, index) => `${x(index).toFixed(1)},${yTemp(hour.temperature ?? minTemp).toFixed(1)}`).join(' ') : ''
  return <div className="weather-chart" aria-label="未来24小时雨雪与温度趋势">
    <div className="weather-chart-heading"><strong>未来 24 小时趋势</strong><span><i className="legend-dot rain" />雨雪量 <i className="legend-line" />气温 <i className="legend-line range" />今日高低温</span></div>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="按小时显示降水量、雨雪类型与气温">
      <line x1={left} x2={width - right} y1={top + chartHeight} y2={top + chartHeight} className="chart-axis" />
      <line x1={left} x2={width - right} y1={top + chartHeight / 2} y2={top + chartHeight / 2} className="chart-grid" />
      <text x="4" y={top + 5} className="chart-scale">{maxTemp}°</text><text x="4" y={top + chartHeight} className="chart-scale">{minTemp}°</text>
      {high != null && <g><line x1={left} x2={width - right} y1={yTemp(high)} y2={yTemp(high)} className="temperature-reference" /><text x={width - right - 2} y={yTemp(high) - 4} textAnchor="end" className="temperature-reference-label">最高 {Math.round(high)}°</text></g>}
      {low != null && <g><line x1={left} x2={width - right} y1={yTemp(low)} y2={yTemp(low)} className="temperature-reference" /><text x={width - right - 2} y={yTemp(low) - 4} textAnchor="end" className="temperature-reference-label">最低 {Math.round(low)}°</text></g>}
      {visible.map((hour, index) => {
        const amount = precipitation[index]
        const kind = precipitationKind(hour)
        const barWidth = Math.max(4, chartWidth / visible.length * 0.52)
        const barHeight = amount ? Math.max(3, top + chartHeight - yPrecip(amount)) : 2
        return <g key={hour.at}>
          <rect x={x(index) - barWidth / 2} y={top + chartHeight - barHeight} width={barWidth} height={barHeight} rx="2" className={`precip-bar ${kind}`} />
          {kind !== 'none' && <text x={x(index)} y={Math.max(top + 12, top + chartHeight - barHeight - 5)} textAnchor="middle" className="precip-label">{kind === 'snow' ? '雪' : kind === 'sleet' ? '雨夹雪' : '雨'}</text>}
          {(index % (visible.length > 12 ? 3 : 2) === 0 || index === visible.length - 1) && <text x={x(index)} y={height - 9} textAnchor="middle" className="chart-time">{hourLabel(hour.at, timezone)}</text>}
        </g>
      })}
      {line && <polyline points={line} fill="none" className="temperature-line" />}
      {visible.map((hour, index) => hour.temperature != null && <circle key={`t-${hour.at}`} cx={x(index)} cy={yTemp(hour.temperature)} r="2.6" className="temperature-point" />)}
    </svg>
    <div className="weather-chart-note">柱高为每小时预报降水量，标签区分雨、雪和雨夹雪；气温线为小时气温。预报会随模型更新。</div>
  </div>
}

interface WeatherMapProps {
  latitude: number
  longitude: number
  disabled: boolean
  onPick: (latitude: number, longitude: number) => void
}

function WeatherMap({ latitude, longitude, disabled, onPick }: WeatherMapProps) {
  const nodeRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<L.Map | null>(null)
  const markerRef = useRef<L.Marker | null>(null)
  const pickRef = useRef(onPick)
  const disabledRef = useRef(disabled)
  const initialPointRef = useRef({ latitude, longitude })
  const [mapError, setMapError] = useState(false)
  useEffect(() => {
    pickRef.current = onPick
    disabledRef.current = disabled
  }, [onPick, disabled])
  useEffect(() => {
    if (!nodeRef.current || mapRef.current) return
    try {
      const map = L.map(nodeRef.current, { zoomControl: true, attributionControl: true }).setView([initialPointRef.current.latitude, initialPointRef.current.longitude], 11)
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map)
      map.on('click', (event) => { if (!disabledRef.current) pickRef.current(Number(event.latlng.lat.toFixed(6)), Number(event.latlng.lng.toFixed(6))) })
      mapRef.current = map
      markerRef.current = L.marker([initialPointRef.current.latitude, initialPointRef.current.longitude]).addTo(map)
      window.setTimeout(() => map.invalidateSize(), 0)
    } catch {
      window.setTimeout(() => setMapError(true), 0)
    }
    return () => { mapRef.current?.remove(); mapRef.current = null; markerRef.current = null }
  }, [])
  useEffect(() => {
    if (!mapRef.current || !markerRef.current) return
    const point: L.LatLngExpression = [latitude, longitude]
    markerRef.current.setLatLng(point)
    mapRef.current.setView(point)
  }, [latitude, longitude])
  return <div className="weather-map-wrap">
    <div className="weather-map" ref={nodeRef} aria-label="地图选取天气地点" />
    {mapError && <div className="weather-map-fallback">地图暂时不可用，请使用搜索或下方坐标输入。你仍可以保存坐标。</div>}
    <span className="weather-map-caption">点击地图设置地点（地图数据 © OpenStreetMap contributors）</span>
  </div>
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
  const [geoState, setGeoState] = useState<'idle' | 'requesting' | 'denied' | 'unavailable' | 'timeout'>('idle')
  const [refreshing, setRefreshing] = useState(false)
  const [resolvingTimezone, setResolvingTimezone] = useState(false)
  const [timezoneWarning, setTimezoneWarning] = useState('')
  const locationRequest = useRef(0)

  const pickCoordinates = async (latitude: number, longitude: number, label: string) => {
    const request = ++locationRequest.current
    setDraft((current) => current && ({ ...current, label, latitude, longitude }))
    setTimezoneWarning('')
    setResolvingTimezone(true)
    try {
      const result = await api.weatherPlaceTimezone(latitude, longitude)
      if (request !== locationRequest.current) return
      if (!result.timezone) throw new Error('时区解析结果为空')
      setDraft((current) => current && ({ ...current, timezone: result.timezone }))
    } catch {
      if (request === locationRequest.current) setTimezoneWarning('无法自动确认该坐标的时区；已保留原时区，请核对并修改后再保存。')
    } finally {
      if (request === locationRequest.current) setResolvingTimezone(false)
    }
  }

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

  const requestLocation = () => {
    if (!navigator.geolocation) {
      setGeoState('unavailable')
      setError('当前浏览器不支持定位，请搜索地点或填写坐标。')
      return
    }
    setGeoState('requesting')
    setBusy(true)
    const request = ++locationRequest.current
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        if (request === locationRequest.current) {
          const latitude = Number(coords.latitude.toFixed(6))
          const longitude = Number(coords.longitude.toFixed(6))
          void pickCoordinates(latitude, longitude, `当前位置 纬度 ${latitude.toFixed(4)}，经度 ${longitude.toFixed(4)}`)
        }
        setBusy(false)
        setGeoState('idle')
        setError('')
      },
      (cause) => {
        if (request !== locationRequest.current) return
        setBusy(false)
        const state = cause.code === 1 ? 'denied' : cause.code === 3 ? 'timeout' : 'unavailable'
        setGeoState(state)
        setError(cause.code === 1
          ? '浏览器拒绝了定位。请在地址栏的网站权限中允许位置访问，然后再次点击“浏览器定位”。'
          : cause.code === 3 ? '定位请求超时，请检查网络后重试。' : '无法获取当前位置，请搜索地点或在地图上点选。')
      },
      { enableHighAccuracy: false, timeout: 12_000, maximumAge: 10 * 60_000 },
    )
  }

  const refreshAfterSave = async (previousSuccess: number | null, revision: number, locationChanged: boolean) => {
    setRefreshing(true)
    let refreshed = false
    for (let attempt = 0; attempt < 8; attempt += 1) {
      await delay(attempt === 0 ? 500 : 1_500)
      try {
        const value = await api.weather()
        setStatus(value)
        if (value.subscription.revision === revision && value.latest && value.last_success_at
            && (locationChanged || !previousSuccess || value.last_success_at > previousSuccess)) {
          refreshed = true
          setMessage('天气订阅已更新，已载入最新数据。')
          break
        }
      } catch { /* 保留保存成功状态，下一次后台轮询会继续取数 */ }
    }
    if (!refreshed) setMessage('天气订阅已保存；新数据仍在后台采集，请稍后刷新页面查看。')
    setRefreshing(false)
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
      const locationChanged = !status || (
        status.subscription.latitude !== response.subscription.latitude
        || status.subscription.longitude !== response.subscription.longitude
        || status.subscription.timezone !== response.subscription.timezone
      )
      setDraft(response.subscription)
      const previousSuccess = status?.last_success_at ?? null
      setMessage('天气订阅已保存，正在立即获取新地点天气…')
      void refreshAfterSave(previousSuccess, response.subscription.revision, locationChanged)
      setPlaces([])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  const choose = (place: WeatherPlace) => {
    locationRequest.current += 1
    setResolvingTimezone(false)
    setTimezoneWarning('')
    setDraft((current) => current && ({ ...current, ...place }))
    setPlaces([])
    setQuery('')
  }

  if (!draft) return error ? <div className="form-error" role="alert">{error}<button className="button subtle" onClick={() => { void load(true) }}>重试</button></div>
    : <div className="loading-state"><RefreshCw className="spin" size={20} />正在读取天气订阅…</div>

  const latest = status?.latest
  const today = latest?.is_today

  const forecastHours = latest?.hourly ?? latest?.forecast_hours ?? []
  return <div className="weather-page">
    <div className="page-actions"><div><h2>本地天气 · {status?.subscription.label || draft.label}</h2><p>{status?.subscription.timezone || draft.timezone}</p></div>
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
    {latest && today && <section className="weather-precipitation" aria-label="今日雨雪预报">
      <div className="weather-section-heading"><Droplets size={19} /><h3>今日雨雪</h3><span className="weather-section-summary">{latest.rain_probability > 0 ? `降水概率 ${Math.round(latest.rain_probability)}% · ${latest.rain_mm.toFixed(1)} mm` : '暂无明显降水预报'}</span></div>
      {forecastHours.length ? <ForecastChart hours={forecastHours} timezone={draft.timezone} high={latest.high} low={latest.low} /> : <p className="weather-chart-empty">小时级雨雪曲线将在下一次天气查询后显示。</p>}
    </section>}
    {latest && <section className="weather-metrics" aria-label={today ? '今日天气详情' : '历史天气详情'}>
      <div><Droplets size={16} /><span>湿度</span><strong>{latest.humidity != null ? `${Math.round(latest.humidity)}%` : '—'}</strong></div>
      <div><Wind size={16} /><span>风</span><strong>{latest.wind_speed_kmh != null ? `${Math.round(latest.wind_speed_kmh)} km/h ${latest.wind_direction_name || ''}` : '—'}</strong></div>
      <div><Sun size={16} /><span>日出 / 日落</span><strong>{localClock(latest.sunrise, draft.timezone)} / {localClock(latest.sunset, draft.timezone)}</strong></div>
      <div><span>空气（模型估计）</span><strong>{latest.air_quality?.european_aqi != null ? `欧洲 AQI ${Math.round(latest.air_quality.european_aqi)}` : latest.air_quality?.us_aqi != null ? `美国 AQI ${Math.round(latest.air_quality.us_aqi)}` : '—'}<br />PM2.5 {latest.air_quality?.pm2_5 ?? '—'} · PM10 {latest.air_quality?.pm10 ?? '—'} μg/m³</strong></div>
      <div><Moon size={16} /><span>月升 / 月落</span><strong>{localClock(latest.astronomy?.moonrise, draft.timezone)} / {localClock(latest.astronomy?.moonset, draft.timezone)}</strong></div>
      <div><Moon size={16} /><span>月相</span><strong>{latest.astronomy?.moon_phase || '—'}{latest.astronomy?.moon_illumination != null ? ` · 照明 ${latest.astronomy.moon_illumination}%` : ''}</strong></div>
      <div><Sun size={16} /><span>今日最高紫外线指数</span><strong>{latest.uv_index_max != null ? `${latest.uv_index_max.toFixed(1)} · ${latest.uv_index_max >= 11 ? '极强' : latest.uv_index_max >= 8 ? '很强' : latest.uv_index_max >= 6 ? '强' : latest.uv_index_max >= 3 ? '中等' : '较弱'}` : '—'}</strong></div>
      <div><Sun size={16} /><span>近似正午太阳高度</span><strong>{latest.astronomy?.solar_noon_elevation != null ? `${latest.astronomy.solar_noon_elevation.toFixed(1)}° · ${localClock(latest.astronomy.solar_noon_at, draft.timezone)} · 海平面基准` : '—'}</strong></div>
      <div><Sun size={16} /><span>查询时太阳角度</span><strong>{latest.astronomy?.solar_elevation != null ? `高度 ${latest.astronomy.solar_elevation.toFixed(1)}° · 方位 ${latest.astronomy.solar_azimuth != null ? `${latest.astronomy.solar_azimuth.toFixed(1)}°` : '—'} · ${localClock(latest.astronomy.updated_at, draft.timezone)} 查询` : '—'}</strong></div>
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
        <button className="button subtle" type="button" aria-label="浏览器定位" onClick={() => { void requestLocation() }} disabled={busy || !canWrite}><LocateFixed size={17} />{geoState === 'requesting' ? '正在请求定位…' : geoState === 'denied' ? '重新请求定位' : '浏览器定位'}</button></div>
      {geoState === 'denied' && <div className="geo-help" role="status">定位权限被浏览器拒绝。请点击地址栏左侧的权限图标，允许此站点访问位置后重试。</div>}
      {resolvingTimezone && <div className="geo-help" role="status">正在根据坐标确认时区…</div>}
      {timezoneWarning && <div className="geo-help" role="alert">{timezoneWarning}</div>}
      {!!places.length && <div className="weather-place-results" role="listbox" aria-label="地点搜索结果">{places.map((place) => <button disabled={!canWrite} type="button" role="option" aria-selected="false" key={`${place.latitude}-${place.longitude}`} onClick={() => choose(place)}>{place.label}<small>{place.latitude.toFixed(3)}, {place.longitude.toFixed(3)}</small></button>)}</div>}
      <WeatherMap latitude={draft.latitude} longitude={draft.longitude} disabled={!canWrite} onPick={(latitude, longitude) => { void pickCoordinates(latitude, longitude, `地图坐标 纬度 ${latitude.toFixed(4)}，经度 ${longitude.toFixed(4)}`) }} />
      <div className="form-grid two"><label><span>地点名称</span><input value={draft.label} maxLength={100} required disabled={!canWrite} onChange={(event) => setDraft({ ...draft, label: event.target.value })} /></label>
        <label><span>时区</span><input value={draft.timezone} required disabled={!canWrite} onChange={(event) => { setTimezoneWarning(''); setDraft({ ...draft, timezone: event.target.value }) }} /></label></div>
      <div className="form-grid two"><label><span>纬度</span><input type="number" step="any" min="-90" max="90" value={draft.latitude} required disabled={!canWrite} onChange={(event) => { locationRequest.current += 1; setResolvingTimezone(false); setTimezoneWarning('坐标已手动修改，请核对时区。'); setDraft({ ...draft, latitude: Number(event.target.value) }) }} /></label>
        <label><span>经度</span><input type="number" step="any" min="-180" max="180" value={draft.longitude} required disabled={!canWrite} onChange={(event) => { locationRequest.current += 1; setResolvingTimezone(false); setTimezoneWarning('坐标已手动修改，请核对时区。'); setDraft({ ...draft, longitude: Number(event.target.value) }) }} /></label></div>
      <div className="weather-section-heading"><CloudSun size={19} /><h3>通知</h3></div>
      <div className="form-grid two"><label><span>每天推送时间</span><input type="time" value={draft.daily_time} disabled={!canWrite} required onChange={(event) => setDraft({ ...draft, daily_time: event.target.value })} /></label>
        <div className="weather-toggles"><label className="switch-field"><input type="checkbox" checked={draft.daily_enabled} disabled={!canWrite} onChange={(event) => setDraft({ ...draft, daily_enabled: event.target.checked })} /><span><strong>每日天气</strong></span></label>
          <label className="switch-field"><input type="checkbox" checked={draft.alerts_enabled} disabled={!canWrite} onChange={(event) => setDraft({ ...draft, alerts_enabled: event.target.checked })} /><span><strong>天气变化提醒</strong></span></label></div></div>
      <div className="weather-footer"><span>预报：<a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo</a>（CC BY 4.0），每小时更新。临近雨雪、官方预警转发：<a href="https://developer.qweather.com/attribution.html" target="_blank" rel="noopener noreferrer">QWeather</a>。模型预报不是官方预警；预警可能延迟，请以发布机构为准。</span><button className="button primary" type="submit" disabled={busy || resolvingTimezone || !canWrite}>{busy || resolvingTimezone ? <RefreshCw className="spin" size={17} /> : <CloudSun size={17} />}{refreshing ? '正在刷新天气…' : '保存天气订阅'}</button></div>
    </form>
  </div>
}
