import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import type { WeatherStatus } from '../../shared/types'
import { WeatherPage } from './WeatherPage'

const status: WeatherStatus = {
  subscription: {
    id: 'home', label: '北京邮电大学沙河校区', latitude: 40.1561163,
    longitude: 116.2835626, timezone: 'Asia/Shanghai', daily_time: '07:00',
    daily_enabled: true, alerts_enabled: true, revision: 1,
  },
  latest: null, last_success_at: null, last_daily_date: null, last_error: null,
  consecutive_failures: 0, rain_expected: null,
}

describe('WeatherPage', () => {
  it('labels provider timestamps as query times, not rain forecasts', async () => {
    const api = { weather: vi.fn().mockResolvedValue({ ...status, qweather: {
      minutely: { last_success_at: 1790816160, last_error: null, consecutive_failures: 0 },
    } }) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    const panel = await screen.findByLabelText('和风天气状态')
    expect(panel).toHaveTextContent('临近雨雪')
    expect(panel).toHaveTextContent('上次查询')
  })
  it('uses the saved timezone and window date while timezone edits are unsaved', async () => {
    const start = 1790352000 + 6 * 3600
    const api = { weather: vi.fn().mockResolvedValue({ ...status, latest: {
      condition: '晴', low: 14, high: 20, rain_mm: 0, rain_probability: 0,
      wind_gust_kmh: 12, temperature_now: 20, observed_at: start - 60,
      is_today: true, window_start_at: start, window_end_at: start + 24 * 3600,
      sunrise: start, sunset: start + 12 * 3600,
    } }) } as unknown as AdminApi
    const user = userEvent.setup()
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('06:00 / 18:00')).toBeVisible()
    expect(screen.getByText(/2026-09-26 ·/)).toBeVisible()
    await user.clear(screen.getByLabelText('时区'))
    await user.type(screen.getByLabelText('时区'), 'UTC')
    expect(screen.getByText('06:00 / 18:00')).toBeVisible()
    expect(screen.getByText(/2026-09-26 ·/)).toBeVisible()
  })

  it('labels hourly fallback conditions as forecast and leaves unknown gusts blank', async () => {
    const api = { weather: vi.fn().mockResolvedValue({ ...status, latest: {
      condition: '多云', low: 12, high: 25, rain_mm: 0, rain_probability: 0,
      wind_gust_kmh: null, temperature_now: 20, observed_at: 1790395200,
      is_today: true, conditions_basis: 'hourly_forecast',
    } }) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('近时段小时预报 · QWeather 兜底')).toBeInTheDocument()
    expect(screen.getByText(/阵风 — km\/h/)).toBeInTheDocument()
  })

  it('removes the precipitation panel when the whole forecast is dry', async () => {
    const api = { weather: vi.fn().mockResolvedValue({ ...status, latest: {
      condition: '晴', low: 8, high: 23, rain_mm: 0, rain_probability: 0,
      wind_gust_kmh: 37, temperature_now: 18, observed_at: 1790818200,
      is_today: true, window_start_at: 1790805600, window_end_at: 1790892000,
      window_complete: true,
      forecast_hours: [
        { at: 1790805600, temperature: 11, precipitation: 0, rain_probability: 0, weather_code: 0 },
        { at: 1790848800, temperature: 23, precipitation: 0, rain_probability: 0, weather_code: 0 },
      ],
    } }) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('晴')).toBeInTheDocument()
    expect(screen.getByText(/无降水/)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '今日与明晨雨雪预报' })).not.toBeInTheDocument()
  })
  it('distinguishes the approximate noon angle from the current angle', async () => {
    const api = { weather: vi.fn().mockResolvedValue({ ...status, latest: {
      condition: '晴', low: 12, high: 25, rain_mm: 0, rain_probability: 0,
      wind_gust_kmh: 12, temperature_now: 20, observed_at: 1790395200, is_today: true,
      uv_index_max: 6.4,
      hourly: [{ at: 1790395200, temperature: 20, precipitation: 1.2, rain_probability: 70, precipitation_type: 'rain' },
        { at: 1790398800, temperature: 19, precipitation: 0, rain_probability: 10, precipitation_type: 'none' }],
      astronomy: { date: '20260926', sunrise: null, sunset: null, moonrise: null,
        moonset: null, moon_phase: null, moon_illumination: null,
        solar_elevation: 22, solar_azimuth: 90, solar_noon_elevation: 48.6,
        solar_noon_at: 1790395560, updated_at: 1790395200 },
    } }) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('近似正午太阳高度')).toBeInTheDocument()
    expect(screen.getByText(/48\.6°.*海平面基准/)).toBeInTheDocument()
    expect(screen.getByText('查询时太阳角度')).toBeInTheDocument()
    expect(screen.getByText(/高度 22\.0°/)).toBeInTheDocument()
    expect(screen.getByText('今日最高紫外线指数')).toBeInTheDocument()
    expect(screen.getByText(/6\.4 · 强/)).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '按小时显示降水量、雨雪类型与气温' })).toBeInTheDocument()
    expect(screen.getByText('最高 25°')).toBeInTheDocument()
    expect(screen.getByText('最低 12°')).toBeInTheDocument()
  })

  it('uses the rolling 24-hour range when hourly data crosses below today\'s low', async () => {
    const api = { weather: vi.fn().mockResolvedValue({ ...status, latest: {
      condition: '晴', low: 16, high: 29, rain_mm: 0, rain_probability: 0,
      wind_gust_kmh: 12, temperature_now: 20, observed_at: 1790395200, is_today: true,
      hourly: [
        { at: 1790395200, temperature: 20, precipitation: 0.1, rain_probability: 10, precipitation_type: 'rain' },
        { at: 1790431200, temperature: 14, precipitation: 0, rain_probability: 0, precipitation_type: 'none' },
      ],
    } }) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText(/未来24小时高低温/)).toBeInTheDocument()
    expect(screen.getByText('最低 14°')).toBeInTheDocument()
    expect(screen.getByText('最高 20°')).toBeInTheDocument()
  })

  it('keeps missing past hours blank in a partial six-to-six window', async () => {
    const start = 1790352000 + 6 * 3600
    const api = { weather: vi.fn().mockResolvedValue({ ...status, latest: {
      condition: '晴', low: 14, high: 20, rain_mm: 2, rain_probability: 80,
      wind_gust_kmh: 12, temperature_now: 20, observed_at: start + 7 * 3600,
      is_today: true, window_start_at: start, window_end_at: start + 24 * 3600,
      window_complete: false,
      forecast_hours: [
        { at: start + 7 * 3600, temperature: 20, precipitation: 0, rain_probability: 0 },
        { at: start + 23 * 3600, temperature: 14, precipitation: 2, rain_probability: 80 },
      ],
    } }) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('今天 06:00—明天 06:00')).toBeInTheDocument()
    expect(screen.getByText(/可用时段高低温/)).toBeInTheDocument()
    expect(screen.getByText(/空白时段表示未提供数据/)).toBeInTheDocument()
    const chart = screen.getByRole('img', { name: '按小时显示降水量、雨雪类型与气温' })
    const firstPoint = chart.querySelector('.temperature-point')
    expect(Number(firstPoint?.getAttribute('cx'))).toBeGreaterThan(150)
    expect(chart.querySelectorAll('.temperature-line')).toHaveLength(2)
  })

  it('labels the before-six window as yesterday through today', async () => {
    const start = 1790352000 - 18 * 3600
    const api = { weather: vi.fn().mockResolvedValue({ ...status, latest: {
      condition: '晴', low: 14, high: 20, rain_mm: 0, rain_probability: 0,
      wind_gust_kmh: 12, temperature_now: 20, observed_at: 1790352000 + 3 * 3600,
      is_today: true, window_start_at: start, window_end_at: 1790352000 + 6 * 3600,
      forecast_hours: [{ at: 1790352000 + 3 * 3600, temperature: 20, precipitation: 0.1, rain_probability: 10, precipitation_type: 'rain' }],
    } }) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('昨天 06:00—今天 06:00')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '昨日至今晨雨雪预报' })).toBeInTheDocument()
    expect(screen.getByText(/2026-09-26 ·/)).toBeInTheDocument()
  })

  it('keeps the previous 30-hour label until an existing snapshot is refreshed', async () => {
    const start = 1790352000
    const api = { weather: vi.fn().mockResolvedValue({ ...status, latest: {
      condition: '晴', low: 14, high: 20, rain_mm: 0, rain_probability: 0,
      wind_gust_kmh: 12, temperature_now: 20, observed_at: start + 7 * 3600,
      is_today: true, window_start_at: start, window_end_at: start + 30 * 3600,
      forecast_hours: [{ at: start + 7 * 3600, temperature: 20, precipitation: 0.1, rain_probability: 10, precipitation_type: 'rain' }],
    } }) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('今天 00:00—明天 06:00')).toBeInTheDocument()
  })

  it('selects a searched place and saves a revisioned subscription', async () => {
    const saveWeather = vi.fn().mockResolvedValue({ subscription: { ...status.subscription, revision: 2 } })
    const api = {
      weather: vi.fn().mockResolvedValue(status),
      weatherPlaces: vi.fn().mockResolvedValue({ places: [{ label: '北京 · 北京 · 中国', latitude: 39.9, longitude: 116.4, timezone: 'Asia/Shanghai' }] }),
      saveWeather,
    } as unknown as AdminApi
    const user = userEvent.setup()
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    await screen.findByText('雨情等待基线')
    await user.type(screen.getByLabelText('搜索城市或地区'), '北京')
    await user.click(screen.getByRole('button', { name: '搜索地点' }))
    await user.click(await screen.findByRole('option', { name: /北京 · 北京 · 中国/ }))
    await user.clear(screen.getByLabelText('每天推送时间'))
    await user.type(screen.getByLabelText('每天推送时间'), '08:00')
    await user.click(screen.getByRole('button', { name: '保存天气订阅' }))
    await waitFor(() => expect(saveWeather).toHaveBeenCalledWith(expect.objectContaining({
      label: '北京 · 北京 · 中国', latitude: 39.9, longitude: 116.4,
      daily_time: '08:00', revision: 1,
    })))
    expect(saveWeather.mock.lastCall?.[0]).not.toHaveProperty('id')
  })

  it('keeps settings read-only for viewers', async () => {
    const api = { weather: vi.fn().mockResolvedValue(status) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite={false} onUnauthorized={vi.fn()} />)
    expect(await screen.findByLabelText('地点名称')).toBeDisabled()
    expect(screen.getByRole('button', { name: '保存天气订阅' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '浏览器定位' })).toBeDisabled()
  })

  it('explains a denied browser location request and offers a retry', async () => {
    const original = navigator.geolocation
    Object.defineProperty(navigator, 'geolocation', {
      configurable: true,
      value: { getCurrentPosition: (_success: PositionCallback, failure: PositionErrorCallback) => failure({ code: 1, message: 'denied' } as GeolocationPositionError) },
    })
    try {
      const api = { weather: vi.fn().mockResolvedValue(status) } as unknown as AdminApi
      const user = userEvent.setup()
      render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
      await screen.findByText('雨情等待基线')
      await user.click(screen.getByRole('button', { name: '浏览器定位' }))
      expect(await screen.findByText(/浏览器拒绝了定位/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: '浏览器定位' })).toHaveTextContent('重新请求定位')
    } finally {
      Object.defineProperty(navigator, 'geolocation', { configurable: true, value: original })
    }
  })

  it('starts browser geolocation directly from the click gesture', async () => {
    const original = navigator.geolocation
    const getCurrentPosition = vi.fn()
    Object.defineProperty(navigator, 'geolocation', {
      configurable: true, value: { getCurrentPosition },
    })
    try {
      const api = { weather: vi.fn().mockResolvedValue(status) } as unknown as AdminApi
      const user = userEvent.setup()
      render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
      await screen.findByText('雨情等待基线')
      await user.click(screen.getByRole('button', { name: '浏览器定位' }))
      expect(getCurrentPosition).toHaveBeenCalledOnce()
    } finally {
      Object.defineProperty(navigator, 'geolocation', { configurable: true, value: original })
    }
  })

  it('uses the latest coordinate timezone response and ignores an older response', async () => {
    const original = navigator.geolocation
    let call = 0
    const pending: Array<(value: { label: string; timezone: string }) => void> = []
    Object.defineProperty(navigator, 'geolocation', {
      configurable: true,
      value: { getCurrentPosition: (success: PositionCallback) => {
        call += 1
        success({ coords: { latitude: call === 1 ? 35.7 : 40.7, longitude: call === 1 ? 139.7 : -74.0 } } as GeolocationPosition)
      } },
    })
    try {
      const resolvePlace = vi.fn().mockImplementation(() => new Promise((resolve) => pending.push(resolve)))
      const api = {
        weather: vi.fn().mockResolvedValue(status),
        weatherPlaceResolution: resolvePlace,
      } as unknown as AdminApi
      const user = userEvent.setup()
      render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
      await screen.findByText('雨情等待基线')
      await user.click(screen.getByRole('button', { name: '浏览器定位' }))
      await waitFor(() => expect(resolvePlace).toHaveBeenCalledTimes(1))
      await user.click(screen.getByRole('button', { name: '浏览器定位' }))
      await waitFor(() => expect(resolvePlace).toHaveBeenCalledTimes(2))
      expect(resolvePlace.mock.calls).toEqual([[35.7, 139.7], [40.7, -74]])
      pending[1]?.({ label: '美国 · 纽约', timezone: 'America/New_York' })
      await waitFor(() => expect(screen.getByLabelText('时区')).toHaveValue('America/New_York'))
      pending[0]?.({ label: '日本 · 东京', timezone: 'Asia/Tokyo' })
      await waitFor(() => expect(screen.getByLabelText('时区')).toHaveValue('America/New_York'))
      expect(screen.getByLabelText('地点名称')).toHaveValue('美国 · 纽约')
    } finally {
      Object.defineProperty(navigator, 'geolocation', { configurable: true, value: original })
    }
  })

  it('retains the existing timezone and asks for review when coordinate lookup fails', async () => {
    const original = navigator.geolocation
    Object.defineProperty(navigator, 'geolocation', {
      configurable: true,
      value: { getCurrentPosition: (success: PositionCallback) => success({ coords: { latitude: 35.7, longitude: 139.7 } } as GeolocationPosition) },
    })
    try {
      const api = {
        weather: vi.fn().mockResolvedValue(status),
        weatherPlaceResolution: vi.fn().mockRejectedValue(new Error('lookup unavailable')),
      } as unknown as AdminApi
      const user = userEvent.setup()
      render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
      await screen.findByText('雨情等待基线')
      await user.click(screen.getByRole('button', { name: '浏览器定位' }))
      expect(await screen.findByText(/无法自动确认该坐标的时区/)).toBeInTheDocument()
      expect(screen.getByLabelText('时区')).toHaveValue('Asia/Shanghai')
    } finally {
      Object.defineProperty(navigator, 'geolocation', { configurable: true, value: original })
    }
  })

  it('fills a coordinate name without changing the selected coordinates', async () => {
    const api = {
      weather: vi.fn().mockResolvedValue(status),
      weatherPlaceResolution: vi.fn().mockResolvedValue({ label: '北京市 · 昌平', timezone: 'Asia/Shanghai' }),
    } as unknown as AdminApi
    const user = userEvent.setup()
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    await screen.findByText('雨情等待基线')
    await user.click(screen.getByRole('button', { name: '识别地名' }))
    await waitFor(() => expect(screen.getByLabelText('地点名称')).toHaveValue('北京市 · 昌平'))
    expect(screen.getByLabelText('纬度')).toHaveValue(status.subscription.latitude)
    expect(screen.getByLabelText('经度')).toHaveValue(status.subscription.longitude)
  })

  it('preserves a name and timezone edited while lookup is pending', async () => {
    let resolve: (value: { label: string; timezone: string }) => void = () => undefined
    const resolvePlace = vi.fn().mockImplementation(() => new Promise((done) => { resolve = done }))
    const api = {
      weather: vi.fn().mockResolvedValue(status),
      weatherPlaceResolution: resolvePlace,
    } as unknown as AdminApi
    const user = userEvent.setup()
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    await screen.findByText('雨情等待基线')
    await user.click(screen.getByRole('button', { name: '识别地名' }))
    await waitFor(() => expect(resolvePlace).toHaveBeenCalledTimes(1))
    await user.clear(screen.getByLabelText('地点名称'))
    await user.type(screen.getByLabelText('地点名称'), '沙河校区')
    await user.clear(screen.getByLabelText('时区'))
    await user.type(screen.getByLabelText('时区'), 'UTC')
    resolve({ label: '北京市 · 昌平', timezone: 'Asia/Shanghai' })
    await screen.findByText(/已识别附近行政区域/)
    expect(screen.getByLabelText('地点名称')).toHaveValue('沙河校区')
    expect(screen.getByLabelText('时区')).toHaveValue('UTC')
  })

  it('resolves timezone but asks for a name when no place provider is available', async () => {
    const api = {
      weather: vi.fn().mockResolvedValue(status),
      weatherPlaceResolution: vi.fn().mockResolvedValue({ label: null, timezone: 'Asia/Tokyo' }),
    } as unknown as AdminApi
    const user = userEvent.setup()
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    await screen.findByText('雨情等待基线')
    await user.click(screen.getByRole('button', { name: '识别地名' }))
    await screen.findByText(/暂未识别到地名/)
    expect(screen.getByLabelText('地点名称')).toHaveValue(status.subscription.label)
    expect(screen.getByLabelText('时区')).toHaveValue('Asia/Tokyo')
  })

  it('keeps legacy coordinate labels out of the title and shows coordinates separately', async () => {
    const api = { weather: vi.fn().mockResolvedValue({ ...status, subscription: {
      ...status.subscription, label: '地图坐标 纬度 40.1563，经度 116.2836',
    } }) } as unknown as AdminApi
    render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByRole('heading', { name: '本地天气 · 已选地点' })).toBeVisible()
    expect(screen.getByText('纬度 40.1561')).toBeVisible()
    expect(screen.getByText('经度 116.2836')).toBeVisible()
  })

  it('ignores a delayed browser position after manual coordinate selection', async () => {
    const original = navigator.geolocation
    let complete: PositionCallback = () => undefined
    Object.defineProperty(navigator, 'geolocation', {
      configurable: true, value: { getCurrentPosition: (success: PositionCallback) => { complete = success } },
    })
    try {
      const api = { weather: vi.fn().mockResolvedValue(status) } as unknown as AdminApi
      const user = userEvent.setup()
      render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
      await screen.findByText('雨情等待基线')
      await user.click(screen.getByRole('button', { name: '浏览器定位' }))
      expect(screen.getByRole('button', { name: '保存天气订阅' })).toBeDisabled()
      await user.clear(screen.getByLabelText('纬度'))
      await user.type(screen.getByLabelText('纬度'), '35.7')
      complete({ coords: { latitude: 40.7, longitude: -74 } } as GeolocationPosition)
      expect(screen.getByLabelText('纬度')).toHaveValue(35.7)
      expect(screen.getByRole('button', { name: '保存天气订阅' })).toBeEnabled()
    } finally {
      Object.defineProperty(navigator, 'geolocation', { configurable: true, value: original })
    }
  })
})
