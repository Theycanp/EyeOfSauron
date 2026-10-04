import { expect, test, type Page } from '@playwright/test'

async function mockAdminApi(page: Page, weatherMode: 'rain' | 'dry' | 'snow' | 'partial' = 'rain') {
  let authenticated = false
  let weatherHasForecast = true
  let weatherRefreshPending = false
  let weatherSubscription = {
    id: 'home', label: '北京邮电大学沙河校区', latitude: 40.1561163,
    longitude: 116.2835626, timezone: 'Asia/Shanghai', daily_time: '07:00',
    daily_enabled: true, alerts_enabled: true, revision: 1,
  }
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const now = Math.floor(Date.now() / 1000)
    const user = {
      id: 1,
      username: 'owner',
      display_name: 'Owner',
      role: 'admin',
      permissions: [
        'read', 'sources:write', 'reminders:write', 'events:write', 'quality:write',
        'operations:write', 'settings:write', 'users:manage',
      ],
    }
    if (path === '/api/auth/session') {
      await route.fulfill(authenticated ? { json: { user } } : { status: 401, json: { error: 'unauthorized' } })
      return
    }
    if (path === '/api/auth/login') {
      authenticated = true
      await route.fulfill({ json: { user } })
      return
    }
    if (path === '/api/auth/logout') {
      authenticated = false
      await route.fulfill({ json: { logged_out: true } })
      return
    }
    if (path === '/api/weather/places') {
      await route.fulfill({ json: { places: [{ label: '北京市天安门', latitude: 39.905, longitude: 116.397, timezone: 'Asia/Shanghai' }] } })
      return
    }
    if (path === '/api/weather/place-resolution') {
      await route.fulfill({ json: { label: '北京市 · 昌平', timezone: 'Asia/Shanghai', provider: 'QWeather', precision: 'administrative' } })
      return
    }
    if (path === '/api/weather') {
      if (route.request().method() === 'POST') {
        const submitted = route.request().postDataJSON() as Partial<typeof weatherSubscription>
        weatherSubscription = { ...weatherSubscription, ...submitted, revision: weatherSubscription.revision + 1 }
        weatherHasForecast = false
        weatherRefreshPending = true
        await route.fulfill({ json: { subscription: weatherSubscription, restart_required: false } })
      } else {
        const localDayStart = now - ((now + 8 * 3600 - 6 * 3600 + 86400) % 86400)
        if (!weatherHasForecast && !weatherRefreshPending) weatherHasForecast = true
        else if (weatherRefreshPending) weatherRefreshPending = false
        await route.fulfill({ json: { subscription: weatherSubscription, latest: weatherHasForecast ? {
          condition: weatherMode === 'snow' ? '小雪' : weatherMode === 'dry' ? '晴' : '多云', low: 13, high: 23, rain_mm: weatherMode === 'dry' ? 0 : 1.6, rain_probability: weatherMode === 'dry' ? 0 : 65,
          uv_index_max: 5.4,
          window_start_at: localDayStart, window_end_at: localDayStart + 24 * 3600,
          window_complete: weatherMode !== 'partial',
          forecast_hours: Array.from({ length: 24 }, (_, index) => ({ at: localDayStart + index * 3600,
            temperature: Number((18 + 5 * Math.sin(index / 24 * 2 * Math.PI)).toFixed(1)),
            precipitation: weatherMode === 'dry' ? 0 : index < 4 ? 0.4 : 0,
            rain_probability: weatherMode === 'dry' ? 0 : index < 4 ? 65 : 10,
            weather_code: weatherMode === 'dry' ? 0 : index < 4 ? weatherMode === 'snow' ? 71 : 61 : 2,
          })).filter((_, index) => weatherMode !== 'partial' || (index >= 7 && index !== 12)),
          wind_gust_kmh: 32, temperature_now: 20, humidity: 58,
          wind_speed_kmh: 12, wind_direction_name: '东南',
          sunrise: 1790373600, sunset: 1790416800,
          air_quality: { observed_at: now, pm2_5: 12, pm10: 20, european_aqi: 27, us_aqi: 39 },
          astronomy: { date: '20260926', sunrise: 1790373600, sunset: 1790416800,
            moonrise: 1790413200, moonset: 1790370000, moon_phase: '盈凸月',
            moon_illumination: 95, solar_elevation: 42.5, solar_azimuth: 230, updated_at: now },
          calendar: { lunar: '农历2026年8月16日', festivals: '' },
          observed_at: now, is_today: true,
        } : null,
          last_success_at: weatherHasForecast ? now + 1 : null, last_daily_date: null, last_error: null,
          consecutive_failures: 0, rain_expected: null,
          qweather: {
            minutely: { last_success_at: now, last_error: null, consecutive_failures: 0 },
            alerts: { last_success_at: now, last_error: null, consecutive_failures: 0 },
            astronomy: { last_success_at: now, last_error: null, consecutive_failures: 0 },
          } } })
      }
      return
    }
    if (path === '/api/alerts/17') {
      await route.fulfill({
        json: {
          alert: {
            id: 17,
            observation_id: 42,
            incident_id: 7,
            title: 'Bloomberg breaking',
            message: '来源：Bloomberg Markets\n\n判断依据：breaking',
            priority: 5,
            confidence: 0.92,
            evidence: ['breaking', 'high-impact'],
            tags: ['warning'],
            source_url: 'https://www.bloomberg.com/news/articles/test',
            status: 'delivered',
            created_at: now,
            delivered_at: now,
          },
          observation: {
            id: 42,
            source_id: 'bloomberg_markets',
            publisher: 'Bloomberg',
            published_at: now - 60,
            fetched_at: now,
            title: 'Prime Minister Resigns',
            summary: 'Saved Bloomberg RSS summary that remains readable without opening the publisher website.',
            url: 'https://www.bloomberg.com/news/articles/test',
            attributes: { section: 'Markets' },
            importance: 5,
            urgency: 5,
            relevance: 4,
            confidence: 0.92,
            region: 'GLOBAL',
            topic: 'politics',
            source_tier: 'secondary',
            information_type: 'report',
            handling: 'immediate',
          },
          incident: { id: 7, status: 'recorded', source_ids: ['bloomberg_markets'] },
        },
      })
      return
    }
    if (path === '/api/events' && route.request().method() === 'POST') {
      const submitted = route.request().postDataJSON() as Record<string, unknown>
      await route.fulfill({
        status: 201,
        json: {
          alert: {
            id: 23,
            observation_id: 43,
            incident_id: 8,
            title: `EyeOfSauron 人工事件：${String(submitted.title)}`,
            message: submitted.summary,
            priority: submitted.importance,
            confidence: 1,
            evidence: ['后台人工录入', '提交者：owner'],
            tags: ['memo'],
            source_url: submitted.source_url,
            status: 'pending',
            created_at: now,
          },
          observation: {
            id: 43,
            source_id: 'manual',
            publisher: '人工录入',
            published_at: now,
            fetched_at: now,
            title: submitted.title,
            summary: submitted.summary,
            url: submitted.source_url,
            attributes: { manual: true, actor: 'owner', submitted_via: 'admin' },
            importance: submitted.importance,
            urgency: submitted.importance,
            relevance: 5,
            confidence: 1,
            region: submitted.region,
            topic: submitted.topic,
            source_tier: 'primary',
            information_type: 'manual',
            handling: 'immediate',
          },
          incident: { id: 8, status: 'recorded', source_ids: ['manual'] },
        },
      })
      return
    }
    const bodies: Record<string, unknown> = {
      '/api/config': {
        managed: { sources: [], rules: [], analysis: { enabled: false, shadow_mode: true }, digest: { enabled: false } },
        revision: { revision: 1, updated_at: now },
        status: {
          database_schema: 13,
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
      '/api/prompts': { prompts: [] },
      '/api/source-quality': { profiles: [], logic: {} },
      '/api/digests': { digests: [], pagination: { total: 0, truncated: false } },
      '/api/news-events': { events: [], window: { hours: 28, until: now }, sort: 'newest', pagination: { next_cursor: null, has_more: false } },
    }
    const body = bodies[path]
    if (body) await route.fulfill({ json: body })
    else await route.fulfill({ status: 404, json: { error: 'not mocked' } })
  })
}

