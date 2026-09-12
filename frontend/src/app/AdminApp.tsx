import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BellRing, CircleAlert, FileText, Gauge, Inbox, LayoutDashboard, LockKeyhole, LogOut, Menu, Moon, Radar, RefreshCw, Settings2, ShieldCheck, Sun, UserRoundCog, X } from 'lucide-react'
import { AdminApi, ApiError } from '../shared/api'
import type { AdminIdentity, AdminResourceName, AnalysisConfig, ConfigRevision, DigestConfig, ManagedSource, NewsCatalogEntry, NewsCatalogFeed, OutboxAlert, Reminder, SourceKind, SourceQualityProfile } from '../shared/types'
import { deriveHealth, formatDate } from '../shared/utils'
import { Brand, EyeMark } from '../shared/ui/Brand'
import { ConfirmDialog, type Confirmation } from '../shared/ui/ConfirmDialog'
import { useAdminData } from './useAdminData'
import { OverviewPage } from '../features/overview/OverviewPage'
import { ReminderDialog, RemindersPage } from '../features/reminders/Reminders'
import { emptyReminderDraft, reminderDraft, reminderPayload, type ReminderDraft } from '../features/reminders/reminderModel'
import { catalogSourceDraft, createSourceDraft, editSourceDraft, ruleForSource, serializeSimpleRule, serializeSource, type SourceDraft } from '../features/sources/providers'
import { SourceDialog, SourcesPage } from '../features/sources/Sources'
import { EventsPage } from '../features/events/EventsPage'
import { DigestsPage } from '../features/digests/DigestsPage'
import { OutboxPanel } from '../features/settings/OutboxPanel'
import { SettingsPage } from '../features/settings/SettingsPage'
import { SourceQualityPage } from '../features/source-quality/SourceQualityPage'
import { UsersPage } from '../features/users/UsersPage'

const pages = [
  { id: 'overview', label: '概览', subtitle: '重要的事情，一眼就能看到', icon: LayoutDashboard },
  { id: 'reminders', label: '提醒', subtitle: '安排未来要发送的消息', icon: BellRing },
  { id: 'sources', label: '监测来源', subtitle: '决定 EyeOfSauron 要观察什么', icon: Radar },
  { id: 'events', label: '事件', subtitle: '异常、恢复和重要动态', icon: Inbox },
  { id: 'digests', label: '日报', subtitle: '阅读每日汇总的重要信息', icon: FileText },
  { id: 'source-quality', label: '信源质量', subtitle: '长期校准来源可信度', icon: Gauge },
  { id: 'users', label: '用户与权限', subtitle: '管理后台访问和登录会话', icon: UserRoundCog },
  { id: 'settings', label: '设置', subtitle: '通知渠道、运行事实和高级选项', icon: Settings2 },
] as const

type PageId = typeof pages[number]['id']
type Toast = { id: number; text: string; tone: 'success' | 'error' }

