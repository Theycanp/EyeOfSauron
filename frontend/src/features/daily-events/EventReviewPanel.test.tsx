import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import type { NewsEvent } from '../../shared/types'
import { EventReviewPanel } from './EventReviewPanel'

describe('EventReviewPanel evidence history', () => {
  it('shows disputed facts, source evidence, chronology and notification detail links', async () => {
    const eventReview = vi.fn().mockResolvedValue({
      reports: [{ report_id: 2, observation_id: 7, title: 'Official correction', publisher: 'Agency', url: 'https://example.com/correction', match_score: 1, evidence: { score: 1, title_similarity: 1, time_distance_hours: 0, shared_entities: [], shared_numbers: [] } }],
      audit: [],
      claims: [{ claim_key: 'fact', text: 'A disputed count', status: 'disputed' }],
      claim_evidence: [{ claim_key: 'fact', report_id: 2, stance: 'supports' }],
      timeline: [{ timeline_id: 3, occurred_at: 100, text: 'Count revised' }],
      notifications: [{ id: 4, created_at: 100, title: 'Important update', status: 'delivered' }],
      history_truncated: { timeline: true },
    })
    render(<EventReviewPanel api={{ eventReview } as unknown as AdminApi} event={{ event_key: 'event' } as NewsEvent} canWrite={false} onChange={vi.fn()} onUnauthorized={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: '查看事件历史与匹配依据' }))
    expect(await screen.findByText('有争议')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Agency：Official correction' })).toHaveAttribute('href', 'https://example.com/correction')
    expect(screen.getByText(/Count revised/)).toBeVisible()
    expect(screen.getByRole('link', { name: 'Important update' })).toHaveAttribute('href', '/events/4')
    expect(screen.getByRole('status')).toHaveTextContent('不是完整历史')
  })

  it('distinguishes absent structured history from an absence of facts', async () => {
    const eventReview = vi.fn().mockResolvedValue({ reports: [], audit: [] })
    render(<EventReviewPanel api={{ eventReview } as unknown as AdminApi} event={{ event_key: 'event' } as NewsEvent} canWrite={false} onChange={vi.fn()} onUnauthorized={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: '查看事件历史与匹配依据' }))
    expect(await screen.findByText('尚无结构化事实记录，不代表报道中没有事实。')).toBeVisible()
    expect(screen.getByText('此事件尚无关联通知。')).toBeVisible()
  })
})
