import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import type { AlertDetailResponse, HealthSummary } from '../../shared/types'
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
    expect(screen.getByText('breaking')).toBeVisible()
    expect(screen.getByRole('link', { name: '查看原文' })).toHaveAttribute(
      'href',
      'https://www.bloomberg.com/news/articles/test',
    )
  })
})
