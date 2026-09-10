import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import type { AlertDetailResponse, ContentDocument, HealthSummary } from '../../shared/types'
import { EventsPage } from './EventsPage'

const health: HealthSummary = {
  level: 'healthy',
  headline: '运行正常',
  detail: '',
  engineState: 'online',
  heartbeatAt: 1,
  desiredRevision: 1,
  appliedRevision: 1,
  configPending: false,
  staleSourceIds: [],
  deadLetters: 0,
  oldestPendingAgeSeconds: 0,
}

const detail: AlertDetailResponse = {
  alert: {
    id: 17,
    observation_id: 42,
    incident_id: 7,
    title: 'Bloomberg breaking',
    message: '来源：Bloomberg Markets\n\n判断依据：breaking',
    priority: 5,
    confidence: 0.92,
    evidence: ['breaking'],
    tags: ['warning'],
    source_url: 'https://www.bloomberg.com/news/articles/test',
    status: 'delivered',
    created_at: 1_788_363_000,
    delivered_at: 1_788_363_001,
  },
  observation: {
    id: 42,
    source_id: 'bloomberg_markets',
    publisher: 'Bloomberg',
    published_at: 1_788_362_900,
    fetched_at: 1_788_363_000,
    title: 'Prime Minister Resigns',
    summary: 'Saved detailed RSS summary available without reopening Bloomberg.',
    url: 'https://www.bloomberg.com/news/articles/test',
    attributes: { section: 'Markets' },
    importance: 5,
    urgency: 5,
    relevance: 4,
    confidence: 0.92,
    region: 'GLOBAL',
    topic: 'politics',
    source_tier: 'secondary',
    information_type: 'report',
    handling: 'immediate',
  },
  incident: {
    id: 7,
    status: 'recorded',
    source_ids: ['bloomberg_markets'],
  },
  documents: [{
    id: 1,
    observation_id: 42,
    level: 'excerpt',
    source_method: 'rss_description',
    body: 'Saved detailed RSS summary available without reopening Bloomberg.',
    media_type: 'text/plain',
    canonical_url: 'https://www.bloomberg.com/news/articles/test',
    content_hash: 'abc',
    rights_policy: 'source_terms_apply',
    fetched_at: 1_788_363_000,
    created_at: 1_788_363_000,
  }],
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
  vi.restoreAllMocks()
})

describe('EventsPage detail reader', () => {
  it('loads a notification deep link and keeps the original article secondary', async () => {
    window.history.replaceState(null, '', '/events/17')
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    const loadAlert = vi.fn().mockResolvedValue(detail)
    const api = { alert: loadAlert } as unknown as AdminApi
    render(
      <EventsPage
        api={api}
        onUnauthorized={vi.fn()}
        initialAlertId="17"
        incidents={[]}
        managedSources={[]}
        health={health}
        openCount={0}
      />,
    )
    await waitFor(() => expect(screen.getByText('Prime Minister Resigns')).toBeVisible())
    expect(loadAlert).toHaveBeenCalledWith(17)
    expect(screen.getByText(/Saved detailed RSS summary/)).toBeVisible()
    expect(screen.getByText('摘要')).toBeVisible()
    expect(screen.getByText(/取得方式：RSS description/)).toBeVisible()
    expect(screen.getByText('breaking')).toBeVisible()
    expect(screen.getByRole('link', { name: '查看原文' })).toHaveAttribute(
      'href',
      'https://www.bloomberg.com/news/articles/test',
    )
  })

  it('prefers a fetched public document over the feed excerpt', async () => {
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    const publicDetail: AlertDetailResponse = {
      ...detail,
      documents: [
        {
          ...(detail.documents![0] as ContentDocument),
          id: 2,
          level: 'document',
          source_method: 'public_pdf',
          body: 'The complete text extracted from the official public PDF.',
          rights_policy: 'public_official_document',
        },
        detail.documents![0] as ContentDocument,
      ],
      content_fetch: { status: 'completed', attempts: 1, updated_at: 1_788_363_001 },
    }
    const api = { alert: vi.fn().mockResolvedValue(publicDetail) } as unknown as AdminApi
    render(<EventsPage api={api} onUnauthorized={vi.fn()} initialAlertId="17" incidents={[]} managedSources={[]} health={health} openCount={0} />)
    expect(await screen.findByText(/complete text extracted/)).toBeVisible()
    expect(screen.getByText('文档全文')).toBeVisible()
    expect(screen.getByText(/公开 PDF 文本提取/)).toBeVisible()
    expect(screen.queryByText(/Saved detailed RSS summary available/)).not.toBeInTheDocument()
  })

  it('creates a manual event through the real event API and opens its detail', async () => {
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    const created = {
      ...detail,
      alert: { ...detail.alert, id: 23, title: 'EyeOfSauron 人工事件：政策更新' },
      observation: {
        ...detail.observation!,
        source_id: 'manual',
        publisher: '人工录入',
        title: '政策更新',
        summary: '人工录入的完整内容。',
        region: 'JP',
        topic: 'politics',
      },
    }
    const createEvent = vi.fn().mockResolvedValue(created)
    const onCreated = vi.fn()
    const notify = vi.fn()
    const api = { createEvent } as unknown as AdminApi
    const user = userEvent.setup()
    render(
      <EventsPage
        api={api}
        onUnauthorized={vi.fn()}
        incidents={[]}
        managedSources={[]}
        health={health}
        openCount={0}
        canCreate
        onCreated={onCreated}
        notify={notify}
      />,
    )

    await user.click(screen.getByRole('button', { name: '添加事件' }))
    await user.type(screen.getByLabelText('标题'), '政策更新')
    await user.type(screen.getByLabelText('详细内容'), '人工录入的完整内容。')
    await user.selectOptions(screen.getByLabelText('重要程度'), '4')
    await user.selectOptions(screen.getByLabelText('地区'), 'JP')
    await user.selectOptions(screen.getByLabelText('主题'), 'politics')
    await user.type(screen.getByLabelText('来源链接（可选）'), 'https://example.com/report')
    await user.click(screen.getByRole('button', { name: '创建并发送' }))

    await waitFor(() => expect(screen.getByText('政策更新')).toBeVisible())
    expect(createEvent).toHaveBeenCalledWith({
      title: '政策更新',
      summary: '人工录入的完整内容。',
      importance: 4,
      region: 'JP',
      topic: 'politics',
      source_url: 'https://example.com/report',
    })
    expect(onCreated).toHaveBeenCalledOnce()
    expect(notify).toHaveBeenCalledWith('事件已创建，正在通过 ntfy 发送')
    expect(window.location.pathname).toBe('/events/23')
  })

  it('does not expose the create action to a read-only viewer', () => {
    const api = {} as AdminApi
    render(
      <EventsPage
        api={api}
        onUnauthorized={vi.fn()}
        incidents={[]}
        managedSources={[]}
        health={health}
        openCount={0}
      />,
    )
    expect(screen.queryByRole('button', { name: '添加事件' })).not.toBeInTheDocument()
  })
})
