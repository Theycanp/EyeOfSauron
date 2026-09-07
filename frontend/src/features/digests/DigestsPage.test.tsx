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
      digest: vi.fn().mockResolvedValue({ digest: { ...digest, items: [{ cluster_key: 'a', title: '政策更新', summary: '公告摘要', score: 4.2, importance: 4, urgency: 3, relevance: 5, confidence: 0.9, regions: ['CN'], topics: ['policy'], links: ['https://example.com'] }], coverage: [{ source_id: 'official', status: 'healthy', observation_count: 1 }] } }),
    } as unknown as AdminApi
    render(<DigestsPage api={api} onUnauthorized={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('今日重点')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /阅读日报/ }))
    await waitFor(() => expect(screen.getByText('政策更新')).toBeInTheDocument())
    expect(screen.getByRole('link', { name: /来源 1/ })).toHaveAttribute('href', 'https://example.com')
    expect(screen.getByText(/来源覆盖：1\/1 正常/)).toBeInTheDocument()
  })
})
