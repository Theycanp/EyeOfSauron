import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import type { NewsEvent, NewsEventResponse } from '../../shared/types'
import { DailyEventsPage } from './DailyEventsPage'

function newsEvent(eventKey: string, title: string, overrides: Partial<NewsEvent> = {}): NewsEvent {
  return {
    event_key: eventKey,
    title,
    summary: `${title}摘要`,
    score: 4.2,
    importance: 4,
    urgency: 3,
    relevance: 5,
    confidence: 0.9,
    first_seen_at: 1_788_360_000,
    last_seen_at: 1_788_363_000,
    regions: ['CN'],
    topics: ['policy'],
    status: 'active',
    independent_source_count: 2,
    reports: [],
    report_count: 0,
    reports_truncated: false,
    ...overrides,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}

describe('DailyEventsPage', () => {
  it('loads the default 28-hour newest window and applies changed filters', async () => {
    const newsEvents = vi.fn().mockResolvedValue({ events: [] })
    const api = { newsEvents } as unknown as AdminApi
    const user = userEvent.setup()
    render(<DailyEventsPage api={api} onUnauthorized={vi.fn()} />)

    await waitFor(() => expect(newsEvents).toHaveBeenCalledWith(expect.objectContaining({ hours: 28, sort: 'newest', limit: 50 })))

    await user.clear(screen.getByLabelText('时间窗（小时）'))
    await user.type(screen.getByLabelText('时间窗（小时）'), '48')
    await user.click(screen.getByRole('button', { name: '应用' }))
    await waitFor(() => expect(newsEvents).toHaveBeenCalledWith(expect.objectContaining({ hours: 48, sort: 'newest' })))

    await user.click(screen.getByRole('button', { name: '重要' }))
    await waitFor(() => expect(newsEvents).toHaveBeenCalledWith(expect.objectContaining({ hours: 48, sort: 'importance' })))
  })

  it('does not let an older request overwrite a newer filter response', async () => {
    const first = deferred<NewsEventResponse>()
    const second = deferred<NewsEventResponse>()
    const newsEvents = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const api = { newsEvents } as unknown as AdminApi
    const user = userEvent.setup()
    render(<DailyEventsPage api={api} onUnauthorized={vi.fn()} />)

    await waitFor(() => expect(newsEvents).toHaveBeenCalledTimes(1))
    await user.click(screen.getByRole('button', { name: '重要' }))
    await waitFor(() => expect(newsEvents).toHaveBeenCalledTimes(2))

    act(() => second.resolve({ events: [newsEvent('new', '新的筛选结果')] }))
    expect(await screen.findByText('新的筛选结果')).toBeVisible()

    act(() => first.resolve({ events: [newsEvent('stale', '过期请求结果')] }))
    expect(screen.queryByText('过期请求结果')).not.toBeInTheDocument()
    expect(screen.getByText('新的筛选结果')).toBeVisible()
  })

  it('loads the next cursor page and deduplicates event identities', async () => {
    const newsEvents = vi.fn()
      .mockResolvedValueOnce({ events: [newsEvent('one', '事件一')], pagination: { next_cursor: 'cursor-1', has_more: true } })
      .mockResolvedValueOnce({ events: [newsEvent('one', '事件一重复'), newsEvent('two', '事件二')], pagination: { next_cursor: null, has_more: false } })
    const api = { newsEvents } as unknown as AdminApi
    const user = userEvent.setup()
    render(<DailyEventsPage api={api} onUnauthorized={vi.fn()} />)

    await user.click(await screen.findByRole('button', { name: '加载更多' }))
    await waitFor(() => expect(newsEvents).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: 'cursor-1', limit: 50 })))
    expect(await screen.findByText('事件二')).toBeVisible()
    expect(screen.queryByText('事件一重复')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '加载更多' })).not.toBeInTheDocument()
  })

  it('expands an event to show reports and original article links', async () => {
    const item = newsEvent('one', '政策发布', {
      report_count: 1,
      reports: [{
        report_id: 9,
        observation_id: 11,
        source_id: 'official_cn',
        source_tier: 'primary',
        relation: 'primary',
        match_score: 0.96,
        is_representative: true,
        published_at: 1_788_362_900,
        title: '官方公告全文',
        summary: '公告详细内容。',
        url: 'https://example.com/notice',
      }],
    })
    const api = { newsEvents: vi.fn().mockResolvedValue({ events: [item] }) } as unknown as AdminApi
    const user = userEvent.setup()
    render(<DailyEventsPage api={api} onUnauthorized={vi.fn()} />)

    await user.click(await screen.findByRole('button', { name: /政策发布/ }))
    expect(screen.getByText('官方公告全文')).toBeVisible()
    expect(screen.getByText('一手')).toBeVisible()
    expect(screen.getByRole('link', { name: '查看原文' })).toHaveAttribute('href', 'https://example.com/notice')
  })

  it('renders empty and retryable error states', async () => {
    const newsEvents = vi.fn()
      .mockRejectedValueOnce(new Error('服务暂时不可用'))
      .mockResolvedValueOnce({ events: [] })
    const api = { newsEvents } as unknown as AdminApi
    const user = userEvent.setup()
    render(<DailyEventsPage api={api} onUnauthorized={vi.fn()} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('服务暂时不可用')
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('这个时间窗内还没有事件')).toBeVisible()
  })
})
