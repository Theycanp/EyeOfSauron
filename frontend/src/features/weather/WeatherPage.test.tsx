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
    const pending: Array<(value: { timezone: string }) => void> = []
    Object.defineProperty(navigator, 'geolocation', {
      configurable: true,
      value: { getCurrentPosition: (success: PositionCallback) => {
        call += 1
        success({ coords: { latitude: call === 1 ? 35.7 : 40.7, longitude: call === 1 ? 139.7 : -74.0 } } as GeolocationPosition)
      } },
    })
    try {
      const api = {
        weather: vi.fn().mockResolvedValue(status),
        weatherPlaceTimezone: vi.fn().mockImplementation(() => new Promise((resolve) => pending.push(resolve))),
      } as unknown as AdminApi
      const user = userEvent.setup()
      render(<WeatherPage api={api} canWrite onUnauthorized={vi.fn()} />)
      await screen.findByText('雨情等待基线')
      await user.click(screen.getByRole('button', { name: '浏览器定位' }))
      await user.click(screen.getByRole('button', { name: '浏览器定位' }))
      expect((api.weatherPlaceTimezone as ReturnType<typeof vi.fn>).mock.calls).toEqual([[35.7, 139.7], [40.7, -74]])
      pending[1]?.({ timezone: 'America/New_York' })
      await waitFor(() => expect(screen.getByLabelText('时区')).toHaveValue('America/New_York'))
      pending[0]?.({ timezone: 'Asia/Tokyo' })
      await waitFor(() => expect(screen.getByLabelText('时区')).toHaveValue('America/New_York'))
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
        weatherPlaceTimezone: vi.fn().mockRejectedValue(new Error('lookup unavailable')),
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
})
