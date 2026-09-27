import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import { WeatherProvidersPanel } from './WeatherProvidersPanel'

const policy = { provider: 'qweather', kind: 'alerts', enabled: true, interval_seconds: 600, daily_budget: 288, requests: 12, updated_at: 100, updated_by: 'owner' }

describe('WeatherProvidersPanel', () => {
  it('shows usage and persists bounded channel settings', async () => {
    const update = vi.fn().mockResolvedValue({ policy })
    const api = { weatherProviderPolicies: vi.fn().mockResolvedValue({ policies: [policy] }), updateWeatherProviderPolicy: update } as unknown as AdminApi
    const user = userEvent.setup()
    render(<WeatherProvidersPanel api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('今日已用 12 / 288')).toBeInTheDocument()
    await user.clear(screen.getByLabelText('QWeather · 官方预警每日预算'))
    await user.type(screen.getByLabelText('QWeather · 官方预警每日预算'), '120')
    await user.click(screen.getByRole('button', { name: '保存QWeather · 官方预警' }))
    await waitFor(() => expect(update).toHaveBeenCalledWith('qweather', 'alerts', { enabled: true, interval_seconds: 600, daily_budget: 120 }))
    expect(await screen.findByText('官方预警设置已保存')).toBeInTheDocument()
  })
  it('keeps policy editing disabled for viewers', async () => {
    const api = { weatherProviderPolicies: vi.fn().mockResolvedValue({ policies: [policy] }) } as unknown as AdminApi
    render(<WeatherProvidersPanel api={api} canWrite={false} onUnauthorized={vi.fn()} />)
    expect(await screen.findByLabelText('QWeather · 官方预警每日预算')).toBeDisabled()
    expect(screen.getByRole('button', { name: '保存QWeather · 官方预警' })).toBeDisabled()
  })
  it('shows a UTC budget day and distinguishes unconfigured and standby channels', async () => {
    const api = { weatherProviderPolicies: vi.fn().mockResolvedValue({ policies: [
      { ...policy, status: 'not_configured', budget_day: '2026-09-27' },
      { ...policy, kind: 'hourly', status: 'conditional_standby' },
    ] }) } as unknown as AdminApi
    render(<WeatherProvidersPanel api={api} canWrite={false} onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('预算日期 2026-09-27（UTC）')).toBeInTheDocument()
    expect(screen.getByText('尚无请求 · 未配置')).toBeInTheDocument()
    expect(screen.getByText('尚无请求 · 条件性待命')).toBeInTheDocument()
  })
})
