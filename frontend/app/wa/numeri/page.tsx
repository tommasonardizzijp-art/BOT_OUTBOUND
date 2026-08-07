'use client'

// Pagina Numeri del canale WhatsApp (Task 10). Mostra solo cio' che il
// backend espone: il numero e' SEMPRE mascherato (mai in chiaro -- vedi il
// commento su WaNumber in lib/waApi.ts, non esiste un campo "intero"). Qui
// non offriamo mai una strada per scrivere sent_today/sent_date/status: le
// uniche azioni che scrivono sono "Riattiva" (manda solo il motivo, e'
// l'endpoint a decidere il resto, SS2.2) e "Modifica giorno rampa", che
// scrive SOLO warmup_day -- override manuale voluto (M5).
//
// Attenzione a come lo si racconta all'operatore: l'override NON congela la
// rampa. Al prossimo avanzamento (boot o cron) il numero risale del passo
// configurato in WA_WARMUP_ADVANCE_STEPS_PER_DAY partendo dal valore impostato
// qui, quindi una frenata fatta da questo dialog dura al massimo fino al
// giorno dopo. La leva che regge nel tempo e' il cap/giorno.
import { useState } from 'react'
import useSWR from 'swr'
import { toast } from 'sonner'
import { waApi, type WaNumber, type WaNumberStatus } from '@/lib/waApi'
import { AggiungiNumeroButton } from './AggiungiNumeroDialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription, DialogTrigger,
} from '@/components/ui/dialog'

const STATO_LABEL: Record<WaNumberStatus, string> = {
  pending_qr: 'QR in attesa',
  active: 'Attivo',
  qr_required: 'QR richiesto',
  disconnected: 'Disconnesso',
  cooldown: 'Cooldown',
  suspended: 'Sospeso',
  retired: 'Ritirato',
}

const STATO_COLORE: Record<WaNumberStatus, string> = {
  pending_qr: '#e0b83c',
  qr_required: '#e0b83c',
  active: '#26e0c4',
  cooldown: '#5b9dd9',
  disconnected: '#e07a3c',
  suspended: '#e05c5c',
  retired: '#8fb3aa',
}

