import { createRef, useState } from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SourceDialog } from './Sources'
import { createSourceDraft, type SourceDraft } from './providers'

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
