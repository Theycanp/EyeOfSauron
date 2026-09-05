import { forwardRef } from 'react'
import { History, Trash2 } from 'lucide-react'
import { Modal } from './Modal'

export interface Confirmation {
  kind: 'delete' | 'rollback'
  title: string
  detail: string
  confirmLabel: string
}

interface ConfirmDialogProps {
  confirmation: Confirmation | null
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}

export const ConfirmDialog = forwardRef<HTMLDialogElement, ConfirmDialogProps>(function ConfirmDialog(
  { confirmation, busy, onCancel, onConfirm },
  ref,
) {
  return <Modal ref={ref} className="small-modal" labelledBy="confirm-title" describedBy="confirm-detail" onRequestClose={onCancel}>
    <form className="modal-card" onSubmit={(event) => { event.preventDefault(); onConfirm() }}>
      <div className="confirm-body">
        <div className={`confirm-icon ${confirmation?.kind === 'rollback' ? 'warning' : ''}`}>
          {confirmation?.kind === 'rollback' ? <History size={23} /> : <Trash2 size={23} />}
        </div>
        <h2 id="confirm-title">{confirmation?.title || '请确认操作'}</h2>
        <p id="confirm-detail">{confirmation?.detail || ''}</p>
      </div>
      <footer className="dialog-actions">
        <button className="button subtle" type="button" onClick={onCancel}>取消</button>
        <button className={confirmation?.kind === 'delete' ? 'button danger-fill' : 'button primary'} type="submit" disabled={busy}>
          {confirmation?.confirmLabel || '确认'}
        </button>
      </footer>
    </form>
  </Modal>
})