export default function AdminApp() {
  const api = useMemo(() => new AdminApi(), [])
  const [identity, setIdentity] = useState<AdminIdentity | null>(null)
  const [authChecking, setAuthChecking] = useState(true)
  const [loginUsername, setLoginUsername] = useState('')
  const [loginPassword, setLoginPassword] = useState('')
  const [loginError, setLoginError] = useState('')
  const [loginBusy, setLoginBusy] = useState(false)
  const [page, setPage] = useState<PageId>(() => {
    if (window.location.pathname.startsWith('/events')) return 'events'
    if (window.location.pathname.startsWith('/digests')) return 'digests'
    const value = window.location.hash.replace('#/', '')
    return pages.some((item) => item.id === value) ? value as PageId : 'overview'
  })
  const [theme, setTheme] = useState(() => localStorage.getItem('eosTheme') || (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'))
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [sourceTesting, setSourceTesting] = useState(false)
  const [sourceQuery, setSourceQuery] = useState('')
  const [toast, setToast] = useState<Toast | null>(null)
  const [advancedKind, setAdvancedKind] = useState<'sources' | 'rules'>('sources')
  const [advancedJson, storeAdvancedJson] = useState('')
  const advancedDraftRevision = useRef<number | null>(null)
  const [pendingRevision, setPendingRevision] = useState<number | null>(() => Number(localStorage.getItem('eosPendingRevision')) || null)
  const [reminderForm, setReminderForm] = useState<ReminderDraft>(emptyReminderDraft)
  const [reminderSnapshot, setReminderSnapshot] = useState('')
  const [sourceForm, setSourceForm] = useState<SourceDraft>(() => createSourceDraft(null))
  const [sourceSnapshot, setSourceSnapshot] = useState('')
  const [outboxStatus, setOutboxStatus] = useState<'dead' | 'pending'>('dead')
  const [outboxAlerts, setOutboxAlerts] = useState<OutboxAlert[]>([])
  const [outboxLoading, setOutboxLoading] = useState(false)
  const [outboxError, setOutboxError] = useState<string | null>(null)
  const [outboxNextCursor, setOutboxNextCursor] = useState<number | null>(null)
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const confirmAction = useRef<(() => Promise<void>) | null>(null)
  const reminderDialog = useRef<HTMLDialogElement>(null)
  const sourceDialog = useRef<HTMLDialogElement>(null)
  const confirmDialog = useRef<HTMLDialogElement>(null)
  const sourceTestAbort = useRef<AbortController | null>(null)
  const sourceDraftRevision = useRef(-1)
  const outboxSequence = useRef(0)

  const clearSession = useCallback(() => {
    setIdentity(null)
    setLoginBusy(false)
  }, [])
  const { resources, refresh, refreshing, reset, lastUpdatedAt, hasAnyData } = useAdminData(api, Boolean(identity), clearSession)

  useEffect(() => {
    api.session().then((response) => setIdentity(response.user)).catch(() => setIdentity(null)).finally(() => setAuthChecking(false))
  }, [api])

  const logout = useCallback(() => {
    void api.logout().catch(() => undefined).finally(() => { reset(); clearSession() })
  }, [api, clearSession, reset])

  const notify = useCallback((text: string, tone: Toast['tone'] = 'success') => setToast({ id: Date.now(), text, tone }), [])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast((current) => current?.id === toast.id ? null : current), 4_200)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('eosTheme', theme)
  }, [theme])

  useEffect(() => {
    const handleHash = () => {
      const next = window.location.hash.replace('#/', '')
      if (pages.some((item) => item.id === next)) setPage(next as PageId)
    }
    window.addEventListener('hashchange', handleHash)
    return () => window.removeEventListener('hashchange', handleHash)
  }, [])

  useEffect(() => {
    const dirty = reminderSnapshot && reminderSnapshot !== JSON.stringify(reminderForm) || sourceSnapshot && sourceSnapshot !== JSON.stringify(sourceForm)
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault()
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [reminderForm, reminderSnapshot, sourceForm, sourceSnapshot])

  const config = resources.config.data
  const status = config?.status || {}
  const managedSources = config?.managed.sources || []
  const managedRules = config?.managed.rules || []
  const reminders = resources.reminders.data?.reminders || []
  const revisions = resources.revisions.data?.revisions || []
  const incidents = resources.incidents.data?.incidents || []
  const newsCatalog = resources.newsCatalog.data?.sources || []
  const prompts = resources.prompts.data?.prompts || []
  const sourceQuality = resources.sourceQuality.data?.profiles || []
  const sourceStates = status.sources || []
  const health = useMemo(() => deriveHealth(config), [config])
  const openIncidents = Number(status.incidents?.open || 0)
  const pending = Number(status.outbox?.pending || 0) + Number(status.outbox?.sending || 0)
  const currentPage = pages.find((item) => item.id === page) || pages[0]
  const resourceNames: AdminResourceName[] = ['config', 'reminders', 'revisions', 'incidents', 'newsCatalog', 'prompts', 'sourceQuality']
  const partialErrors = resourceNames.flatMap((name) => resources[name].error ? [`${name}: ${resources[name].error}`] : [])
  const expectedRevision = Number.isInteger(Number(config?.revision?.revision)) ? Number(config?.revision?.revision) : -1
  const effectivePendingRevision = pendingRevision && health.appliedRevision !== null && health.appliedRevision >= pendingRevision ? null : pendingRevision
  const restartRequired = Boolean(effectivePendingRevision || health.configPending)
  const authenticated = Boolean(identity && config)
  const can = (permission: string) => Boolean(identity?.permissions.includes(permission))

  const setAdvancedJson = (value: string) => {
    if (advancedDraftRevision.current === null || !value) advancedDraftRevision.current = expectedRevision
    storeAdvancedJson(value)
  }

  useEffect(() => {
    if (pendingRevision && health.appliedRevision !== null && health.appliedRevision >= pendingRevision) {
      localStorage.removeItem('eosPendingRevision')
    }
  }, [health.appliedRevision, pendingRevision])

  const markRestart = (revision?: number) => {
    const value = revision || health.desiredRevision
    if (value) {
      localStorage.setItem('eosPendingRevision', String(value))
      setPendingRevision(value)
    }
  }

  const go = (next: PageId) => {
    setPage(next)
    setMobileNavOpen(false)
    window.history.pushState(null, '', `#/${next}`)
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    window.scrollTo({ top: 0, behavior: reducedMotion ? 'auto' : 'smooth' })
  }

  const login = async () => {
    const username = loginUsername.trim()
    if (!username || !loginPassword) return
    setLoginBusy(true)
    setLoginError('')
    try {
      const response = await api.login(username, loginPassword)
      setIdentity(response.user)
      setLoginPassword('')
    } catch (error) {
      setLoginError(error instanceof Error ? error.message : '无法登录')
      setLoginBusy(false)
    }
  }

  const refreshNow = async () => {
    const ok = await refresh()
    notify(ok ? '数据已刷新' : '所有接口均刷新失败', ok ? 'success' : 'error')
  }

  const reportMutationError = async (error: unknown, fallback: string) => {
    if (error instanceof ApiError && error.status === 401) {
      logout()
      return
    }
    if (error instanceof ApiError && error.code === 'revision_conflict') {
      await refresh(true)
      notify('配置已由其他会话修改。页面数据已刷新；请关闭并重新打开编辑器，核对最新配置后再修改。', 'error')
      return
    }
    notify(error instanceof Error ? error.message : fallback, 'error')
  }

  const loadOutbox = useCallback(async (view: 'dead' | 'pending', append = false, beforeId?: number | null) => {
    const sequence = ++outboxSequence.current
    setOutboxLoading(true)
    setOutboxError(null)
    try {
      const response = await api.outbox(view, append ? beforeId || undefined : undefined)
      if (sequence !== outboxSequence.current) return
      const rows = response.alerts || []
      setOutboxAlerts((current) => append ? [...current, ...rows.filter((row) => !current.some((item) => item.id === row.id))] : rows)
      const cursor = Number(response.pagination?.next_cursor)
      setOutboxNextCursor(Number.isInteger(cursor) && cursor > 0 ? cursor : null)
    } catch (error) {
      if (sequence !== outboxSequence.current) return
      if (error instanceof ApiError && error.status === 401) logout()
      else setOutboxError(error instanceof Error ? error.message : '无法读取通知队列')
    } finally {
      if (sequence === outboxSequence.current) setOutboxLoading(false)
    }
  }, [api, logout])

  useEffect(() => {
    if (!authenticated || page !== 'settings') return
    const timer = window.setTimeout(() => { void loadOutbox(outboxStatus) }, 0)
    return () => window.clearTimeout(timer)
  }, [authenticated, loadOutbox, outboxStatus, page])

  const changeOutboxStatus = (view: 'dead' | 'pending') => {
    if (view === outboxStatus) return
    outboxSequence.current += 1
    setOutboxAlerts([])
    setOutboxNextCursor(null)
    setOutboxStatus(view)
  }

  const openReminder = (item?: Reminder) => {
    const draft = item ? reminderDraft(item) : emptyReminderDraft()
    setReminderForm(draft)
    setReminderSnapshot(JSON.stringify(draft))
    reminderDialog.current?.showModal()
  }

  const closeReminder = () => {
    reminderDialog.current?.close()
    setReminderSnapshot('')
  }

  const saveReminder = async () => {
    setBusy(true)
    try {
      await api.mutate('/api/reminders', 'POST', reminderPayload(reminderForm))
      closeReminder()
      await refresh(true)
      notify(reminderForm.id ? '提醒已更新' : '提醒已创建，会按计划发送到 ntfy')
    } catch (error) { notify(error instanceof Error ? error.message : '无法保存提醒', 'error') } finally { setBusy(false) }
  }

  const toggleReminder = async (item: Reminder) => {
    setBusy(true)
    try {
      await api.mutate(`/api/reminders/${encodeURIComponent(item.id)}/${item.enabled ? 'disable' : 'enable'}`, 'POST', {})
      await refresh(true)
      notify(item.enabled ? '提醒已暂停' : '提醒已启用')
    } catch (error) { notify(error instanceof Error ? error.message : '无法更新提醒', 'error') } finally { setBusy(false) }
  }

  const openSource = (kind: SourceKind | null, mode: 'generic' | 'typed') => {
    sourceDraftRevision.current = expectedRevision
    const draft = createSourceDraft(kind, mode)
    setSourceForm(draft)
    setSourceSnapshot(JSON.stringify(draft))
    sourceDialog.current?.showModal()
  }

  const editSource = (source: ManagedSource) => {
    try {
      sourceDraftRevision.current = expectedRevision
      const draft = editSourceDraft(source, ruleForSource(managedRules, source.id))
      setSourceForm(draft)
      setSourceSnapshot(JSON.stringify(draft))
      sourceDialog.current?.showModal()
    } catch (error) { notify(error instanceof Error ? error.message : '这个来源只能通过高级 JSON 编辑', 'error') }
  }

  const closeSource = () => {
    sourceTestAbort.current?.abort()
    sourceTestAbort.current = null
    setSourceTesting(false)
    sourceDialog.current?.close()
    setSourceSnapshot('')
  }

  const resetSourcePicker = () => {
    const draft = createSourceDraft(null, 'generic')
    setSourceForm(draft)
  }

  const saveSource = async () => {
    setBusy(true)
    try {
      const source = serializeSource(sourceForm)
      const rule = serializeSimpleRule(sourceForm, source)
      const response = await api.configMutate('/api/source-bundles', 'POST', sourceDraftRevision.current, { source, rule })
      markRestart(response.revision)
      closeSource()
      await refresh(true)
      notify(sourceForm.editing ? '来源已更新；等待 Argus 应用新配置' : '来源已添加；等待 Argus 应用新配置')
    } catch (error) { await reportMutationError(error, '无法保存来源') } finally { setBusy(false) }
  }

  const testSource = async () => {
    sourceTestAbort.current?.abort()
    const controller = new AbortController()
    sourceTestAbort.current = controller
    setSourceTesting(true)
    try {
      const result = await api.testSource(serializeSource(sourceForm), { signal: controller.signal, timeoutMs: 90_000 })
      notify(`连接成功：读取到 ${result.observations} 条新记录，耗时 ${result.elapsed_ms} 毫秒`)
    } catch (error) {
      if (error instanceof ApiError && ['request_cancelled', 'job_cancelled'].includes(error.code || '')) notify('已停止等待测试结果；排队中的任务已请求取消，已开始的检查会自行结束。')
      else notify(error instanceof Error ? `连接测试失败：${error.message}` : '连接测试失败', 'error')
    } finally {
      if (sourceTestAbort.current === controller) {
        sourceTestAbort.current = null
        setSourceTesting(false)
      }
    }
  }

  const cancelSourceTest = () => sourceTestAbort.current?.abort()

  const toggleSource = async (source: ManagedSource) => {
    setBusy(true)
    try {
      const response = await api.configMutate(`/api/sources/${encodeURIComponent(source.id)}/${source.enabled === false ? 'enable' : 'disable'}`, 'POST', expectedRevision, {})
      markRestart(response.revision)
      await refresh(true)
      notify(`${source.enabled === false ? '启用' : '停用'}请求已保存；等待 Argus 应用`)
    } catch (error) { await reportMutationError(error, '无法更新来源') } finally { setBusy(false) }
  }

  const requestConfirmation = (next: Confirmation, action: () => Promise<void>) => {
    setConfirmation(next)
    confirmAction.current = action
    confirmDialog.current?.showModal()
  }

  const runConfirmed = async () => {
    if (!confirmAction.current) return
    setBusy(true)
    try { await confirmAction.current(); confirmDialog.current?.close(); setConfirmation(null); confirmAction.current = null } catch (error) { await reportMutationError(error, '操作失败') } finally { setBusy(false) }
  }

  const prepareCatalogFeed = (entry: NewsCatalogEntry, feed: NewsCatalogFeed) => {
    if (entry.integration_mode === 'licensed_provider') {
      notify(`${entry.publisher} 需要专用授权适配器，不能伪装成公开 RSS 启用。`, 'error')
      return
    }
    requestConfirmation({
      kind: 'rollback',
      title: `从官方目录添加“${entry.publisher} · ${feed.label}”？`,
      detail: '系统会生成一个默认启用的来源草稿，保留官方链接与精确主机白名单。请在下一步检查配置并主动保存；保存前不会采集，付费正文不会被抓取。',
      confirmLabel: '生成来源草稿',
    }, () => {
      sourceDraftRevision.current = expectedRevision
      const draft = catalogSourceDraft(entry, feed, managedSources.map((source) => source.id))
      setSourceForm(draft)
      setSourceSnapshot(JSON.stringify(draft))
      window.setTimeout(() => sourceDialog.current?.showModal(), 0)
      return Promise.resolve()
    })
  }

  const retryOutbox = (alert: OutboxAlert) => requestConfirmation({ kind: 'rollback', title: `重新投递“${alert.title || `通知 #${alert.id}`}”？`, detail: '它会回到待发送队列并清零失败次数。若故障仍存在，Argus 会按退避策略重试。', confirmLabel: '重新投递' }, async () => {
    await api.mutate(`/api/outbox/${alert.id}/retry`, 'POST', {})
    await Promise.all([loadOutbox(outboxStatus), refresh(true)])
    notify('通知已重新加入待发送队列')
  })

  const cancelOutbox = (alert: OutboxAlert) => requestConfirmation({ kind: 'delete', title: `取消发送“${alert.title || `通知 #${alert.id}`}”？`, detail: '仅尚未被发送进程认领的待发通知可以取消；已经送达 ntfy 的消息无法撤回。', confirmLabel: '取消发送' }, async () => {
    await api.mutate(`/api/outbox/${alert.id}/cancel`, 'POST', {})
    await Promise.all([loadOutbox(outboxStatus), refresh(true)])
    notify('待发通知已取消')
  })

  const discardOutbox = (alert: OutboxAlert) => requestConfirmation({ kind: 'delete', title: `永久删除“${alert.title || `通知 #${alert.id}`}”？`, detail: '这会删除该死信的审计记录，且无法恢复。通常只有确认不再需要投递时才应删除。', confirmLabel: '永久删除' }, async () => {
    const response = await api.mutate(`/api/outbox/${alert.id}`, 'DELETE')
    await Promise.all([loadOutbox(outboxStatus), refresh(true)])
    if (response.removed !== true) throw new ApiError('这条通知的状态已改变，未执行删除。队列已刷新。', 409, 'invalid_state')
    notify('死信记录已删除')
  })

  const deleteReminder = (item: Reminder) => requestConfirmation({ kind: 'delete', title: `删除“${item.title}”？`, detail: '尚未开始发送的待发消息也会取消；已经送达 ntfy 的消息无法撤回。', confirmLabel: '确认删除' }, async () => {
    await api.mutate(`/api/reminders/${encodeURIComponent(item.id)}`, 'DELETE')
    await refresh(true)
    notify('提醒已删除')
  })

  const deleteSource = (source: ManagedSource) => requestConfirmation({ kind: 'delete', title: `删除“${source.publisher || source.id}”？`, detail: '这个监测来源及其关联规则会一起删除，随后等待 Argus 应用。', confirmLabel: '删除来源' }, async () => {
    const response = await api.configMutate(`/api/source-bundles/${encodeURIComponent(source.id)}`, 'DELETE', expectedRevision)
    markRestart(response.revision)
    await refresh(true)
    notify('来源和关联规则已删除；等待 Argus 应用')
  })

  const rollback = (revision: ConfigRevision) => requestConfirmation({ kind: 'rollback', title: `回滚到修订 ${revision.revision}？`, detail: '回滚会创建一个新修订，不会删除历史。Argus 需要重新加载后才会应用。', confirmLabel: '确认回滚' }, async () => {
    const response = await api.configMutate(`/api/revisions/${revision.revision}/rollback`, 'POST', expectedRevision, {})
    markRestart(response.revision)
    await refresh(true)
    notify(`已创建回滚修订；等待 Argus 应用`)
  })

  const loadAdvancedExample = () => setAdvancedJson(advancedKind === 'sources' ? JSON.stringify({ id: 'custom_source', kind: 'rss', publisher: '自定义来源', section: 'News', dedupe_scope: 'custom', url: 'https://example.com/feed.xml', allowed_hosts: ['example.com'], enabled: false, poll_interval_seconds: 300, request_timeout_seconds: 20, request_attempts: 3, retry_base_seconds: 2, max_response_bytes: 2_097_152, settings: {} }, null, 2) : JSON.stringify({ id: 'custom_notify', kind: 'weighted_text', source_ids: ['custom_source'], threshold: 1, max_item_age_seconds: 86_400, notification_title: '自定义提醒', priority: 3, tags: ['bell'], patterns: [{ label: '全部更新', regex: '.', title_weight: 2, summary_weight: 1 }] }, null, 2))

  const saveAdvanced = async () => {
    setBusy(true)
    try {
      const response = await api.configMutate(`/api/${advancedKind}`, 'POST', advancedDraftRevision.current ?? expectedRevision, JSON.parse(advancedJson) as unknown)
      advancedDraftRevision.current = null
      markRestart(response.revision)
      await refresh(true)
      notify('高级配置已保存；等待 Argus 应用')
    } catch (error) { await reportMutationError(error, '高级 JSON 无效') } finally { setBusy(false) }
  }

  const saveAnalysis = async (analysis: AnalysisConfig) => {
    setBusy(true)
    try {
      const response = await api.configMutate('/api/analysis', 'POST', expectedRevision, analysis)
      markRestart(response.revision)
      await refresh(true)
      notify('分析策略已保存；等待 Argus 应用')
      return true
    } catch (error) { await reportMutationError(error, '无法保存分析策略'); return false } finally { setBusy(false) }
  }

  const savePrompt = async (prompt: { prompt_id: string; version: number; system_text: string }) => {
    setBusy(true)
    try {
      await api.mutate('/api/prompts', 'POST', prompt)
      await refresh(true)
      notify(`Prompt ${prompt.prompt_id}@${prompt.version} 已保存`)
    } catch (error) { await reportMutationError(error, '无法保存 Prompt') } finally { setBusy(false) }
  }

  const saveDigest = async (digest: DigestConfig) => {
    setBusy(true)
    try {
      const response = await api.configMutate('/api/digest-config', 'POST', expectedRevision, digest)
      markRestart(response.revision)
      await refresh(true)
      notify('日报配置已保存；等待 Argus 应用')
      return true
    } catch (error) { await reportMutationError(error, '无法保存日报配置'); return false } finally { setBusy(false) }
  }

  const submitQualityFeedback = async (profile: SourceQualityProfile, signal: -1 | 0 | 1, reason: string) => {
    setBusy(true)
    try { await api.sourceQualityFeedback(profile.source_id, { signal, reason }); await refresh(true); notify(signal > 0 ? '已记录正反馈' : '已记录负反馈') }
    catch (error) { await reportMutationError(error, '无法记录信源反馈') }
    finally { setBusy(false) }
  }

  const setQualityOverride = async (profile: SourceQualityProfile, weight: number, reason: string) => {
    setBusy(true)
    try { await api.setSourceQualityOverride(profile.source_id, { weight, reason }); await refresh(true); notify('人工权重已保存') }
    catch (error) { await reportMutationError(error, '无法保存人工权重') }
    finally { setBusy(false) }
  }

  const clearQualityOverride = async (profile: SourceQualityProfile) => {
    setBusy(true)
    try { await api.clearSourceQualityOverride(profile.source_id); await refresh(true); notify('已恢复自动权重') }
    catch (error) { await reportMutationError(error, '无法清除人工权重') }
    finally { setBusy(false) }
  }

  if (authChecking || identity && !config) return <div className="loading-state full-page"><RefreshCw className="spin" size={24} />正在验证登录状态…</div>
  if (!authenticated) return <Login username={loginUsername} password={loginPassword} setUsername={setLoginUsername} setPassword={setLoginPassword} login={login} loading={loginBusy} error={loginError || resources.config.error || ''} />

  return <div className="app-shell">
    <aside className={`sidebar ${mobileNavOpen ? 'open' : ''}`} aria-label="主导航"><div className="sidebar-brand"><Brand /><button className="icon-button mobile-close" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)}><X size={18} /></button></div><nav className="main-nav" aria-label="后台导航">{pages.filter((item) => item.id !== 'users' || identity?.permissions.includes('users:manage')).map((item) => <button key={item.id} aria-current={page === item.id ? 'page' : undefined} className={page === item.id ? 'active' : ''} onClick={() => go(item.id)}><item.icon size={19} /><span>{item.label}</span></button>)}</nav><div className="sidebar-health"><span className={`health-dot ${health.level === 'healthy' ? 'ok' : health.level === 'attention' ? 'warning' : 'unknown'}`} /><div><strong>{identity?.display_name}</strong><span>{identity?.role} · {sourceStates.length} 个任务</span></div></div><button className="sidebar-logout" onClick={logout}><LogOut size={18} /><span>退出登录</span></button></aside>
    {mobileNavOpen && <button className="nav-backdrop" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)} />}
    <section className="workspace"><header className="topbar"><button className="icon-button mobile-menu" aria-label="打开导航" aria-expanded={mobileNavOpen} onClick={() => setMobileNavOpen(true)}><Menu size={20} /></button><div className="page-heading"><h1>{currentPage.label}</h1><p>{currentPage.subtitle}</p></div><div className="topbar-actions"><span className="connection-state"><span className={`health-dot ${health.level === 'healthy' ? 'ok' : health.level === 'attention' ? 'warning' : 'unknown'}`} />{health.level === 'healthy' ? '实时正常' : health.level === 'attention' ? '需要留意' : '待验证'}{lastUpdatedAt && <small> · {formatDate(lastUpdatedAt / 1000)}</small>}</span><button className="icon-button" aria-label={`切换为${theme === 'dark' ? '浅色' : '深色'}主题`} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</button><button className="icon-button" aria-label="刷新数据" disabled={refreshing} onClick={() => { void refreshNow() }}><RefreshCw className={refreshing ? 'spin' : ''} size={18} /></button></div></header>
      {restartRequired && <div className="restart-banner"><CircleAlert size={18} /><div><strong>有配置等待 Argus 应用</strong><span>{health.appliedRevision !== null ? `期望修订 ${effectivePendingRevision || health.desiredRevision}，引擎已应用 ${health.appliedRevision}。` : '引擎尚未报告已应用修订。'}</span></div><button className="button subtle" onClick={() => go('settings')}>查看状态</button></div>}
      {partialErrors.length > 0 && <div className="partial-error" role="status"><CircleAlert size={17} /><span>部分数据暂时无法刷新，已保留上次成功结果：{partialErrors.join('；')}</span></div>}
      <main className="page-content">{!hasAnyData ? <div className="loading-state"><RefreshCw className="spin" size={24} />正在读取管理数据…</div> : <>
        {page === 'overview' && <OverviewPage health={health} pending={pending} observations={Number(status.observations || 0)} sourceStates={sourceStates} managedSources={managedSources} incidents={incidents} reminders={reminders} go={(value) => go(value as PageId)} onCreateReminder={() => openReminder()} />}
        {page === 'reminders' && <RemindersPage reminders={reminders} total={resources.reminders.data?.pagination?.total} busy={busy || !can('reminders:write')} onOpen={openReminder} onToggle={(item) => { void toggleReminder(item) }} onDelete={deleteReminder} />}
        {page === 'sources' && <SourcesPage sources={managedSources} sourceStates={sourceStates} busy={busy || !can('sources:write')} query={sourceQuery} onQuery={setSourceQuery} onCreate={openSource} catalog={newsCatalog} catalogError={resources.newsCatalog.error} onCatalogFeed={prepareCatalogFeed} onEdit={editSource} onToggle={(source) => { void toggleSource(source) }} onDelete={deleteSource} />}
        {page === 'events' && <EventsPage api={api} onUnauthorized={logout} initialAlertId={/^\/events\/([1-9]\d*)$/.exec(window.location.pathname)?.[1]} incidents={incidents} managedSources={managedSources} total={resources.incidents.data?.pagination?.total} health={health} openCount={openIncidents} canCreate={can('events:write')} onCreated={() => { void refresh(true) }} notify={notify} />}
        {page === 'digests' && <DigestsPage api={api} onUnauthorized={logout} initialKey={window.location.pathname.startsWith('/digests/') ? decodeURIComponent(window.location.pathname.slice('/digests/'.length)) : undefined} />}
        {page === 'source-quality' && <SourceQualityPage profiles={sourceQuality} busy={busy || !can('quality:write')} onFeedback={submitQualityFeedback} onOverride={setQualityOverride} onClearOverride={clearQualityOverride} />}
        {page === 'users' && identity?.permissions.includes('users:manage') && <UsersPage api={api} currentUserId={identity.id} notify={notify} onUnauthorized={clearSession} />}
        {page === 'settings' && <><SettingsPage status={status} health={health} revisions={revisions} revisionTotal={resources.revisions.data?.pagination?.total} busy={busy || !can('settings:write')} analysis={config?.managed.analysis} digest={config?.managed.digest} prompts={prompts} analysisError={resources.prompts.error} onSaveAnalysis={saveAnalysis} onSaveDigest={saveDigest} onSavePrompt={(value) => { void savePrompt(value) }} advancedKind={advancedKind} advancedJson={advancedJson} onAdvancedKind={setAdvancedKind} onAdvancedJson={setAdvancedJson} onLoadExample={loadAdvancedExample} onSaveAdvanced={() => { void saveAdvanced() }} onRollback={rollback} /><OutboxPanel status={outboxStatus} alerts={outboxAlerts} loading={outboxLoading} error={outboxError} nextCursor={outboxNextCursor} busy={busy || !can('operations:write')} onStatus={changeOutboxStatus} onReload={() => { void loadOutbox(outboxStatus) }} onLoadMore={() => { void loadOutbox(outboxStatus, true, outboxNextCursor) }} onRetry={retryOutbox} onCancel={cancelOutbox} onDelete={discardOutbox} /></>}
      </>}</main>
    </section>
    <ReminderDialog ref={reminderDialog} draft={reminderForm} setDraft={setReminderForm} busy={busy} dirty={Boolean(reminderSnapshot && reminderSnapshot !== JSON.stringify(reminderForm))} onClose={closeReminder} onSubmit={() => { void saveReminder() }} />
    <SourceDialog ref={sourceDialog} draft={sourceForm} setDraft={setSourceForm} busy={busy} testing={sourceTesting} dirty={Boolean(sourceSnapshot && sourceSnapshot !== JSON.stringify(sourceForm))} onClose={closeSource} onSave={() => { void saveSource() }} onTest={() => { void testSource() }} onCancelTest={cancelSourceTest} onResetToPicker={resetSourcePicker} />
    <ConfirmDialog ref={confirmDialog} confirmation={confirmation} busy={busy} onCancel={() => { confirmDialog.current?.close(); setConfirmation(null); confirmAction.current = null }} onConfirm={() => { void runConfirmed() }} />
    <datalist id="timezone-list"><option value="Asia/Shanghai" /><option value="Asia/Hong_Kong" /><option value="UTC" /><option value="America/New_York" /><option value="Europe/London" /></datalist>
    {toast && <div className={`toast ${toast.tone}`} role={toast.tone === 'error' ? 'alert' : 'status'} aria-live={toast.tone === 'error' ? 'assertive' : 'polite'}>{toast.tone === 'success' ? <ShieldCheck size={19} /> : <CircleAlert size={19} />}<span>{toast.text}</span><button aria-label="关闭通知" onClick={() => setToast(null)}><X size={15} /></button></div>}
  </div>
}

