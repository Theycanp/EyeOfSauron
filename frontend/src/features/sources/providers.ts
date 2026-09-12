import { AtSign, Mail, Rss, Server, TrendingUp, Video, type LucideIcon } from 'lucide-react'
import type { JsonRecord, ManagedRule, ManagedSource, NewsCatalogEntry, NewsCatalogFeed, SourceKind } from '../../shared/types'
import { browserZone, escapeRegex, splitWords } from '../../shared/utils'

export interface ProviderDefinition {
  kind: SourceKind
  label: string
  description: string
  defaultSection: string
  targetLabel: string
  targetHint: string
  icon: LucideIcon
}

export const providerRegistry: Record<SourceKind, ProviderDefinition> = {
  host: { kind: 'host', label: '主机健康', description: '磁盘、内存、负载和关键服务', defaultSection: 'Host', targetLabel: '检查路径', targetHint: '以服务账户只读检查；未知状态不会当作恢复。', icon: Server },
  official_list: { kind: 'official_list', label: '官方公告', description: '没有 RSS 的政府及公共机构公告', defaultSection: 'Policy', targetLabel: '官方公告列表地址', targetHint: '只提取指定目录下带日期的公告链接；网站改版或列表为空会报告异常。', icon: Rss },
  rss: { kind: 'rss', label: 'RSS / Atom', description: '新闻网站、博客和公告', defaultSection: 'News', targetLabel: '官方 RSS / Atom 地址', targetHint: '只接受 HTTPS；付费墙来源只保存标题、摘要和链接。', icon: Rss },
  youtube: { kind: 'youtube', label: 'YouTube', description: '频道发布新视频时提醒', defaultSection: 'Videos', targetLabel: 'YouTube 频道 ID', targetHint: '使用 UC 开头的稳定频道 ID，不使用显示名或 @handle。', icon: Video },
  x: { kind: 'x', label: 'X 账号', description: '指定账号发布内容时提醒', defaultSection: 'Social', targetLabel: 'X 账号数字 ID', targetHint: '使用平台数字 user ID，避免账号改名后监控错人。', icon: AtSign },
  market: { kind: 'market', label: '股票', description: '美股价格或成交量异动', defaultSection: 'Markets', targetLabel: '股票代码', targetHint: '当前 Alpaca 适配器适合美股和美股 ETF。', icon: TrendingUp },
  imap: { kind: 'imap', label: '邮箱', description: '符合搜索条件的新邮件', defaultSection: 'Inbox', targetLabel: 'IMAP 主机', targetHint: '凭据通过服务器环境变量提供，不在这里保存密码。', icon: Mail },
}

export const providerList = Object.values(providerRegistry)

export type SourceEntryMode = 'generic' | 'typed' | 'edit'
export type NotificationMode = 'preserve' | 'simple'
export type ContentPolicy = 'feed_metadata_and_original_link_only' | 'feed_full_text_allowed' | 'public_document_full_text'

export interface SourceDraft {
  entryMode: SourceEntryMode
  editing: boolean
  kind: SourceKind | null
  id: string
  publisher: string
  section: string
  enabled: boolean
  target: string
  maxContentAgeSeconds: number
  contentPolicy: ContentPolicy
  articleUrlPrefixes: string
  sourceTimezone: string
  keywords: string
  excludes: string
  notificationMode: NotificationMode
  originalSource: ManagedSource | null
  templateSource: ManagedSource | null
  originalRule: ManagedRule | null
  apiBaseUrl: string
  apiKeyEnv: string
  apiSecretEnv: string
  threshold: number
  volume: number
  gap: number
  cooldown: number
  bearerTokenEnv: string
  mailbox: string
  usernameEnv: string
  passwordEnv: string
  search: string
  timezone: string
  hostPaths: string
  hostUnits: string
  hostDisk: number
  hostInode: number
  hostMemory: number
  hostLoad: number
}

function generatedId(): string {
  return `source_${Date.now().toString(36)}`
}