async function login(page: Page) {
  await page.goto('/')
  await page.getByLabel('用户名').fill('owner')
  await page.getByLabel('密码').fill('correct-horse-battery')
  await page.getByRole('button', { name: '进入后台' }).click()
  await expect(page.getByRole('heading', { name: '概览' }).first()).toBeVisible()
}

async function openSources(page: Page) {
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '监测来源' }).click()
}

test('notification topics route, mute and test safely on desktop and mobile', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  const destinations = [
    { id: 'news', topic: 'eos-news', label: '新闻与日报', description: '' },
    { id: 'weather', topic: 'eos-weather', label: '天气', description: '' },
    { id: 'reminders', topic: 'eos-reminders', label: '提醒', description: '' },
    { id: 'system', topic: 'eos-system', label: '系统运行', description: '' },
  ]
  let policy = { destinations, routes: { news: 'news', weather: 'weather' as string | null, reminders: 'reminders', system: 'system' }, fallback: 'news' }
  let revision = 22
  let tests = 0
  await page.route('**/api/notifications', async route => {
    if (route.request().method() === 'POST') {
      expect(route.request().headers()['if-match']).toBe('"22"')
      policy = route.request().postDataJSON() as typeof policy
      revision += 1
      await route.fulfill({ json: { revision, restart_required: false } })
      return
    }
    await route.fulfill({ json: { policy, revision, allowed_topics: destinations.map(item => item.topic),
      base_url: 'https://ntfy.example.com:10008', topics: destinations.map(item => ({ topic: item.topic, counts: { delivered: 3 },
        latest: { id: 17, status: 'delivered', created_at: 1790668800, delivered_at: 1790668801, last_error: null } })) } })
  })
  await page.route('**/api/notifications/test', async route => {
    expect(route.request().postDataJSON()).toEqual({ topic: 'eos-weather' })
    tests += 1
    await route.fulfill({ status: 202, json: { alert_id: 17, queued: true } })
  })
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '通知管理', exact: true }).click()
  await expect(page.getByRole('heading', { name: '通知主题与路由' })).toBeVisible()
  await expect(page.getByRole('button', { name: '删除 eos-weather 目的地' })).toBeDisabled()
  await expect(page.getByRole('link', { name: '订阅' }).first()).toHaveAttribute('href', 'https://ntfy.example.com:10008/eos-news')
  await page.getByLabel('天气目的地').selectOption('')
  await page.getByRole('button', { name: '保存通知设置' }).click()
  await expect(page.getByRole('status')).toHaveText('通知设置已保存')
  expect(policy.routes.weather).toBeNull()
  await page.getByRole('button', { name: '发送 eos-weather 测试' }).click()
  await expect(page.getByRole('link', { name: /查看测试消息/ })).toHaveAttribute('href', '/events/17')
  expect(tests).toBe(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: testInfo.outputPath('notification-topics.png'), fullPage: true })
})

