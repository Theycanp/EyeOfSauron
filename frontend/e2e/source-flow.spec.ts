import { expect, test, type Page } from '@playwright/test'

async function mockAdminApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const now = Math.floor(Date.now() / 1000)
    const bodies: Record<string, unknown> = {
      '/api/config': {
        managed: { sources: [], rules: [] },
        revision: { revision: 1, updated_at: now },
        status: {
          database_schema: 7,
          observations: 0,
          sources: [],
          incidents: { open: 0 },
          outbox: { pending: 0, sending: 0 },
          runtime: { heartbeat_at: now, applied_revision: 1 },
        },
      },
      '/api/reminders': { reminders: [], pagination: { total: 0 } },
      '/api/revisions': { revisions: [], pagination: { total: 0 } },
      '/api/incidents': { incidents: [], pagination: { total: 0 } },
      '/api/news-catalog': { sources: [{ id: 'wsj', publisher: 'The Wall Street Journal', homepage_url: 'https://www.wsj.com', access_model: 'mixed', integration_mode: 'verified_rss', notes: 'Headlines and original links.', feeds: [{ id: 'markets', label: 'Markets', section: 'Markets', url: 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml', allowed_hosts: ['feeds.a.dj.com'] }] }] },
      '/api/outbox': { alerts: [], pagination: { next_cursor: null } },
    }
    const body = bodies[path]
    if (body) await route.fulfill({ json: body })
    else await route.fulfill({ status: 404, json: { error: 'not mocked' } })
  })
}

async function login(page: Page) {
  await page.goto('/')
  await page.getByLabel('管理 Token').fill('test-token')
  await page.getByRole('button', { name: '进入后台' }).click()
  await expect(page.getByRole('heading', { name: '概览' }).first()).toBeVisible()
}

async function openSources(page: Page) {
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '监测来源' }).click()
}

test('login screen exposes the EyeOfSauron identity and accessible token field', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '欢迎回到 EyeOfSauron' })).toBeVisible()
  await expect(page.getByLabel('管理 Token')).toBeVisible()
})

test('typed source entry never repeats the provider picker', async ({ page }) => {
  await mockAdminApi(page)
  await login(page)
  await openSources(page)
  await page.getByRole('button', { name: /RSS \/ Atom/ }).first().click()

  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: 'RSS / Atom 来源' })).toBeVisible()
  await expect(dialog.getByRole('list', { name: '来源类型' })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: /YouTube/ })).toHaveCount(0)
})

test('generic source entry presents exactly one provider choice step', async ({ page }) => {
  await mockAdminApi(page)
  await login(page)
  await openSources(page)
  await page.getByRole('button', { name: '添加来源', exact: true }).click()

  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('list', { name: '来源类型' })).toBeVisible()
  await dialog.getByRole('button', { name: /RSS \/ Atom/ }).click()
  await expect(dialog.getByRole('heading', { name: 'RSS / Atom 来源' })).toBeVisible()
  await expect(dialog.getByRole('list', { name: '来源类型' })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: /YouTube/ })).toHaveCount(0)
})

test('catalog creates a disabled draft and saves with its opening revision', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  let saved: Record<string, unknown> | null = null
  await page.route('**/api/source-bundles', async (route) => {
    expect(route.request().headers()['if-match']).toBe('"1"')
    saved = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({ json: { revision: 2 } })
  })
  await login(page)
  await openSources(page)
  await expect(page.getByRole('heading', { name: 'The Wall Street Journal' })).toBeVisible()
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate((node) => node.clientWidth))
  await page.screenshot({ path: testInfo.outputPath('sources.png'), fullPage: true })
  await page.getByRole('button', { name: '生成草稿', exact: true }).click()
  await page.getByRole('button', { name: '生成停用草稿', exact: true }).click()
  const dialog = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: 'RSS / Atom 来源' }) })
  await expect(dialog.getByLabel('保存后启用')).not.toBeChecked()
  await expect(dialog.getByLabel('官方 RSS / Atom 地址')).toHaveValue('https://feeds.a.dj.com/rss/RSSMarketsMain.xml')
  await page.screenshot({ path: testInfo.outputPath('source-dialog.png'), fullPage: true })
  let backgroundRefreshed = false
  await page.route('**/api/config', (route) => {
    backgroundRefreshed = true
    return route.fulfill({ json: { managed: { sources: [], rules: [] }, revision: { revision: 2 }, status: { runtime: { heartbeat_at: Math.floor(Date.now() / 1_000), applied_revision: 1 } } } })
  })
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect.poll(() => backgroundRefreshed).toBe(true)
  await dialog.getByRole('button', { name: '保存来源' }).click()
  await expect.poll(() => saved).not.toBeNull()
  expect(saved).toMatchObject({ source: { enabled: false, settings: { catalog_entry: 'wsj' } } })
})

test('connection test polls the daemon job without saving the draft', async ({ page }) => {
  await mockAdminApi(page)
  let polls = 0
  await page.route('**/api/test-source', (route) => route.fulfill({ status: 202, json: { job: { id: 'test-job', status: 'queued' } } }))
  await page.route('**/api/jobs/test-job', (route) => {
    polls += 1
    return route.fulfill({ json: { job: { id: 'test-job', status: 'succeeded', result: { observations: 3, elapsed_ms: 40, warnings: [] } } } })
  })
  await login(page)
  await openSources(page)
  await page.getByRole('button', { name: /RSS \/ Atom/ }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('显示名称').fill('Example')
  await dialog.getByLabel('官方 RSS / Atom 地址').fill('https://example.com/feed.xml')
  await dialog.getByRole('button', { name: '测试连接' }).click()
  await expect(page.getByText(/连接成功：读取到 3 条新记录/)).toBeVisible()
  expect(polls).toBe(1)
  await expect(dialog).toBeVisible()
})

test('dead letters can be retried and pending delivery can be cancelled', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  let status = 'dead'
  const alert = { id: 42, rule_id: 'test', topic: 'eos', title: 'Delivery failure', message: 'A notification waiting for recovery.', status: 'dead', attempts: 12, priority: 3, created_at: 1_780_000_000, failure_kind: 'http_503', last_error: 'Service unavailable' }
  await page.route('**/api/outbox?*', (route) => {
    const requested = new URL(route.request().url()).searchParams.get('status')
    return route.fulfill({ json: { alerts: requested === status ? [{ ...alert, status }] : [], pagination: { next_cursor: null } } })
  })
  await page.route('**/api/outbox/42/retry', (route) => { status = 'pending'; return route.fulfill({ json: { changed: true } }) })
  await page.route('**/api/outbox/42/cancel', (route) => { status = 'cancelled'; return route.fulfill({ json: { changed: true } }) })
  await page.route('**/api/outbox/42', (route) => route.fulfill({ json: { removed: false } }))
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '设置', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Delivery failure' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('outbox.png'), fullPage: true })
  await page.getByRole('button', { name: '删除通知 Delivery failure' }).click()
  await page.getByRole('button', { name: '永久删除', exact: true }).click()
  await expect(page.getByText('这条通知的状态已改变，未执行删除。队列已刷新。')).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '重试', exact: true }).click()
  await page.getByRole('button', { name: '重新投递', exact: true }).click()
  await expect(page.getByText('没有需要人工处理的死信')).toBeVisible()
  await page.getByRole('button', { name: '待发送', exact: true }).click()
  await page.getByRole('button', { name: '取消发送', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '取消发送', exact: true }).click()
  await expect(page.getByText('当前没有等待发送的通知')).toBeVisible()
})
