'use client'

// Task 12: dettaglio campagna WhatsApp -- ultima pagina frontend di M2.
// KPI, azioni di ciclo vita, tabella contatti. Nessun import da lib/api.ts:
// solo waApi.ts, come da regola del mondo /wa.
//
// Vincolo duro del piano/contratto: nessuna azione qui scrive su colonne di
// M3 (locked_by/locked_at, sent/failed/replied...). Niente "forza invio",
// niente "sblocca" -- il lock stale lo rilascia solo il cron di M3
// (invariante I1). Le uniche scritture di questa pagina sono i quattro
// verbi di ciclo vita (start/pause/resume/stop) e la rimozione di un
// contatto non terminale e non sotto lock fresco.
import { use, useEffect, useState } from 'react'
import Link from 'next/link'
import useSWR from 'swr'
import { toast } from 'sonner'
import {
  ArrowLeft, AlertTriangle, Lock, Play, Pause, Square, RotateCcw, Wrench,
  ChevronLeft, ChevronRight, ChevronDown, Trash2,
} from 'lucide-react'
import {
  waApi, type WaCampaignStatus, type WaContactStatus, type WaCampaignContactRow,
  type WaCampaignDetail,
} from '@/lib/waApi'

// Lo stesso segnaposto che il wizard scrive alla creazione: qui serve solo a
// riconoscere una bozza il cui messaggio non e' MAI stato scritto, e dirlo.
const SEGNAPOSTO_MESSAGGIO = 'Messaggio da scrivere: completalo nel passo 3.'
import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ModaleMotivo } from '@/components/wa/ModaleMotivo'

const STATO_CAMPAGNA_LABEL: Record<WaCampaignStatus, string> = {
  draft: 'Bozza',
  running: 'In corso',
  paused: 'In pausa',
  stopped: 'Fermata',
  completed: 'Completata',
  error: 'Errore',
}

const STATO_CAMPAGNA_COLORE: Record<WaCampaignStatus, string> = {
  draft: 'var(--wa-muted)',
  running: '#26e0c4',
  paused: '#e0b83c',
  stopped: '#8fb3aa',
  completed: '#5b9dd9',
  error: '#e05c5c',
}

const TIPO_LABEL: Record<string, string> = {
  marketing: 'Marketing',
  followup: 'Follow-up',
}

const STATO_CONTATTO_LABEL: Record<WaContactStatus, string> = {
  queued: 'In coda',
  in_sequence: 'In sequenza',
  replied: 'Ha risposto',
  completed: 'Completato',
  opted_out: 'Opt-out',
  skipped: 'Saltato',
  failed: 'Fallito (in retry)',
}

const STATO_CONTATTO_COLORE: Record<WaContactStatus, string> = {
  queued: 'var(--wa-muted)',
  in_sequence: '#5b9dd9',
  replied: '#26e0c4',
  completed: '#8fb3aa',
  opted_out: '#e07a3c',
  skipped: 'var(--wa-muted)',
  failed: '#e0553c',
}

// Numero di righe per pagina della tabella contatti. Il backend accetta
// fino a 500 (wa_contacts.lista_contatti), 50 basta per una lista leggibile
// e mantiene la paginazione visibile anche su campagne piccole.
const RIGHE_PER_PAGINA = 50

function Riquadro({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="rounded-2xl border p-6"
      style={{ borderColor: 'var(--wa-border)', backgroundColor: 'var(--wa-surface)' }}
    >
      {children}
    </div>
  )
}

function Errore({ messaggio }: { messaggio: string }) {
  return (
    <div
      className="rounded-lg border px-4 py-3 text-sm"
      style={{ borderColor: '#7a3a3a', backgroundColor: 'rgba(122, 58, 58, 0.15)', color: '#f2b8b8' }}
    >
      {messaggio}
    </div>
  )
}

function StatCard({ label, value, color }: { label: string; value: number | string; color?: string }) {
  return (
    <div
      className="rounded-xl border p-4"
      style={{ borderColor: 'var(--wa-border)', backgroundColor: 'var(--wa-bg)' }}
    >
      <p className="text-xs font-medium" style={{ color: 'var(--wa-muted)' }}>{label}</p>
      <p className="mt-1 text-2xl font-bold" style={{ color: color ?? '#fff' }}>{value}</p>
    </div>
  )
}