test('weather budgets and fact retries fit desktop and mobile', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  const now = Math.floor(Date.now() / 1000)
  let policy = { provider: 'qweather', kind: 'alerts', enabled: true, interval_seconds: 600, daily_budget: 288, requests: 12, updated_at: now, updated_by: 'owner', last_error: 'HTTP 429: provider channel rate limited', configured: 1, configuration_checked_at: now, budget_day: '2026-09-27', status: 'error' }
  await page.route('**/api/weather/providers**', route => {
    if (route.request().method() === 'POST') {
      policy = { ...policy, ...route.request().postDataJSON() as object }
      return route.fulfill({ json: { policy } })
    }
    return route.fulfill({ json: { subscription_id: 'home', policies: [policy] } })
  })
  let dead = 3
  await page.route('**/api/event-facts/diagnostics**', route => route.fulfill({ json: {
    extractor_version: null, as_of: now, active_worker_version: 1, completed_count: 90, completed_with_claim: 10, completed_without_claim: 80, explanation: '',
    versions: [{ extractor_version: 1, history_highwater: 100, created_at: now }], worker_batch_limit: 10, worker_active_pause_seconds: 1,
    jobs: [{ extractor_version: 1, status: 'completed', count: 90, newest_updated_at: now }, { extractor_version: 1, status: 'dead', count: dead, newest_updated_at: now }],
  } }))
  await page.route('**/api/event-facts/retry', route => {
    expect(route.request().postDataJSON()).toEqual({ version: 1, limit: 100 })
    dead = 0
    return route.fulfill({ json: { count: 3, changed: 3 } })
  })
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '天气', exact: true }).click()
  await page.getByRole('button', { name: '天气来源与预算', exact: true }).click()
  await expect(page.getByText('今日已用 12 / 288')).toBeVisible()
  await expect(page.getByText('预算日期 2026-09-27（UTC）')).toBeVisible()
  await expect(page.getByText('尚无请求 · 查询失败')).toBeVisible()
  await page.getByLabel('QWeather · 官方预警每日预算').fill('120')
  await page.getByRole('button', { name: '保存QWeather · 官方预警' }).click()
  await expect(page.getByText('官方预警设置已保存')).toBeVisible()
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate(node => node.clientWidth))
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
  await page.screenshot({ path: testInfo.outputPath('weather-provider-policies.png'), fullPage: true })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '日常事件', exact: true }).click()
  await page.getByRole('button', { name: '事实任务诊断', exact: true }).click()
  await expect(page.getByText(/不代表报道没有价值/)).toBeVisible()
  await page.getByLabel('重试提取器版本').selectOption('1')
  await page.getByRole('button', { name: '重试失败任务（3）' }).click()
  await expect(page.getByText('已将 3 个失败任务重新排队')).toBeVisible()
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate(node => node.clientWidth))
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
  await page.screenshot({ path: testInfo.outputPath('event-fact-diagnostics.png'), fullPage: true })
})

