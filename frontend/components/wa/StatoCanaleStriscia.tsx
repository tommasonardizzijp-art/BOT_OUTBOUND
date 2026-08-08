'use client'

// Striscia di stato del canale WhatsApp (G3): montata nel layout di TUTTE le
// pagine /wa/* perche' il momento in cui serve e' proprio mentre si guarda
// una campagna che sembra "In corso" e non lo e' -- e' cosi' che il collaudo
// M5 e' finito 90/90 verde con zero messaggi spediti. Polling ogni 10s,
// stesso ritmo del polling KPI della pagina di dettaglio campagna.
//
// Master switch: SOLA LETTURA. Renderlo comandabile vorrebbe dire spostarlo
// dal .env al DB (decisione di architettura rimandata, vedi design doc §2.2).
import { useState } from 'react'
import useSWR from 'swr'
import { toast } from 'sonner'
import { AlertTriangle, Ban, Play } from 'lucide-react'
import { waApi } from '@/lib/waApi'
import { Button } from '@/components/ui/button'
import { ModaleMotivo } from './ModaleMotivo'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from '@/components/ui/dialog'

function Voce({ label, valore, colore }: { label: string; valore: React.ReactNode; colore?: string }) {
  return (
    <span style={{ color: 'var(--wa-muted)' }}>
      {label}: <strong style={{ color: colore ?? '#e7f3ef' }}>{valore}</strong>
    </span>
  )
}

export default function StatoCanaleStriscia() {
  const { data, mutate } = useSWR('wa-ops-status', () => waApi.ops.status(), { refreshInterval: 10000 })

  const [haltOpen, setHaltOpen] = useState(false)
  const [haltLoading, setHaltLoading] = useState(false)
  const [resumeOpen, setResumeOpen] = useState(false)
  const [resumeLoading, setResumeLoading] = useState(false)

  async function handleHalt(motivo: string) {
    setHaltLoading(true)
    try {
      await waApi.ops.halt(motivo)
      toast.success('Canale WhatsApp fermato.')
      setHaltOpen(false)
      await mutate()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore nel fermare il canale.')
    } finally {
      setHaltLoading(false)
    }
  }

  async function handleResume() {
    setResumeLoading(true)
    try {
      await waApi.ops.resume()
      toast.success('Canale WhatsApp ripreso.')
      setResumeOpen(false)
      await mutate()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore nel riprendere il canale.')
    } finally {
      setResumeLoading(false)
    }
  }

  // Niente da mostrare finche' il primo fetch non torna: meglio muto che un
  // "canale attivo" sparato a caso mentre i dati veri sono ancora in volo.
  if (!data) return null

  // motivo_stop/cap_effettivo arrivano da una PR backend separata in corso in
  // parallelo (stesso design 08/08): possono non esserci ancora. undefined e
  // null si trattano allo stesso modo, mai un crash sulla UI.
  const motivoStop = data.motivo_stop ?? null
  const capEffettivo = data.cap_effettivo ?? null

  return (
    <div
      className="mb-6 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border px-4 py-3 text-sm"
      style={{
        borderColor: data.wa_halted ? '#7a3a3a' : 'var(--wa-border)',
        backgroundColor: data.wa_halted ? 'rgba(122, 58, 58, 0.15)' : 'var(--wa-surface)',
      }}
    >
      <div className="flex items-center gap-2">
        {data.wa_halted
          ? <AlertTriangle className="h-4 w-4 shrink-0" style={{ color: '#f2b8b8' }} />
          : <span className="h-2 w-2 rounded-full" style={{ backgroundColor: '#26e0c4' }} />}
        <span className="font-medium" style={{ color: data.wa_halted ? '#f2b8b8' : '#e7f3ef' }}>
          {data.wa_halted ? 'Canale fermo' : 'Canale attivo'}
        </span>
        {data.wa_halted && motivoStop && (
          <span style={{ color: '#f2b8b8' }}>— {motivoStop}</span>
        )}
      </div>

      <Voce
        label="Master switch"
        valore={data.send_enabled ? 'acceso (.env, sola lettura)' : 'spento (.env, sola lettura)'}
        colore={data.send_enabled ? '#26e0c4' : '#e07a3c'}
      />
      <Voce label="Numeri attivi" valore={data.numeri_attivi} />
      <Voce label="Campagne in corso" valore={data.campagne_running} />
      <Voce label="Inviati oggi" valore={data.inviati_oggi} />
      <Voce label="Cap effettivo" valore={capEffettivo ?? 'n/d'} />

      <div className="ml-auto flex gap-2">
        {!data.wa_halted && (
          <Button
            size="sm" variant="outline" type="button" onClick={() => setHaltOpen(true)}
            style={{ borderColor: '#e05c5c', color: '#e05c5c' }}
          >
            <Ban className="h-3.5 w-3.5" /> Ferma il canale
          </Button>
        )}
        {data.wa_halted && (
          <Button
            size="sm" variant="outline" type="button" onClick={() => setResumeOpen(true)}
            style={{ borderColor: 'var(--wa-accent)', color: 'var(--wa-accent)' }}
          >
            <Play className="h-3.5 w-3.5" /> Riprendi
          </Button>
        )}
      </div>

      <ModaleMotivo
        open={haltOpen}
        onOpenChange={setHaltOpen}
        title="Ferma il canale WhatsApp"
        description="Il canale si ferma per TUTTI i numeri e TUTTE le campagne: nessun invio finche' non lo riprendi da qui."
        confirmLabel="Ferma il canale"
        loading={haltLoading}
        onConfirm={handleHalt}
      />

      {/* Non e' un ConfirmDialog qualunque: prima mostra il motivo per cui il
          canale si era fermato (se lo si conosce), poi chiede conferma. Il
          caso brutto e' riprendere senza aver capito che il numero sta
          bruciando -- vedi design doc §2.2. */}
      <Dialog open={resumeOpen} onOpenChange={setResumeOpen}>
        <DialogContent className="bg-gray-900 border-gray-800 text-white">
          <DialogHeader>
            <DialogTitle>Riprendi il canale WhatsApp</DialogTitle>
            <DialogDescription className="text-gray-400">
              {motivoStop
                ? <>Il canale si era fermato per: <strong>{motivoStop}</strong>. Verifica che il motivo sia risolto prima di riprendere.</>
                : 'Nessun motivo registrato per lo stop (o il backend non lo espone ancora). Verifica lo stato del numero prima di riprendere.'}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button" variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => setResumeOpen(false)} disabled={resumeLoading}
            >
              Annulla
            </Button>
            <Button
              type="button" disabled={resumeLoading} className="bg-blue-600 hover:bg-blue-700"
              onClick={handleResume}
            >
              {resumeLoading ? 'Riprendo...' : 'Riprendi comunque'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
