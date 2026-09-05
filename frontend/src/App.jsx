import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity, AlarmClock, BellRing, CalendarClock, Check, CheckCircle2,
  ChevronRight, CircleAlert, Clock3, Database, FileClock, History, Inbox,
  LayoutDashboard, LockKeyhole, LogOut, Mail, Menu, Moon, Newspaper,
  Pencil, Plus, RadioTower, Radar, RefreshCw, Rss, Search, Send,
  Settings2, ShieldCheck, Sun, Trash2, TrendingUp, Twitter, X, Youtube,
} from 'lucide-react'

const pages = [
  { id: 'overview', label: '概览', subtitle: '重要的事情，一眼就能看到', icon: LayoutDashboard },
  { id: 'reminders', label: '提醒', subtitle: '安排未来要发送的消息', icon: BellRing },
  { id: 'sources', label: '监测源', subtitle: '决定 EyeOfSauron 要关注什么', icon: Radar },
  { id: 'events', label: '事件', subtitle: '异常、恢复和重要动态', icon: Inbox },
  { id: 'settings', label: '设置', subtitle: '通知、历史和高级选项', icon: Settings2 },
]

const sourceKinds = [
  { value: 'rss', label: 'RSS / Atom', description: '新闻网站、博客和公告', icon: Rss },
  { value: 'youtube', label: 'YouTube', description: '频道发布新视频时提醒', icon: Youtube },
  { value: 'x', label: 'X 账号', description: '指定账号发布内容时提醒', icon: Twitter },
  { value: 'market', label: '股票', description: '美股价格或成交量异动', icon: TrendingUp },
  { value: 'imap', label: '邮箱', description: '符合搜索条件的新邮件', icon: Mail },
]

const knownSources = {
  bloomberg_markets: 'Bloomberg Markets',
  bloomberg_politics: 'Bloomberg Politics',
  bloomberg_technology: 'Bloomberg Technology',
  bloomberg_economics: 'Bloomberg Economics',
}

const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai'

function dateTimeValue(epoch) {
  const date = new Date((epoch || Math.floor(Date.now() / 1000) + 3600) * 1000)
  const pad = (value) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function formatDate(epoch) {
  if (!epoch) return '尚无记录'
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(epoch * 1000))
}

function relativeTime(epoch) {
  if (!epoch) return '尚未运行'
  const seconds = Math.round(epoch - Date.now() / 1000)
  const absolute = Math.abs(seconds)
  if (absolute < 60) return seconds > 0 ? '不到 1 分钟后' : '刚刚'
  if (absolute < 3600) return `${Math.round(absolute / 60)} 分钟${seconds > 0 ? '后' : '前'}`
  if (absolute < 86400) return `${Math.round(absolute / 3600)} 小时${seconds > 0 ? '后' : '前'}`
  return `${Math.round(absolute / 86400)} 天${seconds > 0 ? '后' : '前'}`
}

function emptyReminder() {
  return {
    id: '', title: '定时提醒', message: '', schedule_kind: 'once',
    run_at_local: dateTimeValue(), delay_value: 30, delay_unit: 60,
    daily_time: '09:00', timezone: browserZone, priority: 3, enabled: true,
  }
}

function emptySource() {
  return {
    editing: false, id: `source_${Date.now().toString(36)}`, kind: 'rss',
    publisher: '', section: 'News', enabled: false, target: '', keywords: '', excludes: '',
    updateRule: true, apiBaseUrl: 'https://data.alpaca.markets', apiKeyEnv: 'ALPACA_API_KEY',
    apiSecretEnv: 'ALPACA_API_SECRET', threshold: 5, volume: 3, gap: 3, cooldown: 1800,
    bearerTokenEnv: 'X_BEARER_TOKEN', mailbox: 'INBOX', usernameEnv: 'IMAP_USERNAME',
    passwordEnv: 'IMAP_PASSWORD', search: 'ALL', showAdvanced: false,
  }
}

function sourceMeta(kind) {
  return sourceKinds.find((item) => item.value === kind) || { label: kind, icon: RadioTower }
}

function incidentMeta(incident) {
  if (incident.status === 'recorded') return { label: '已记录', tone: 'info', Icon: Newspaper }
  if (incident.status === 'recovered') return { label: '已恢复', tone: 'positive', Icon: CheckCircle2 }
  return { label: '进行中', tone: 'negative', Icon: CircleAlert }
}

