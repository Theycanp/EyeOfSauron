import { useCallback, useEffect, useState } from 'react'
import { History, KeyRound, LogOut, Plus, RefreshCw, Save, Shield, UserRoundCog } from 'lucide-react'
import type { AdminApi } from '../../shared/api'
import { ApiError } from '../../shared/api'
import type { AdminAuthAudit, AdminRole, AdminUser } from '../../shared/types'
import { formatDate } from '../../shared/utils'

type Props = {
  api: AdminApi
  currentUserId: number | null
  notify: (text: string, tone?: 'success' | 'error') => void
  onUnauthorized: () => void
}

const roles: { value: AdminRole; label: string; detail: string }[] = [
  { value: 'admin', label: '管理员', detail: '全部配置、用户和运行操作' },
  { value: 'operator', label: '操作员', detail: '来源、提醒、质量和队列操作' },
  { value: 'viewer', label: '只读', detail: '只查看状态、事件和日报' },
]

const actionLabels: Record<string, string> = {
  user_created: '创建账户',
  user_updated: '修改账户',
  password_changed: '重置密码',
  sessions_revoked: '撤销会话',
  login: '登录成功',
}

type UserEditorProps = {
  user: AdminUser
  current: boolean
  busy: boolean
  run: (action: () => Promise<unknown>, success: string) => Promise<boolean>
  api: AdminApi
}

function UserEditor({ user, current, busy, run, api }: UserEditorProps) {
  const [name, setName] = useState(user.display_name)
  const [role, setRole] = useState<AdminRole>(user.role)
  const [enabled, setEnabled] = useState(Boolean(user.enabled))
  const [password, setPassword] = useState('')

  const resetPassword = async () => {
    if (await run(
      () => api.resetUserPassword(user.id, password),
      '密码已重置，旧会话已撤销',
    )) setPassword('')
  }

  return <article className="user-row">
    <div className="user-identity">
      <span className={`user-state ${user.enabled ? 'active' : ''}`} />
      <div>
        <strong>{user.display_name}</strong>
        <small>
          @{user.username} · {user.last_login_at ? `上次登录 ${formatDate(user.last_login_at)}` : '尚未登录'}
          {' · '}{user.active_sessions || 0} 个有效会话
        </small>
      </div>
      {current && <span className="status-pill info">当前账户</span>}
    </div>
    <div className="user-controls">
      <input aria-label={`${user.username}显示名称`} value={name} onChange={(event) => setName(event.target.value)} maxLength={80} />
      <select aria-label={`${user.username}角色`} value={role} onChange={(event) => setRole(event.target.value as AdminRole)}>
        {roles.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
      </select>
      <label className="compact-toggle">
        <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
        <span>启用</span>
      </label>
      <button
        className="icon-button"
        title="保存账户设置"
        aria-label={`保存 ${user.username} 的账户设置`}
        disabled={busy || !name.trim()}
        onClick={() => void run(
          () => api.updateUser(user.id, { display_name: name, role, enabled }),
          '账户设置已保存',
        )}
      ><Save size={16} /></button>
    </div>
    <div className="password-controls">
      <input
        aria-label={`${user.username}新密码`}
        type="password"
        minLength={12}
        maxLength={128}
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        placeholder="设置新密码"
        autoComplete="new-password"
      />
      <button className="button subtle" disabled={busy || password.length < 12} onClick={() => void resetPassword()}>
        <KeyRound size={16} />重置密码
      </button>
      <button
        className="button subtle"
        disabled={busy || (current && (user.active_sessions || 0) <= 1)}
        title={current && (user.active_sessions || 0) <= 1 ? '当前仅有这个会话，退出请使用侧栏按钮' : undefined}
        onClick={() => void run(() => api.revokeUserSessions(user.id), '该用户的会话已撤销')}
      ><LogOut size={16} />强制退出</button>
    </div>
  </article>
}

export function UsersPage({ api, currentUserId, notify, onUnauthorized }: Props) {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [audit, setAudit] = useState<AdminAuthAudit[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<AdminRole>('viewer')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [userResponse, auditResponse] = await Promise.all([api.users(), api.authAudit()])
      setUsers(userResponse.users || [])
      setAudit(auditResponse.audit || [])
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
      else setError(failure instanceof Error ? failure.message : '无法读取用户')
    } finally {
      setLoading(false)
    }
  }, [api, onUnauthorized])

  useEffect(() => {
    const timer = window.setTimeout(() => { void load() }, 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const run = async (action: () => Promise<unknown>, success: string): Promise<boolean> => {
    setBusy(true)
    setError('')
    try {
      await action()
      await load()
      notify(success)
      return true
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized()
      setError(failure instanceof Error ? failure.message : '操作失败')
      notify('用户操作失败', 'error')
      return false
    } finally {
      setBusy(false)
    }
  }

  const create = async () => {
    const created = await run(
      () => api.createUser({ username, display_name: displayName || username, password, role }),
      '用户已创建',
    )
    if (created) {
      setUsername('')
      setDisplayName('')
      setPassword('')
      setRole('viewer')
    }
  }

  return <div className="page-stack">
    <section className="page-heading">
      <div><p className="eyebrow">ACCESS CONTROL</p><h1>用户与权限</h1><p>每个账户独立授权；修改角色、禁用账户或重置密码会撤销它的现有会话。</p></div>
      <div className="heading-mark"><UserRoundCog size={24} /></div>
    </section>
    <section className="panel">
      <div className="panel-header"><div><h2><Plus size={17} />新增用户</h2><p>密码至少 12 个字符。建议每个人使用自己的账户。</p></div></div>
      <form className="user-create-form" onSubmit={(event) => { event.preventDefault(); void create() }}>
        <label><span>用户名</span><input value={username} onChange={(event) => setUsername(event.target.value)} pattern="[A-Za-z][A-Za-z0-9_.-]{2,31}" autoComplete="off" required /></label>
        <label><span>显示名称</span><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={80} /></label>
        <label><span>初始密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} minLength={12} maxLength={128} autoComplete="new-password" required /></label>
        <label><span>角色</span><select value={role} onChange={(event) => setRole(event.target.value as AdminRole)}>{roles.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <button className="button primary" disabled={busy}><Plus size={16} />创建用户</button>
      </form>
    </section>
    <section className="panel">
      <div className="panel-header">
        <div><h2><Shield size={17} />账户</h2><p>{roles.map((item) => `${item.label}：${item.detail}`).join('；')}</p></div>
        <button className="icon-button" aria-label="刷新用户" onClick={() => void load()} disabled={loading}><RefreshCw size={17} className={loading ? 'spin' : ''} /></button>
      </div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      <div className="user-list">
        {users.map((user) => <UserEditor key={`${user.id}:${user.updated_at}`} user={user} current={user.id === currentUserId} busy={busy} run={run} api={api} />)}
      </div>
    </section>
    <section className="panel">
      <div className="panel-header"><div><h2><History size={17} />最近安全记录</h2><p>保留账户、密码、会话和成功登录的审计轨迹。</p></div></div>
      <div className="auth-audit-list">
        {audit.slice(0, 30).map((item) => <div className="auth-audit-row" key={item.id}>
          <span><strong>{actionLabels[item.action] || item.action}</strong><small>{item.username ? `@${item.username}` : '系统账户'}</small></span>
          <span><strong>{item.actor}</strong><small>执行者</small></span>
          <time dateTime={new Date(item.created_at * 1000).toISOString()}>{formatDate(item.created_at)}</time>
        </div>)}
        {!loading && audit.length === 0 && <div className="empty-compact">暂无安全记录</div>}
      </div>
    </section>
  </div>
}
