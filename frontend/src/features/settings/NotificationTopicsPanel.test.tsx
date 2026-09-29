import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { AdminApi, ApiError } from '../../shared/api'
import type { NotificationSettings } from '../../shared/types'
import { NotificationTopicsPanel } from './NotificationTopicsPanel'

function settings(): NotificationSettings {
  return {
    policy: {
      destinations: [
        { id: 'news', topic: 'eos-news', label: '新闻与日报', description: '' },
        { id: 'weather', topic: 'eos-weather', label: '天气', description: '' },
        { id: 'reminders', topic: 'eos-reminders', label: '提醒', description: '' },
        { id: 'system', topic: 'eos-system', label: '系统运行', description: '' },
      ],
      routes: { news: 'news', weather: 'weather', reminders: 'reminders', system: 'system' }, fallback: 'news',
    },
    allowed_topics: ['eos-news', 'eos-weather', 'eos-reminders', 'eos-system'],
    base_url: 'https://ntfy.example.com:10008', revision: 22, topics: [],
  }
}

function setup(canWrite = true) {
  const api = new AdminApi()
  const read = vi.spyOn(api, 'notifications').mockResolvedValue(settings())
  const save = vi.spyOn(api, 'saveNotifications').mockResolvedValue({ revision: 23 })
  const test = vi.spyOn(api, 'testNotificationTopic').mockResolvedValue({ alert_id: 200, queued: true })
  const saved = vi.fn()
  const unauthorized = vi.fn()
  render(<NotificationTopicsPanel api={api} canWrite={canWrite} onUnauthorized={unauthorized} onSaved={saved} />)
  return { api, read, save, test, saved, unauthorized }
}

describe('NotificationTopicsPanel', () => {
  it('saves category muting against the original revision', async () => {
    const user = userEvent.setup()
    const { save, saved } = setup()
    await screen.findByLabelText('天气目的地')
    await user.selectOptions(screen.getByLabelText('天气目的地'), '')
    await user.click(screen.getByRole('button', { name: '保存通知设置' }))
    expect(save.mock.calls[0]?.[0].routes.weather).toBeNull()
    expect(save.mock.calls[0]?.[1]).toBe(22)
    expect(saved).toHaveBeenCalledOnce()
    expect(screen.getByText('通知设置已保存')).toBeInTheDocument()
  })

  it('keeps unsaved settings after a revision conflict', async () => {
    const user = userEvent.setup()
    const { save } = setup()
    save.mockRejectedValue(new ApiError('配置已经改变', 409, 'revision_conflict'))
    await screen.findByLabelText('天气目的地')
    await user.selectOptions(screen.getByLabelText('天气目的地'), 'system')
    await user.click(screen.getByRole('button', { name: '保存通知设置' }))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('天气目的地')).toHaveValue('system')
    expect(screen.getByRole('button', { name: '保存通知设置' })).toBeEnabled()
  })

  it('protects referenced destinations and limits new entries to provisioned topics', async () => {
    const user = userEvent.setup()
    setup()
    await screen.findByLabelText('天气目的地')
    expect(screen.getByRole('button', { name: '删除 eos-weather 目的地' })).toBeDisabled()
    await user.selectOptions(screen.getByLabelText('天气目的地'), 'news')
    await user.click(screen.getByRole('button', { name: '删除 eos-weather 目的地' }))
    expect(screen.getByLabelText('新增目的地主题')).toHaveValue('eos-weather')
    await user.click(screen.getByRole('button', { name: '新增目的地' }))
    expect(screen.getByLabelText('eos-weather名称')).toBeInTheDocument()
    expect(screen.queryByLabelText('新增目的地主题')).not.toBeInTheDocument()
  })

  it('shows safe subscription links and a queued test without claiming delivery', async () => {
    const user = userEvent.setup()
    const { test } = setup()
    await screen.findByLabelText('天气目的地')
    expect(screen.getAllByRole('link', { name: '订阅' })[0]).toHaveAttribute('href', 'https://ntfy.example.com:10008/eos-news')
    await user.click(screen.getByRole('button', { name: '发送 eos-weather 测试' }))
    expect(test).toHaveBeenCalledWith('eos-weather')
    await waitFor(() => expect(screen.getByRole('link', { name: /查看测试消息/ })).toHaveAttribute('href', '/events/200'))
    expect(screen.getByRole('status')).toHaveTextContent('测试已排队')
  })

  it('does not offer edits or sending tests to non-admin users', async () => {
    setup(false)
    await screen.findByLabelText('天气目的地')
    expect(screen.getByLabelText('天气目的地')).toBeDisabled()
    expect(screen.getByRole('button', { name: '发送 eos-news 测试' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '保存通知设置' })).toBeDisabled()
  })
})