function Login({ username, password, setUsername, setPassword, login, loading, error }: { username: string; password: string; setUsername: (value: string) => void; setPassword: (value: string) => void; login: () => Promise<void>; loading: boolean; error: string }) {
  return <div className="login-shell"><div className="argus-field" aria-hidden="true"><span /><span /><span /><span /><span /></div><main className="login-card"><EyeMark size="large" /><div className="login-copy"><span className="eyebrow">ARGUS WATCHER CONSOLE</span><h1>欢迎回到 EyeOfSauron</h1><p>使用后台账户登录。登录状态由安全 Cookie 保存，最长有效 14 天。</p></div><form className="login-form" onSubmit={(event) => { event.preventDefault(); void login() }}><label htmlFor="admin-username">用户名</label><div className="input-with-icon"><UserRoundCog size={18} /><input id="admin-username" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" autoFocus required /></div><label htmlFor="admin-password">密码</label><div className="input-with-icon"><LockKeyhole size={18} /><input id="admin-password" value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" required /></div>{error && <p className="form-error" role="alert">{error}</p>}<button className="button primary wide" type="submit" disabled={loading}>{loading ? <RefreshCw className="spin" size={18} /> : <ShieldCheck size={18} />}{loading ? '正在验证…' : '进入后台'}</button></form><div className="login-security"><ShieldCheck size={16} /><span>TLS 加密 · 防暴力尝试 · 服务端会话可随时撤销</span></div></main></div>
}