test('source diagnostics keep recovered polling and blocked content distinct', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  const now = Math.floor(Date.now() / 1000)
  await page.route('**/api/config', (route) => route.fulfill({ json: {
    managed: { sources: [], rules: [], analysis: { enabled: false }, digest: { enabled: false } },
    revision: { revision: 1 },
    status: { observations: 10, sources: [{ source_id: 'official', last_success_at: now, consecutive_failures: 0, runtime_status: 'active' }], outbox: {}, incidents: {}, runtime: { heartbeat_at: now, applied_revision: 1 } },
  } }))
  await page.route('**/api/source-health/official', (route) => route.fulfill({ json: { health: {
    source_id: 'official', as_of: now,
    current: { source_id: 'official', last_success_at: now, consecutive_failures: 0 },
    polling: { basis: 'cumulative_persisted_counters', attempts: 10, successes: 9, failures: 1, success_rate: 0.9, window_success_rate: null },
    evidence: { basis: 'retained_observations_by_ingestion_time', since: now - 604800, until: now, observations_24h: 2, observations_7d: 10, with_full_text: 6, full_text_coverage: 0.6, content_jobs: { completed: 6, dead: 1 }, content_failure_kinds: { http_403: 1 } },
  } } }))
  await login(page)
  await openSources(page)
  await page.getByLabel('查看来源诊断').selectOption('official')
  const diagnostics = page.getByRole('region', { name: 'official来源诊断' })
  await expect(diagnostics.getByText('90.0%')).toBeVisible()
  await expect(diagnostics.getByText(/正文受限不代表来源停止采集/)).toBeVisible()
  await expect(diagnostics.getByText(/无法计算近 7 天采集成功率/)).toBeVisible()
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate((node) => node.clientWidth))
  await page.screenshot({ path: testInfo.outputPath('source-diagnostics.png'), fullPage: true })
})

test('digest run history exposes model evidence and advances a bounded retry', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  const now = Math.floor(Date.now() / 1000)
  let advanced = false
  let postCount = 0
  const run = {
    digest_key: 'daily:2026-09-21', state: 'ai_retrying', published_version: 1,
    generation_kind: 'algorithm', published_at: now - 60, reserved_attempts: 1,
    retry: { status: 'pending', attempts: 1, next_attempt_at: now + 4500,
      retry_deadline_at: now + 18000, last_error: 'Provider unavailable (redacted)',
      started_at: now - 60, updated_at: now - 60 },
    attempts: [{ id: 1, digest_key: 'daily:2026-09-21', status: 'failed',
      started_at: now - 72, finished_at: now - 60, error: 'Provider unavailable (redacted)',
      providers: [{ provider: 'models.example', model: 'primary-model', prompt_id: 'digest',
        prompt_version: '4', prompt_hash: 'a'.repeat(64), status: 'failed', elapsed_ms: '12000' }] }],
    attempt_history_available: true, can_retry_now: true,
  }
  await page.route('**/api/digest-runs**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { request_id: string }
      expect(body.request_id).toMatch(/^[0-9a-f-]{36}$/)
      postCount++
      advanced = true
      await route.fulfill({ json: { job: { id: body.request_id, status: 'succeeded' } } })
      return
    }
    const current = { ...run, can_retry_now: !advanced }
    await route.fulfill({ json: path === '/api/digest-runs' ? { runs: [current] } : { run: current } })
  })
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '日报', exact: true }).click()
  await page.getByRole('button', { name: '运行记录', exact: true }).click()
  await expect(page.getByText('等待 AI 重试')).toBeVisible()
  await page.getByText('展开尝试记录（1）').click()
  await expect(page.getByText('models.example')).toBeVisible()
  await expect(page.getByText('digest@4')).toBeVisible()
  await expect(page.getByText('1 / 5 次逻辑生成')).toBeVisible()
  await page.getByRole('button', { name: '提前重试', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('已安排提前重试')
  await expect(page.getByRole('button', { name: '提前重试', exact: true })).toBeDisabled()
  expect(postCount).toBe(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('digest-runs.png'), fullPage: true })
})

