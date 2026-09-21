import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, type AdminApi } from '../../shared/api'
import type { DigestRun } from '../../shared/types'
import { DigestRunsPanel } from './DigestRunsPanel'
import { DigestsPage } from './DigestsPage'

const run: DigestRun = {
  digest_key: 'daily:2026-09-21', state: 'ai_retrying', published_version: 1,
  generation_kind: 'algorithm', published_at: 1_800_000_000,
  retry: { status: 'pending', attempts: 1, next_attempt_at: 1_800_004_500,
    retry_deadline_at: 1_800_018_000, last_error: 'upstream unavailable',
    started_at: 1_800_000_000, updated_at: 1_800_000_000 },
  attempts: [{ id: 9, digest_key: 'daily:2026-09-21', status: 'failed',
    started_at: 1_800_000_000, finished_at: 1_800_000_012, error: 'upstream unavailable',
    providers: [{ provider: 'models.example', model: 'primary-model', prompt_id: 'digest',
      prompt_version: '4', prompt_hash: 'abcdef0123456789', status: 'failed', elapsed_ms: '12000',
      error: 'request failed (redacted)' }] }],
  reserved_attempts: 1, attempt_history_available: true, can_retry_now: true,
}

describe('digest run diagnostics', () => {
  it('shows unpublished runs and recorded model evidence without inventing older history', async () => {
    const old = { ...run, digest_key: 'daily:old', state: 'algorithm_published',
      attempts: [], attempt_history_available: false, retry: null, can_retry_now: false }
    const generating = { ...run, digest_key: 'daily:next', state: 'generating',
      published_version: null, published_at: null, generation_kind: null, retry: null,
      attempts: [{ ...run.attempts[0], status: 'running', finished_at: null, providers: [], error: null }], can_retry_now: false }
    const api = { digests: vi.fn().mockResolvedValue({ digests: [] }),
      digestRuns: vi.fn().mockResolvedValue({ runs: [run, old, generating] }) } as unknown as AdminApi
    render(<DigestsPage api={api} onUnauthorized={vi.fn()} canRetry />)
    await userEvent.click(screen.getByRole('button', { name: '运行记录' }))
    expect(await screen.findByText('等待 AI 重试')).toBeVisible()
    expect(screen.getByText('正在生成')).toBeVisible()
    expect(screen.getByText(/这期没有逐次运行历史/)).toBeVisible()
    expect(screen.getByText(/不等于 HTTP 请求次数/)).toBeVisible()
    const current = within(screen.getByRole('article', { name: `运行记录 ${run.digest_key}` }))
    expect(current.getByText('v1 · 算法版')).toBeVisible()
    await userEvent.click(current.getByText('展开尝试记录（1）'))
    expect(current.getByText('models.example')).toBeVisible()
    expect(current.getByText('primary-model')).toBeVisible()
    expect(current.getByText('digest@4')).toBeVisible()
    expect(current.getByText('abcdef0123456789')).toBeVisible()
    expect(current.getByText('失败 · 12.0 秒')).toBeVisible()
    expect(current.getByText('request failed (redacted)')).toBeVisible()
  })

  it('does not offer write controls to a read-only user', async () => {
    const api = { digestRuns: vi.fn().mockResolvedValue({ runs: [run] }) } as unknown as AdminApi
    render(<DigestRunsPanel api={api} onUnauthorized={vi.fn()} canRetry={false} />)
    await screen.findByText('等待 AI 重试')
    expect(screen.queryByRole('button', { name: '提前重试' })).not.toBeInTheDocument()
  })

  it('disables exhausted budgets even if a stale eligibility flag is true', async () => {
    const exhausted = { ...run, reserved_attempts: 5, state: 'retry_exhausted' }
    const api = { digestRuns: vi.fn().mockResolvedValue({ runs: [exhausted] }) } as unknown as AdminApi
    render(<DigestRunsPanel api={api} onUnauthorized={vi.fn()} canRetry />)
    await screen.findByText('重试已结束')
    expect(screen.getByRole('button', { name: '提前重试' })).toBeDisabled()
  })

  it('guards double clicks and reuses an idempotency identity after uncertain network failure', async () => {
    let rejectFirst!: (failure: Error) => void
    const first = new Promise<never>((_resolve, reject) => { rejectFirst = reject })
    const retry = vi.fn().mockReturnValueOnce(first).mockResolvedValueOnce({ job: { status: 'succeeded' } })
    const api = { digestRuns: vi.fn().mockResolvedValue({ runs: [run] }), retryDigestNow: retry,
      digestRun: vi.fn().mockResolvedValue({ run: { ...run, can_retry_now: false } }) } as unknown as AdminApi
    render(<DigestRunsPanel api={api} onUnauthorized={vi.fn()} canRetry />)
    const button = await screen.findByRole('button', { name: '提前重试' })
    await userEvent.dblClick(button)
    expect(retry).toHaveBeenCalledTimes(1)
    expect(button).toBeDisabled()
    act(() => { rejectFirst(new Error('connection interrupted')) })
    expect(await screen.findByRole('alert')).toHaveTextContent('connection interrupted')
    await userEvent.click(screen.getByRole('button', { name: '提前重试' }))
    await screen.findByText(/已安排提前重试；现有次数与截止时间不变/)
    expect(retry).toHaveBeenCalledTimes(2)
    expect(retry.mock.calls[0]).toEqual(retry.mock.calls[1])
    expect(retry.mock.calls[0]?.[1]).toMatch(/^[0-9a-f-]{36}$/)
    await waitFor(() => expect(screen.getByRole('button', { name: '提前重试' })).toBeDisabled())
  })

  it('retains diagnostics after refresh failure and propagates unauthorized responses', async () => {
    const unauthorized = vi.fn()
    const list = vi.fn().mockResolvedValueOnce({ runs: [run] })
      .mockRejectedValueOnce(new Error('temporary read failure'))
      .mockRejectedValueOnce(new ApiError('expired', 401))
    const api = { digestRuns: list } as unknown as AdminApi
    render(<DigestRunsPanel api={api} onUnauthorized={unauthorized} canRetry={false} />)
    await screen.findByText('等待 AI 重试')
    await userEvent.click(screen.getByRole('button', { name: '刷新运行记录' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('已保留上次成功读取的记录')
    expect(screen.getByText('等待 AI 重试')).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: '刷新运行记录' }))
    await waitFor(() => expect(unauthorized).toHaveBeenCalledTimes(1))
  })

  it('preserves accepted scheduling when its follow-up read fails', async () => {
    const api = { digestRuns: vi.fn().mockResolvedValue({ runs: [run] }),
      retryDigestNow: vi.fn().mockResolvedValue({ job: { status: 'succeeded' } }),
      digestRun: vi.fn().mockRejectedValue(new Error('read unavailable')) } as unknown as AdminApi
    render(<DigestRunsPanel api={api} onUnauthorized={vi.fn()} canRetry />)
    await userEvent.click(await screen.findByRole('button', { name: '提前重试' }))
    expect(await screen.findByRole('status')).toHaveTextContent('已安排提前重试')
    expect(await screen.findByRole('alert')).toHaveTextContent('read unavailable')
    expect(screen.getByText('1 / 5 次逻辑生成')).toBeVisible()
    expect(screen.getByRole('button', { name: '提前重试' })).toBeDisabled()
  })
})
