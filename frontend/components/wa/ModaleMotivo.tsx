'use client'

// Modale motivo condiviso fra G2 (recupera campagna in error) e G3 (ferma il
// canale): tendina di motivi frequenti + casella libera, sempre disponibile.
// La casella libera e' obbligatoria solo quando si sceglie "Altro" -- per le
// altre voci resta un dettaglio facoltativo che si aggiunge all'etichetta.
// Il valore consegnato a onConfirm e' gia' la stringa unica da mandare
// all'API (mai un oggetto {tendina, testo}): il backend vede solo `motivo`.
import { useState } from 'react'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

const MOTIVI_FREQUENTI = [
  { value: 'selettore_rotto', label: 'Selettore rotto' },
  { value: 'sessione_caduta', label: 'Sessione WhatsApp caduta' },
  { value: 'altro', label: 'Altro' },
] as const

export function ModaleMotivo({
  open, onOpenChange, title, description, confirmLabel, loading, onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel: string
  loading: boolean
  onConfirm: (motivo: string) => void
}) {
  const [motivoTendina, setMotivoTendina] = useState<string>(MOTIVI_FREQUENTI[0].value)
  const [testoLibero, setTestoLibero] = useState('')

  // Si riapre sempre da capo: non resta appeso a un tentativo precedente
  // annullato (stesso criterio di RiattivaButton/ModificaWarmupDayButton).
  const handleOpenChange = (v: boolean) => {
    onOpenChange(v)
    if (v) {
      setMotivoTendina(MOTIVI_FREQUENTI[0].value)
      setTestoLibero('')
    }
  }

  const isAltro = motivoTendina === 'altro'
  const testoValido = testoLibero.trim().length > 0
  const motivoValido = isAltro ? testoValido : true

  function componiMotivo(): string {
    if (isAltro) return testoLibero.trim()
    const etichetta = MOTIVI_FREQUENTI.find((m) => m.value === motivoTendina)?.label ?? ''
    return testoValido ? `${etichetta}: ${testoLibero.trim()}` : etichetta
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    // Guardia raddoppiata come nel resto del mondo /wa: il bottone e' gia'
    // disabled senza dettaglio su "Altro", ma il submit da tastiera deve
    // rifiutare comunque.
    if (!motivoValido) return
    onConfirm(componiMotivo())
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="bg-gray-900 border-gray-800 text-white">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription className="text-gray-400">{description}</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="space-y-1.5">
            <label className="text-sm text-gray-300">Motivo frequente</label>
            <select
              value={motivoTendina}
              onChange={(e) => setMotivoTendina(e.target.value)}
              className="w-full rounded-md border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white"
            >
              {MOTIVI_FREQUENTI.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <label className="text-sm text-gray-300">
              Dettaglio {isAltro ? '(obbligatorio)' : '(facoltativo)'}
            </label>
            <Textarea
              value={testoLibero}
              onChange={(e) => setTestoLibero(e.target.value)}
              placeholder={isAltro ? "Descrivi il motivo..." : 'Aggiungi dettagli, se servono...'}
              className="bg-gray-800 border-gray-700 text-white"
            />
            {isAltro && !testoValido && (
              <p className="text-xs text-gray-400">
                Il dettaglio e&apos; obbligatorio quando si sceglie &quot;Altro&quot;.
              </p>
            )}
          </div>
          <DialogFooter>
            <Button
              type="button" variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => onOpenChange(false)} disabled={loading}
            >
              Annulla
            </Button>
            <Button
              type="submit" disabled={loading || !motivoValido}
              className="bg-blue-600 hover:bg-blue-700"
            >
              {loading ? `${confirmLabel}...` : confirmLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