test('login screen exposes the EyeOfSauron identity and accessible account fields', async ({ page }) => {
  await mockAdminApi(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '欢迎回到 EyeOfSauron' })).toBeVisible()
  await expect(page.getByLabel('用户名')).toBeVisible()
  await expect(page.getByLabel('密码')).toBeVisible()
})

test('weather notification opens saved content after login and returns to weather home', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  await page.route('**/api/alerts/18', route => route.fulfill({ json: {
    alert: { id: 18, rule_id: 'weather.daily', title: '今日天气 · 易县',
      message: '易县：多云，当前 20℃。\n今天 00:00 至明天 06:00 模型小时数据：13~20℃。\n从现在至明天 06:00 预计降水 2 mm，最高降雨概率 80%。\n阳历 2026-09-29，农历八月十九。\n数据：Open-Meteo 预报，非官方气象预警。',
      priority: 3, confidence: 0.7, status: 'delivered', created_at: 1790636400,
      delivered_at: 1790636401, source_url: '/#/weather' },
    observation: null, incident: null, documents: [],
  } }))
  await page.goto('/events/18')
  await page.getByLabel('用户名').fill('owner')
  await page.getByLabel('密码').fill('correct-horse-battery')
  await page.getByRole('button', { name: '进入后台' }).click()
  await expect(page.getByRole('article', { name: '天气通知详情' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '今日天气 · 易县' })).toBeVisible()
  await expect(page.getByText(/从现在至明天 06:00 预计降水 2 mm/)).toBeVisible()
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate(node => node.clientWidth))
  await page.screenshot({ path: testInfo.outputPath('weather-notification-detail.png'), fullPage: true })
  await page.getByRole('link', { name: '天气主页' }).click()
  await expect(page.getByRole('heading', { name: /本地天气/ })).toBeVisible()
  await expect(page).toHaveURL(/\/#\/weather$/)
})

test('notification deep link survives login and opens the saved article detail', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  await page.goto('/events/17')
  await expect(page.getByRole('heading', { name: '欢迎回到 EyeOfSauron' })).toBeVisible()
  await page.getByLabel('用户名').fill('owner')
  await page.getByLabel('密码').fill('correct-horse-battery')
  await page.getByRole('button', { name: '进入后台' }).click()

  await expect(page.getByRole('heading', { name: 'Prime Minister Resigns' })).toBeVisible()
  await expect(page.getByText(/Saved Bloomberg RSS summary/)).toBeVisible()
  await expect(page.getByText('breaking', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '查看原文' })).toHaveAttribute(
    'href',
    'https://www.bloomberg.com/news/articles/test',
  )
  await expect(page.locator('body')).toHaveJSProperty(
    'scrollWidth',
    await page.locator('body').evaluate((node) => node.clientWidth),
  )
  await page.screenshot({ path: testInfo.outputPath('notification-detail.png'), fullPage: true })
})

