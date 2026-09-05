import { describe, expect, it } from 'vitest'
import type { ConfigResponse, Reminder } from './types'
import { deriveHealth, reminderBucket } from './utils'

describe('dashboard semantics', () => {
  it('does not classify a paused reminder as completed', () => {
    const reminder = { id: 'one', title: 'One', message: 'One', schedule_kind: 'daily', enabled: false, priority: 3 } satisfies Reminder
    expect(reminderBucket(reminder)).toBe('paused')
  })

  it('reports unknown instead of healthy when the backend has no engine heartbeat', () => {
    const config: ConfigResponse = { managed: { sources: [], rules: [] }, revision: { revision: 4 }, status: { incidents: { open: 0 }, sources: [] } }
    expect(deriveHealth(config, 1_000).level).not.toBe('healthy')
    expect(deriveHealth(config, 1_000).engineState).toBe('unknown')
  })

  it('detects desired and applied revision drift when compatible fields exist', () => {
    const config: ConfigResponse = { managed: { sources: [], rules: [] }, revision: { revision: 8 }, status: { runtime: { heartbeat_at: 990, applied_revision: 7 }, sources: [] } }
    const health = deriveHealth(config, 1_000)
    expect(health.configPending).toBe(true)
    expect(health.level).toBe('attention')
  })

  it('treats dead-letter notifications as an attention condition', () => {
    const config: ConfigResponse = { managed: { sources: [], rules: [] }, revision: { revision: 8 }, status: { runtime: { heartbeat_at: 990, applied_revision: 8, state: 'running' }, sources: [], outbox: { dead: 1 }, outbox_metrics: { oldest_pending_age_seconds: 0 } } }
    const health = deriveHealth(config, 1_000)
    expect(health.level).toBe('attention')
    expect(health.deadLetters).toBe(1)
  })

  it('does not count old failures from disabled sources as current failures', () => {
    const config: ConfigResponse = { managed: {}, status: { runtime: { heartbeat_at: 990, state: 'running' }, sources: [{ source_id: 'paused', runtime_status: 'disabled', last_success_at: 1, consecutive_failures: 4 }] } }
    expect(deriveHealth(config, 1_000).level).toBe('healthy')
    expect(deriveHealth(config, 1_000).staleSourceIds).toEqual([])
  })

  it('uses the timeout reported by the engine', () => {
    const config: ConfigResponse = { managed: {}, status: { runtime: { heartbeat_at: 900, state: 'running', heartbeat_timeout_seconds: 120 } } }
    expect(deriveHealth(config, 1_000).engineState).toBe('online')
    expect(deriveHealth(config, 1_030).engineState).toBe('offline')
  })

  it('preserves revision zero and detects pending first configuration', () => {
    const config: ConfigResponse = { managed: {}, revision: { revision: 1 }, status: { runtime: { heartbeat_at: 990, state: 'running', applied_revision: 0 } } }
    expect(deriveHealth(config, 1_000)).toMatchObject({ appliedRevision: 0, configPending: true, level: 'attention' })
  })

  it('does not report healthy before the first source check completes', () => {
    const config: ConfigResponse = { managed: {}, status: { runtime: { heartbeat_at: 990, state: 'running' }, sources: [{ source_id: 'news', runtime_status: 'starting', initialized: false }] } }
    expect(deriveHealth(config, 1_000).level).toBe('unknown')
  })
})
