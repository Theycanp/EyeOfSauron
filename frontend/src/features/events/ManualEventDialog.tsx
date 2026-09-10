import { forwardRef, type Dispatch, type SetStateAction } from 'react'
import { Check, RefreshCw, X } from 'lucide-react'
import { Modal } from '../../shared/ui/Modal'
import type { ManualEventDraft } from '../../shared/types'


interface ManualEventDialogProps {
  draft: ManualEventDraft
  setDraft: Dispatch<SetStateAction<ManualEventDraft>>
  busy: boolean
  error: string | null
  dirty: boolean
  onClose: () => void
  onSubmit: () => void
}

const change = (
  setDraft: Dispatch<SetStateAction<ManualEventDraft>>,
  key: keyof ManualEventDraft,
) => (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
  setDraft((current) => ({ ...current, [key]: event.target.value }))
}

export const ManualEventDialog = forwardRef<HTMLDialogElement, ManualEventDialogProps>(
  function ManualEventDialog(
    { draft, setDraft, busy, error, dirty, onClose, onSubmit },
    ref,
  ) {
    const requestClose = () => {
      if (!busy && (!dirty || window.confirm('放弃尚未提交的事件？'))) onClose()
    }
    return <Modal
      ref={ref}
      labelledBy="manual-event-dialog-title"
      describedBy="manual-event-dialog-description"
      onRequestClose={requestClose}
    >
      <form className="modal-card" onSubmit={(event) => { event.preventDefault(); onSubmit() }}>
        <header className="modal-header">
          <div>
            <span className="eyebrow">MANUAL EVENT</span>
            <h2 id="manual-event-dialog-title">添加事件</h2>
            <p className="modal-description" id="manual-event-dialog-description">提交后立即进入通知队列。</p>
          </div>
          <button className="icon-button" type="button" aria-label="关闭事件编辑" disabled={busy} onClick={requestClose}><X size={19} /></button>
        </header>
        <div className="modal-body">
          <label><span>标题</span><input value={draft.title} onChange={change(setDraft, 'title')} maxLength={180} autoFocus required /></label>
          <label><span>详细内容</span><textarea value={draft.summary} onChange={change(setDraft, 'summary')} rows={7} maxLength={5000} required /></label>
          <div className="form-grid three">
            <label><span>重要程度</span><select value={draft.importance} onChange={(event) => setDraft((current) => ({ ...current, importance: Number(event.target.value) }))}>
              <option value="1">1 · 很低</option><option value="2">2 · 低</option><option value="3">3 · 普通</option><option value="4">4 · 高</option><option value="5">5 · 最高</option>
            </select></label>
            <label><span>地区</span><select value={draft.region} onChange={change(setDraft, 'region')}>
              <option value="GLOBAL">全球</option><option value="CN">中国</option><option value="JP">日本</option><option value="US">美国</option><option value="EU">欧洲</option>
            </select></label>
            <label><span>主题</span><select value={draft.topic} onChange={change(setDraft, 'topic')}>
              <option value="general">综合</option><option value="politics">政治</option><option value="economy">经济</option><option value="markets">市场</option><option value="technology">科技</option><option value="security">安全</option><option value="health">健康</option><option value="weather">天气</option><option value="personal">个人</option>
            </select></label>
          </div>
          <label><span>来源链接（可选）</span><input value={draft.source_url} onChange={change(setDraft, 'source_url')} type="url" inputMode="url" maxLength={2048} placeholder="https://example.com/report" /></label>
          {error && <p className="form-error" role="alert">{error}</p>}
        </div>
        <footer className="dialog-actions">
          <button className="button subtle" type="button" disabled={busy} onClick={requestClose}>取消</button>
          <button className="button primary" type="submit" disabled={busy || !draft.title.trim() || !draft.summary.trim()}>{busy ? <RefreshCw className="spin" size={17} /> : <Check size={17} />}{busy ? '正在创建…' : '创建并发送'}</button>
        </footer>
      </form>
    </Modal>
  },
)
