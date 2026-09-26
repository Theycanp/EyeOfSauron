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
})
