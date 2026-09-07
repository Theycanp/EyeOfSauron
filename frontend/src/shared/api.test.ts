import { afterEach, describe, expect, it, vi } from 'vitest'
import { AdminApi, ApiError } from './api'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function requestUrl(input: string | URL | Request): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.href
  return input.url
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('AdminApi', () => {
  it('uses same-origin cookies and binds mutations to the CSRF cookie', async () => {
    vi.spyOn(document, 'cookie', 'get').mockReturnValue('__Host-eos_csrf=csrf-value')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ user: {} }))
    const api = new AdminApi()

    await api.login('owner', 'password-value')
    await api.createUser({
      username: 'reader', display_name: 'Reader', password: 'reader-password-value', role: 'viewer',
    })

    const loginInit = fetchMock.mock.calls[0]?.[1]
    expect(loginInit?.credentials).toBe('same-origin')
    expect(new Headers(loginInit?.headers).has('Authorization')).toBe(false)
    const mutationInit = fetchMock.mock.calls[1]?.[1]
    expect(new Headers(mutationInit?.headers).get('X-CSRF-Token')).toBe('csrf-value')
  })

  it('sends the current revision with every configuration mutation', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ revision: 8 }))
    const api = new AdminApi('secret')

    await api.configMutate('/api/source-bundles', 'POST', 7, { source: { id: 'news' } })

    const firstCall = fetchMock.mock.calls[0]
    expect(firstCall).toBeDefined()
    const [url, init] = firstCall!
    expect(requestUrl(url)).toBe('/api/source-bundles')
    const headers = new Headers(init?.headers)
    expect(headers.get('If-Match')).toBe('"7"')
  })

  it('turns a revision conflict into a clear refresh instruction', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ error: 'stale', code: 'revision_conflict' }, 409))
    const api = new AdminApi('secret')

    const error: unknown = await api.configMutate('/api/sources', 'POST', 3, {}).catch((cause: unknown) => cause)
    expect(error).toBeInstanceOf(ApiError)
    if (!(error instanceof ApiError)) throw new Error('expected ApiError')
    expect(error.status).toBe(409)
    expect(error.code).toBe('revision_conflict')
    expect(error.message).toContain('请先刷新数据')
  })

  it('polls an asynchronous source test until Argus returns a result', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ job: { id: 'job-1', status: 'queued' } }, 202))
      .mockResolvedValueOnce(jsonResponse({ job: { id: 'job-1', status: 'running' } }))
      .mockResolvedValueOnce(jsonResponse({ job: { id: 'job-1', status: 'succeeded', result: { observations: 4, elapsed_ms: 27, warnings: [] } } }))
    const api = new AdminApi('secret')

    await expect(api.testSource({ id: 'feed' }, { timeoutMs: 2_000, pollIntervalMs: 100 })).resolves.toMatchObject({ observations: 4, elapsed_ms: 27 })
    expect(fetchMock.mock.calls.map(([url]) => requestUrl(url))).toEqual([
      '/api/test-source',
      '/api/jobs/job-1',
      '/api/jobs/job-1',
    ])
  })

  it('requests job cancellation when the user aborts a source test', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = requestUrl(input)
      if (url === '/api/test-source') return Promise.resolve(jsonResponse({ job: { id: 'job-cancel', status: 'queued' } }, 202))
      if (url.endsWith('/cancel')) return Promise.resolve(jsonResponse({ changed: true }))
      return Promise.resolve(jsonResponse({ job: { id: 'job-cancel', status: 'queued' } }))
    })
    const controller = new AbortController()
    const api = new AdminApi('secret')
    const pending = api.testSource({ id: 'feed' }, { signal: controller.signal, timeoutMs: 2_000, pollIntervalMs: 100 })

    window.setTimeout(() => controller.abort(), 10)
    await expect(pending).rejects.toMatchObject({ code: 'request_cancelled' })
    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([url]) => requestUrl(url) === '/api/jobs/job-cancel/cancel')).toBe(true))
  })

  it('enforces a total source-test deadline and requests cancellation', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = requestUrl(input)
      if (url === '/api/test-source') return Promise.resolve(jsonResponse({ job: { id: 'job-timeout', status: 'queued' } }, 202))
      if (url.endsWith('/cancel')) return Promise.resolve(jsonResponse({ changed: true }))
      return Promise.resolve(jsonResponse({ job: { id: 'job-timeout', status: 'queued' } }))
    })
    const api = new AdminApi('secret')

    await expect(api.testSource({ id: 'feed' }, { timeoutMs: 125, pollIntervalMs: 100 })).rejects.toMatchObject({ code: 'job_timeout' })
    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([url]) => requestUrl(url) === '/api/jobs/job-timeout/cancel')).toBe(true))
  })
})
