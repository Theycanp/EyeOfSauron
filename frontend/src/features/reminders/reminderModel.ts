import type { Reminder } from '../../shared/types'
import { browserZone, dateTimeValue } from '../../shared/utils'

export type ReminderFilter = 'active' | 'paused' | 'completed' | 'all'

export interface ReminderDraft {
  id: string
  title: string
  message: string
  scheduleKind: 'once' | 'after' | 'daily'
  runAtLocal: string
  delayValue: number
  delayUnit: number
  dailyTime: string
  timezone: string
  priority: number
  enabled: boolean
}

export function emptyReminderDraft(): ReminderDraft {
  return { id: '', title: '定时提醒', message: '', scheduleKind: 'once', runAtLocal: dateTimeValue(), delayValue: 30, delayUnit: 60, dailyTime: '09:00', timezone: browserZone, priority: 3, enabled: true }
}

export function reminderDraft(item: Reminder): ReminderDraft {
  return { id: item.id, title: item.title, message: item.message, scheduleKind: item.schedule_kind, runAtLocal: dateTimeValue(item.run_at), delayValue: 30, delayUnit: 60, dailyTime: item.daily_time || '09:00', timezone: item.timezone || browserZone, priority: Number(item.priority), enabled: item.enabled }
}

export function reminderPayload(draft: ReminderDraft): Record<string, unknown> {
  const payload: Record<string, unknown> = { title: draft.title.trim(), message: draft.message.trim(), schedule_kind: draft.scheduleKind, timezone: draft.timezone.trim(), enabled: draft.enabled, priority: draft.priority, tags: ['alarm_clock'] }
  if (draft.id) payload.id = draft.id
  if (draft.scheduleKind === 'once') payload.run_at = Math.floor(new Date(draft.runAtLocal).getTime() / 1000)
  else if (draft.scheduleKind === 'after') payload.delay_seconds = Math.round(draft.delayValue * draft.delayUnit)
  else payload.daily_time = draft.dailyTime
  return payload
}
