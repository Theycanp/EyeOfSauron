import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SourceDiagnostics } from './SourceDiagnostics'
import type { SourceHealth } from '../../shared/types'

const health: SourceHealth = {
  source_id: 'official', as_of: 1789704000,
  current: { source_id: 'official', last_success_at: 1789704000, consecutive_failures: 0 },
  polling: { basis: 'cumulative_persisted_counters', attempts: 10, successes: 9, failures: 1, success_rate: 0.9, window_success_rate: null },
  evidence: { basis: 'retained_observations_by_ingestion_time', since: 1789099200, until: 1789704000, observations_24h: 2, observations_7d: 10, with_full_text: 6, full_text_coverage: 0.6, content_jobs: { completed: 6, dead: 1 }, content_failure_kinds: { http_403: 1 } },
}

describe('source diagnostics', () => {
  it('labels lifetime and ingestion metrics without inferring a recent poll success rate', async () => {
    const api = { sourceHealth: vi.fn().mockResolvedValue({ health }) }
    render(<SourceDiagnostics sourceId="official" api={api} onClose={() => undefined} />)
    expect(await screen.findByText('90.0%')).toBeVisible()
    expect(screen.getByText('累计采集成功率')).toBeVisible()
    expect(screen.getByText('60.0%')).toBeVisible()
    expect(screen.getByText(/无法计算近 7 天采集成功率/)).toBeVisible()
    expect(screen.getByText(/正文受限不代表来源停止采集/)).toBeVisible()
    expect(api.sourceHealth).toHaveBeenCalledWith('official', expect.any(AbortSignal))
  })

  it('retains diagnostics if a refresh fails and cancels a request when closed', async () => {
    let pendingSignal: AbortSignal | undefined
    const api = { sourceHealth: vi.fn<(id: string, signal?: AbortSignal) => Promise<{ health: SourceHealth }>>().mockResolvedValueOnce({ health }).mockRejectedValueOnce(new Error('暂时不可用')).mockImplementationOnce((_id, signal) => { pendingSignal = signal; return new Promise(() => undefined) }) }
    const view = render(<SourceDiagnostics sourceId="official" api={api} onClose={() => undefined} />)
    await screen.findByText('90.0%')
    fireEvent.click(screen.getByRole('button', { name: '刷新来源诊断' }))
    expect(await screen.findByRole('status')).toHaveTextContent('暂时不可用')
    expect(screen.getByText('90.0%')).toBeVisible()
    act(() => { fireEvent.click(screen.getByRole('button', { name: '刷新来源诊断' })) })
    view.unmount()
    expect(pendingSignal?.aborted).toBe(true)
  })
})
