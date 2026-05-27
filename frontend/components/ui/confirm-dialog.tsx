'use client'

import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from './dialog'
import { Button } from './button'

interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel?: string
  variant?: 'destructive' | 'warning' | 'default'
  onConfirm: () => void
}

export function ConfirmDialog({
  open, onOpenChange, title, description,
  confirmLabel = 'Conferma', variant = 'destructive', onConfirm,
}: ConfirmDialogProps) {
  const btnClass =
    variant === 'destructive' ? 'bg-red-600 hover:bg-red-700' :
    variant === 'warning' ? 'bg-yellow-600 hover:bg-yellow-700' :
    'bg-purple-600 hover:bg-purple-700'

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-gray-900 border-gray-700 text-white max-w-sm">
        <DialogHeader>
          <DialogTitle className="text-white">{title}</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-gray-400 py-1">{description}</p>
        <DialogFooter>
          <Button variant="outline" className="border-gray-700 text-gray-300"
            onClick={() => onOpenChange(false)}>
            Annulla
          </Button>
          <Button
            className={btnClass}
            onClick={() => { onOpenChange(false); onConfirm() }}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
