import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import { EventFactDiagnosticsPanel } from './EventFactDiagnosticsPanel'

const data = { extractor_version: null, as_of: 200, active_worker_version: 1, completed_count: 90, completed_with_claim: 10, completed_without_claim: 80, explanation: '', jobs: [{ extractor_version: 1, status: 'completed', count: 90 }, { extractor_version: 1, status: 'dead', count: 3 }] }

describe('EventFactDiagnosticsPanel', () => {
  it('explains unrecognized facts and requires an explicit retry version', async () => {
    const retry = vi.fn().mockResolvedValue({ count: 3, changed: 3 })
    const api = { eventFactDiagnostics: vi.fn().mockResolvedValue(data), retryEventFactJobs: retry } as unknown as AdminApi
    const user = userEvent.setup()
    render(<EventFactDiagnosticsPanel api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText(/不代表报道没有价值/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试失败任务（0）' })).toBeDisabled()
    await user.selectOptions(screen.getByLabelText('重试提取器版本'), '1')
    await user.click(screen.getByRole('button', { name: '重试失败任务（3）' }))
    await waitFor(() => expect(retry).toHaveBeenCalledWith(1, 100))
    expect(await screen.findByText('已将 3 个失败任务重新排队')).toBeInTheDocument()
  })
  it('disallows operational changes for viewers', async () => {
    const api = { eventFactDiagnostics: vi.fn().mockResolvedValue(data) } as unknown as AdminApi
    render(<EventFactDiagnosticsPanel api={api} canWrite={false} onUnauthorized={vi.fn()} />)
    expect(await screen.findByLabelText('重试提取器版本')).toBeDisabled()
    expect(screen.getByLabelText('事实任务重试上限')).toBeDisabled()
  })
  it('enqueues bounded history backfill for a registered version', async () => {
    const backfill = vi.fn().mockResolvedValue({ count: 10, changed: 10 })
    const api = { eventFactDiagnostics: vi.fn().mockResolvedValue({ ...data, active_worker_version: 2, versions: [{ extractor_version: 2, history_highwater: 100, created_at: 200 }] }), backfillEventFactJobs: backfill } as unknown as AdminApi
    const user = userEvent.setup()
    render(<EventFactDiagnosticsPanel api={api} canWrite onUnauthorized={vi.fn()} />)
    await user.selectOptions(await screen.findByLabelText('重试提取器版本'), '2')
    await user.clear(screen.getByLabelText('事实任务重试上限'))
    await user.type(screen.getByLabelText('事实任务重试上限'), '10')
    await user.click(screen.getByRole('button', { name: '回填历史任务' }))
    expect(backfill).toHaveBeenCalledWith(2, 10)
    expect(await screen.findByText('已将 10 个历史任务加入回填')).toBeInTheDocument()
  })
  it('shows the hit denominator and disables history operations for an inactive version', async () => {
    const api = { eventFactDiagnostics: vi.fn().mockResolvedValue({ ...data, active_worker_version: 2 }) } as unknown as AdminApi
    const user = userEvent.setup()
    render(<EventFactDiagnosticsPanel api={api} canWrite onUnauthorized={vi.fn()} />)
    expect(await screen.findByText('所有版本完成任务 90 个，其中提取到事实 10 个。')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('重试提取器版本'), '1')
    expect(screen.getByRole('button', { name: '重试失败任务（3）' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '回填历史任务' })).toBeDisabled()
  })
})