export function createSourceDraft(kind: SourceKind | null, entryMode: SourceEntryMode = 'generic'): SourceDraft {
  return {
    entryMode,
    editing: false,
    kind,
    id: generatedId(),
    publisher: '',
    section: kind ? providerRegistry[kind].defaultSection : '',
    enabled: kind !== 'x' && kind !== 'market' && kind !== 'imap',
    target: '',
    maxContentAgeSeconds: 0,
    contentPolicy: 'feed_metadata_and_original_link_only',
    articleUrlPrefixes: '',
    sourceTimezone: 'Asia/Shanghai',
    keywords: '',
    excludes: '',
    notificationMode: 'simple',
    originalSource: null,
    templateSource: null,
    originalRule: null,
    apiBaseUrl: 'https://data.alpaca.markets',
    apiKeyEnv: 'ALPACA_API_KEY',
    apiSecretEnv: 'ALPACA_API_SECRET',
    threshold: 5,
    volume: 3,
    gap: 3,
    cooldown: 1800,
    bearerTokenEnv: 'X_BEARER_TOKEN',
    mailbox: 'INBOX',
    usernameEnv: 'IMAP_USERNAME',
    passwordEnv: 'IMAP_PASSWORD',
    search: 'ALL',
    timezone: browserZone,
    hostPaths: '/', hostUnits: '', hostDisk: 90, hostInode: 90, hostMemory: 90, hostLoad: 8,
  }
}

function setting(settings: JsonRecord, key: string, fallback = ''): string {
  const value = settings[key]
  return typeof value === 'string' ? value : fallback
}

function settingNumber(settings: JsonRecord, key: string, fallback: number): number {
  const value = Number(settings[key])
  return Number.isFinite(value) ? value : fallback
}

export function isSourceKind(value: string): value is SourceKind {
  return value in providerRegistry
}

export function editSourceDraft(source: ManagedSource, rule: ManagedRule | null): SourceDraft {
  if (!isSourceKind(source.kind)) throw new Error(`普通表单尚不支持 ${source.kind} 来源`)
  const draft = createSourceDraft(source.kind, 'edit')
  const settings = source.settings || {}
  draft.editing = true
  draft.id = source.id
  draft.publisher = source.publisher || ''
  draft.section = source.section || providerRegistry[source.kind].defaultSection
  draft.enabled = source.enabled !== false
  draft.originalSource = structuredClone(source)
  draft.originalRule = rule ? structuredClone(rule) : null
  draft.notificationMode = 'preserve'
  if (source.kind === 'host') {
    draft.hostPaths = Array.isArray(settings.paths) ? settings.paths.map(String).join('\n') : '/'
    draft.hostUnits = Array.isArray(settings.units) ? settings.units.map(String).join('\n') : ''
    draft.hostDisk = settingNumber(settings, 'disk_used_percent', 90)
    draft.hostInode = settingNumber(settings, 'inode_used_percent', 90)
    draft.hostMemory = settingNumber(settings, 'memory_used_percent', 90)
    draft.hostLoad = settingNumber(settings, 'load1', 8)
  }
  if (source.kind === 'rss' || source.kind === 'official_list') {
    draft.target = source.url || ''
    draft.maxContentAgeSeconds = settingNumber(settings, 'max_content_age_seconds', 0)
    draft.articleUrlPrefixes = Array.isArray(settings.article_url_prefixes) ? settings.article_url_prefixes.map(String).join('\n') : ''
    draft.sourceTimezone = setting(settings, 'timezone', draft.sourceTimezone)
    const contentPolicy = setting(settings, 'content_policy')
    if (contentPolicy === 'feed_full_text_allowed' || contentPolicy === 'public_document_full_text') {
      draft.contentPolicy = contentPolicy
    }
  }
  if (source.kind === 'youtube') draft.target = setting(settings, 'channel_id')
  if (source.kind === 'x') {
    draft.target = setting(settings, 'user_id')
    draft.bearerTokenEnv = setting(settings, 'bearer_token_env', draft.bearerTokenEnv)
  }
  if (source.kind === 'market') {
    const symbols = Array.isArray(settings.symbols) ? settings.symbols.map(String) : []
    draft.target = symbols.join(', ')
    draft.apiBaseUrl = setting(settings, 'api_base_url', draft.apiBaseUrl)
    draft.apiKeyEnv = setting(settings, 'api_key_env', draft.apiKeyEnv)
    draft.apiSecretEnv = setting(settings, 'api_secret_env', draft.apiSecretEnv)
    draft.threshold = settingNumber(settings, 'price_change_threshold', draft.threshold)
    draft.volume = settingNumber(settings, 'volume_multiplier', draft.volume)
    draft.gap = settingNumber(settings, 'gap_threshold', draft.gap)
    draft.cooldown = settingNumber(settings, 'cooldown_seconds', draft.cooldown)
  }
  if (source.kind === 'imap') {
    draft.target = setting(settings, 'host')
    draft.mailbox = setting(settings, 'mailbox', draft.mailbox)
    draft.usernameEnv = setting(settings, 'username_env', draft.usernameEnv)
    draft.passwordEnv = setting(settings, 'password_env', draft.passwordEnv)
    draft.search = setting(settings, 'search', draft.search)
  }
  return draft
}