test('manual event form creates a durable event and opens its saved detail', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '事件', exact: true }).click()
  await page.getByRole('button', { name: '添加事件' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('标题').fill('东京政策更新')
  await dialog.getByLabel('详细内容').fill('这是一次端到端人工事件测试。')
  await dialog.getByLabel('重要程度').selectOption('4')
  await dialog.getByLabel('地区').selectOption('JP')
  await dialog.getByLabel('主题').selectOption('politics')
  await dialog.getByLabel('来源链接（可选）').fill('https://example.com/report')
  await dialog.getByRole('button', { name: '创建并发送' }).click()

  await expect(page).toHaveURL(/\/events\/23$/)
  await expect(page.getByRole('heading', { name: '东京政策更新' })).toBeVisible()
  await expect(page.getByText('这是一次端到端人工事件测试。')).toBeVisible()
  await expect(page.getByText('人工录入', { exact: true }).first()).toBeVisible()
  await expect(page.locator('body')).toHaveJSProperty(
    'scrollWidth',
    await page.locator('body').evaluate((node) => node.clientWidth),
  )
  await page.screenshot({ path: testInfo.outputPath('manual-event-detail.png'), fullPage: true })
})

test('daily events reader is usable on desktop and mobile', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  await page.route('**/api/news-events**', async (route) => route.fulfill({ json: {
    events: [{
      event_key: 'fed-decision', title: 'Federal Reserve issues FOMC statement',
      summary: '美联储利率决议，官方声明、媒体报道和市场反应并列保留。',
      score: 4.8, importance: 5, urgency: 4, relevance: 4, confidence: 0.9,
      first_seen_at: 1789581600, last_seen_at: 1789583306, status: 'active',
      regions: ['US'], topics: ['policy'], independent_source_count: 3,
      handling: 'immediate', report_count: 3, reports_truncated: false,
      reports: [
        { report_id: 1, observation_id: 1, source_id: 'fed', publisher: 'Federal Reserve', source_tier: 'primary', relation: 'primary', title: 'Federal Reserve issues FOMC statement', summary: '官方政策声明。', url: 'https://www.federalreserve.gov/', published_at: 1789581600 },
        { report_id: 2, observation_id: 2, source_id: 'bloomberg', publisher: 'Bloomberg', source_tier: 'secondary', relation: 'corroborates', title: 'Fed Raises Rates as Warsh Bucks Trump to Contain Inflation', summary: '媒体对利率决议的报道。', url: 'https://www.bloomberg.com/', published_at: 1789581601 },
        { report_id: 3, observation_id: 3, source_id: 'ft', publisher: 'Financial Times', source_tier: 'secondary', relation: 'context', title: 'Dollar Jumps After Fed Raises Rates, Sends Hawkish Signal', summary: '市场反应属于背景报道。', url: 'https://www.ft.com/', published_at: 1789583306 },
      ],
    }], pagination: { next_cursor: null, has_more: false },
  } }))
  await page.route('**/api/news-events/fed-decision', route => route.fulfill({ json: {
    reports: [], audit: [], claims: [{ claim_key: 'rate', text: '利率维持 5.25%', status: 'active' }],
    claim_evidence: [], timeline: [], notifications: [],
  } }))
  await page.route('**/api/news-events/fed-decision/quality-label', route => {
    expect(route.request().postDataJSON()).toEqual({ label: 'correct_merge', reason: '官方声明与媒体报道为同一决议' })
    return route.fulfill({ json: { label: { event_key: 'fed-decision', label: 'correct_merge' } } })
  })
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '日常事件' }).click()
  await expect(page.getByRole('main').getByRole('heading', { name: '日常事件', level: 2 })).toBeVisible()
  await expect(page.getByLabel('时间窗（小时）')).toHaveValue('28')
  await expect(page.getByRole('button', { name: '最新' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page).toHaveURL(/\/daily-events$/)
  await page.getByRole('button', { name: /Federal Reserve issues FOMC statement/ }).click()
  await expect(page.getByText('一手', { exact: true })).toBeVisible()
  await expect(page.getByText('背景信息', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '查看事件历史与匹配依据' }).click()
  await expect(page.getByText('利率维持 5.25%')).toBeVisible()
  await page.getByLabel('聚合评估原因').fill('官方声明与媒体报道为同一决议')
  await page.getByRole('button', { name: '保存人工评估' }).click()
  await expect(page.getByText('人工评估已记录')).toBeVisible()
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
  await page.screenshot({ path: testInfo.outputPath('daily-events-expanded.png'), fullPage: true })
  await page.getByRole('button', { name: '重要', exact: true }).click()
  await expect(page).toHaveURL(/sort=importance/)
  await page.getByLabel('时间窗（小时）').fill('48')
  await page.getByRole('button', { name: '应用', exact: true }).click()
  await expect(page).toHaveURL(/hours=48/)
  await page.goBack()
  await expect(page.getByLabel('时间窗（小时）')).toHaveValue('28')
  await expect(page.locator('body')).toHaveJSProperty(
    'scrollWidth',
    await page.locator('body').evaluate((node) => node.clientWidth),
  )
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

test('catalog creates an enabled draft and saves with its opening revision', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  let saved: Record<string, unknown> | null = null
  await page.route('**/api/source-bundles', async (route) => {
    expect(route.request().headers()['if-match']).toBe('"1"')
    saved = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({ json: { revision: 2 } })
  })
  await login(page)
  await openSources(page)
  await expect(page.getByRole('heading', { name: /新闻目录/ })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'The Wall Street Journal' })).toBeVisible()
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate((node) => node.clientWidth))
  await page.screenshot({ path: testInfo.outputPath('sources.png'), fullPage: true })
  await page.getByRole('button', { name: '生成草稿', exact: true }).click()
  await page.getByRole('button', { name: '生成来源草稿', exact: true }).click()
  const dialog = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: 'RSS / Atom 来源' }) })
  await expect(dialog.getByLabel('保存后启用')).toBeChecked()
  await expect(dialog.getByLabel('官方 RSS / Atom 地址')).toHaveValue('https://feeds.a.dj.com/rss/RSSMarketsMain.xml')
  await page.screenshot({ path: testInfo.outputPath('source-dialog.png'), fullPage: true })
  let backgroundRefreshed = false
  await page.route('**/api/config', (route) => {
    backgroundRefreshed = true
    return route.fulfill({ json: { managed: { sources: [], rules: [], analysis: { enabled: false, shadow_mode: true }, digest: { enabled: false } }, revision: { revision: 2 }, status: { runtime: { heartbeat_at: Math.floor(Date.now() / 1_000), applied_revision: 1 } } } })
  })
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect.poll(() => backgroundRefreshed).toBe(true)
  await dialog.getByRole('button', { name: '保存来源' }).click()
  await expect.poll(() => saved).not.toBeNull()
  expect(saved).toMatchObject({ source: { enabled: true, settings: { catalog_entry: 'wsj' } } })
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

