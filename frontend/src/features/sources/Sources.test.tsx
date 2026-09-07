import { createRef, useState } from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SourceDialog, SourcesPage } from './Sources'
import { createSourceDraft, type SourceDraft } from './providers'
import type { NewsCatalogEntry } from '../../shared/types'

function Harness({ initial }: { initial: SourceDraft }) {
  const [draft, setDraft] = useState(initial)
  return <SourceDialog ref={createRef<HTMLDialogElement>()} draft={draft} setDraft={setDraft} busy={false} testing={false} dirty={false} onClose={() => undefined} onSave={() => undefined} onTest={() => undefined} onCancelTest={() => undefined} onResetToPicker={() => undefined} />
}

describe('source creation flow', () => {
  it('does not repeat the provider picker after a typed quick entry', () => {
    render(<Harness initial={createSourceDraft('youtube', 'typed')} />)
    expect(screen.getByText('YouTube')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: '来源类型', hidden: true })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'X 账号' })).not.toBeInTheDocument()
  })

  it('shows exactly one provider picker for the generic add action', () => {
    render(<Harness initial={createSourceDraft(null, 'generic')} />)
    expect(screen.getByRole('list', { name: '来源类型', hidden: true })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /RSS \/ Atom/, hidden: true })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /YouTube/, hidden: true })).toBeInTheDocument()
  })
})

describe('source catalog', () => {
  it('shows catalog candidates without requiring an expand action', () => {
    const catalog: NewsCatalogEntry[] = [{
      id: 'bank_of_japan',
      publisher: 'Bank of Japan',
      homepage_url: 'https://www.boj.or.jp/en/',
      access_model: 'public',
      integration_mode: 'verified_rss',
      feeds: [{ id: 'whats_new', label: "What's New", section: 'Policy', url: 'https://www.boj.or.jp/en/rss/whatsnew.xml', allowed_hosts: ['www.boj.or.jp'] }],
      evidence_url: 'https://www.boj.or.jp/en/rss/',
      notes: 'Official feed.',
      verified_on: '2026-09-05',
      default_enabled: false,
      requires_user_confirmation: true,
      content_policy: 'feed_metadata_and_original_link_only',
    }]
    render(<SourcesPage sources={[]} sourceStates={[]} busy={false} query="" onQuery={() => undefined} onCreate={() => undefined} catalog={catalog} catalogError={null} onCatalogFeed={() => undefined} onEdit={() => undefined} onToggle={() => undefined} onDelete={() => undefined} />)
    expect(screen.getByText('Bank of Japan')).toBeVisible()
    expect(screen.getByText('生成草稿')).toBeVisible()
  })
})
