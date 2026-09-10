import type {
  AdminJob,
  AdminAuthAuditResponse,
  AdminUserResponse,
  AlertDetailResponse,
  AuthResponse,
  ConfigResponse,
  DigestDetailResponse,
  DigestListResponse,
  IncidentResponse,
  JobResponse,
  ManualEventDraft,
  MutationResponse,
  NewsCatalogResponse,
  OutboxResponse,
  OutboxStatus,
  PromptResponse,
  ReminderResponse,
  RevisionResponse,
  SourceQualityResponse,
  SourceTestResult,
} from './types'

type ApiErrorPayload = {
  error?: string | { code?: string; message?: string }
  code?: string
}

type SourceTestStart = Partial<SourceTestResult> & { job?: AdminJob }

export class ApiError extends Error {
  readonly status: number
  readonly code: string | null

  constructor(message: string, status: number, code: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

function errorDetails(body: ApiErrorPayload, status: number): { message: string; code: string | null } {
  const nested = typeof body.error === 'object' && body.error ? body.error : null
  const code = body.code || nested?.code || null
  const serverMessage = typeof body.error === 'string' ? body.error : nested?.message
  if (status === 401) return { message: '登录已失效，请重新登录', code: code || 'unauthorized' }
  if (status === 403) return { message: code === 'csrf_failed' ? '安全校验已失效，请刷新页面后重试' : '当前账户没有执行此操作的权限', code: code || 'forbidden' }
  if (status === 409 && code === 'revision_conflict') {
    return {
      message: '配置已在其他标签页或会话中修改。请先刷新数据，再重新执行刚才的操作。',
      code,
    }
  }
  return { message: serverMessage || `请求失败（${status}）`, code }
}

function aborted(): never {
  throw new ApiError('操作已取消', 499, 'request_cancelled')
}

function wait(ms: number, signal?: AbortSignal | null): Promise<void> {
  if (signal?.aborted) aborted()
  return new Promise((resolve, reject) => {
    const finish = () => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }
    const timer = window.setTimeout(finish, ms)
    const onAbort = () => {
      window.clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      reject(new DOMException('Operation aborted', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function sourceTestResult(job: AdminJob): SourceTestResult | null {
  if (!['succeeded', 'completed'].includes(job.status)) return null
  const result = job.result
  if (!result || !Number.isFinite(Number(result.observations)) || !Number.isFinite(Number(result.elapsed_ms))) {
    throw new ApiError('连接测试已完成，但服务器没有返回可用结果', 502, 'invalid_job_result')
  }
  return {
    ...result,
    observations: Number(result.observations),
    elapsed_ms: Number(result.elapsed_ms),
    warnings: Array.isArray(result.warnings) ? result.warnings.map(String) : [],
  }
}

export class AdminApi {
  private token: string

  constructor(token = '') {
    this.token = token
  }

  setToken(token: string): void {
    this.token = token
  }

  async request<T>(path: string, options: RequestInit = {}, timeoutMs = 15_000): Promise<T> {
    const controller = new AbortController()
    const externalSignal = options.signal
    let callerAborted = Boolean(externalSignal?.aborted)
    const abortFromCaller = () => {
      callerAborted = true
      controller.abort()
    }
    if (callerAborted) controller.abort()
    else externalSignal?.addEventListener('abort', abortFromCaller, { once: true })
    const timer = window.setTimeout(() => controller.abort(), timeoutMs)
    const headers = new Headers(options.headers)
    if (this.token) headers.set('Authorization', `Bearer ${this.token}`)
    const method = (options.method || 'GET').toUpperCase()
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && !this.token) {
      const name = '__Host-eos_csrf='
      const csrf = document.cookie.split('; ').find((part) => part.startsWith(name))?.slice(name.length)
      if (csrf) headers.set('X-CSRF-Token', decodeURIComponent(csrf))
    }
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
    try {
      const response = await fetch(path, { ...options, headers, credentials: 'same-origin', signal: controller.signal })
      const body = await response.json().catch(() => ({ error: '服务器返回了无法识别的响应' })) as ApiErrorPayload
      if (!response.ok) {
        const details = errorDetails(body, response.status)
        throw new ApiError(details.message, response.status, details.code)
      }
      return body as T
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        if (callerAborted) throw new ApiError('操作已取消', 499, 'request_cancelled')
        throw new ApiError('请求超时，请稍后重试', 408, 'request_timeout')
      }
      throw error
    } finally {
      window.clearTimeout(timer)
      externalSignal?.removeEventListener('abort', abortFromCaller)
    }
  }

  config(): Promise<ConfigResponse> { return this.request('/api/config') }
  session(): Promise<AuthResponse> { return this.request('/api/auth/session') }
  login(username: string, password: string): Promise<AuthResponse> {
    return this.request('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) })
  }
  logout(): Promise<{ logged_out: boolean }> { return this.request('/api/auth/logout', { method: 'POST', body: '{}' }) }
  users(): Promise<AdminUserResponse> { return this.request('/api/users') }
  authAudit(): Promise<AdminAuthAuditResponse> { return this.request('/api/users/audit') }
  createUser(body: { username: string; display_name: string; password: string; role: string }): Promise<MutationResponse> {
    return this.request('/api/users', { method: 'POST', body: JSON.stringify(body) })
  }
  updateUser(id: number, body: { display_name: string; role: string; enabled: boolean }): Promise<MutationResponse> {
    return this.request(`/api/users/${id}`, { method: 'POST', body: JSON.stringify(body) })
  }
  resetUserPassword(id: number, password: string): Promise<MutationResponse> {
    return this.request(`/api/users/${id}/password`, { method: 'POST', body: JSON.stringify({ password }) })
  }
  revokeUserSessions(id: number): Promise<MutationResponse> {
    return this.request(`/api/users/${id}/revoke-sessions`, { method: 'POST', body: '{}' })
  }
  reminders(): Promise<ReminderResponse> { return this.request('/api/reminders') }
  revisions(): Promise<RevisionResponse> { return this.request('/api/revisions') }
  incidents(): Promise<IncidentResponse> { return this.request('/api/incidents') }
  alert(id: number): Promise<AlertDetailResponse> { return this.request(`/api/alerts/${id}`) }
  createEvent(body: ManualEventDraft): Promise<AlertDetailResponse> {
    return this.request('/api/events', { method: 'POST', body: JSON.stringify(body) })
  }
  newsCatalog(): Promise<NewsCatalogResponse> { return this.request('/api/news-catalog') }
  prompts(): Promise<PromptResponse> { return this.request('/api/prompts') }
  sourceQuality(): Promise<SourceQualityResponse> { return this.request('/api/source-quality') }
  sourceQualityFeedback(sourceId: string, body: { signal: -1 | 0 | 1; reason: string; observation_id?: number }): Promise<MutationResponse> {
    return this.request(`/api/source-quality/${encodeURIComponent(sourceId)}/feedback`, { method: 'POST', body: JSON.stringify(body) })
  }
  setSourceQualityOverride(sourceId: string, body: { weight: number; reason: string }): Promise<MutationResponse> {
    return this.request(`/api/source-quality/${encodeURIComponent(sourceId)}/override`, { method: 'POST', body: JSON.stringify(body) })
  }
  clearSourceQualityOverride(sourceId: string): Promise<MutationResponse> {
    return this.request(`/api/source-quality/${encodeURIComponent(sourceId)}/override`, { method: 'DELETE' })
  }
  digests(status: 'published' | 'draft' | 'superseded' | 'all' = 'published'): Promise<DigestListResponse> {
    return this.request(`/api/digests?${new URLSearchParams({ status, limit: '30' })}`)
  }
  digest(key: string, version?: number): Promise<DigestDetailResponse> {
    const query = version ? `?${new URLSearchParams({ version: String(version) })}` : ''
    return this.request(`/api/digests/${encodeURIComponent(key)}${query}`)
  }

  outbox(status: Extract<OutboxStatus, 'dead' | 'pending'>, beforeId?: number): Promise<OutboxResponse> {
    const query = new URLSearchParams({ status, limit: '50' })
    if (beforeId) query.set('before_id', String(beforeId))
    return this.request(`/api/outbox?${query}`)
  }

  mutate(path: string, method: 'POST' | 'DELETE', body?: unknown): Promise<MutationResponse> {
    return this.request(path, { method, body: body === undefined ? undefined : JSON.stringify(body) })
  }

  configMutate(path: string, method: 'POST' | 'DELETE', expectedRevision: number, body?: unknown): Promise<MutationResponse> {
    if (!Number.isInteger(expectedRevision) || expectedRevision < 0) {
      throw new ApiError('无法确认当前配置修订，请先刷新数据', 409, 'revision_unknown')
    }
    return this.request(path, {
      method,
      headers: {
        'If-Match': `"${expectedRevision}"`,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  }

  async testSource(source: unknown, options: { signal?: AbortSignal; timeoutMs?: number; pollIntervalMs?: number } = {}): Promise<SourceTestResult> {
    const timeoutMs = options.timeoutMs ?? 90_000
    const pollIntervalMs = Math.max(100, options.pollIntervalMs ?? 1_000)
    const deadline = Date.now() + timeoutMs
    let jobId: string | null = null
    try {
      const started = await this.request<SourceTestStart>(
        '/api/test-source',
        { method: 'POST', body: JSON.stringify({ source }), signal: options.signal },
        Math.min(15_000, timeoutMs),
      )
      if (Number.isFinite(Number(started.observations)) && Number.isFinite(Number(started.elapsed_ms))) {
        return { ...started, observations: Number(started.observations), elapsed_ms: Number(started.elapsed_ms) }
      }
      if (!started.job?.id) throw new ApiError('服务器没有返回连接测试任务', 502, 'missing_job')
      jobId = started.job.id
      let job = started.job
      while (Date.now() < deadline) {
        const result = sourceTestResult(job)
        if (result) return result
        if (job.status === 'failed' || job.status === 'expired') {
          const message = job.error || (job.status === 'expired' ? '连接测试任务已过期' : '连接测试失败')
          throw new ApiError(message, 422, job.status === 'expired' ? 'job_expired' : 'job_failed')
        }
        if (job.status === 'cancelled') throw new ApiError('连接测试已取消', 499, 'job_cancelled')
        await wait(Math.min(pollIntervalMs, Math.max(1, deadline - Date.now())), options.signal)
        const remaining = deadline - Date.now()
        if (remaining <= 0) break
        const response = await this.request<JobResponse>(
          `/api/jobs/${encodeURIComponent(jobId)}`,
          { signal: options.signal },
          Math.min(15_000, remaining),
        )
        job = response.job
      }
      void this.cancelJob(jobId)
      throw new ApiError(`连接测试超过 ${Math.ceil(timeoutMs / 1_000)} 秒仍未完成，任务已请求取消`, 408, 'job_timeout')
    } catch (error) {
      if (jobId && options.signal?.aborted) void this.cancelJob(jobId)
      if (typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError') aborted()
      throw error
    }
  }

  async cancelJob(jobId: string): Promise<boolean> {
    try {
      const response = await this.request<{ changed?: boolean }>(
        `/api/jobs/${encodeURIComponent(jobId)}/cancel`,
        { method: 'POST', body: '{}' },
      )
      return Boolean(response.changed)
    } catch {
      return false
    }
  }
}