test('new acknowledgement reminders default to five-minute repeats', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '提醒', exact: true }).click()
  await page.getByRole('button', { name: '新建提醒' }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('checkbox', { name: /需要确认收到/ }).check()
  await expect(dialog.getByLabel('重复间隔（分钟）')).toHaveValue('5')
  await expect(dialog.getByLabel('重复次数')).toHaveValue('3')
  await dialog.getByLabel('重复间隔（分钟）').scrollIntoViewIfNeeded()
  await page.screenshot({ path: testInfo.outputPath('reminder-five-minutes.png'), fullPage: true })
})

test('weather location and schedule are usable on desktop and mobile', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '天气', exact: true }).click()
  await expect(page.getByRole('heading', { name: '本地天气' })).toBeVisible()
  await expect(page.getByRole('region', { name: '今日天气详情' })).toContainText('欧洲 AQI 27')
  await expect(page.getByRole('region', { name: '今日天气详情' })).toContainText('月升 / 月落')
  await expect(page.getByLabel('和风天气状态')).toContainText('上次查询')
  await expect(page.getByRole('region', { name: '今日天气详情' })).toContainText('太阳角度')
  await expect(page.getByRole('region', { name: '今日天气详情' })).toContainText('方位 230.0°')
  await expect(page.getByRole('region', { name: '今日天气详情' })).toContainText('农历2026年8月16日')
  await page.getByLabel('搜索城市或地区').fill('天安门')
  await page.getByRole('button', { name: '搜索地点' }).click()
  await page.getByRole('option', { name: /北京市天安门/ }).click()
  await expect(page.getByLabel('地点名称')).toHaveValue('北京市天安门')
  await page.getByLabel('每天推送时间').fill('08:00')
  await page.getByRole('button', { name: '保存天气订阅' }).click()
  await expect(page.getByText('天气订阅已保存，正在立即获取新地点天气…')).toBeVisible()
  await expect(page.getByText('天气订阅已更新，已载入最新数据。')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByRole('heading', { name: '本地天气 · 北京市天安门' })).toBeVisible()
  await expect(page.getByRole('region', { name: /^(今日与明晨|昨日至今晨)雨雪预报$/ })).toBeVisible()
  await expect(page.getByText(/^(今天 06:00—明天 06:00|昨天 06:00—今天 06:00)$/)).toBeVisible()
  await expect(page.getByText('时段高低温')).toBeVisible()
  const marker = page.locator('.weather-map .leaflet-marker-icon')
  await expect.poll(() => marker.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
  await expect(page.locator('.weather-map .leaflet-tile').first()).toHaveAttribute('src', /^https:\/\/tile\.openstreetmap\.org\//)
  if (testInfo.project.name === 'mobile') {
    const timeline = page.getByRole('region', { name: '天气时间轴', exact: true })
    expect(await timeline.evaluate(node => node.scrollWidth > node.clientWidth)).toBe(true)
    await timeline.evaluate(node => { node.scrollLeft = node.scrollWidth })
    expect(await timeline.evaluate(node => node.scrollLeft)).toBeGreaterThan(0)
  }
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate((node) => node.clientWidth))
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: testInfo.outputPath('weather.png'), fullPage: true })
})