export function chooseProvider(draft: SourceDraft, kind: SourceKind): SourceDraft {
  const fresh = createSourceDraft(kind, draft.entryMode)
  return { ...fresh, id: draft.id, publisher: draft.publisher, enabled: draft.enabled }
}

function baseSource(draft: SourceDraft): ManagedSource {
  if (!draft.kind) throw new Error('请先选择来源类型')
  const original: JsonRecord = draft.originalSource || draft.templateSource
    ? structuredClone(draft.originalSource || draft.templateSource || {})
    : {}
  return {
    ...original,
    id: draft.id.trim(),
    kind: draft.kind,
    publisher: draft.publisher.trim(),
    section: draft.section.trim(),
    dedupe_scope: typeof original.dedupe_scope === 'string' ? original.dedupe_scope : draft.id.trim(),
    enabled: draft.enabled,
    poll_interval_seconds: Number(original.poll_interval_seconds || 300),
    request_timeout_seconds: Number(original.request_timeout_seconds || 20),
    request_attempts: Number(original.request_attempts || 3),
    retry_base_seconds: Number(original.retry_base_seconds || 2),
    max_response_bytes: Number(original.max_response_bytes || 2_097_152),
    settings: { ...((original.settings as JsonRecord | undefined) || {}) },
  }
}

export function serializeSource(draft: SourceDraft): ManagedSource {
  const source = baseSource(draft)
  const settings = { ...(source.settings || {}) }
  const target = draft.target.trim()
  if (draft.kind === 'rss' || draft.kind === 'official_list') {
    const url = new URL(target)
    const reference = draft.originalSource || draft.templateSource
    source.url = target
    source.allowed_hosts = reference?.url === target && reference.allowed_hosts?.length ? [...reference.allowed_hosts] : [url.hostname]
    settings.max_content_age_seconds = Number(draft.maxContentAgeSeconds)
    settings.content_policy = draft.contentPolicy
    if (draft.kind === 'official_list') {
      settings.article_url_prefixes = splitWords(draft.articleUrlPrefixes)
      settings.timezone = draft.sourceTimezone
      source.allowed_hosts = [...new Set([...(source.allowed_hosts || []), ...splitWords(draft.articleUrlPrefixes).map((value) => new URL(value).hostname)])]
    }
  } else if (draft.kind === 'host') {
    settings.paths = splitWords(draft.hostPaths)
    settings.units = splitWords(draft.hostUnits)
    settings.disk_used_percent = Number(draft.hostDisk)
    settings.inode_used_percent = Number(draft.hostInode)
    settings.memory_used_percent = Number(draft.hostMemory)
    settings.load1 = Number(draft.hostLoad)
  } else if (draft.kind === 'youtube') {
    settings.channel_id = target
  } else if (draft.kind === 'x') {
    settings.user_id = target
    settings.bearer_token_env = draft.bearerTokenEnv.trim()
  } else if (draft.kind === 'market') {
    settings.api_base_url = draft.apiBaseUrl.trim()
    settings.path_template = typeof settings.path_template === 'string' ? settings.path_template : '/v2/stocks/{symbol}/snapshot'
    settings.api_key_env = draft.apiKeyEnv.trim()
    settings.api_secret_env = draft.apiSecretEnv.trim()
    settings.symbols = splitWords(target).map((item) => item.toUpperCase())
    settings.price_change_threshold = Number(draft.threshold)
    settings.volume_multiplier = Number(draft.volume)
    settings.gap_threshold = Number(draft.gap)
    settings.cooldown_seconds = Number(draft.cooldown)
  } else if (draft.kind === 'imap') {
    settings.host = target
    settings.port = Number(settings.port || 993)
    settings.mailbox = draft.mailbox.trim()
    settings.search = draft.search.trim()
    settings.username_env = draft.usernameEnv.trim()
    settings.password_env = draft.passwordEnv.trim()
  }
  source.settings = settings
  return source
}