export default function DettaglioCampagnaPage({ params }: { params: Promise<{ id: string }> }) {
  const { id: campaignId } = use(params)

  const {
    data: campagna, error: erroreCampagna, isLoading: caricandoCampagna, mutate: refreshCampagna,
  } = useSWR(['wa-campagna-detail', campaignId], () => waApi.campagne.get(campaignId))

  const { data: kpi, mutate: refreshKpi } = useSWR(
    ['wa-campagna-kpi', campaignId],
    () => waApi.campagne.kpi(campaignId),
    { refreshInterval: 10000 },
  )

  const [offset, setOffset] = useState(0)
  const {
    data: contattiData, error: erroreContatti, isLoading: caricandoContatti, mutate: refreshContatti,
  } = useSWR(
    ['wa-campagna-contatti', campaignId, offset],
    () => waApi.contatti.list(campaignId, { limit: RIGHE_PER_PAGINA, offset }),
    { keepPreviousData: true },
  )

  async function refreshTutto() {
    await Promise.all([refreshCampagna(), refreshKpi(), refreshContatti()])
  }

  // ---- Azioni di ciclo vita: start/pause/resume/stop -----------------------
  // Il messaggio d'errore che arriva qui e' testuale, gia' scritto per un
  // umano dal servizio di dominio (es. "Il numero non e' gia' attivo: serve
  // una sessione WhatsApp valida (QR) prima di far partire la campagna.").
  // Si mostra COSI' COM'E', in un banner persistente (non solo un toast che
  // sparisce) perche' la verifica di questa pagina lo richiede per intero.
  const [erroreAzione, setErroreAzione] = useState<string | null>(null)
  const [azioneInCorso, setAzioneInCorso] = useState<string | null>(null)

  async function eseguiAzione(nome: string, chiamata: () => Promise<unknown>, messaggioOk: string) {
    setErroreAzione(null)
    setAzioneInCorso(nome)
    try {
      await chiamata()
      await refreshTutto()
      toast.success(messaggioOk)
    } catch (err: unknown) {
      const messaggio = err instanceof Error ? err.message : 'Errore imprevisto.'
      setErroreAzione(messaggio)
      toast.error(messaggio)
    } finally {
      setAzioneInCorso(null)
    }
  }

  // ---- Recupero da errore (G2) -----------------------------------------------
  // error -> paused, MAI -> running: due click consapevoli, e' voluto (design
  // doc §2.1). Percorso a parte da eseguiAzione perche' il modale (tendina +
  // testo libero) resta aperto se la chiamata fallisce, cosi' non si perde il
  // motivo gia' scritto.
  const [recuperaOpen, setRecuperaOpen] = useState(false)

  async function handleRecupera(motivo: string) {
    setErroreAzione(null)
    setAzioneInCorso('recupera')
    try {
      await waApi.campagne.recover(campaignId, motivo)
      await refreshTutto()
      toast.success('Campagna recuperata: e\' in pausa. Riprendila con il bottone "Riprendi" quando sei pronto.')
      setRecuperaOpen(false)
    } catch (err: unknown) {
      const messaggio = err instanceof Error ? err.message : 'Errore imprevisto.'
      setErroreAzione(messaggio)
      toast.error(messaggio)
    } finally {
      setAzioneInCorso(null)
    }
  }

  // ---- Rimozione contatto (Q18) ---------------------------------------------
  const [confermaRimozioneId, setConfermaRimozioneId] = useState<string | null>(null)
  const [rimuovendoId, setRimuovendoId] = useState<string | null>(null)

  async function handleRimuovi(cc: WaCampaignContactRow) {
    setRimuovendoId(cc.id)
    try {
      await waApi.contatti.rimuovi(cc.id)
      toast.success('Contatto rimosso dalla campagna.')
      const aggiornati = await refreshContatti()
      // Trovato in review: rimuovere l'ultimo contatto rimasto sull'ULTIMA
      // pagina svuota la pagina corrente senza toccare l'offset -- il
      // messaggio "nessun contatto caricato" diventa fuorviante (i contatti
      // ci sono ancora, sono solo sulla pagina precedente). Si torna
      // indietro di una pagina, non si finge che la campagna sia vuota.
      if (aggiornati && aggiornati.contatti.length === 0 && offset > 0) {
        setOffset(Math.max(0, offset - RIGHE_PER_PAGINA))
      }
    } catch (err: unknown) {
      // Caso raro (race col worker fra il render e il click, malgrado il
      // bottone gia' disabilitato per i casi noti): messaggio del backend
      // testuale, mai riscritto.
      toast.error(err instanceof Error ? err.message : 'Errore nella rimozione del contatto.')
    } finally {
      setRimuovendoId(null)
    }
  }

  function motivoRimozioneBloccata(cc: WaCampaignContactRow): string | null {
    if (cc.in_lavorazione) return 'In lavorazione dal worker: non rimovibile finche\' il lock non scade.'
    // I quattro stati terminali della state machine (WaContactStatus):
    // replied, completed, opted_out, skipped. failed e' transitorio.
    // Trovato in whole-branch review: mancavano replied/skipped, coerente
    // (ma sbagliato) col backend prima del fix -- invisibile finche' M2
    // non crea mai righe in quegli stati (li scrive M3).
    if (cc.stato === 'opted_out' || cc.stato === 'completed'
        || cc.stato === 'replied' || cc.stato === 'skipped') {
      return `Riga in stato terminale (${STATO_CONTATTO_LABEL[cc.stato]}): resta come storico.`
    }
    return null
  }

  if (erroreCampagna) {
    return (
      <div className="space-y-4">
        <BackLink />
        <Errore messaggio="Backend non raggiungibile o campagna inesistente. Avviare il server e ricaricare la pagina." />
      </div>
    )
  }

  if (caricandoCampagna || !campagna) {
    return (
      <div className="space-y-4">
        <BackLink />
        <p style={{ color: 'var(--wa-muted)' }}>Caricamento...</p>
      </div>
    )
  }

  const puoAvviare = campagna.status === 'draft'
  const puoPausare = campagna.status === 'running'
  const puoRiprendere = campagna.status === 'paused'
  const puoFermare = campagna.status !== 'completed' && campagna.status !== 'stopped'
  const puoRecuperare = campagna.status === 'error'

  return (
    <div className="space-y-6">
      <BackLink />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">{campagna.name}</h1>
          <p className="mt-1 text-sm" style={{ color: 'var(--wa-muted)' }}>
            {TIPO_LABEL[campagna.campaign_type] ?? campagna.campaign_type}
          </p>
        </div>
        <span
          className="rounded px-3 py-1 text-sm font-medium"
          style={{ backgroundColor: 'var(--wa-accent-soft)', color: STATO_CAMPAGNA_COLORE[campagna.status] }}
        >
          {STATO_CAMPAGNA_LABEL[campagna.status] ?? campagna.status}
        </span>
      </div>

      {/* ---- KPI --------------------------------------------------------- */}
      <Riquadro>
        <div className="space-y-4">
          <p className="text-sm font-medium text-white">KPI</p>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <StatCard label="Caricati" value={kpi?.caricati ?? '-'} />
            <StatCard label="Inviati" value={kpi?.inviati ?? '-'} />
            <StatCard label="Da inviare" value={kpi?.da_inviare ?? '-'} />
            <StatCard label="Risposti" value={kpi?.risposti ?? '-'} color="#26e0c4" />
            <StatCard label="Opt-out" value={kpi?.optout ?? '-'} color="#e07a3c" />
            <StatCard label="Falliti" value={kpi?.falliti ?? '-'} color="#e05c5c" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <StatCard label="Tasso di risposta" value={kpi ? `${kpi.tasso_risposta}%` : '-'} color="#26e0c4" />
            <StatCard label="Tasso di opt-out" value={kpi ? `${kpi.tasso_optout}%` : '-'} color="#e07a3c" />
          </div>

          {/* Nota di onesta' del dato: SEMPRE visibile, non in un tooltip
              nascosto. Testo del backend, mai riscritto qui. */}
          {kpi?.nota && (
            <p className="text-xs" style={{ color: 'var(--wa-muted)' }}>{kpi.nota}</p>
          )}

          {kpi?.allarme_optout && (
            <div
              className="flex items-start gap-2 rounded-lg border px-4 py-3 text-sm"
              style={{ borderColor: '#7a5a2a', backgroundColor: 'rgba(224, 122, 60, 0.12)', color: '#f2c9a0' }}
            >
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                Tasso di opt-out oltre la soglia di allarme ({kpi.tasso_optout}%): valutare di mettere
                la campagna in pausa. La decisione resta dell&apos;operatore, questa pagina non mette
                in pausa da sola.
              </span>
            </div>
          )}
        </div>
      </Riquadro>

      {/* ---- Modifica, solo in bozza ---------------------------------------
          Il backend accetta PATCH /{id} e PUT /{id}/steps/0 SOLO in draft
          (una campagna partita si mette in pausa prima di toccarla). Finche'
          questo riquadro non esisteva, una bozza lasciata a meta' -- messaggio
          non scritto perche' non era ancora pronto -- non era piu'
          modificabile da nessuna parte: il wizard non si riapre su una
          campagna esistente. */}
      {campagna.status === 'draft' && (
        <ModificaBozza campagna={campagna} onSalvato={refreshTutto} />
      )}

      {/* ---- Azioni di ciclo vita ------------------------------------------ */}
      <Riquadro>
        <div className="space-y-3">
          <p className="text-sm font-medium text-white">Azioni</p>
          {erroreAzione && <Errore messaggio={erroreAzione} />}
          <div className="flex flex-wrap gap-2">
            {puoAvviare && (
              <AzioneButton
                icona={<Play className="h-4 w-4" />}
                etichetta="Avvia"
                titolo="Avvia campagna"
                descrizione="La campagna passera' in corso: da questo momento M3 potra' iniziare gli invii secondo il pacing configurato."
                confirmLabel="Avvia"
                variant="default"
                colore="var(--wa-accent)"
                loading={azioneInCorso === 'avvia'}
                onConfirm={() => eseguiAzione('avvia', () => waApi.campagne.start(campaignId), 'Campagna avviata.')}
              />
            )}
            {puoPausare && (
              <AzioneButton
                icona={<Pause className="h-4 w-4" />}
                etichetta="Pausa"
                titolo="Metti in pausa la campagna"
                descrizione="Gli invii si fermano al prossimo controllo del worker. I contatti e i contatori restano invariati."
                confirmLabel="Metti in pausa"
                variant="warning"
                colore="#e0b83c"
                loading={azioneInCorso === 'pausa'}
                onConfirm={() => eseguiAzione('pausa', () => waApi.campagne.pause(campaignId), 'Campagna in pausa.')}
              />
            )}
            {puoRiprendere && (
              <AzioneButton
                icona={<RotateCcw className="h-4 w-4" />}
                etichetta="Riprendi"
                titolo="Riprendi la campagna"
                descrizione="Ripassano le stesse validazioni dell'avvio (numero attivo, nessun'altra campagna in corso sullo stesso numero)."
                confirmLabel="Riprendi"
                variant="default"
                colore="var(--wa-accent)"
                loading={azioneInCorso === 'riprendi'}
                onConfirm={() => eseguiAzione('riprendi', () => waApi.campagne.resume(campaignId), 'Campagna ripresa.')}
              />
            )}
            {puoRecuperare && (
              <Button
                type="button"
                variant="outline"
                disabled={azioneInCorso === 'recupera'}
                onClick={() => setRecuperaOpen(true)}
                style={{ borderColor: '#5b9dd9', color: '#5b9dd9' }}
              >
                <Wrench className="h-4 w-4" />
                {azioneInCorso === 'recupera' ? 'Recupero...' : 'Recupera'}
              </Button>
            )}
            {puoFermare && (
              <AzioneButton
                icona={<Square className="h-4 w-4" />}
                etichetta="Stop"
                titolo="Ferma la campagna"
                descrizione="Stop definitivo: non si puo' ripartire da qui. I contatti e i KPI restano come storico."
                confirmLabel="Ferma"
                variant="destructive"
                colore="#e05c5c"
                loading={azioneInCorso === 'stop'}
                onConfirm={() => eseguiAzione('stop', () => waApi.campagne.stop(campaignId), 'Campagna fermata.')}
              />
            )}
          </div>
          <ModaleMotivo
            open={recuperaOpen}
            onOpenChange={setRecuperaOpen}
            title="Recupera campagna da errore"
            description="La campagna torna in 'In pausa': resta da riprendere a mano col bottone 'Riprendi', qui sotto, quando sei pronto. Il motivo finisce nel log e nell'evento della campagna, non in una colonna visibile."
            confirmLabel="Recupera"
            loading={azioneInCorso === 'recupera'}
            onConfirm={handleRecupera}
          />
        </div>
      </Riquadro>

      {/* ---- Tabella contatti ----------------------------------------------- */}
      <Riquadro>
        <div className="space-y-3">
          <p className="text-sm font-medium text-white">Contatti</p>

          {erroreContatti && (
            <Errore messaggio="Errore nel caricamento della lista contatti." />
          )}

          {caricandoContatti && !contattiData && (
            <p style={{ color: 'var(--wa-muted)' }}>Caricamento...</p>
          )}

          {contattiData && contattiData.contatti.length === 0 && (
            <p style={{ color: 'var(--wa-muted)' }}>Nessun contatto caricato per questa campagna.</p>
          )}

          {contattiData && contattiData.contatti.length > 0 && (
            <div className="overflow-hidden rounded-xl border" style={{ borderColor: 'var(--wa-border)' }}>
              <table className="w-full text-sm">
                <thead>
                  <tr style={{ backgroundColor: 'var(--wa-bg)', color: 'var(--wa-muted)' }}>
                    <th className="px-3 py-2 text-left font-medium">Numero</th>
                    <th className="px-3 py-2 text-left font-medium">Nome</th>
                    <th className="px-3 py-2 text-left font-medium">Stato</th>
                    <th className="px-3 py-2 text-right font-medium">Tentativi falliti</th>
                    <th className="px-3 py-2 text-left font-medium">Ultimo errore</th>
                    <th className="px-3 py-2 text-left font-medium">Azioni</th>
                  </tr>
                </thead>
                <tbody>
                  {contattiData.contatti.map((cc) => {
                    const motivoBloccato = motivoRimozioneBloccata(cc)
                    return (
                      <tr key={cc.id} style={{ borderTop: '1px solid var(--wa-border)' }}>
                        <td className="px-3 py-2 font-mono" style={{ color: '#e7f3ef' }}>
                          <div className="flex items-center gap-1.5">
                            {cc.in_lavorazione && (
                              <Lock className="h-3.5 w-3.5 shrink-0" style={{ color: '#e0b83c' }} />
                            )}
                            {cc.numero}
                          </div>
                        </td>
                        <td className="px-3 py-2" style={{ color: 'var(--wa-muted)' }}>
                          {cc.nome ?? <span style={{ opacity: 0.6 }}>-</span>}
                        </td>
                        <td className="px-3 py-2">
                          <span
                            className="rounded px-2 py-0.5 text-xs font-medium"
                            style={{ backgroundColor: 'var(--wa-accent-soft)', color: STATO_CONTATTO_COLORE[cc.stato] }}
                          >
                            {STATO_CONTATTO_LABEL[cc.stato] ?? cc.stato}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right" style={{ color: 'var(--wa-muted)' }}>
                          {cc.tentativi_falliti}
                        </td>
                        <td className="px-3 py-2 text-xs" style={{ color: 'var(--wa-muted)' }}>
                          {cc.ultimo_errore ?? <span style={{ opacity: 0.6 }}>-</span>}
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex flex-col gap-1">
                            <Button
                              size="sm"
                              variant="outline"
                              type="button"
                              disabled={!!motivoBloccato || rimuovendoId === cc.id}
                              title={motivoBloccato ?? 'Rimuovi il contatto dalla campagna'}
                              onClick={() => setConfermaRimozioneId(cc.id)}
                              style={{ borderColor: '#e05c5c', color: '#e05c5c' }}
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                              {rimuovendoId === cc.id ? 'Rimozione...' : 'Rimuovi'}
                            </Button>
                            {/* Spiegazione SEMPRE visibile quando disabilitato:
                                non e' un tooltip da scoprire al passaggio del
                                mouse, e non e' un 409 dopo il click. */}
                            {motivoBloccato && (
                              <span className="text-xs" style={{ color: 'var(--wa-muted)' }}>{motivoBloccato}</span>
                            )}
                          </div>
                          <ConfirmDialog
                            open={confermaRimozioneId === cc.id}
                            onOpenChange={(v) => setConfermaRimozioneId(v ? cc.id : null)}
                            title="Rimuovi contatto dalla campagna"
                            description={`Il contatto ${cc.numero} viene tolto da questa campagna. Non e' un'azione su un contatto opted_out/do-not-contact: solo la riga di questa campagna.`}
                            confirmLabel="Rimuovi"
                            variant="destructive"
                            onConfirm={() => handleRimuovi(cc)}
                          />
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          <div className="flex items-center justify-between pt-1">
            <span className="text-xs" style={{ color: 'var(--wa-muted)' }}>
              {kpi ? `${kpi.caricati} contatti caricati in totale` : ''}
            </span>
            <div className="flex items-center gap-2">
              <Button
                size="sm" variant="outline" type="button"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - RIGHE_PER_PAGINA))}
                style={{ borderColor: 'var(--wa-border)', color: 'var(--wa-muted)' }}
              >
                <ChevronLeft className="h-3.5 w-3.5" /> Precedente
              </Button>
              <Button
                size="sm" variant="outline" type="button"
                disabled={(contattiData?.contatti.length ?? 0) < RIGHE_PER_PAGINA}
                onClick={() => setOffset(offset + RIGHE_PER_PAGINA)}
                style={{ borderColor: 'var(--wa-border)', color: 'var(--wa-muted)' }}
              >
                Successiva <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
        </div>
      </Riquadro>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Modifica di una bozza: messaggio e impostazioni.
//
// Due form separati e due salvataggi separati perche' sono due rotte diverse
// con due validazioni diverse: il messaggio passa da PUT /steps/0, che
// rivalida i segnaposto contro le colonne EFFETTIVAMENTE presenti nei
// contatti caricati; le impostazioni da PATCH /{id}, che rifiuta 422 se
// l'opt-out resta attivo senza CTA. Un salvataggio unico nasconderebbe quale
// delle due ha rifiutato.
// ---------------------------------------------------------------------------
function ModificaBozza({ campagna, onSalvato }: {
  campagna: WaCampaignDetail; onSalvato: () => Promise<void>
}) {
  const [aperto, setAperto] = useState(false)

  const [testo, setTesto] = useState(campagna.step_0.template_a ?? '')
  const [salvandoTesto, setSalvandoTesto] = useState(false)
  const [erroreTesto, setErroreTesto] = useState<string | null>(null)

  const [nome, setNome] = useState(campagna.name)
  const [cta, setCta] = useState(campagna.optout_cta ?? '')
  const [salvandoImp, setSalvandoImp] = useState(false)
  const [erroreImp, setErroreImp] = useState<string | null>(null)

  // Il testo a DB puo' cambiare sotto (un altro salvataggio, un refresh):
  // si riallinea la textarea solo quando il pannello e' chiuso, cosi' non si
  // cancella sotto le dita cio' che si sta scrivendo.
  useEffect(() => {
    if (!aperto) {
      setTesto(campagna.step_0.template_a ?? '')
      setNome(campagna.name)
      setCta(campagna.optout_cta ?? '')
    }
  }, [aperto, campagna.step_0.template_a, campagna.name, campagna.optout_cta])

  async function salvaTesto() {
    setErroreTesto(null)
    setSalvandoTesto(true)
    try {
      await waApi.campagne.updateStep0(campagna.id, { template_a: testo })
      await onSalvato()
      toast.success('Messaggio salvato.')
    } catch (e: unknown) {
      // messaggio del backend testuale (es. segnaposto ignoto), mai un
      // "Errore 422" generico
      setErroreTesto(e instanceof Error ? e.message : 'Errore nel salvataggio del messaggio.')
    } finally {
      setSalvandoTesto(false)
    }
  }

  async function salvaImpostazioni() {
    setErroreImp(null)
    setSalvandoImp(true)
    try {
      await waApi.campagne.update(campagna.id, {
        name: nome.trim(),
        optout_cta: cta.trim() || null,
      })
      await onSalvato()
      toast.success('Impostazioni salvate.')
    } catch (e: unknown) {
      setErroreImp(e instanceof Error ? e.message : 'Errore nel salvataggio delle impostazioni.')
    } finally {
      setSalvandoImp(false)
    }
  }

  const testoVuoto = testo.trim().length === 0
  const eSegnaposto = testo.trim() === SEGNAPOSTO_MESSAGGIO

  return (
    <Riquadro>
      <div className="space-y-4">
        <button
          type="button"
          onClick={() => setAperto((v) => !v)}
          className="flex w-full items-center justify-between text-left"
        >
          <span className="text-sm font-medium text-white">
            Modifica la bozza
            {(testoVuoto || eSegnaposto) && (
              <span className="ml-2 text-xs" style={{ color: '#e07a3c' }}>
                — il messaggio non e&apos; ancora scritto
              </span>
            )}
          </span>
          {aperto ? <ChevronDown className="h-4 w-4" style={{ color: 'var(--wa-muted)' }} />
            : <ChevronRight className="h-4 w-4" style={{ color: 'var(--wa-muted)' }} />}
        </button>

        {aperto && (
          <div className="space-y-6">
            {/* ---- Messaggio ---- */}
            <div className="space-y-2">
              <label className="block text-sm font-medium text-white">Messaggio</label>
              {erroreTesto && <Errore messaggio={erroreTesto} />}
              <textarea
                value={testo}
                onChange={(e) => setTesto(e.target.value)}
                rows={8}
                className="w-full rounded-lg border px-3 py-2 text-sm"
                style={{ backgroundColor: 'transparent', borderColor: 'var(--wa-border)', color: '#e7f3ef' }}
              />
              <div className="flex items-center gap-3">
                <Button
                  type="button"
                  disabled={salvandoTesto || testoVuoto}
                  onClick={salvaTesto}
                  style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
                >
                  {salvandoTesto ? 'Salvataggio...' : 'Salva il messaggio'}
                </Button>
                {campagna.optout_enabled && campagna.optout_cta && (
                  <span className="text-xs" style={{ color: 'var(--wa-muted)' }}>
                    La CTA di opt-out viene aggiunta dal sistema, non va scritta qui.
                  </span>
                )}
              </div>
            </div>

            {/* ---- Impostazioni ---- */}
            <div className="space-y-2">
              <label className="block text-sm font-medium text-white">Impostazioni</label>
              {erroreImp && <Errore messaggio={erroreImp} />}
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <span className="mb-1 block text-xs" style={{ color: 'var(--wa-muted)' }}>Nome</span>
                  <input
                    value={nome}
                    onChange={(e) => setNome(e.target.value)}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{ backgroundColor: 'transparent', borderColor: 'var(--wa-border)', color: '#e7f3ef' }}
                  />
                </div>
                <div>
                  <span className="mb-1 block text-xs" style={{ color: 'var(--wa-muted)' }}>
                    CTA di opt-out
                  </span>
                  <input
                    value={cta}
                    onChange={(e) => setCta(e.target.value)}
                    className="w-full rounded-lg border px-3 py-2 text-sm"
                    style={{ backgroundColor: 'transparent', borderColor: 'var(--wa-border)', color: '#e7f3ef' }}
                  />
                </div>
              </div>
              <Button
                type="button"
                disabled={salvandoImp || !nome.trim()}
                onClick={salvaImpostazioni}
                variant="outline"
                style={{ borderColor: 'var(--wa-border)', color: 'var(--wa-muted)' }}
              >
                {salvandoImp ? 'Salvataggio...' : 'Salva le impostazioni'}
              </Button>
            </div>
          </div>
        )}
      </div>
    </Riquadro>
  )
}

function BackLink() {
  return (
    <Link
      href="/wa"
      className="inline-flex items-center gap-1.5 text-sm"
      style={{ color: 'var(--wa-muted)' }}
    >
      <ArrowLeft className="h-4 w-4" /> Torna alle campagne
    </Link>
  )
}

// Bottone di ciclo vita con conferma incorporata: stesso pattern di
// AvviaLoginQrButton (frontend/app/wa/numeri/page.tsx) -- apre un
// ConfirmDialog, e solo alla conferma esplicita chiama l'azione.
function AzioneButton({
  icona, etichetta, titolo, descrizione, confirmLabel, variant, colore, loading, onConfirm,
}: {
  icona: React.ReactNode
  etichetta: string
  titolo: string
  descrizione: string
  confirmLabel: string
  variant: 'destructive' | 'warning' | 'default'
  colore: string
  loading: boolean
  onConfirm: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button
        type="button"
        variant="outline"
        disabled={loading}
        onClick={() => setOpen(true)}
        style={{ borderColor: colore, color: colore }}
      >
        {icona} {loading ? `${etichetta}...` : etichetta}
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={titolo}
        description={descrizione}
        confirmLabel={confirmLabel}
        variant={variant}
        onConfirm={onConfirm}
      />
    </>
  )
}
