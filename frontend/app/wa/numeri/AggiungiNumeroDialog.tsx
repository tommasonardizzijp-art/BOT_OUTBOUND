'use client'

// Creazione di un numero WhatsApp dalla UI.
//
// Fino a qui la pagina Numeri sapeva solo MOSTRARE numeri che qualcun altro
// aveva creato via API o script: `waApi.numeri.create` esisteva gia' e non lo
// chiamava nessuna interfaccia -- lo diceva testualmente il commento in
// backend/app/api/wa_numbers.py (`crea`). Chi non ha un terminale non poteva
// aggiungere un numero, cioe' non poteva fare il primo passo del canale.
//
// Il numero si scrive UNA volta sola e non si rivede piu': il backend lo
// cifra e da quel momento ogni endpoint lo restituisce mascherato (P12,
// contratto §2.3). Per questo il campo va compilato con attenzione, il
// dialog lo dice a chiare lettere, e l'errore di normalizzazione E.164 deve
// restare leggibile accanto al campo invece di sparire in un toast.
import { useState } from 'react'
import useSWR from 'swr'
import { toast } from 'sonner'

import { waApi } from '@/lib/waApi'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription, DialogTrigger,
} from '@/components/ui/dialog'

export function AggiungiNumeroButton({ onChanged }: { onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [tenantId, setTenantId] = useState('')
  const [label, setLabel] = useState('')
  const [numero, setNumero] = useState('')
  const [dailyCap, setDailyCap] = useState('')
  const [proxyUrl, setProxyUrl] = useState('')
  const [errore, setErrore] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  // I tenant si caricano solo a dialog aperto: la pagina Numeri serve anche a
  // chi non sta creando niente, e una GET /tenants a ogni visita sarebbe
  // lavoro speso per il caso raro.
  const { data: tenantsData } = useSWR(
    open ? 'wa-tenants-nuovo-numero' : null,
    () => waApi.tenants.list(),
  )

  const handleOpenChange = (v: boolean) => {
    setOpen(v)
    // Si riapre sempre pulito: un tentativo annullato non deve lasciare in
    // giro un numero di telefono mezzo digitato.
    if (v) {
      setTenantId(''); setLabel(''); setNumero('')
      setDailyCap(''); setProxyUrl(''); setErrore(null)
    }
  }

  // Stessa validazione di ModificaWarmupDayButton e per lo stesso motivo:
  // <input type="number"> accetta la notazione esponenziale e Number("1e3")
  // e' l'intero 1000, quindi la forma va controllata sulla STRINGA. Vuoto e'
  // legittimo: il backend applica WA_DAILY_CAP_DEFAULT.
  const capVuoto = dailyCap.trim().length === 0
  const capValido = capVuoto || (/^\d+$/.test(dailyCap.trim()) && Number(dailyCap) > 0)
  const valido = tenantId !== '' && label.trim() !== '' && numero.trim() !== '' && capValido

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    // Guardia raddoppiata come negli altri dialog della pagina: il bottone e'
    // gia' disabled, ma il submit da tastiera deve rifiutare comunque.
    if (!valido) return
    setLoading(true)
    setErrore(null)
    try {
      const creato = await waApi.numeri.create({
        tenant_id: tenantId,
        label: label.trim(),
        numero: numero.trim(),
        ...(capVuoto ? {} : { daily_cap: Number(dailyCap) }),
        ...(proxyUrl.trim() ? { proxy_url: proxyUrl.trim() } : {}),
      })
      toast.success(`${creato.label} aggiunto: ora va collegato con "Avvia login QR"`)
      setOpen(false)
      onChanged()
    } catch (e: unknown) {
      // In linea nel dialog e NON in un toast: il toast sparisce e il modulo
      // resta compilato senza che si sappia piu' cosa non andava. I 422 che
      // arrivano qui sono azionabili (numero non normalizzabile, numero gia'
      // registrato), quindi vanno letti accanto al campo da correggere.
      setErrore(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger
        render={
          <Button
            type="button"
            className="shrink-0"
            style={{ backgroundColor: 'var(--wa-accent)', color: '#06231e' }}
          />
        }
      >
        Aggiungi numero
      </DialogTrigger>
      <DialogContent className="bg-gray-900 border-gray-800 text-white">
        <DialogHeader>
          <DialogTitle>Aggiungi un numero WhatsApp</DialogTitle>
          <DialogDescription className="text-gray-400">
            Il numero nasce in <code>pending_qr</code> e non invia nulla finche&apos; non lo
            colleghi con &quot;Avvia login QR&quot;, che apre un browser sulla macchina che
            ospita il backend. Dopo il salvataggio il numero e&apos; cifrato e non sara&apos;
            piu&apos; visibile in chiaro da nessuna schermata: controllalo adesso.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="space-y-1.5">
            <label htmlFor="nuovo-numero-tenant" className="text-sm text-gray-300">Tenant</label>
            <select
              id="nuovo-numero-tenant"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              className="w-full rounded-md border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white"
            >
              <option value="">Seleziona un tenant...</option>
              {(tenantsData?.tenants ?? []).map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="nuovo-numero-label" className="text-sm text-gray-300">Etichetta</label>
            <Input
              id="nuovo-numero-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Es. Numero di servizio Primero"
              className="bg-gray-800 border-gray-700 text-white"
            />
            <p className="text-xs text-gray-400">
              E&apos; l&apos;unico modo per riconoscere questo numero nelle altre
              schermate: il numero vero e&apos; sempre mascherato.
            </p>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="nuovo-numero-numero" className="text-sm text-gray-300">
              Numero (formato internazionale)
            </label>
            <Input
              id="nuovo-numero-numero"
              value={numero}
              onChange={(e) => setNumero(e.target.value)}
              placeholder="+39 333 1234567"
              className="bg-gray-800 border-gray-700 text-white font-mono"
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="nuovo-numero-cap" className="text-sm text-gray-300">
              Cap giornaliero (facoltativo)
            </label>
            <Input
              id="nuovo-numero-cap"
              type="number"
              min={1}
              step={1}
              value={dailyCap}
              onChange={(e) => setDailyCap(e.target.value)}
              placeholder="Vuoto = default di sistema"
              className="bg-gray-800 border-gray-700 text-white"
            />
            {!capValido && (
              <p className="text-xs" style={{ color: '#e07a3c' }}>
                Serve un numero intero maggiore di zero, senza segni, decimali o
                notazione esponenziale.
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <label htmlFor="nuovo-numero-proxy" className="text-sm text-gray-300">
              Proxy (facoltativo)
            </label>
            <Input
              id="nuovo-numero-proxy"
              value={proxyUrl}
              onChange={(e) => setProxyUrl(e.target.value)}
              placeholder="http://utente:password@host:porta"
              className="bg-gray-800 border-gray-700 text-white font-mono"
            />
            {/* Stesso avviso della riga in tabella, ma detto PRIMA: qui si puo'
                ancora evitare il problema invece di leggerne il referto dopo
                (minaccia T3 della SDD). */}
            {proxyUrl.trim() === '' && (
              <p className="text-xs" style={{ color: '#e07a3c' }}>
                Senza proxy il traffico esce dall&apos;IP locale: se altri numeri escono
                dallo stesso indirizzo, Meta puo&apos; correlarli come stesso operatore.
              </p>
            )}
          </div>

          {errore && (
            <div
              className="rounded-lg border px-3 py-2 text-sm"
              style={{ borderColor: '#7a3a3a', backgroundColor: 'rgba(122, 58, 58, 0.15)', color: '#f2b8b8' }}
            >
              {errore}
            </div>
          )}

          <DialogFooter>
            <Button
              type="button" variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => setOpen(false)} disabled={loading}
            >
              Annulla
            </Button>
            <Button
              type="submit" disabled={loading || !valido}
              className="bg-blue-600 hover:bg-blue-700"
            >
              {loading ? 'Salvo...' : 'Aggiungi'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
