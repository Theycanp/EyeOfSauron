import { useRef, useState } from 'react'
import type { WeatherHour } from '../../shared/types'

import { localClock, localDate, precipitationKind } from './weatherPresentation'

const finite = (value: number | null | undefined): value is number => value != null && Number.isFinite(value)
const temperatureText = (value: number) => `${Number(value.toFixed(1))}°`
const phaseLabels = { rain: '雨', snow: '雪', sleet: '雨夹雪', none: '无降水' }

interface Props {
  hours: WeatherHour[]; timezone: string; high: number | null; low: number | null
  start?: number; end?: number; complete?: boolean; observedAt: number
}

/** One shared time axis, separate temperature/precipitation scales; never bridge missing hours. */
export function ForecastChart({ hours, timezone, high, low, start, end, complete, observedAt }: Props) {
  const [selectedAt, setSelectedAt] = useState<number | null>(null)
  const plotRef = useRef<HTMLDivElement>(null)
  const fixedWindow = finite(start) && finite(end) && end > start
  const visible = hours.filter((hour) => finite(hour.at) && (!fixedWindow || (hour.at >= start && hour.at < end)))
    .sort((a, b) => a.at - b.at).slice(0, fixedWindow ? 32 : 24)
  const firstHour = visible[0]
  const lastHour = visible.at(-1)
  if (!firstHour || !lastHour) return <p className="weather-chart-empty">暂无逐小时预报，下一次成功查询后更新。</p>
  const firstAt = fixedWindow ? start : firstHour.at
  const lastAt = fixedWindow ? end : lastHour.at + 3600
  const legacyWindow = fixedWindow && localClock(start, timezone) === '00:00'
  const windowStartsToday = localDate(firstAt, timezone) === localDate(observedAt, timezone)
  const heading = fixedWindow ? legacyWindow ? '今天 00:00—明天 06:00'
    : windowStartsToday ? '今天 06:00—明天 06:00' : '昨天 06:00—今天 06:00' : '未来 24 小时趋势'
  const temperatures = visible.map(hour => hour.temperature).filter(finite)
  const visibleLow = temperatures.length ? Math.min(...temperatures) : null
  const visibleHigh = temperatures.length ? Math.max(...temperatures) : null
  const useDaily = !fixedWindow && finite(high) && finite(low) && finite(visibleLow) && finite(visibleHigh)
    && visibleLow >= low && visibleHigh <= high
  const referenceLow = useDaily ? low : visibleLow
  const referenceHigh = useDaily ? high : visibleHigh
  const scope = fixedWindow ? complete ? '时段' : '可用时段' : useDaily ? '今日' : '未来24小时'
  const minTemp = Math.floor(Math.min(...(temperatures.length ? temperatures : [0]), referenceLow ?? Infinity) - 2)
  const maxTemp = Math.ceil(Math.max(...(temperatures.length ? temperatures : [1]), referenceHigh ?? -Infinity) + 2)
  const maxPrecip = Math.max(1, ...visible.map(hour => finite(hour.precipitation) ? Math.max(0, hour.precipitation) : 0))
  const width = 760, left = 42, right = 45, top = 24, tempHeight = 142, precipTop = 217, precipHeight = 60, height = 317
  const chartWidth = width - left - right
  const x = (at: number) => left + (at - firstAt) / (lastAt - firstAt) * chartWidth
  const yTemp = (value: number) => top + (maxTemp - value) / (maxTemp - minTemp) * tempHeight
  const yPrecip = (value: number) => precipTop + precipHeight - value / maxPrecip * precipHeight
  const segments: string[] = []
  let points: string[] = []
  for (const [index, hour] of visible.entries()) {
    const previous = visible[index - 1]
    if (!finite(hour.temperature) || (previous && hour.at - previous.at !== 3600)) {
      if (points.length) segments.push(points.join(' '))
      points = []
    }
    if (finite(hour.temperature)) points.push(`${x(hour.at).toFixed(1)},${yTemp(hour.temperature).toFixed(1)}`)
  }
  if (points.length) segments.push(points.join(' '))
  const closest = visible.reduce((best, hour) => Math.abs(hour.at - (selectedAt ?? observedAt)) < Math.abs(best.at - (selectedAt ?? observedAt)) ? hour : best, firstHour)
  const selectedIndex = visible.indexOf(closest)
  const hasWet = visible.some(hour => precipitationKind(hour) !== 'none' || (hour.rain_probability ?? 0) > 0)
  const hasRainData = visible.some(hour => finite(hour.precipitation))
  const hasRainAmount = visible.some(hour => (hour.precipitation ?? 0) > 0)
  const ticks = Array.from({ length: Math.floor((lastAt - firstAt) / (3 * 3600)) + 1 }, (_, index) => firstAt + index * 3 * 3600)
  if (ticks.at(-1) !== lastAt) ticks.push(lastAt)
  const label = (at: number) => `${localDate(at, timezone) !== localDate(firstAt, timezone) ? '次日 ' : ''}${localClock(at, timezone)}`
  const pickHour = (event: React.PointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect()
    const at = firstAt + ((event.clientX - bounds.left) / bounds.width * width - left) / chartWidth * (lastAt - firstAt)
    const nearest = visible.reduce((best, hour) => Math.abs(hour.at - at) < Math.abs(best.at - at) ? hour : best, firstHour)
    setSelectedAt(nearest.at)
  }
  return <div className="weather-chart" aria-label="逐小时雨雪与温度趋势">
    <div className="weather-chart-heading"><strong>{heading}</strong><span><i className="legend-line" />气温 <i className="legend-dot rain" />雨雪量 <i className="legend-line range" />{scope}高低温</span></div>
    <div className="weather-chart-plot" ref={plotRef} tabIndex={0} role="region" aria-label="天气时间轴">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="按小时显示降水量、雨雪类型与气温" onPointerDown={pickHour}>
        <text x={left} y="13" className="chart-unit">气温 · °C</text>
        {[0, .5, 1].map(ratio => <g key={ratio}>
          <line x1={left} x2={width - right} y1={top + ratio * tempHeight} y2={top + ratio * tempHeight} className="chart-grid" />
          <text x={left - 8} y={top + ratio * tempHeight + 4} textAnchor="end" className="chart-scale">{Number((maxTemp - ratio * (maxTemp - minTemp)).toFixed(1))}°</text>
        </g>)}
        {referenceHigh != null && <g><line x1={left} x2={width - right} y1={yTemp(referenceHigh)} y2={yTemp(referenceHigh)} className="temperature-reference" /><text x={width - right - 2} y={yTemp(referenceHigh) - 8} textAnchor="end" className="temperature-reference-label">最高 {temperatureText(referenceHigh)}</text></g>}
        {referenceLow != null && <g><line x1={left} x2={width - right} y1={yTemp(referenceLow)} y2={yTemp(referenceLow)} className="temperature-reference" /><text x={width - right - 2} y={yTemp(referenceLow) + 15} textAnchor="end" className="temperature-reference-label">最低 {temperatureText(referenceLow)}</text></g>}
        {segments.map((segment, index) => <polyline key={index} points={segment} fill="none" className="temperature-line" />)}
        {visible.map(hour => finite(hour.temperature) && <circle key={`t-${hour.at}`} cx={x(hour.at)} cy={yTemp(hour.temperature)} r={hour.at === closest.at ? 4 : 2.6} className="temperature-point" />)}
        {!temperatures.length && <text x={width / 2} y={top + tempHeight / 2} textAnchor="middle" className="chart-time">温度数据待获取</text>}
        <text x={left} y={precipTop - 19} className="chart-unit">雨雪 · mm/h</text>
        <line x1={left} x2={width - right} y1={precipTop + precipHeight} y2={precipTop + precipHeight} className="chart-axis" />
        <text x={left - 8} y={precipTop + 4} textAnchor="end" className="chart-scale">{Number(maxPrecip.toFixed(1))}</text>
        <text x={left - 8} y={precipTop + precipHeight + 4} textAnchor="end" className="chart-scale">0</text>
        {visible.map(hour => {
          if (!finite(hour.precipitation)) return null
          const amount = Math.max(0, hour.precipitation), kind = precipitationKind(hour)
          const barHeight = amount > 0 ? Math.max(2, precipTop + precipHeight - yPrecip(amount)) : 0
          return <g key={hour.at}>
            {barHeight > 0 && <rect x={x(hour.at)} y={precipTop + precipHeight - barHeight} width={Math.min(x(hour.at + 3600) - x(hour.at), width - right - x(hour.at)) * .7} height={barHeight} rx="2" className={`precip-bar ${kind}${hour.at < observedAt ? ' past' : ''}`} />}
            {kind !== 'none' && (amount > 0 || (hour.rain_probability ?? 0) > 0) && <text x={x(hour.at) + 5} y={precipTop + precipHeight - barHeight - 5} textAnchor="middle" className="precip-label">{phaseLabels[kind]}</text>}
          </g>
        })}
        {!hasRainAmount && <text x={width / 2} y={precipTop + precipHeight / 2} textAnchor="middle" className="chart-dry-label">{!hasRainData ? '降水数据待获取' : hasWet ? '暂无明显降水量预报，留意逐小时概率' : '可用小时暂无降水预报'}</text>}
        <line x1={x(closest.at)} x2={x(closest.at)} y1={top} y2={precipTop + precipHeight} className="chart-selection" />
        {ticks.map(at => <text key={at} x={x(at)} y={height - 12} textAnchor="middle" className="chart-time">{label(at)}</text>)}
      </svg>
    </div>
    <div className="weather-hour-detail" aria-live="polite" aria-label="选中小时天气">
      <strong>{label(closest.at)}</strong>
      <span>{finite(closest.temperature) ? `${temperatureText(closest.temperature)}C` : '气温待获取'}</span>
      <span>{finite(closest.precipitation) ? closest.precipitation > 0 ? `${phaseLabels[precipitationKind(closest)]} ${closest.precipitation} mm/h` : '无降水量预报' : '降水量待获取'}</span>
      <span>{finite(closest.rain_probability) ? `降水概率 ${closest.rain_probability}%` : '概率待获取'}</span>
    </div>
    <label className="weather-hour-picker">逐小时查看
      <input aria-label="选择预报小时" type="range" min={0} max={visible.length - 1} value={selectedIndex} aria-valuetext={label(closest.at)} onChange={event => {
        const hour = visible[Number(event.target.value)]
        if (!hour) return
        setSelectedAt(hour.at)
        const plot = plotRef.current
        if (plot) plot.scrollLeft = x(hour.at) / width * plot.scrollWidth - plot.clientWidth / 2
      }} />
    </label>
    <p className="weather-chart-note">{fixedWindow ? '已过去小时的模型数据不是实测；空白时段表示未提供数据。' : ''}上下两图共享时间轴，分别使用气温和降水量刻度。点击图表或拖动滑块查看，手机可左右滑动时间轴。</p>
  </div>
}
