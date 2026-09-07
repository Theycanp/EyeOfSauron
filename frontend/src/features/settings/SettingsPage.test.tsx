import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { HealthSummary } from '../../shared/types'
import { SettingsPage } from './SettingsPage'

const health: HealthSummary = {
  level: 'healthy', headline: '正常', detail: '', engineState: 'online', heartbeatAt: 1,
  desiredRevision: 3, appliedRevision: 3, configPending: false, staleSourceIds: [], deadLetters: 0,
  oldestPendingAgeSeconds: 0,
}

describe('SettingsPage', () => {
  it('edits analysis policy without exposing a secret value field', async () => {
    const user = userEvent.setup()
    const save = vi.fn()
    render(<SettingsPage status={{}} health={health} revisions={[]} busy={false}
      analysis={{ enabled: true, shadow_mode: true, region_weights: { CN: 5, JP: 4, US: 5, GLOBAL: 3, OTHER: 3 } }}
      digest={{ enabled: false }} onSaveDigest={vi.fn()}
      prompts={[{ prompt_id: 'triage', version: 1, system_text: 'system' }]}
      onSaveAnalysis={save} onSavePrompt={vi.fn()} advancedKind="sources" advancedJson=""
      onAdvancedKind={vi.fn()} onAdvancedJson={vi.fn()} onLoadExample={vi.fn()} onSaveAdvanced={vi.fn()} onRollback={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('checkbox', { name: /启用语义分析/ })).toBeChecked())
    expect(screen.getByText('只保存变量名，不保存 Token。')).toBeInTheDocument()
    await user.clear(screen.getByLabelText('API 每日预算'))
    await user.type(screen.getByLabelText('API 每日预算'), '7')
    await user.click(screen.getByRole('button', { name: /保存分析配置/ }))
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ daily_api_budget: 7, shadow_mode: true }))
  })
})
