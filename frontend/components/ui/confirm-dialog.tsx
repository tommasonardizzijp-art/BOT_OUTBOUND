'use client'

import { useEffect, useRef } from 'react'

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

  // Guardia sul DOPPIO INVIO, e deve stare in un ref, non in uno state.
  // onClick chiude il dialog e poi chiama onConfirm: due click nello STESSO
  // tick (doppio click vero, o un click ripetuto da tastiera) girano
  // entrambi prima che React ri-renderizzi, quindi ne' `open=false` ne' un
  // eventuale `disabled` da state fanno in tempo a bloccare il secondo. Un
  // ref si aggiorna subito e regge anche dentro lo stesso tick.
  // Misurato il 16/08 su "Scansiona contatti": due click partivano come DUE
  // POST /wa/numbers/{id}/discover. A DB non nasceva una seconda run --
  // l'indice unico parziale su stato='running' la rifiuta e l'endpoint
  // traduce in 409 -- ma l'operatore si vedeva un errore rosso subito dopo
  // aver avviato una scansione riuscita.
  const inCorso = useRef(false)
  useEffect(() => {
    if (open) inCorso.current = false
  }, [open])

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
            onClick={() => {
              if (inCorso.current) return
              inCorso.current = true
              onOpenChange(false)
              onConfirm()
            }}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
