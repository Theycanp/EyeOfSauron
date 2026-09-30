import { useEffect, useRef, useState, type FormEvent } from 'react'
import L from 'leaflet'
import markerIconUrl from 'leaflet/dist/images/marker-icon.png'
import markerShadowUrl from 'leaflet/dist/images/marker-shadow.png'
import { CloudSun, Droplets, LocateFixed, MapPin, Moon, RefreshCw, Search, Sun, Wind } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import type { WeatherHour, WeatherPlace, WeatherStatus, WeatherSubscription } from '../../shared/types'
import { formatDate } from '../../shared/utils'
import 'leaflet/dist/leaflet.css'
import './weather.css'
import { WeatherProvidersPanel } from './WeatherProvidersPanel'

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

function locationTitle(label: string): string {
  return /^(地图坐标|当前位置) 纬度 /.test(label) ? '已选地点' : label
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

function ForecastChart({ hours, timezone, high, low, start, end, complete, observedAt }: {
  hours: WeatherHour[]; timezone: string; high: number | null; low: number | null
  start?: number; end?: number; complete?: boolean; observedAt: number
}) {
  const fixedWindow = start != null && end != null && Number.isFinite(start) && Number.isFinite(end) && end > start
  const windowStart = start ?? 0
  const windowEnd = end ?? 0
  const legacyWindow = fixedWindow && localClock(windowStart, timezone) === '00:00'
  const visible = hours.filter((hour) => Number.isFinite(hour.at) && (!fixedWindow || (hour.at >= windowStart && hour.at < windowEnd)))
    .slice(0, fixedWindow ? legacyWindow ? 32 : 26 : 24)
  const firstHour = visible[0]
  const lastHour = visible[visible.length - 1]
  if (!firstHour || !lastHour) return null
  const firstAt = fixedWindow ? windowStart : firstHour.at
  const lastAt = fixedWindow ? windowEnd : lastHour.at
  const temps = visible.map((hour) => hour.temperature).filter((value): value is number => value != null && Number.isFinite(value))
  const visibleLow = temps.length ? Math.min(...temps) : null
  const visibleHigh = temps.length ? Math.max(...temps) : null
  const useDailyReferences = !fixedWindow && high != null && low != null && visibleLow != null && visibleHigh != null
    && visibleLow >= low && visibleHigh <= high
  const referenceLow = fixedWindow ? visibleLow : useDailyReferences ? low : visibleLow ?? low
  const referenceHigh = fixedWindow ? visibleHigh : useDailyReferences ? high : visibleHigh ?? high
  const referenceScope = fixedWindow ? (complete ? '时段' : '可用时段') : useDailyReferences ? '今日' : '未来24小时'
  const precipitation = visible.map((hour) => Math.max(0, Number(hour.precipitation) || 0))
  const maxPrecip = Math.max(1, ...precipitation)
  const minTemp = Math.floor(Math.min(...(temps.length ? temps : [0]), referenceLow ?? Infinity) - 1)
  const maxTemp = Math.ceil(Math.max(...(temps.length ? temps : [1]), referenceHigh ?? -Infinity) + 1)
  const width = 760
  const height = 216
  const left = 34
  const right = 18
  const top = 18
  const bottom = 42
  const chartWidth = width - left - right
  const chartHeight = height - top - bottom
  const x = (at: number) => left + (lastAt <= firstAt ? chartWidth / 2 : ((at - firstAt) / (lastAt - firstAt)) * chartWidth)
  const yTemp = (temp: number) => top + ((maxTemp - temp) / Math.max(1, maxTemp - minTemp)) * chartHeight
  const yPrecip = (amount: number) => top + chartHeight - (amount / maxPrecip) * chartHeight
  const lineSegments: string[] = []
  let segment: string[] = []
  for (const [index, hour] of visible.entries()) {
    if (hour.temperature == null || !Number.isFinite(hour.temperature)) {
      if (segment.length) lineSegments.push(segment.join(' '))
      segment = []
      continue
    }
    const previousHour = visible[index - 1]
    if (previousHour && hour.at - previousHour.at !== 3600 && segment.length) {
      lineSegments.push(segment.join(' '))
      segment = []
    }
    segment.push(`${x(hour.at).toFixed(1)},${yTemp(hour.temperature).toFixed(1)}`)
  }
  if (segment.length) lineSegments.push(segment.join(' '))
  const nowX = fixedWindow && firstAt <= observedAt && observedAt < windowEnd ? x(observedAt) : null
  const windowStartsToday = fixedWindow && localDate(windowStart, timezone) === localDate(observedAt, timezone)
  const heading = fixedWindow ? legacyWindow ? '今天 00:00—明天 06:00'
    : windowStartsToday ? '今天 06:00—明天 06:00' : '昨天 06:00—今天 06:00' : '未来 24 小时趋势'
  return <div className="weather-chart" aria-label="逐小时雨雪与温度趋势">
    <div className="weather-chart-heading"><strong>{heading}</strong><span><i className="legend-dot rain" />雨雪量 <i className="legend-line" />气温 <i className="legend-line range" />{referenceScope}高低温</span></div>
    <div className="weather-chart-plot" tabIndex={0} role="region" aria-label="天气时间轴"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="按小时显示降水量、雨雪类型与气温">
      <line x1={left} x2={width - right} y1={top + chartHeight} y2={top + chartHeight} className="chart-axis" />
      <line x1={left} x2={width - right} y1={top + chartHeight / 2} y2={top + chartHeight / 2} className="chart-grid" />
      <text x="4" y={top + 5} className="chart-scale">{maxTemp}°</text><text x="4" y={top + chartHeight} className="chart-scale">{minTemp}°</text>
      {nowX != null && <g><line x1={nowX} x2={nowX} y1={top} y2={top + chartHeight} className="chart-now" /><text x={Math.min(nowX + 4, width - right - 20)} y={top + 12} className="chart-now-label">查询</text></g>}
      {referenceHigh != null && <g><line x1={left} x2={width - right} y1={yTemp(referenceHigh)} y2={yTemp(referenceHigh)} className="temperature-reference" /><text x={width - right - 2} y={yTemp(referenceHigh) - 8} textAnchor="end" className="temperature-reference-label">最高 {Math.round(referenceHigh)}°</text></g>}
      {referenceLow != null && <g><line x1={left} x2={width - right} y1={yTemp(referenceLow)} y2={yTemp(referenceLow)} className="temperature-reference" /><text x={width - right - 2} y={yTemp(referenceLow) - 8} textAnchor="end" className="temperature-reference-label">最低 {Math.round(referenceLow)}°</text></g>}
      {visible.map((hour, index) => {
        const amount = precipitation[index]
        const kind = precipitationKind(hour)
        const barWidth = Math.max(4, chartWidth / visible.length * 0.52)
        const barHeight = amount ? Math.max(3, top + chartHeight - yPrecip(amount)) : 2
        return <g key={hour.at}>
          <rect x={x(hour.at) - barWidth / 2} y={top + chartHeight - barHeight} width={barWidth} height={barHeight} rx="2" className={`precip-bar ${kind}${hour.at < observedAt ? ' past' : ''}`} />
          {kind !== 'none' && <text x={x(hour.at)} y={Math.max(top + 12, top + chartHeight - barHeight - 5)} textAnchor="middle" className="precip-label">{kind === 'snow' ? '雪' : kind === 'sleet' ? '雨夹雪' : '雨'}</text>}
          {!fixedWindow && (index % (visible.length > 12 ? 3 : 2) === 0 || index === visible.length - 1) && <text x={x(hour.at)} y={height - 9} textAnchor="middle" className="chart-time">{hourLabel(hour.at, timezone)}</text>}
        </g>
      })}
      {fixedWindow && Array.from({ length: 6 }, (_, index) => firstAt + (lastAt - firstAt) * index / 5).map((at) => <text key={at} x={x(at)} y={height - 9} textAnchor="middle" className="chart-time">{localDate(at, timezone) !== localDate(firstAt, timezone) ? '明 ' : ''}{hourLabel(at, timezone)}</text>)}
      {lineSegments.map((points, index) => <polyline key={index} points={points} fill="none" className="temperature-line" />)}
      {visible.map((hour) => hour.temperature != null && <circle key={`t-${hour.at}`} cx={x(hour.at)} cy={yTemp(hour.temperature)} r="2.6" className="temperature-point" />)}
    </svg></div>
    <div className="weather-chart-note">{fixedWindow ? '已过去小时的模型数据不是实测；空白时段表示未提供数据。' : ''}柱高为每小时预报降水量，气温线为小时气温。</div>
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
      const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
      }).addTo(map)
      let tileLoaded = false
      tiles.on('tileload', () => { tileLoaded = true; setMapError(false) })
      tiles.on('tileerror', () => { if (!tileLoaded) setMapError(true) })
      map.on('click', (event) => { if (!disabledRef.current) pickRef.current(Number(event.latlng.lat.toFixed(6)), Number(event.latlng.lng.toFixed(6))) })
      mapRef.current = map
      markerRef.current = L.marker([initialPointRef.current.latitude, initialPointRef.current.longitude], {
        icon: L.icon({ iconUrl: markerIconUrl, shadowUrl: markerShadowUrl, iconSize: [25, 41],
          iconAnchor: [12, 41], popupAnchor: [1, -34], shadowSize: [41, 41] }),
      }).addTo(map)
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
  const [showProviders, setShowProviders] = useState(false)
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
  const [locationNameNotice, setLocationNameNotice] = useState('')
  const locationRequest = useRef(0)
  const timezoneEdit = useRef(0)

  const pickCoordinates = async (latitude: number, longitude: number, label: string) => {
    const request = ++locationRequest.current
    const timezoneVersion = timezoneEdit.current
    setGeoState('idle')
    setDraft((current) => current && ({ ...current, label, latitude, longitude }))
    setTimezoneWarning('')
    setLocationNameNotice('')
    setResolvingTimezone(true)
    try {
      await delay(350)
      if (request !== locationRequest.current) return
      const result = await api.weatherPlaceResolution(latitude, longitude)
      if (request !== locationRequest.current) return
      if (!result.timezone) throw new Error('时区解析结果为空')
      setDraft((current) => current && ({ ...current,
        timezone: timezoneVersion === timezoneEdit.current ? result.timezone : current.timezone,
        label: current.label === label && result.label ? result.label : current.label,
      }))
      setLocationNameNotice(result.label
        ? '已识别附近行政区域，可将名称改为学校、住宅或其他便于辨认的名称。'
        : '暂未识别到地名，请手动填写地点名称。')
    } catch {
      if (request === locationRequest.current) setTimezoneWarning('无法自动确认该坐标的时区；已保留原时区，请核对并修改后再保存。')
      if (request === locationRequest.current) setLocationNameNotice('地点识别暂不可用，请手动填写地点名称。')
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
    return () => { active = false; locationRequest.current += 1 }
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
    const request = ++locationRequest.current
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        if (request !== locationRequest.current) return
        const latitude = Number(coords.latitude.toFixed(6))
        const longitude = Number(coords.longitude.toFixed(6))
        void pickCoordinates(latitude, longitude, '当前位置')
        setGeoState('idle')
        setError('')
      },
      (cause) => {
        if (request !== locationRequest.current) return
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
    setLocationNameNotice('')
    setGeoState('idle')
    setDraft((current) => current && ({ ...current, ...place }))
    setPlaces([])
    setQuery('')
  }

  const editCoordinate = (field: 'latitude' | 'longitude', value: number) => {
    locationRequest.current += 1
    setGeoState('idle')
    setResolvingTimezone(false)
    setLocationNameNotice('')
    setTimezoneWarning('坐标已手动修改，请识别地名并核对时区。')
    setDraft((current) => current && ({ ...current, label: '地图选点', [field]: value }))
  }

  if (!draft) return error ? <div className="form-error" role="alert">{error}<button className="button subtle" onClick={() => { void load(true) }}>重试</button></div>
    : <div className="loading-state"><RefreshCw className="spin" size={20} />正在读取天气订阅…</div>

  const latest = status?.latest
  const today = latest?.is_today
  const snapshotTimezone = status?.subscription.timezone || draft.timezone
  const windowStartsToday = latest?.window_start_at == null ||
    localDate(latest.window_start_at, snapshotTimezone) === localDate(latest.observed_at, snapshotTimezone)

  const forecastHours = latest?.hourly ?? latest?.forecast_hours ?? []
  const selectingLocation = resolvingTimezone || geoState === 'requesting'
  return <div className="weather-page">
    <div className="page-actions"><button className="button subtle" aria-expanded={showProviders} onClick={() => setShowProviders(value => !value)}>天气来源与预算</button></div>
    {showProviders && <WeatherProvidersPanel api={api} canWrite={canWrite} onUnauthorized={onUnauthorized} />}
    <div className="page-actions"><div><h2>本地天气 · {locationTitle(status?.subscription.label || draft.label)}</h2><p className="weather-location-detail">
      <span>纬度 {(status?.subscription.latitude ?? draft.latitude).toFixed(4)}</span>
      <span>经度 {(status?.subscription.longitude ?? draft.longitude).toFixed(4)}</span>
      <span>{status?.subscription.timezone || draft.timezone}</span>
    </p></div>
      <button className="icon-button" aria-label="刷新天气状态" title="刷新天气状态" onClick={() => { void load() }}><RefreshCw size={18} /></button></div>

    <section className="weather-current" aria-label="最新天气预报">
      <div className="weather-current-icon"><CloudSun size={34} /></div>
      <div><span className="eyebrow">{status?.last_success_at ? `${today ? '今日预报' : '历史预报'} · 上次查询 ${formatDate(status.last_success_at)}` : '等待首次查询'}</span>
        {latest?.conditions_basis === 'hourly_forecast' && <p>近时段小时预报 · QWeather 兜底</p>}
        <h3>{status?.latest?.condition || '暂无预报'}</h3>
        <p>{latest?.low != null && latest.high != null ? `${Math.round(latest.low)}~${Math.round(latest.high)}℃` : '温度待获取'}
          <span> · </span>{latest?.window_start_at ? '至时段结束06时预计' : today ? '今日' : '当日'}降水 {latest?.rain_mm ?? '—'} mm<span> · </span>阵风 {latest?.wind_gust_kmh ?? '—'} km/h</p>
      </div>
      <div className="weather-current-side"><Wind size={17} />{status?.rain_expected === true ? '今日内预计有雨' : status?.rain_expected === false ? '今日内暂无明显降雨信号' : '雨情等待基线'}</div>
    </section>
    {latest && today && <section className="weather-precipitation" aria-label={windowStartsToday ? '今日与明晨雨雪预报' : '昨日至今晨雨雪预报'}>
      <div className="weather-section-heading"><Droplets size={19} /><h3>{windowStartsToday ? '今日与明晨雨雪' : '昨日至今晨雨雪'}</h3><span className="weather-section-summary">{latest.rain_probability > 0 ? `至${windowStartsToday ? '明晨' : '今晨'}06时 · 降水概率 ${Math.round(latest.rain_probability)}% · ${latest.rain_mm.toFixed(1)} mm` : `至${windowStartsToday ? '明晨' : '今晨'}06时暂无明显降水预报`}</span></div>
      {forecastHours.length ? <ForecastChart hours={forecastHours} timezone={snapshotTimezone} high={latest.high} low={latest.low} start={latest.window_start_at} end={latest.window_end_at} complete={latest.window_complete} observedAt={status?.last_success_at ?? latest.observed_at} /> : <p className="weather-chart-empty">小时级雨雪曲线将在下一次天气查询后显示。</p>}
    </section>}
    {latest && <section className="weather-metrics" aria-label={today ? '今日天气详情' : '历史天气详情'}>
      <div><Droplets size={16} /><span>湿度</span><strong>{latest.humidity != null ? `${Math.round(latest.humidity)}%` : '—'}</strong></div>
      <div><Wind size={16} /><span>风</span><strong>{latest.wind_speed_kmh != null ? `${Math.round(latest.wind_speed_kmh)} km/h ${latest.wind_direction_name || ''}` : '—'}</strong></div>
      <div><Sun size={16} /><span>日出 / 日落</span><strong>{localClock(latest.sunrise, snapshotTimezone)} / {localClock(latest.sunset, snapshotTimezone)}</strong></div>
      <div><span>空气（模型估计）</span><strong>{latest.air_quality?.european_aqi != null ? `欧洲 AQI ${Math.round(latest.air_quality.european_aqi)}` : latest.air_quality?.us_aqi != null ? `美国 AQI ${Math.round(latest.air_quality.us_aqi)}` : '—'}<br />PM2.5 {latest.air_quality?.pm2_5 ?? '—'} · PM10 {latest.air_quality?.pm10 ?? '—'} μg/m³</strong></div>
      <div><Moon size={16} /><span>月升 / 月落</span><strong>{localClock(latest.astronomy?.moonrise, snapshotTimezone)} / {localClock(latest.astronomy?.moonset, snapshotTimezone)}</strong></div>
      <div><Moon size={16} /><span>月相</span><strong>{latest.astronomy?.moon_phase || '—'}{latest.astronomy?.moon_illumination != null ? ` · 照明 ${latest.astronomy.moon_illumination}%` : ''}</strong></div>
      <div><Sun size={16} /><span>今日最高紫外线指数</span><strong>{latest.uv_index_max != null ? `${latest.uv_index_max.toFixed(1)} · ${latest.uv_index_max >= 11 ? '极强' : latest.uv_index_max >= 8 ? '很强' : latest.uv_index_max >= 6 ? '强' : latest.uv_index_max >= 3 ? '中等' : '较弱'}` : '—'}</strong></div>
      <div><Sun size={16} /><span>近似正午太阳高度</span><strong>{latest.astronomy?.solar_noon_elevation != null ? `${latest.astronomy.solar_noon_elevation.toFixed(1)}° · ${localClock(latest.astronomy.solar_noon_at, snapshotTimezone)} · 海平面基准` : '—'}</strong></div>
      <div><Sun size={16} /><span>查询时太阳角度</span><strong>{latest.astronomy?.solar_elevation != null ? `高度 ${latest.astronomy.solar_elevation.toFixed(1)}° · 方位 ${latest.astronomy.solar_azimuth != null ? `${latest.astronomy.solar_azimuth.toFixed(1)}°` : '—'} · ${localClock(latest.astronomy.updated_at, snapshotTimezone)} 查询` : '—'}</strong></div>
      <div><span>日期</span><strong>{localDate(latest.observed_at, snapshotTimezone)} · {latest.calendar?.lunar || '—'} {latest.calendar?.festivals || ''}</strong></div>
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
        <button className="button subtle" type="button" aria-label="浏览器定位" onClick={() => { void requestLocation() }} disabled={busy || geoState === 'requesting' || !canWrite}><LocateFixed size={17} />{geoState === 'requesting' ? '正在请求定位…' : geoState === 'denied' ? '重新请求定位' : '浏览器定位'}</button></div>
      {geoState === 'denied' && <div className="geo-help" role="status">定位权限被浏览器拒绝。请点击地址栏左侧的权限图标，允许此站点访问位置后重试。</div>}
      {resolvingTimezone && <div className="geo-help" role="status">正在识别地点名称与时区…</div>}
      {timezoneWarning && <div className="geo-help" role="alert">{timezoneWarning}</div>}
      {locationNameNotice && <div className="geo-help" role="status">{locationNameNotice}</div>}
      {!!places.length && <div className="weather-place-results" role="listbox" aria-label="地点搜索结果">{places.map((place) => <button disabled={!canWrite} type="button" role="option" aria-selected="false" key={`${place.latitude}-${place.longitude}`} onClick={() => choose(place)}>{place.label}<small>{place.latitude.toFixed(3)}, {place.longitude.toFixed(3)}</small></button>)}</div>}
      <WeatherMap latitude={draft.latitude} longitude={draft.longitude} disabled={busy || !canWrite} onPick={(latitude, longitude) => { void pickCoordinates(latitude, longitude, '地图选点') }} />
      <div className="form-grid two"><div className="weather-name-field"><label htmlFor="weather-location-name">地点名称</label><div className="weather-name-input"><input id="weather-location-name" value={draft.label} maxLength={100} required disabled={!canWrite} onChange={(event) => setDraft({ ...draft, label: event.target.value })} />
        <button className="icon-button" type="button" aria-label="识别地名" title="根据坐标识别地名" disabled={busy || resolvingTimezone || !canWrite} onClick={() => { void pickCoordinates(draft.latitude, draft.longitude, draft.label) }}><MapPin size={17} /></button></div></div>
        <label><span>时区</span><input value={draft.timezone} required disabled={!canWrite} onChange={(event) => { timezoneEdit.current += 1; setTimezoneWarning(''); setDraft({ ...draft, timezone: event.target.value }) }} /></label></div>
      <div className="form-grid two"><label><span>纬度</span><input type="number" step="any" min="-90" max="90" value={draft.latitude} required disabled={!canWrite} onChange={(event) => editCoordinate('latitude', Number(event.target.value))} /></label>
        <label><span>经度</span><input type="number" step="any" min="-180" max="180" value={draft.longitude} required disabled={!canWrite} onChange={(event) => editCoordinate('longitude', Number(event.target.value))} /></label></div>
      <div className="weather-section-heading"><CloudSun size={19} /><h3>通知</h3></div>
      <div className="form-grid two"><label><span>每天推送时间</span><input type="time" value={draft.daily_time} disabled={!canWrite} required onChange={(event) => setDraft({ ...draft, daily_time: event.target.value })} /></label>
        <div className="weather-toggles"><label className="switch-field"><input type="checkbox" checked={draft.daily_enabled} disabled={!canWrite} onChange={(event) => setDraft({ ...draft, daily_enabled: event.target.checked })} /><span><strong>每日天气</strong></span></label>
          <label className="switch-field"><input type="checkbox" checked={draft.alerts_enabled} disabled={!canWrite} onChange={(event) => setDraft({ ...draft, alerts_enabled: event.target.checked })} /><span><strong>天气变化提醒</strong></span></label></div></div>
      <div className="weather-footer"><span>预报：<a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo</a>（CC BY 4.0），每小时更新。地名、临近雨雪、官方预警转发：<a href="https://developer.qweather.com/attribution.html" target="_blank" rel="noopener noreferrer">QWeather</a>。模型预报不是官方预警；预警可能延迟，请以发布机构为准。</span><button className="button primary" type="submit" disabled={busy || selectingLocation || !canWrite}>{busy || selectingLocation ? <RefreshCw className="spin" size={17} /> : <CloudSun size={17} />}{refreshing ? '正在刷新天气…' : '保存天气订阅'}</button></div>
    </form>
  </div>
}
