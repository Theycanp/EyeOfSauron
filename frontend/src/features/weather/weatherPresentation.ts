import type { WeatherHour } from '../../shared/types'

export function localClock(timestamp: number | null | undefined, timezone: string): string {
  if (timestamp == null) return '—'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(timestamp * 1000)
}

export function localDate(timestamp: number, timezone: string): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(timestamp * 1000)
}

export function precipitationKind(hour: WeatherHour): 'rain' | 'snow' | 'sleet' | 'none' {
  if (hour.precipitation_type) return hour.precipitation_type
  const code = hour.weather_code ?? 0
  if ([71, 73, 75, 77, 85, 86].includes(code)) return 'snow'
  if ([66, 67].includes(code)) return 'sleet'
  if ((hour.precipitation ?? 0) > 0 || [51, 53, 55, 56, 57, 61, 63, 65, 80, 81, 82, 95, 96, 99].includes(code)) return 'rain'
  return 'none'
}