export function serializeSimpleRule(draft: SourceDraft, source: ManagedSource): ManagedRule | null {
  if (draft.notificationMode === 'preserve') return null
  if (draft.originalRule && (draft.originalRule.source_ids.length !== 1 || draft.originalRule.source_ids[0] !== source.id)) {
    throw new Error('这条规则由多个来源共享，请在高级规则中修改，以免覆盖其他来源的通知设置。')
  }
  const include = splitWords(draft.keywords)
  const exclude = splitWords(draft.excludes)
  const patterns = [{
    label: include.length ? '关注关键词' : '全部更新',
    regex: include.length ? `(?i)(?:${include.map(escapeRegex).join('|')})` : '.',
    title_weight: 2,
    summary_weight: 1,
  }]
  if (exclude.length) patterns.push({
    label: '排除内容',
    regex: `(?i)(?:${exclude.map(escapeRegex).join('|')})`,
    title_weight: -100,
    summary_weight: -100,
  })
  const original: JsonRecord = draft.originalRule ? structuredClone(draft.originalRule) : {}
  return {
    ...original,
    id: draft.originalRule?.id || `${source.id}_notify`,
    kind: 'weighted_text',
    source_ids: [source.id],
    threshold: 1,
    max_item_age_seconds: Number(original.max_item_age_seconds || 86_400),
    notification_title: source.kind === 'market' ? '股票异动' : `${source.publisher || source.id} 更新`,
    priority: Number(original.priority || (source.kind === 'market' ? 4 : 3)),
    tags: Array.isArray(original.tags) ? original.tags : source.kind === 'market' ? ['chart_with_upwards_trend'] : ['bell'],
    patterns,
  }
}

export function ruleForSource(rules: ManagedRule[], sourceId: string): ManagedRule | null {
  return rules.find((rule) => rule.id === `${sourceId}_notify`) || rules.find((rule) => rule.source_ids.includes(sourceId)) || null
}


function availableCatalogId(entry: NewsCatalogEntry, feed: NewsCatalogFeed, existingIds: string[]): string {
  const base = `${entry.id}_${feed.id}`.slice(0, 64)
  if (!existingIds.includes(base)) return base
  for (let suffix = 2; suffix < 1_000; suffix += 1) {
    const candidate = `${base.slice(0, 64 - String(suffix).length - 1)}_${suffix}`
    if (!existingIds.includes(candidate)) return candidate
  }
  return `source_${Date.now().toString(36)}`
}

export function catalogSourceDraft(entry: NewsCatalogEntry, feed: NewsCatalogFeed, existingIds: string[]): SourceDraft {
  const draft = createSourceDraft(entry.source_kind || 'rss', 'typed')
  const id = availableCatalogId(entry, feed, existingIds)
  const template: ManagedSource = {
    id,
    kind: entry.source_kind || 'rss',
    publisher: entry.publisher,
    section: feed.section,
    dedupe_scope: entry.id,
    url: feed.url,
    allowed_hosts: [...feed.allowed_hosts],
    enabled: true,
    poll_interval_seconds: entry.id === 'sec' ? 600 : 300,
    request_timeout_seconds: 20,
    request_attempts: 3,
    retry_base_seconds: 2,
    max_response_bytes: 2_097_152,
    region: entry.region || 'GLOBAL',
    source_tier: entry.source_tier || 'secondary',
    default_importance: entry.default_importance || 3,
    settings: {
      catalog_entry: entry.id,
      catalog_feed: feed.id,
      max_content_age_seconds: feed.max_content_age_seconds ?? 0,
      content_policy: entry.content_policy,
      topic: entry.topic || 'general',
      ...(entry.id === 'japan_meteorological_agency' ? { headline_from_summary: true } : {}),
      ...(entry.source_kind === 'official_list' ? {
        article_url_prefixes: feed.article_url_prefixes || [],
        index_format: feed.index_format || 'html',
        timezone: entry.region === 'JP' ? 'Asia/Tokyo' : 'Asia/Shanghai',
      } : {}),
    },
  }
  return {
    ...draft,
    id,
    publisher: entry.publisher,
    section: feed.section,
    target: feed.url,
    articleUrlPrefixes: (feed.article_url_prefixes || []).join('\n'),
    sourceTimezone: entry.region === 'JP' ? 'Asia/Tokyo' : 'Asia/Shanghai',
    maxContentAgeSeconds: feed.max_content_age_seconds ?? 0,
    contentPolicy: entry.content_policy === 'public_document_full_text'
      ? 'public_document_full_text'
      : entry.content_policy === 'feed_full_text_allowed'
        ? 'feed_full_text_allowed'
        : 'feed_metadata_and_original_link_only',
    enabled: true,
    templateSource: template,
  }
}
