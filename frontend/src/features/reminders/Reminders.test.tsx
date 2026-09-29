import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import type { ReminderOccurrence } from '../../shared/types'
import { RemindersPage } from './Reminders'
import { emptyReminderDraft, reminderDraft, reminderPayload } from './reminderModel'
import type { Reminder } from '../../shared/types'

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
  it('defaults new acknowledgement reminders to five minutes without rewriting saved intervals', () => {
    const fresh = { ...emptyReminderDraft(), ackEnabled: true }
    expect(fresh.repeatIntervalSeconds).toBe(300)
    expect(reminderPayload(fresh).repeat_interval_seconds).toBe(300)
    const saved = { id: 'rem_1234567890abcdef', title: '检查供水', message: '查看',
      schedule_kind: 'daily', daily_time: '09:00', timezone: 'Asia/Shanghai',
      priority: 3, enabled: true, ack_enabled: true, repeat_interval_seconds: 3600,
      repeat_max_attempts: 3 } as Reminder
    expect(reminderDraft(saved).repeatIntervalSeconds).toBe(3600)
  })

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
