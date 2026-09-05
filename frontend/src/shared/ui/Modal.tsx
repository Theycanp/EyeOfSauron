import { forwardRef, type MouseEvent, type ReactNode } from 'react'

interface ModalProps {
  children: ReactNode
  labelledBy: string
  describedBy?: string
  className?: string
  onRequestClose?: () => void
}

export const Modal = forwardRef<HTMLDialogElement, ModalProps>(function Modal(
  { children, labelledBy, describedBy, className = '', onRequestClose },
  ref,
) {
  const handleBackdrop = (event: MouseEvent<HTMLDialogElement>) => {
    if (event.target === event.currentTarget) onRequestClose?.()
  }
  return <dialog
    ref={ref}
    className={`modal ${className}`}
    aria-labelledby={labelledBy}
    aria-describedby={describedBy}
    onClick={handleBackdrop}
    onCancel={(event) => {
      if (onRequestClose) {
        event.preventDefault()
        onRequestClose()
      }
    }}
  >{children}</dialog>
})
