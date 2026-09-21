import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SourceQualityAuditPanel } from './SourceQualityAuditPanel'

describe('source quality audit', () => {
  it('shows who changed a weight and preserves the feedback explanation', async () => {
    const api = { sourceQualityAudit: vi.fn().mockResolvedValue({ audit: [{ id: 1, source_id: 'official', action: 'override_set', actor: 'owner', created_at: 1789704000, details: { reason: '长期观察后人工调整', weight: 0.95 } }] }) }
    render(<SourceQualityAuditPanel sourceId="official" api={api} />)
    expect(await screen.findByText('设置人工权重')).toBeVisible()
    expect(screen.getByText('长期观察后人工调整')).toBeVisible()
    expect(screen.getByText('权重：0.95')).toBeVisible()
    expect(screen.getByText(/owner/)).toBeVisible()
  })
})
