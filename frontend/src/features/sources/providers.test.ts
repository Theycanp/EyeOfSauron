import { describe, expect, it } from 'vitest'
import type { ManagedRule, ManagedSource, NewsCatalogEntry, NewsCatalogFeed } from '../../shared/types'
import { catalogSourceDraft, createSourceDraft, editSourceDraft, serializeSimpleRule, serializeSource } from './providers'

describe('source provider registry', () => {
  it('opens a typed quick-add draft without another type-selection state', () => {
    const draft = createSourceDraft('rss', 'typed')
    expect(draft.kind).toBe('rss')
    expect(draft.entryMode).toBe('typed')
    expect(draft.section).toBe('News')
  })

  it('preserves unknown source and provider settings during visual edits', () => {
    const source: ManagedSource = {
      id: 'market_us',
      kind: 'market',
      publisher: 'US market',
      section: 'Markets',
      enabled: true,
      poll_interval_seconds: 47,
      request_timeout_seconds: 9,
      custom_top_level: { keep: true },
      settings: {
        symbols: ['AAPL'],
        api_base_url: 'https://data.alpaca.markets',
        api_key_env: 'ALPACA_API_KEY',
        api_secret_env: 'ALPACA_API_SECRET',
        path_template: '/custom/{symbol}',
        provider_extension: 'keep-me',
      },
    }
    const draft = editSourceDraft(source, null)
    draft.publisher = 'Renamed market'
    const saved = serializeSource(draft)
    expect(saved.poll_interval_seconds).toBe(47)
    expect(saved.request_timeout_seconds).toBe(9)
    expect(saved.custom_top_level).toEqual({ keep: true })
    expect(saved.settings?.path_template).toBe('/custom/{symbol}')
    expect(saved.settings?.provider_extension).toBe('keep-me')
    expect(saved.publisher).toBe('Renamed market')
  })

  it('preserves an existing advanced rule unless replacement is explicit', () => {
    const source: ManagedSource = { id: 'news', kind: 'rss', publisher: 'News', section: 'News', url: 'https://example.com/feed.xml', allowed_hosts: ['example.com'], settings: {} }
    const rule: ManagedRule = { id: 'advanced', kind: 'weighted_text', source_ids: ['news'], patterns: [{ label: 'advanced', regex: '(?i)urgent', title_weight: 8 }] }
    const draft = editSourceDraft(source, rule)
    expect(draft.notificationMode).toBe('preserve')
    expect(serializeSimpleRule(draft, serializeSource(draft))).toBeNull()
  })

  it('refuses to overwrite a shared rule from a single-source editor', () => {
    const source: ManagedSource = { id: 'news', kind: 'rss', publisher: 'News', section: 'News', url: 'https://example.com/feed.xml' }
    const draft = editSourceDraft(source, { id: 'shared', kind: 'weighted_text', source_ids: ['news', 'other_news'] })
    draft.notificationMode = 'simple'
    expect(() => serializeSimpleRule(draft, serializeSource(draft))).toThrow('多个来源共享')
  })

  it('preserves catalog freshness defaults through creation and visual editing', () => {
    const feed: NewsCatalogFeed = { id: 'markets', label: 'Markets', section: 'News', url: 'https://example.com/feed.xml', allowed_hosts: ['example.com'], max_content_age_seconds: 172800 }
    const entry = { id: 'news', publisher: 'News', content_policy: 'feed_metadata_and_original_link_only' } as NewsCatalogEntry
    const source = serializeSource(catalogSourceDraft(entry, feed, []))
    expect(source.settings?.max_content_age_seconds).toBe(172800)
    const edited = editSourceDraft(source, null)
    edited.publisher = 'Renamed'
    expect(serializeSource(edited).settings?.max_content_age_seconds).toBe(172800)
    edited.maxContentAgeSeconds = 0
    expect(serializeSource(edited).settings?.max_content_age_seconds).toBe(0)
    expect(serializeSource(catalogSourceDraft(entry, { ...feed, max_content_age_seconds: undefined }, [])).settings?.max_content_age_seconds).toBe(0)
  })

  it('serializes and preserves the explicit RSS content policy', () => {
    const draft = createSourceDraft('rss', 'typed')
    draft.publisher = 'Official agency'
    draft.target = 'https://official.example/feed.xml'
    draft.contentPolicy = 'public_document_full_text'
    const source = serializeSource(draft)
    expect(source.settings?.content_policy).toBe('public_document_full_text')
    expect(editSourceDraft(source, null).contentPolicy).toBe('public_document_full_text')

    const feed: NewsCatalogFeed = { id: 'releases', label: 'Releases', section: 'Policy', url: 'https://official.example/feed.xml', allowed_hosts: ['official.example'] }
    const entry = { id: 'agency', publisher: 'Agency', content_policy: 'public_document_full_text' } as NewsCatalogEntry
    expect(catalogSourceDraft(entry, feed, []).contentPolicy).toBe('public_document_full_text')
  })
})