function splitWords(value) {
  return String(value || '').split(/[,，\n]/).map((item) => item.trim()).filter(Boolean)
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function App() {
  const [auth, setAuth] = useState(() => sessionStorage.getItem('eosAdminToken') || '')
  const [token, setToken] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [loginError, setLoginError] = useState('')
  const [config, setConfig] = useState(null)
  const [reminders, setReminders] = useState([])
  const [revisions, setRevisions] = useState([])
  const [incidents, setIncidents] = useState([])
  const [page, setPage] = useState(() => window.location.hash.replace('#/', '') || 'overview')
  const [theme, setTheme] = useState(() => localStorage.getItem('eosTheme') || (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'))
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [restartRequired, setRestartRequired] = useState(false)
  const [reminderFilter, setReminderFilter] = useState('upcoming')
  const [incidentFilter, setIncidentFilter] = useState('all')
  const [sourceQuery, setSourceQuery] = useState('')
  const [toast, setToast] = useState(null)
  const [reminderForm, setReminderForm] = useState(emptyReminder)
  const [sourceForm, setSourceForm] = useState(emptySource)
  const [advancedKind, setAdvancedKind] = useState('sources')
  const [advancedJson, setAdvancedJson] = useState('')
  const reminderDialog = useRef(null)
  const sourceDialog = useRef(null)
  const deleteDialog = useRef(null)
  const [pendingDelete, setPendingDelete] = useState({ type: '', id: '', title: '' })

  const notify = useCallback((text, tone = 'success') => {
    setToast({ text, tone })
    window.setTimeout(() => setToast(null), 4200)
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('eosTheme', theme)
  }, [theme])

  useEffect(() => {
    const handleHash = () => {
      const next = window.location.hash.replace('#/', '')
      if (pages.some((item) => item.id === next)) setPage(next)
    }
    window.addEventListener('hashchange', handleHash)
    return () => window.removeEventListener('hashchange', handleHash)
  }, [])

  const api = useCallback(async (path, options = {}, tokenOverride = auth) => {
    const headers = { Authorization: `Bearer ${tokenOverride}`, ...(options.headers || {}) }
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json'
    const response = await fetch(path, { ...options, headers })
    const body = await response.json().catch(() => ({ error: '服务器返回了无法识别的响应' }))
    if (!response.ok) {
      if (response.status === 401) throw new Error('管理 Token 不正确或已失效')
      throw new Error(body.error || `请求失败（${response.status}）`)
    }
    return body
  }, [auth])

  const refresh = useCallback(async (quiet = false, tokenOverride = auth) => {
    if (!quiet) setRefreshing(true)
    try {
      const [configuration, reminderData, revisionData, incidentData] = await Promise.all([
        api('/api/config', {}, tokenOverride), api('/api/reminders', {}, tokenOverride), api('/api/revisions', {}, tokenOverride), api('/api/incidents', {}, tokenOverride),
      ])
      setConfig(configuration)
      setReminders(reminderData.reminders || [])
      setRevisions(revisionData.revisions || [])
      setIncidents(incidentData.incidents || [])
      setAuthenticated(true)
    } finally {
      if (!quiet) setRefreshing(false)
    }
  }, [api])

  useEffect(() => {
    if (!auth) return
    setLoading(true)
    refresh(true).catch(() => {
      sessionStorage.removeItem('eosAdminToken')
      setAuth('')
      setAuthenticated(false)
    }).finally(() => setLoading(false))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const status = config?.status || {}
  const managedSources = config?.managed?.sources || []
  const sourceStates = status.sources || []
  const activeReminders = reminders.filter((item) => item.enabled && item.next_run_at)
  const nextReminder = activeReminders[0]
  const openIncidents = Number(status.incidents?.open || 0)
  const unhealthySources = sourceStates.filter((item) => Number(item.consecutive_failures || 0) > 0 || item.outage_alerted)
  const healthySourceCount = Math.max(0, sourceStates.length - unhealthySources.length)
  const healthy = openIncidents === 0 && unhealthySources.length === 0
  const pending = Number(status.outbox?.pending || 0) + Number(status.outbox?.sending || 0)
  const filteredReminders = reminders.filter((item) => {
    if (reminderFilter === 'upcoming') return Boolean(item.enabled && item.next_run_at)
    if (reminderFilter === 'completed') return Boolean(item.completed_at || (!item.enabled && !item.next_run_at))
    return true
  })
  const filteredSources = managedSources.filter((source) => {
    const query = sourceQuery.trim().toLowerCase()
    return !query || [source.publisher, source.id, source.kind, source.section].some((value) => String(value || '').toLowerCase().includes(query))
  })
  const visibleIncidents = incidents.filter((incident) => incidentFilter === 'all' || incident.status === incidentFilter)
  const currentPage = pages.find((item) => item.id === page) || pages[0]

  const go = (nextPage) => {
    setPage(nextPage)
    setMobileNavOpen(false)
    window.location.hash = `/${nextPage}`
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const login = async () => {
    setLoginError('')
    if (!token.trim()) return
    setAuth(token.trim())
    setLoading(true)
    try {
      const headers = { Authorization: `Bearer ${token.trim()}` }
      const response = await fetch('/api/config', { headers })
      if (!response.ok) throw new Error('管理 Token 不正确或已失效')
      sessionStorage.setItem('eosAdminToken', token.trim())
      setToken('')
      await refresh(true, token.trim())
    } catch (error) {
      setLoginError(error.message)
      setAuth('')
    } finally {
      setLoading(false)
    }
  }

  const logout = () => {
    sessionStorage.removeItem('eosAdminToken')
    setAuth('')
    setAuthenticated(false)
    setConfig(null)
  }

  const refreshNow = async () => {
    try { await refresh(); notify('数据已刷新') } catch (error) { notify(error.message, 'error') }
  }

  const openReminder = (item = null) => {
    const next = emptyReminder()
    if (item) {
      Object.assign(next, {
        id: item.id, title: item.title, message: item.message, priority: Number(item.priority),
        enabled: Boolean(item.enabled), schedule_kind: item.schedule_kind, timezone: item.timezone || browserZone,
        run_at_local: dateTimeValue(item.run_at), daily_time: item.daily_time || '09:00',
      })
    }
    setReminderForm(next)
    reminderDialog.current?.showModal()
  }

  const saveReminder = async (event) => {
    event.preventDefault()
    setBusy(true)
    try {
      const payload = {
        title: reminderForm.title.trim(), message: reminderForm.message.trim(), schedule_kind: reminderForm.schedule_kind,
        timezone: reminderForm.timezone.trim(), enabled: reminderForm.enabled, priority: Number(reminderForm.priority), tags: ['alarm_clock'],
      }
      if (reminderForm.id) payload.id = reminderForm.id
      if (reminderForm.schedule_kind === 'once') payload.run_at = Math.floor(new Date(reminderForm.run_at_local).getTime() / 1000)
      else if (reminderForm.schedule_kind === 'after') payload.delay_seconds = Math.round(Number(reminderForm.delay_value) * Number(reminderForm.delay_unit))
      else payload.daily_time = reminderForm.daily_time
      await api('/api/reminders', { method: 'POST', body: JSON.stringify(payload) })
      reminderDialog.current?.close()
      await refresh(true)
      notify(reminderForm.id ? '提醒已更新' : '提醒已创建，会按计划发送到 ntfy')
    } catch (error) { notify(error.message, 'error') } finally { setBusy(false) }
  }

  const toggleReminder = async (item) => {
    setBusy(true)
    try {
      await api(`/api/reminders/${encodeURIComponent(item.id)}/${item.enabled ? 'disable' : 'enable'}`, { method: 'POST', body: '{}' })
      await refresh(true)
      notify(item.enabled ? '提醒已停用' : '提醒已启用')
    } catch (error) { notify(error.message, 'error') } finally { setBusy(false) }
  }

  const openSource = (source = null, kind = null) => {
    const next = emptySource()
    if (kind) next.kind = kind
    if (source) {
      const settings = source.settings || {}
      Object.assign(next, { editing: true, id: source.id, kind: source.kind, publisher: source.publisher || '', section: source.section || '', enabled: source.enabled !== false, updateRule: false })
      if (source.kind === 'rss') next.target = source.url || ''
      if (source.kind === 'youtube') next.target = settings.channel_id || ''
      if (source.kind === 'x') { next.target = settings.user_id || ''; next.bearerTokenEnv = settings.bearer_token_env || next.bearerTokenEnv }
      if (source.kind === 'market') { next.target = (settings.symbols || []).join(', '); next.apiBaseUrl = settings.api_base_url || next.apiBaseUrl; next.apiKeyEnv = settings.api_key_env || next.apiKeyEnv; next.apiSecretEnv = settings.api_secret_env || next.apiSecretEnv; next.threshold = settings.price_change_threshold ?? next.threshold; next.volume = settings.volume_multiplier ?? next.volume; next.gap = settings.gap_threshold ?? next.gap; next.cooldown = settings.cooldown_seconds ?? next.cooldown }
      if (source.kind === 'imap') { next.target = settings.host || ''; next.mailbox = settings.mailbox || next.mailbox; next.usernameEnv = settings.username_env || next.usernameEnv; next.passwordEnv = settings.password_env || next.passwordEnv; next.search = settings.search || next.search }
    }
    setSourceForm(next)
    sourceDialog.current?.showModal()
  }

  const sourcePayload = () => {
    const source = { id: sourceForm.id.trim(), kind: sourceForm.kind, publisher: sourceForm.publisher.trim(), section: sourceForm.section.trim(), dedupe_scope: sourceForm.id.trim(), enabled: sourceForm.enabled, poll_interval_seconds: 300, request_timeout_seconds: 20, request_attempts: 3, retry_base_seconds: 2, max_response_bytes: 2097152, settings: {} }
    const target = sourceForm.target.trim()
    if (sourceForm.kind === 'rss') { const url = new URL(target); source.url = target; source.allowed_hosts = [url.hostname] }
    else if (sourceForm.kind === 'youtube') source.settings = { channel_id: target }
    else if (sourceForm.kind === 'x') source.settings = { user_id: target, bearer_token_env: sourceForm.bearerTokenEnv.trim() }
    else if (sourceForm.kind === 'market') source.settings = { api_base_url: sourceForm.apiBaseUrl.trim(), path_template: '/v2/stocks/{symbol}/snapshot', api_key_env: sourceForm.apiKeyEnv.trim(), api_secret_env: sourceForm.apiSecretEnv.trim(), symbols: splitWords(target).map((item) => item.toUpperCase()), price_change_threshold: Number(sourceForm.threshold), volume_multiplier: Number(sourceForm.volume), gap_threshold: Number(sourceForm.gap), cooldown_seconds: Number(sourceForm.cooldown) }
    else source.settings = { host: target, port: 993, mailbox: sourceForm.mailbox.trim(), search: sourceForm.search.trim(), username_env: sourceForm.usernameEnv.trim(), password_env: sourceForm.passwordEnv.trim() }
    return source
  }

  const rulePayload = (source) => {
    const include = splitWords(sourceForm.keywords)
    const exclude = splitWords(sourceForm.excludes)
    const patterns = [{ label: include.length ? '关注关键词' : '全部更新', regex: include.length ? `(?i)(?:${include.map(escapeRegex).join('|')})` : '.', title_weight: 2, summary_weight: 1 }]
    if (exclude.length) patterns.push({ label: '排除内容', regex: `(?i)(?:${exclude.map(escapeRegex).join('|')})`, title_weight: -100, summary_weight: -100 })
    return { id: `${source.id}_notify`, kind: 'weighted_text', source_ids: [source.id], threshold: 1, max_item_age_seconds: 86400, notification_title: source.kind === 'market' ? '股票异动' : `${source.publisher} 更新`, priority: source.kind === 'market' ? 4 : 3, tags: source.kind === 'market' ? ['chart_with_upwards_trend'] : ['bell'], patterns }
  }

  const saveSource = async (event) => {
    event.preventDefault()
    setBusy(true)
    try {
      const source = sourcePayload()
      await api('/api/source-bundles', { method: 'POST', body: JSON.stringify({ source, rule: (!sourceForm.editing || sourceForm.updateRule) ? rulePayload(source) : null }) })
      sourceDialog.current?.close()
      setRestartRequired(true)
      await refresh(true)
      notify(sourceForm.editing ? '监测源已更新；重启主服务后生效' : '监测源已添加；重启主服务后生效')
    } catch (error) { notify(error.message, 'error') } finally { setBusy(false) }
  }

  const testSource = async () => {
    setBusy(true)
    try { const result = await api('/api/test-source', { method: 'POST', body: JSON.stringify({ source: sourcePayload() }) }); notify(`连接成功：读取到 ${result.observations} 条新记录，耗时 ${result.elapsed_ms} 毫秒`) }
    catch (error) { notify(`连接测试失败：${error.message}`, 'error') } finally { setBusy(false) }
  }

  const toggleSource = async (source) => {
    setBusy(true)
    try { await api(`/api/sources/${encodeURIComponent(source.id)}/${source.enabled === false ? 'enable' : 'disable'}`, { method: 'POST', body: '{}' }); setRestartRequired(true); await refresh(true); notify(source.enabled === false ? '监测源已启用；重启主服务后生效' : '监测源已停用；重启主服务后生效') }
    catch (error) { notify(error.message, 'error') } finally { setBusy(false) }
  }

  const remove = async () => {
    setBusy(true)
    try {
      if (pendingDelete.type === 'reminder') await api(`/api/reminders/${encodeURIComponent(pendingDelete.id)}`, { method: 'DELETE' })
      else { await api(`/api/source-bundles/${encodeURIComponent(pendingDelete.id)}`, { method: 'DELETE' }); setRestartRequired(true) }
      deleteDialog.current?.close(); await refresh(true); notify(pendingDelete.type === 'reminder' ? '提醒已删除' : '监测源和关联规则已删除；重启主服务后生效')
    } catch (error) { notify(error.message, 'error') } finally { setBusy(false) }
  }

  const incidentTitle = (incident) => {
    if (incident.latest_title) return incident.latest_title
    const names = (incident.source_ids || []).map((id) => managedSources.find((source) => source.id === id)?.publisher || knownSources[id] || id)
    if (incident.status === 'recovered') return `${names.join('、') || '异常'} 已恢复`
    return incident.status === 'recorded' ? `${names.join('、') || '系统'} 重要动态` : `${names.join('、') || '系统'} 需要留意`
  }

  if (!authenticated) return <Login token={token} setToken={setToken} login={login} loading={loading} error={loginError} />

  return <div className="app-shell">
    <aside className={`sidebar ${mobileNavOpen ? 'open' : ''}`}>
      <div className="brand"><div className="brand-mark"><RadioTower size={20} /></div><div><strong>EyeOfSauron</strong><span>powered by Argus</span></div><button className="icon-button mobile-close" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)}><X size={18} /></button></div>
      <nav className="main-nav" aria-label="后台导航">{pages.map((item) => <button key={item.id} className={page === item.id ? 'active' : ''} onClick={() => go(item.id)}><item.icon size={19} /><span>{item.label}</span><ChevronRight className="nav-chevron" size={16} /></button>)}</nav>
      <div className="sidebar-health"><span className={`health-dot ${healthy ? 'ok' : 'warning'}`}></span><div><strong>{healthy ? '运行正常' : '有事项需留意'}</strong><span>{sourceStates.length} 个监测任务</span></div></div>
      <button className="sidebar-logout" onClick={logout}><LogOut size={18} /><span>锁定后台</span></button>
    </aside>
    {mobileNavOpen && <button className="nav-backdrop" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)} />}
    <section className="workspace">
      <header className="topbar"><button className="icon-button mobile-menu" aria-label="打开导航" onClick={() => setMobileNavOpen(true)}><Menu size={20} /></button><div className="page-heading"><h1>{currentPage.label}</h1><p>{currentPage.subtitle}</p></div><div className="topbar-actions"><span className="connection-state"><span className={`health-dot ${healthy ? 'ok' : 'warning'}`}></span>{healthy ? '系统正常' : '需要留意'}</span><button className="icon-button" aria-label="切换主题" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</button><button className="icon-button" aria-label="刷新数据" disabled={refreshing} onClick={refreshNow}><RefreshCw className={refreshing ? 'spin' : ''} size={18} /></button></div></header>
      {restartRequired && <div className="restart-banner"><CircleAlert size={18} /><div><strong>监测配置已更改</strong><span>新配置需要重启 Argus 引擎后才会开始运行；提醒功能不受影响。</span></div><button className="button subtle" onClick={() => setRestartRequired(false)}>知道了</button></div>}
      <main className="page-content">{page === 'overview' && <Overview healthy={healthy} pending={pending} status={status} sourceStates={sourceStates} nextReminder={nextReminder} activeReminders={activeReminders} healthySourceCount={healthySourceCount} unhealthySources={unhealthySources} openIncidents={openIncidents} go={go} openReminder={openReminder} managedSources={managedSources} incidents={incidents} incidentTitle={incidentTitle} />}{page === 'reminders' && <Reminders reminders={reminders} filtered={filteredReminders} filter={reminderFilter} setFilter={setReminderFilter} open={openReminder} toggle={toggleReminder} busy={busy} requestDelete={(item) => { setPendingDelete({ type: 'reminder', id: item.id, title: item.title }); deleteDialog.current?.showModal() }} />}{page === 'sources' && <Sources sources={managedSources} filtered={filteredSources} sourceStates={sourceStates} query={sourceQuery} setQuery={setSourceQuery} open={openSource} toggle={toggleSource} requestDelete={(source) => { setPendingDelete({ type: 'source', id: source.id, title: source.publisher || source.id }); deleteDialog.current?.showModal() }} />}{page === 'events' && <Events incidents={visibleIncidents} filter={incidentFilter} setFilter={setIncidentFilter} healthy={healthy} openCount={openIncidents} title={incidentTitle} managedSources={managedSources} />}{page === 'settings' && <Settings status={status} revisions={revisions} config={config} busy={busy} rollback={async (revision) => { if (!window.confirm(`确定回滚到修订 ${revision}？`)) return; setBusy(true); try { await api(`/api/revisions/${revision}/rollback`, { method: 'POST', body: '{}' }); setRestartRequired(true); await refresh(true); notify(`已回滚到修订 ${revision}；重启主服务后生效`) } catch (error) { notify(error.message, 'error') } finally { setBusy(false) } }} advancedKind={advancedKind} setAdvancedKind={setAdvancedKind} advancedJson={advancedJson} setAdvancedJson={setAdvancedJson} loadExample={() => setAdvancedJson(advancedKind === 'sources' ? JSON.stringify({ id: 'custom_source', kind: 'rss', publisher: '自定义来源', section: 'News', dedupe_scope: 'custom', url: 'https://example.com/feed.xml', allowed_hosts: ['example.com'], enabled: false, poll_interval_seconds: 300, request_timeout_seconds: 20, request_attempts: 3, retry_base_seconds: 2, max_response_bytes: 2097152, settings: {} }, null, 2) : JSON.stringify({ id: 'custom_notify', kind: 'weighted_text', source_ids: ['custom_source'], threshold: 1, max_item_age_seconds: 86400, notification_title: '自定义提醒', priority: 3, tags: ['bell'], patterns: [{ label: '全部更新', regex: '.', title_weight: 2, summary_weight: 1 }] }, null, 2))} saveAdvanced={async () => { try { await api(`/api/${advancedKind}`, { method: 'POST', body: JSON.stringify(JSON.parse(advancedJson)) }); setRestartRequired(true); await refresh(true); notify('高级配置已保存；重启主服务后生效') } catch (error) { notify(error.message, 'error') } }} />}</main>
    </section>
    <ReminderDialog ref={reminderDialog} form={reminderForm} setForm={setReminderForm} onSubmit={saveReminder} busy={busy} />
    <SourceDialog ref={sourceDialog} form={sourceForm} setForm={setSourceForm} onSubmit={saveSource} test={testSource} busy={busy} />
    <dialog ref={deleteDialog} className="modal small-modal"><form className="modal-card" onSubmit={(event) => { event.preventDefault(); remove() }}><div className="confirm-body"><div className="confirm-icon"><Trash2 size={23} /></div><h2>删除“{pendingDelete.title}”？</h2><p>{pendingDelete.type === 'reminder' ? '尚未开始发送的待发消息也会取消；已经送达 ntfy 的消息无法撤回。' : '这个监测源及其关联通知规则会一起删除。重启主服务后生效。'}</p></div><footer className="dialog-actions"><button className="button subtle" type="button" onClick={() => deleteDialog.current?.close()}>取消</button><button className="button danger-fill" type="submit" disabled={busy}>确认删除</button></footer></form></dialog>
    {toast && <div className={`toast ${toast.tone}`} role="status">{toast.tone === 'success' ? <CheckCircle2 size={19} /> : <CircleAlert size={19} />}<span>{toast.text}</span></div>}
  </div>
}

function Login({ token, setToken, login, loading, error }) {
  return <div className="login-shell"><div className="login-atmosphere login-atmosphere-one"></div><div className="login-atmosphere login-atmosphere-two"></div><main className="login-card"><div className="brand-mark large"><RadioTower size={25} /></div><div className="login-copy"><span className="eyebrow">私人消息雷达</span><h1>欢迎回到 EyeOfSauron</h1><p>用管理 Token 解锁。凭据只保存在当前浏览器标签页，关闭后会自动清除。</p></div><form className="login-form" onSubmit={(event) => { event.preventDefault(); login() }}><label htmlFor="admin-token">管理 Token</label><div className="input-with-icon"><LockKeyhole size={18} /><input id="admin-token" value={token} onChange={(event) => setToken(event.target.value)} type="password" autoComplete="current-password" autoFocus required placeholder="粘贴本机生成的 Token" /></div>{error && <p className="form-error" role="alert">{error}</p>}<button className="button primary wide" type="submit" disabled={loading}>{loading ? <RefreshCw className="spin" size={18} /> : <ShieldCheck size={18} />}{loading ? '正在验证…' : '进入后台'}</button></form><div className="login-security"><ShieldCheck size={16} /><span>后台仍只监听本机回环地址，不增加公网入口</span></div></main></div>
}

function Overview({ healthy, pending, status, sourceStates, nextReminder, activeReminders, healthySourceCount, unhealthySources, openIncidents, go, openReminder, managedSources, incidents, incidentTitle }) {
  const sourceName = (id) => managedSources.find((source) => source.id === id)?.publisher || knownSources[id] || id
  return <>
    <section className="hero-grid">
      <article className={`health-hero ${healthy ? 'healthy' : 'attention'}`}>
        <div>
          <span className="eyebrow">系统健康</span>
          <h2>{healthy ? '一切运行正常' : '有新的情况需要留意'}</h2>
          <p>{healthy ? `监测任务运行稳定，当前没有进行中的异常，通知队列${pending ? `有 ${pending} 条待处理` : '为空'}。` : `${unhealthySources.length} 个监测任务正在重试，${openIncidents} 个异常仍在进行。`}</p>
        </div>
        <div className="hero-icon"><Activity size={31} /></div>
      </article>
      <article className="next-card">
        <div className="card-title-row"><span className="eyebrow">下一条提醒</span><AlarmClock size={19} /></div>
        {nextReminder ? <>
          <h3>{nextReminder.title}</h3>
          <p>{nextReminder.message}</p>
          <button className="time-link" onClick={() => go('reminders')}>{formatDate(nextReminder.next_run_at)}<ChevronRight size={16} /></button>
        </> : <>
          <h3>还没有安排提醒</h3>
          <p>可以设定未来某天、多久以后或每天固定时间。</p>
          <button className="time-link" onClick={() => openReminder()}>现在创建<ChevronRight size={16} /></button>
        </>}
      </article>
    </section>
    <section className="stats-grid">
      <article className="stat-card"><span>启用的提醒</span><strong>{activeReminders.length}</strong><small>{status.reminders?.disabled || 0} 条未启用</small></article>
      <article className="stat-card"><span>正常监测源</span><strong>{healthySourceCount} / {sourceStates.length}</strong><small>{unhealthySources.length ? `${unhealthySources.length} 个正在重试` : '全部运行稳定'}</small></article>
      <article className="stat-card"><span>已收集消息</span><strong>{Number(status.observations || 0).toLocaleString('zh-CN')}</strong><small>按保留策略自动清理</small></article>
      <article className="stat-card"><span>待发送</span><strong>{pending}</strong><small>{pending ? '正在可靠投递' : '队列为空'}</small></article>
    </section>
    <section className="content-grid">
      <article className="panel">
        <div className="panel-header"><div><h2>监测源状态</h2><p>最近一次采集结果</p></div><button className="text-button" onClick={() => go('sources')}>查看全部<ChevronRight size={15} /></button></div>
        <div className="list-stack">{sourceStates.length ? sourceStates.slice(0, 5).map((source) => <div key={source.source_id} className="source-row">
          <div className="source-icon"><Newspaper size={18} /></div>
          <div className="row-main"><strong>{sourceName(source.source_id)}</strong><span>{source.last_success_at ? `${relativeTime(source.last_success_at)}成功` : '等待首次检查'}</span></div>
          <span className={`status-pill ${source.outage_alerted ? 'negative' : Number(source.consecutive_failures || 0) ? 'warning' : 'positive'}`}>{source.outage_alerted ? '异常' : Number(source.consecutive_failures || 0) ? '重试中' : '正常'}</span>
        </div>) : <div className="empty-compact"><Radar size={22} /><span>服务启动后会在这里显示监测状态</span></div>}</div>
      </article>
      <article className="panel">
        <div className="panel-header"><div><h2>最近事件</h2><p>重要动态、异常与恢复</p></div><button className="text-button" onClick={() => go('events')}>全部事件<ChevronRight size={15} /></button></div>
        {incidents.length ? <div className="timeline-list">{incidents.slice(0, 4).map((incident) => {
          const meta = incidentMeta(incident)
          return <div key={incident.id} className="timeline-row"><div className={`timeline-icon ${meta.tone}`}><meta.Icon size={17} /></div><div><strong>{incidentTitle(incident)}</strong><span>{meta.label} · {formatDate(incident.last_seen_at)}</span></div></div>
        })}</div> : <div className="empty-compact positive"><CheckCircle2 size={22} /><span>目前没有重要事件记录</span></div>}
      </article>
    </section>
  </>
}

function Reminders({ reminders, filtered, filter, setFilter, open, toggle, busy, requestDelete }) {
  return <><div className="page-actions"><div><h2>定时提醒</h2><p>保存后立即生效，不需要重启服务。</p></div><button className="button primary" onClick={() => open()}><Plus size={18} />新建提醒</button></div><div className="notice-card"><Send size={19} /><div><strong>发送到 eos 主题</strong><span>所有已经订阅这个主题的 ntfy 客户端都会收到。</span></div></div><div className="filter-row"><div className="segmented"><button className={filter === 'upcoming' ? 'active' : ''} onClick={() => setFilter('upcoming')}>即将到来</button><button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>全部</button><button className={filter === 'completed' ? 'active' : ''} onClick={() => setFilter('completed')}>已完成</button></div><span className="result-count">{filtered.length} 条</span></div>{filtered.length ? <section className="panel reminder-list">{filtered.map((item) => <article key={item.id} className="reminder-row"><div className="date-tile"><span>{item.schedule_kind === 'daily' ? '每天' : new Intl.DateTimeFormat('zh-CN', { month: 'short' }).format(new Date((item.next_run_at || item.run_at) * 1000))}</span><strong>{item.schedule_kind === 'daily' ? item.daily_time.slice(0, 2) : new Intl.DateTimeFormat('zh-CN', { day: '2-digit' }).format(new Date((item.next_run_at || item.run_at) * 1000))}</strong></div><div className="reminder-copy"><div className="row-title"><h3>{item.title}</h3><span className={`status-pill ${item.enabled && item.next_run_at ? 'positive' : 'neutral'}`}>{item.next_run_at ? `下次 ${relativeTime(item.next_run_at)}` : item.completed_at ? '已完成' : item.enabled ? '已启用' : '已停用'}</span></div><p>{item.message}</p><div className="meta-line"><CalendarClock size={15} />{item.schedule_kind === 'daily' ? `每天 ${item.daily_time} · ${item.timezone}` : formatDate(item.run_at)}<span>·</span>优先级 {item.priority}</div></div><div className="row-actions"><button className="icon-button" aria-label="编辑提醒" onClick={() => open(item)}><Pencil size={17} /></button><button className="button subtle" disabled={busy || (!item.enabled && item.completed_at)} onClick={() => toggle(item)}>{item.enabled ? '停用' : '启用'}</button><button className="icon-button danger" aria-label="删除提醒" onClick={() => requestDelete(item)}><Trash2 size={17} /></button></div></article>)}</section> : <section className="empty-state"><div className="empty-icon"><BellRing size={29} /></div><h3>{filter === 'upcoming' ? '没有即将发送的提醒' : '这里还没有提醒'}</h3><p>需要记住的事情交给 EyeOfSauron，到时间会通过 ntfy 告诉你。</p><button className="button primary" onClick={() => open()}><Plus size={18} />新建提醒</button></section>}</>
}

function Sources({ sources, filtered, sourceStates, query, setQuery, open, toggle, requestDelete }) {
  const state = (id) => sourceStates.find((item) => item.source_id === id)
  const tone = (source) => source.enabled === false ? 'neutral' : state(source.id)?.outage_alerted ? 'negative' : Number(state(source.id)?.consecutive_failures || 0) ? 'warning' : 'positive'
  const label = (source) => source.enabled === false ? '已停用' : state(source.id)?.outage_alerted ? '异常' : Number(state(source.id)?.consecutive_failures || 0) ? '重试中' : state(source.id) ? '正常' : '等待启动'
  return <><div className="page-actions"><div><h2>监测源</h2><p>添加想关注的内容；技术配置只在需要时出现。</p></div><button className="button primary" onClick={() => open()}><Plus size={18} />添加监测</button></div><div className="source-kind-grid">{sourceKinds.map((kind) => <button key={kind.value} className="source-kind-card" onClick={() => open(null, kind.value)}><span className="source-kind-icon"><kind.icon size={21} /></span><span><strong>{kind.label}</strong><small>{kind.description}</small></span><Plus size={17} /></button>)}</div><div className="filter-row source-filter"><div className="search-field"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} type="search" placeholder="搜索已管理的监测源" /></div><span className="result-count">{sources.length} 个自定义来源</span></div><section className="source-section"><div className="section-heading"><div><h3>内置新闻源</h3><p>基础配置由服务器维护，后台不会误改。</p></div><LockKeyhole size={18} /></div><div className="source-cards">{sourceStates.filter((item) => !sources.some((source) => source.id === item.source_id)).map((item) => <article key={item.source_id} className="source-card"><div className="source-card-top"><div className="source-icon large"><Newspaper size={20} /></div><span className={`status-pill ${item.outage_alerted ? 'negative' : Number(item.consecutive_failures || 0) ? 'warning' : 'positive'}`}>{item.outage_alerted ? '异常' : Number(item.consecutive_failures || 0) ? '重试中' : '正常'}</span></div><h3>{knownSources[item.source_id] || item.source_id}</h3><p>{item.last_success_at ? `${relativeTime(item.last_success_at)}完成检查` : '等待首次检查'}</p><div className="source-card-meta"><span><Clock3 size={14} />每 2 分钟</span><span><LockKeyhole size={14} />内置</span></div></article>)}</div></section><section className="source-section"><div className="section-heading"><div><h3>自定义监测</h3><p>股票、账号、频道、网站和邮箱。</p></div></div>{filtered.length ? <div className="source-cards">{filtered.map((source) => { const meta = sourceMeta(source.kind); return <article key={source.id} className="source-card"><div className="source-card-top"><div className="source-icon large"><meta.icon size={20} /></div><span className={`status-pill ${tone(source)}`}>{label(source)}</span></div><h3>{source.publisher || source.id}</h3><p>{meta.label} · {source.section}</p><div className="source-card-meta"><span><Clock3 size={14} />每 {Math.round((source.poll_interval_seconds || 300) / 60)} 分钟</span>{state(source.id)?.last_success_at && <span><Check size={14} />{relativeTime(state(source.id).last_success_at)}</span>}</div><div className="source-card-actions"><button className="button subtle" onClick={() => open(source)}><Pencil size={16} />编辑</button><button className="button subtle" disabled={busy} onClick={() => toggle(source)}>{source.enabled === false ? '启用' : '停用'}</button><button className="icon-button danger" aria-label="删除来源" onClick={() => requestDelete(source)}><Trash2 size={16} /></button></div></article> })}</div> : <div className="empty-state compact"><div className="empty-icon"><Radar size={27} /></div><h3>{query ? '没有匹配的监测源' : '还没有自定义监测源'}</h3><p>{query ? '换一个关键词试试。' : '可以先从一个 RSS、YouTube 频道或股票列表开始。'}</p>{!query && <button className="button primary" onClick={() => open()}><Plus size={18} />添加第一个</button>}</div>}</section></>
}

function Events({ incidents, filter, setFilter, healthy, openCount, title }) {
  return <>
    <div className="page-actions"><div><h2>事件</h2><p>清楚区分一次性重要动态与能够自动恢复的异常。</p></div></div>
    <section className={`event-summary ${healthy ? 'positive' : 'warning'}`}>
      {healthy ? <CheckCircle2 size={25} /> : <CircleAlert size={25} />}
      <div><strong>{healthy ? '目前没有进行中的异常' : `${openCount} 个异常仍在进行`}</strong><span>{healthy ? '普通重要动态会保留为“已记录”，不会影响系统健康。' : '处理问题后，同一监测项确认恢复时会自动转为“已恢复”。'}</span></div>
    </section>
    <div className="filter-row">
      <div className="segmented">
        <button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>全部</button>
        <button className={filter === 'open' ? 'active' : ''} onClick={() => setFilter('open')}>进行中</button>
        <button className={filter === 'recovered' ? 'active' : ''} onClick={() => setFilter('recovered')}>已恢复</button>
        <button className={filter === 'recorded' ? 'active' : ''} onClick={() => setFilter('recorded')}>已记录</button>
      </div>
      <span className="result-count">{incidents.length} 条</span>
    </div>
    {incidents.length ? <section className="panel event-list">{incidents.map((incident) => {
      const meta = incidentMeta(incident)
      const heading = title(incident)
      return <article key={incident.id} className="event-row">
        <div className={`event-icon ${meta.tone}`}><meta.Icon size={20} /></div>
        <div className="event-copy">
          <div className="row-title"><h3>{incident.latest_click_url ? <a href={incident.latest_click_url} target="_blank" rel="noreferrer">{heading}</a> : heading}</h3><span className={`status-pill ${meta.tone}`}>{meta.label}</span></div>
          <p>{incident.latest_message || (incident.evidence || []).slice(0, 2).join(' · ') || `累计 ${incident.observation_count || 0} 条相关记录`}</p>
          <div className="meta-line"><Clock3 size={15} />{formatDate(incident.last_seen_at)}<span>·</span>置信度 {Math.round(Number(incident.confidence || 0) * 100)}%</div>
        </div>
      </article>
    })}</section> : <section className="empty-state"><div className="empty-icon positive"><CheckCircle2 size={29} /></div><h3>这个范围内没有事件</h3><p>EyeOfSauron 会记录重要动态；只有可恢复异常才会出现“进行中”和“已恢复”。</p></section>}
  </>
}

function Settings({ status, revisions, config, busy, rollback, advancedKind, setAdvancedKind, advancedJson, setAdvancedJson, loadExample, saveAdvanced }) {
  return <><div className="page-actions"><div><h2>设置</h2><p>日常操作保持简单，技术选项集中收在这里。</p></div></div><section className="settings-grid"><article className="settings-card"><div className="settings-icon"><Send size={21} /></div><div><h3>ntfy 通知</h3><p>所有提醒和重要事件发送到 <strong>eos</strong> 主题。</p><span className="status-pill positive">已连接</span></div></article><article className="settings-card"><div className="settings-icon"><Database size={21} /></div><div><h3>状态数据库</h3><p>SQLite schema {status.database_schema || '—'}，共 {Number(status.observations || 0).toLocaleString('zh-CN')} 条观察记录。</p><span className="status-pill positive">运行中</span></div></article><article className="settings-card"><div className="settings-icon"><ShieldCheck size={21} /></div><div><h3>访问保护</h3><p>仅监听 127.0.0.1:18080，通过 SSH 隧道访问。</p><span className="status-pill positive">仅本机</span></div></article></section><section className="panel settings-panel"><div className="panel-header"><div><h2>配置历史</h2><p>回滚会创建新修订，不会删除历史。</p></div><span className="revision-chip">当前修订 {config?.revision?.revision || 0}</span></div>{revisions.length ? <div className="revision-list">{revisions.map((revision) => <article key={revision.revision} className="revision-row"><div className="revision-line"><span className="revision-node">{revision.active && <Check size={14} />}</span></div><div><strong>修订 {revision.revision} {revision.active && <span className="status-pill positive">当前</span>}</strong><p>{revision.reason || '配置更新'}</p><small>{formatDate(revision.created_at)} · {revision.actor || 'admin'}</small></div>{!revision.active && <button className="button subtle" disabled={busy} onClick={() => rollback(revision.revision)}><History size={16} />回滚</button>}</article>)}</div> : <div className="empty-compact"><History size={21} /><span>还没有自定义配置修订</span></div>}</section><details className="advanced-panel"><summary><span><Settings2 size={19} /><span><strong>高级 JSON</strong><small>仅在普通表单无法表达配置时使用</small></span></span><ChevronRight size={18} /></summary><div className="advanced-content"><div className="form-grid two"><label><span>对象类型</span><select value={advancedKind} onChange={(event) => setAdvancedKind(event.target.value)}><option value="sources">监测源</option><option value="rules">通知规则</option></select></label><div className="field-action"><button className="button subtle" type="button" onClick={loadExample}><FileClock size={16} />载入示例</button></div></div><label><span>单个 JSON 对象</span><textarea value={advancedJson} onChange={(event) => setAdvancedJson(event.target.value)} className="code-input" rows="14" spellCheck="false" placeholder="在这里粘贴单个来源或规则对象"></textarea></label><div className="dialog-actions"><button className="button primary" disabled={busy || !advancedJson.trim()} onClick={saveAdvanced}>保存高级配置</button></div></div></details></>
}

const update = (setForm, key) => (event) => setForm((current) => ({ ...current, [key]: event.target.type === 'checkbox' ? event.target.checked : event.target.value }))

const Dialog = ({ children, className = '', dialogRef }) => <dialog ref={dialogRef} className={`modal ${className}`}>{children}</dialog>

function ReminderDialog({ form, setForm, onSubmit, busy, ref: dialogRef }) {
  return <Dialog dialogRef={dialogRef}><form className="modal-card" onSubmit={onSubmit}><header className="modal-header"><div><span className="eyebrow">{form.id ? '编辑' : '新建'}</span><h2>{form.id ? '更新提醒' : '安排一条提醒'}</h2></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => dialogRef.current?.close()}><X size={19} /></button></header><div className="modal-body"><div className="form-grid two"><label><span>标题</span><input value={form.title} onChange={update(setForm, 'title')} maxLength="128" required /></label><label><span>发送方式</span><select value={form.schedule_kind} onChange={update(setForm, 'schedule_kind')}><option value="once">指定日期时间</option><option value="after" disabled={Boolean(form.id)}>多久以后</option><option value="daily">每天固定时间</option></select></label></div>{form.schedule_kind === 'once' && <label><span>发送时间</span><input value={form.run_at_local} onChange={update(setForm, 'run_at_local')} type="datetime-local" required /><small>按当前浏览器所在时区解释。</small></label>}{form.schedule_kind === 'after' && <div className="form-grid two"><label><span>等待时长</span><input value={form.delay_value} onChange={(event) => setForm((current) => ({ ...current, delay_value: event.target.value }))} type="number" min="1" required /></label><label><span>单位</span><select value={form.delay_unit} onChange={(event) => setForm((current) => ({ ...current, delay_unit: Number(event.target.value) }))}><option value="60">分钟</option><option value="3600">小时</option><option value="86400">天</option></select></label></div>}{form.schedule_kind === 'daily' && <div className="form-grid two"><label><span>每天时间</span><input value={form.daily_time} onChange={update(setForm, 'daily_time')} type="time" required /></label><label><span>时区</span><input value={form.timezone} onChange={update(setForm, 'timezone')} list="timezone-list" required /><small>会正确处理夏令时。</small></label></div>}<label><span>提醒内容</span><textarea value={form.message} onChange={update(setForm, 'message')} rows="4" maxLength="4096" required placeholder="届时要发送给 ntfy 的消息"></textarea></label><div className="form-grid two"><label><span>通知优先级</span><select value={form.priority} onChange={(event) => setForm((current) => ({ ...current, priority: Number(event.target.value) }))}><option value="2">低</option><option value="3">普通</option><option value="4">高</option><option value="5">最高</option></select></label><label className="switch-field"><input checked={form.enabled} onChange={update(setForm, 'enabled')} type="checkbox" /><span><strong>保存后启用</strong><small>停用后仍保留配置</small></span></label></div></div><footer className="dialog-actions"><button className="button subtle" type="button" onClick={() => dialogRef.current?.close()}>取消</button><button className="button primary" type="submit" disabled={busy}>{busy ? <RefreshCw className="spin" size={17} /> : <Check size={17} />}{form.id ? '保存修改' : '创建提醒'}</button></footer></form></Dialog>
}