function formatCheck(iso: string | null): string {
  if (!iso) return 'Mai'
  return new Date(iso).toLocaleString('it-IT', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

export default function WaNumeriPage() {
  const { data, error, isLoading, mutate } = useSWR(
    'wa-numeri',
    () => waApi.numeri.list(),
    { refreshInterval: 15000 },
  )

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-white">Numeri WhatsApp</h1>
          <p className="mt-1 text-sm" style={{ color: 'var(--wa-muted)' }}>
            Numeri collegati al canale WhatsApp, un profilo browser per numero.
          </p>
        </div>
        <AggiungiNumeroButton onChanged={mutate} />
      </div>

      {error && (
        <div
          className="rounded-lg border px-4 py-3 text-sm"
          style={{ borderColor: '#7a3a3a', backgroundColor: 'rgba(122, 58, 58, 0.15)', color: '#f2b8b8' }}
        >
          Backend non raggiungibile. Avviare il server e ricaricare la pagina.
        </div>
      )}

      {isLoading && !error && <p style={{ color: 'var(--wa-muted)' }}>Caricamento...</p>}

      {data && data.numeri.length === 0 && (
        <div
          className="rounded-2xl border p-10 text-center"
          style={{ borderColor: 'var(--wa-border)', backgroundColor: 'var(--wa-surface)' }}
        >
          <p className="text-lg font-medium text-white">Nessun numero configurato</p>
          <p className="mt-2 text-sm" style={{ color: 'var(--wa-muted)' }}>
            Usa &quot;Aggiungi numero&quot; qui sopra. Dopo averlo creato il numero nasce in
            &quot;QR in attesa&quot;: va collegato con &quot;Avvia login QR&quot; davanti allo schermo
            della macchina che ospita il backend.
          </p>
        </div>
      )}

      {data && data.numeri.length > 0 && (
        <div className="overflow-hidden rounded-2xl border" style={{ borderColor: 'var(--wa-border)' }}>
          <table className="w-full text-sm">
            <thead>
              <tr style={{ backgroundColor: 'var(--wa-surface)', color: 'var(--wa-muted)' }}>
                <th className="px-4 py-3 text-left font-medium">Etichetta</th>
                <th className="px-4 py-3 text-left font-medium">Numero</th>
                <th className="px-4 py-3 text-left font-medium">Stato</th>
                <th className="px-4 py-3 text-right font-medium">Cap/giorno</th>
                <th className="px-4 py-3 text-right font-medium">Giorno rampa</th>
                <th className="px-4 py-3 text-right font-medium">Inviati oggi</th>
                <th className="px-4 py-3 text-left font-medium">Proxy</th>
                <th className="px-4 py-3 text-left font-medium">Ultimo check</th>
                <th className="px-4 py-3 text-left font-medium">Azioni</th>
              </tr>
            </thead>
            <tbody>
              {data.numeri.map((n) => (
                <RigaNumero key={n.id} numero={n} onChanged={mutate} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function RigaNumero({ numero, onChanged }: { numero: WaNumber; onChanged: () => void }) {
  const senzaProxy = !numero.proxy_url

  return (
    <>
      <tr style={{ borderTop: '1px solid var(--wa-border)' }}>
        <td className="px-4 py-3 font-medium text-white">{numero.label}</td>
        {/* Numero SEMPRE mascherato dal backend: nessuna formattazione qui
            che possa ricostruire o esporre la parte nascosta. */}
        <td className="px-4 py-3 font-mono" style={{ color: 'var(--wa-muted)' }}>{numero.numero}</td>
        <td className="px-4 py-3">
          <span
            className="rounded px-2 py-0.5 text-xs font-medium"
            style={{ backgroundColor: 'var(--wa-accent-soft)', color: STATO_COLORE[numero.status] }}
          >
            {STATO_LABEL[numero.status] ?? numero.status}
          </span>
        </td>
        <td className="px-4 py-3 text-right" style={{ color: 'var(--wa-muted)' }}>{numero.daily_cap}</td>
        {/* Il giorno da solo non dice niente a chi guarda: "3" non fa capire
            che sono 40 messaggi. Il cap del gradino va accanto, e quando la
            rampa non pone alcun tetto (warmup_day 0) va detto a parole --
            altrimenti sembra solo un numero piu' basso, cioe' il contrario. */}
        <td className="px-4 py-3 text-right" style={{ color: 'var(--wa-muted)' }}>
          {numero.warmup_day}
          {numero.warmup_cap !== null
            ? <span className="ml-1 text-xs opacity-70">({numero.warmup_cap} msg)</span>
            : <span className="ml-1 text-xs" style={{ color: '#e07a3c' }}>(nessun tetto)</span>}
        </td>
        <td className="px-4 py-3 text-right" style={{ color: 'var(--wa-muted)' }}>{numero.sent_today}</td>
        <td className="px-4 py-3">
          {senzaProxy
            ? <span className="text-xs font-medium" style={{ color: '#e07a3c' }}>No</span>
            : <span className="text-xs" style={{ color: 'var(--wa-muted)' }}>Si</span>}
        </td>
        <td className="px-4 py-3" style={{ color: 'var(--wa-muted)' }}>{formatCheck(numero.session_checked_at)}</td>
        <td className="px-4 py-3">
          <div className="flex flex-wrap gap-2">
            {(numero.status === 'pending_qr' || numero.status === 'qr_required') && (
              <AvviaLoginQrButton numero={numero} onChanged={onChanged} />
            )}
            {numero.status !== 'retired' && (
              <VerificaSessioneButton numero={numero} onChanged={onChanged} />
            )}
            {(numero.status === 'retired' || numero.status === 'suspended') && (
              <RiattivaButton numero={numero} onChanged={onChanged} />
            )}
            {/* Su retired/suspended il bottone non e' solo inutile, e'
                ingannevole: "Riattiva" riporta warmup_day a 1 (wa_numbers.py,
                riattiva), quindi il valore impostato qui verrebbe buttato in
                silenzio subito dopo un toast di successo. Stesso criterio di
                gating delle altre tre azioni della riga. */}
            {numero.status !== 'retired' && numero.status !== 'suspended' && (
              <ModificaWarmupDayButton numero={numero} onChanged={onChanged} />
            )}
          </div>
        </td>
      </tr>
      {senzaProxy && (
        // Avviso ESPLICITO in UI (non solo nel log del backend): minaccia T3
        // della SDD -- numeri diversi correlati perche' escono dallo stesso IP.
        <tr>
          <td colSpan={9} className="px-4 pb-3 pt-0">
            <div
              className="rounded-lg border px-3 py-2 text-xs"
              style={{ borderColor: '#7a5a2a', backgroundColor: 'rgba(224, 122, 60, 0.12)', color: '#f2c9a0' }}
            >
              Nessun proxy configurato per <strong>{numero.label}</strong>: se altri numeri escono
              dallo stesso IP, Meta puo&apos; correlarli come stesso operatore (rischio SDD T3).
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function AvviaLoginQrButton({ numero, onChanged }: { numero: WaNumber; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)

  const handleConfirm = async () => {
    setLoading(true)
    try {
      await waApi.numeri.avviaLoginQr(numero.id)
      toast.success(`Login QR avviato per ${numero.label}`)
      onChanged()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <Button
        size="sm" variant="outline" type="button" disabled={loading}
        onClick={() => setOpen(true)}
        style={{ borderColor: 'var(--wa-accent)', color: 'var(--wa-accent)' }}
      >
        Avvia login QR
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Avvia login QR per ${numero.label}`}
        description="Apre un browser VISIBILE sulla macchina che ospita il backend. Premere solo se c'e' qualcuno davanti a quello schermo, pronto a scansionare il QR."
        confirmLabel="Avvia"
        variant="warning"
        onConfirm={handleConfirm}
      />
    </>
  )
}

function VerificaSessioneButton({ numero, onChanged }: { numero: WaNumber; onChanged: () => void }) {
  const [loading, setLoading] = useState(false)

  const handleClick = async () => {
    setLoading(true)
    try {
      const res = await waApi.numeri.verificaSessione(numero.id)
      toast.success(`${numero.label}: sessione ${res.status}`)
      onChanged()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Button
      size="sm" variant="ghost" type="button" disabled={loading}
      onClick={handleClick}
      style={{ color: 'var(--wa-muted)' }}
    >
      {loading ? 'Verifica...' : 'Verifica sessione'}
    </Button>
  )
}

function RiattivaButton({ numero, onChanged }: { numero: WaNumber; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [motivo, setMotivo] = useState('')
  const [loading, setLoading] = useState(false)

  const handleOpenChange = (v: boolean) => {
    setOpen(v)
    if (v) setMotivo('')
  }

  const motivoValido = motivo.trim().length > 0

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    // Guardia raddoppiata: il bottone e' gia' disabled senza motivo, ma il
    // submit del form (invio da tastiera) deve rifiutare comunque.
    if (!motivoValido) return
    setLoading(true)
    try {
      const res = await waApi.numeri.riattiva(numero.id, motivo.trim())
      toast.success(`${numero.label} riattivato: ${res.prossimo_passo}`)
      setOpen(false)
      onChanged()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger
        render={
          <Button
            size="sm" variant="outline" type="button"
            style={{ borderColor: '#5b9dd9', color: '#5b9dd9' }}
          />
        }
      >
        Riattiva
      </DialogTrigger>
      <DialogContent className="bg-gray-900 border-gray-800 text-white">
        <DialogHeader>
          <DialogTitle>Riattiva {numero.label}</DialogTitle>
          <DialogDescription className="text-gray-400">
            Il numero torna a <code>pending_qr</code>: contatori azzerati (inviati oggi, giorno di
            warmup) e va rifatto il login QR. Il warmup riparte dal giorno 1 (contratto SS2.2).
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="space-y-1.5">
            <label className="text-sm text-gray-300">Motivo (obbligatorio)</label>
            <Textarea
              value={motivo}
              onChange={(e) => setMotivo(e.target.value)}
              placeholder="Perche' si riattiva questo numero..."
              required
              className="bg-gray-800 border-gray-700 text-white"
            />
          </div>
          <DialogFooter>
            <Button
              type="button" variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => setOpen(false)} disabled={loading}
            >
              Annulla
            </Button>
            <Button
              type="submit" disabled={loading || !motivoValido}
              className="bg-blue-600 hover:bg-blue-700"
            >
              {loading ? 'Riattivo...' : 'Riattiva'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function ModificaWarmupDayButton({ numero, onChanged }: { numero: WaNumber; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [valore, setValore] = useState('')
  const [loading, setLoading] = useState(false)

  const handleOpenChange = (v: boolean) => {
    setOpen(v)
    // Precompilato col valore corrente: si riapre sempre allineato, non
    // resta appeso a un tentativo precedente annullato.
    if (v) setValore(String(numero.warmup_day))
  }

  const numerico = Number(valore)
  // `Number.isInteger` da solo non basta: `<input type="number">` accetta la
  // notazione esponenziale, e `Number("1e3")` e' l'intero 1000 -- passava in
  // silenzio e veniva salvato senza che chi ha digitato "1e3" se ne
  // accorgesse. La forma va controllata sulla STRINGA, non sul numero.
  const soloCifre = /^\d+$/.test(valore.trim())
  const valoreValido = soloCifre && Number.isInteger(numerico) && numerico >= 0
  // Distingue "non ho ancora scritto niente" da "ho scritto qualcosa di
  // sbagliato": senza, il bottone si disabilita e non dice perche'.
  const motivoNonValido = valore.trim().length === 0 || valoreValido
    ? null
    : 'Serve un numero intero senza segni, decimali o notazione esponenziale.'

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    // Guardia raddoppiata come in RiattivaButton: il bottone e' gia'
    // disabled su input non valido, ma il submit da tastiera deve rifiutare
    // comunque (stesso motivo).
    if (!valoreValido) return
    setLoading(true)
    try {
      await waApi.numeri.update(numero.id, { warmup_day: numerico })
      toast.success(`${numero.label}: giorno rampa aggiornato a ${numerico}`)
      setOpen(false)
      onChanged()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger
        render={
          <Button
            size="sm" variant="outline" type="button"
            style={{ borderColor: 'var(--wa-accent)', color: 'var(--wa-accent)' }}
          />
        }
      >
        Modifica giorno rampa
      </DialogTrigger>
      <DialogContent className="bg-gray-900 border-gray-800 text-white">
        <DialogHeader>
          <DialogTitle>Modifica giorno rampa — {numero.label}</DialogTitle>
          <DialogDescription className="text-gray-400">
            Il giorno di rampa e&apos; il gradino della rampa di volume: concorre al tetto di
            invio giornaliero insieme al cap/giorno, e il tetto effettivo e&apos; il piu&apos;
            basso dei due.
            {numero.warmup_cap !== null && (
              <> Oggi questo numero e&apos; al giorno {numero.warmup_day}, che vale{' '}
                <strong>{numero.warmup_cap} messaggi al giorno</strong>.</>
            )}
            {' '}Cambiarlo qui non ferma l&apos;avanzamento automatico: la rampa riprende
            comunque a salire al prossimo avanzamento, ripartendo dal valore che imposti ora.
            Per fermare davvero il volume dopo un warning, la leva e&apos; il cap/giorno.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="space-y-1.5">
            <label htmlFor={`warmup-day-${numero.id}`} className="text-sm text-gray-300">
              Giorno rampa (numero intero)
            </label>
            <Input
              id={`warmup-day-${numero.id}`}
              type="number"
              min={0}
              step={1}
              value={valore}
              onChange={(e) => setValore(e.target.value)}
              className="bg-gray-800 border-gray-700 text-white"
            />
            {/* Lo zero NON e' "il valore piu' prudente": toglie del tutto il
                tetto di rampa (resta solo il cap/giorno) e il numero non
                viene piu' avanzato in automatico. Chi lo sceglie pensando di
                frenare ottiene l'opposto, quindi va detto qui, dove sta per
                sbagliare. */}
            {valore.trim() === '0' && (
              <p className="text-xs" style={{ color: '#e07a3c' }}>
                Attenzione: 0 non e&apos; il valore piu&apos; prudente. Toglie del tutto il
                tetto della rampa (resta solo il cap/giorno) e questo numero non verra&apos;
                piu&apos; avanzato in automatico.
              </p>
            )}
            {motivoNonValido && (
              <p className="text-xs text-gray-400">{motivoNonValido}</p>
            )}
          </div>
          <DialogFooter>
            <Button
              type="button" variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => setOpen(false)} disabled={loading}
            >
              Annulla
            </Button>
            <Button
              type="submit" disabled={loading || !valoreValido}
              className="bg-blue-600 hover:bg-blue-700"
            >
              {loading ? 'Salvo...' : 'Salva'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
