import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import type { ReminderOccurrence } from '../../shared/types'
import { RemindersPage } from './Reminders'

const occurrence: ReminderOccurrence = {
  id: 7, reminder_id: 'rem_1234567890abcdef', title: '检查供水', message: '确认传感器读数',
  scheduled_for: 1790410000, acknowledged_at: null, repeat_count: 1,
  next_repeat_at: 1790413600, ack_enabled: true,
}

function page(item: ReminderOccurrence, onAcknowledge = vi.fn()) {
  const api = { reminderOccurrence: vi.fn().mockResolvedValue({ occurrence: item }) } as unknown as AdminApi
  render(<RemindersPage api={api} reminders={[]} busy={false} onOpen={vi.fn()} onToggle={vi.fn()}
    onDelete={vi.fn()} acknowledgementId={item.id} onAcknowledge={onAcknowledge} />)
  return onAcknowledge
}

describe('reminder acknowledgement', () => {
  it('shows the exact occurrence before offering acknowledgement', async () => {
    const onAcknowledge = page(occurrence)
    expect(await screen.findByText('检查供水')).toBeInTheDocument()
    expect(screen.getByText(/确认传感器读数/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: '已收到' }))
    expect(onAcknowledge).toHaveBeenCalledWith(7)
  })

  it('does not offer a second acknowledgement for a completed occurrence', async () => {
    page({ ...occurrence, acknowledged_at: 1790411000 })
    expect(await screen.findByText('检查供水')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '已收到' })).not.toBeInTheDocument()
  })
})