for (const mode of ['dry', 'snow', 'partial'] as const) {
  test(`weather ${mode} forecast is readable and interactive`, async ({ page }, testInfo) => {
    await mockAdminApi(page, mode)
    await login(page)
    const menu = page.getByRole('button', { name: '打开导航' })
    if (await menu.isVisible()) await menu.click()
    await page.getByRole('button', { name: '天气', exact: true }).click()
    await expect(page.getByRole('img', { name: '按小时显示降水量、雨雪类型与气温' })).toBeVisible()
    const slider = page.getByRole('slider', { name: '选择预报小时' })
    await slider.focus()
    await slider.press('Home')
    await expect(page.getByLabel('选中小时天气')).toContainText(mode === 'snow' ? '雪 0.4 mm/h' : '无降水量预报')
    await slider.press('End')
    await expect(page.getByLabel('选中小时天气')).toContainText('05:00')
    if (mode === 'partial') await expect(page.getByText('可用时段高低温')).toBeVisible()
    if (mode === 'dry') await expect(page.getByText('可用小时暂无降水预报')).toBeVisible()
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate(node => node.clientWidth))
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: testInfo.outputPath(`weather-${mode}.png`), fullPage: true })
    await page.evaluate(() => localStorage.setItem('eosTheme', 'dark'))
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await expect(page.getByRole('img', { name: '按小时显示降水量、雨雪类型与气温' })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath(`weather-${mode}-dark.png`), fullPage: true })
  })
}

test('blocked map tiles show a usable location fallback', async ({ page }) => {
  await page.route('https://tile.openstreetmap.org/**', (route) => route.abort())
  await mockAdminApi(page)
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '天气', exact: true }).click()
  await expect(page.getByText('地图暂时不可用，请使用搜索或下方坐标输入。你仍可以保存坐标。')).toBeVisible()
  await expect(page.getByLabel('纬度')).toBeEnabled()
  await expect(page.getByLabel('经度')).toBeEnabled()
})

test('map selection resolves a name and saves the original pin on desktop and mobile', async ({ page }, testInfo) => {
  await mockAdminApi(page)
  await login(page)
  const menu = page.getByRole('button', { name: '打开导航' })
  if (await menu.isVisible()) await menu.click()
  await page.getByRole('button', { name: '天气', exact: true }).click()
  await page.locator('.weather-map').click({ position: { x: 140, y: 110 } })
  const latitude = await page.getByLabel('纬度').inputValue()
  const longitude = await page.getByLabel('经度').inputValue()
  await expect(page.getByLabel('地点名称')).toHaveValue('北京市 · 昌平')
  await expect(page.getByLabel('纬度')).toHaveValue(latitude)
  await expect(page.getByLabel('经度')).toHaveValue(longitude)
  await page.getByRole('button', { name: '保存天气订阅' }).click()
  await expect(page.getByRole('heading', { name: '本地天气 · 北京市 · 昌平' })).toBeVisible()
  await expect(page.getByText('天气订阅已更新，已载入最新数据。')).toBeVisible({ timeout: 10_000 })
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: testInfo.outputPath('weather-place-name.png'), fullPage: true })
  expect(await page.locator('body').evaluate(node => node.scrollWidth)).toBe(await page.locator('body').evaluate(node => node.clientWidth))
})