function SourceDialog({ form, setForm, onSubmit, test, busy, ref: dialogRef }) {
  const setKind = (kind) => setForm((current) => ({ ...current, kind, section: { rss: 'News', youtube: 'Videos', x: 'Social', market: 'Markets', imap: 'Inbox' }[kind] }))
  return <Dialog dialogRef={dialogRef} className="wide-modal"><form className="modal-card" onSubmit={onSubmit}><header className="modal-header"><div><span className="eyebrow">{form.editing ? '编辑监测' : '添加监测'}</span><h2>{form.editing ? form.publisher || '监测源' : 'EyeOfSauron 要关注什么？'}</h2></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => dialogRef.current?.close()}><X size={19} /></button></header><div className="modal-body">{!form.editing && <div className="kind-picker">{sourceKinds.map((kind) => <button key={kind.value} type="button" className={form.kind === kind.value ? 'active' : ''} onClick={() => setKind(kind.value)}><kind.icon size={19} /><span>{kind.label}</span></button>)}</div>}<div className="form-grid two"><label><span>显示名称</span><input value={form.publisher} onChange={update(setForm, 'publisher')} required placeholder="例如：公司公告 / 关注的频道" /></label><label><span>分组</span><input value={form.section} onChange={update(setForm, 'section')} required placeholder="例如：News" /></label></div>{form.kind === 'rss' && <label><span>官方 RSS / Atom 地址</span><input value={form.target} onChange={update(setForm, 'target')} type="url" pattern="https://.*" required placeholder="https://example.com/feed.xml" /><small>只接受 HTTPS；付费墙来源只保存标题、摘要和链接。</small></label>}{form.kind === 'youtube' && <label><span>YouTube 频道 ID</span><input value={form.target} onChange={update(setForm, 'target')} required placeholder="UC 开头的稳定频道 ID" /><small>不是频道显示名或 @handle。</small></label>}{form.kind === 'x' && <label><span>X 账号数字 ID</span><input value={form.target} onChange={update(setForm, 'target')} inputMode="numeric" pattern="[0-9]+" required placeholder="平台分配的数字 user ID" /><small>使用稳定数字 ID，避免账号改名后监控错人。</small></label>}{form.kind === 'market' && <><label><span>股票代码</span><input value={form.target} onChange={update(setForm, 'target')} required placeholder="AAPL, MSFT, NVDA" /><small>当前适配器使用 Alpaca 市场数据，适合美股和美股 ETF。</small></label><div className="form-grid three"><label><span>涨跌阈值</span><div className="input-suffix"><input value={form.threshold} onChange={update(setForm, 'threshold')} type="number" min="0.1" step="0.1" required /><span>%</span></div></label><label><span>跳空阈值</span><div className="input-suffix"><input value={form.gap} onChange={update(setForm, 'gap')} type="number" min="0.1" step="0.1" required /><span>%</span></div></label><label><span>成交量倍数</span><div className="input-suffix"><input value={form.volume} onChange={update(setForm, 'volume')} type="number" min="1" step="0.1" required /><span>×</span></div></label></div></>}{form.kind === 'imap' && <><div className="form-grid two"><label><span>IMAP 主机</span><input value={form.target} onChange={update(setForm, 'target')} required placeholder="imap.example.com" /></label><label><span>邮箱文件夹</span><input value={form.mailbox} onChange={update(setForm, 'mailbox')} required /></label></div><label><span>搜索条件</span><input value={form.search} onChange={update(setForm, 'search')} required placeholder="ALL" /></label></>}<div className="form-grid two"><label><span>关注关键词（可选）</span><input value={form.keywords} onChange={update(setForm, 'keywords')} placeholder="逗号分隔；留空则每条更新都通知" /></label><label><span>排除词（可选）</span><input value={form.excludes} onChange={update(setForm, 'excludes')} placeholder="例如：podcast, sponsored" /></label></div>{form.editing && <label className="switch-field"><input checked={form.updateRule} onChange={update(setForm, 'updateRule')} type="checkbox" /><span><strong>同时更新通知关键词</strong><small>关闭时会保留现有通知规则</small></span></label>}<label className="switch-field"><input checked={form.enabled} onChange={update(setForm, 'enabled')} type="checkbox" /><span><strong>保存后启用</strong><small>凭据尚未准备好时建议先关闭</small></span></label><details><summary>凭据与高级选项</summary><div className="advanced-fields"><div className="form-grid two"><label><span>内部标识</span><input value={form.id} onChange={update(setForm, 'id')} disabled={form.editing} pattern="[a-z][a-z0-9_-]{1,63}" required /><small>保存后保持稳定。</small></label>{form.kind === 'market' && <label><span>冷却时间（秒）</span><input value={form.cooldown} onChange={update(setForm, 'cooldown')} type="number" min="60" required /></label>}</div>{form.kind === 'market' && <div className="form-grid two"><label><span>API Key 环境变量</span><input value={form.apiKeyEnv} onChange={update(setForm, 'apiKeyEnv')} required /></label><label><span>API Secret 环境变量</span><input value={form.apiSecretEnv} onChange={update(setForm, 'apiSecretEnv')} required /></label><label><span>市场数据 API 地址</span><input value={form.apiBaseUrl} onChange={update(setForm, 'apiBaseUrl')} type="url" required /></label></div>}{form.kind === 'x' && <label><span>Bearer Token 环境变量</span><input value={form.bearerTokenEnv} onChange={update(setForm, 'bearerTokenEnv')} required /></label>}{form.kind === 'imap' && <div className="form-grid two"><label><span>用户名环境变量</span><input value={form.usernameEnv} onChange={update(setForm, 'usernameEnv')} required /></label><label><span>密码环境变量</span><input value={form.passwordEnv} onChange={update(setForm, 'passwordEnv')} required /></label></div>}<p className="security-note"><ShieldCheck size={17} />这里只填写服务器上的环境变量名称，绝不填写真实密码或 Token。</p></div></details></div><footer className="dialog-actions split"><button className="button subtle" type="button" disabled={busy} onClick={test}><Activity size={17} />测试连接</button><div><button className="button subtle" type="button" onClick={() => dialogRef.current?.close()}>取消</button><button className="button primary" type="submit" disabled={busy}>{busy ? <RefreshCw className="spin" size={17} /> : <Check size={17} />}保存监测</button></div></footer></form></Dialog>
}

export default App
