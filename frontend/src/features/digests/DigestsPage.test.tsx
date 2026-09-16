import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import { DigestsPage } from './DigestsPage'

describe('DigestsPage', () => {
  it('moves from the digest list to an evidence-rich reader', async () => {
    const digest = { digest_key: '2026-09-06', version: 1, period_start: 1, period_end: 2, timezone: 'Asia/Shanghai', title: '今日重点', summary: '三地重要动态', generation_kind: 'algorithm', status: 'published' as const, created_at: 2, published_at: 2, item_count: 1, source_count: 2 }
    const api = {
      digests: vi.fn().mockResolvedValue({ digests: [digest] }),
      digest: vi.fn().mockResolvedValue({ digest: { ...digest, items: [{ cluster_key: 'a', title: '政策更新', summary: '公告摘要', score: 4.2, importance: 4, urgency: 3, relevance: 5, confidence: 0.9, regions: ['CN'], topics: ['policy'], source_ids: ['official'], observation_ids: [1, 2, 3], links: ['https://example.com', 'https://example.com/update-2', 'https://example.com/update-3'] }], coverage: [{ source_id: 'official', status: 'healthy', observation_count: 1 }] } }),
    } as unknown as AdminApi
    render(<DigestsPage api={api} onUnauthorized={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('今日重点')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /阅读日报/ }))
    await waitFor(() => expect(screen.getByText('政策更新')).toBeInTheDocument())
    expect(screen.getByRole('link', { name: '查看原文' })).toHaveAttribute('href', 'https://example.com')
    expect(screen.getAllByRole('link')).toHaveLength(1)
    expect(screen.getByText('合并 3 次更新')).toBeVisible()
    expect(screen.getByText(/来源覆盖：1\/1 正常/)).toBeInTheDocument()
  })

  it('renders event reports in parallel source-tier groups', async () => {
    const digest = { digest_key: 'daily:event', version: 1, period_start: 1, period_end: 2, timezone: 'Asia/Shanghai', title: '事件日报', summary: '事件视图', generation_kind: 'algorithm', status: 'published' as const, created_at: 2, published_at: 2, item_count: 1, source_count: 3 }
    const api = {
      digests: vi.fn().mockResolvedValue({ digests: [digest] }),
      digest: vi.fn().mockResolvedValue({ digest: { ...digest, items: [{ cluster_key: 'legacy', event_key: 'event-1', title: '同一事件', summary: '多层来源并列展示', score: 5, importance: 5, urgency: 4, relevance: 4, confidence: 0.9, reports: [
        { report_id: 'r1', source_tier: 'primary', relation: 'primary', source_id: 'official', url: 'https://example.com/official', title: '官方公告' },
        { report_id: 'r2', source_tier: 'secondary', relation: 'corroborates', source_id: 'wire', url: 'https://example.com/wire', title: '媒体报道' },
        { report_id: 'r3', source_tier: 'social', relation: 'context', source_id: 'social', url: 'https://example.com/social', title: '社交讨论' },
      ] }], coverage: [] } }),
    } as unknown as AdminApi
    render(<DigestsPage api={api} onUnauthorized={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('事件日报')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /阅读日报/ }))
    await waitFor(() => expect(screen.getByText('同一事件')).toBeInTheDocument())
    expect(screen.getByText('一手')).toBeInTheDocument()
    expect(screen.getByText('二手')).toBeInTheDocument()
    expect(screen.getByText('社交/热度')).toBeInTheDocument()
    expect(screen.getByText('交叉印证')).toBeInTheDocument()
    expect(screen.getAllByRole('link')).toHaveLength(3)
  })
})
