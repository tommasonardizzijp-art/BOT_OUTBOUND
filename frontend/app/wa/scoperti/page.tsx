'use client'

// Pagina "Scoperti" del canale WhatsApp (Fase B, Task 6): approvazione delle
// chat trovate dall'auto-discover (Fase A) verso WaContact, poi arruolamento
// in una campagna in bozza. Nessun import da lib/api.ts: solo waApi.ts, come
// da regola del mondo /wa.
//
// Difesa "per costruzione" gia' in backend/app/services/wa_promote/regole.py:
// un gruppo (o qualunque riga non promuovibile) inviato nella POST viene
// SEMPRE scartato con un motivo, chiunque l'abbia messo nel body. Qui non
// serve una guardia client-side perfetta -- la checkbox di una riga non
// promuovibile resta scurita e spiegata, ma NON e' HTML-disabled: impedire
// il click non e' il punto (lo e' impedire l'esito, e quello lo fa gia' il
// backend). Chi la seleziona comunque la vede tornare nei "scarti" del
// report con il motivo, mai in silenzio.
import { useEffect, useState, useRef } from 'react'
import Link from 'next/link'
import useSWR from 'swr'
import { toast } from 'sonner'
import {
  ChevronDown, ChevronRight, ChevronLeft, ArrowRight, Users, AlertTriangle,
} from 'lucide-react'
import {
  waApi,
  type WaDiscoveredChat,
  type ReportPromozione,
  type ReportArruolamento,
} from '@/lib/waApi'
import { Button } from '@/components/ui/button'

const TIPO_CHAT_LABEL: Record<WaDiscoveredChat['tipo_chat'], string> = {
  individuale: 'Individuale',
  gruppo: 'Gruppo',
  ignoto: 'Ignoto',
}

const TIPO_CHAT_COLORE: Record<WaDiscoveredChat['tipo_chat'], string> = {
  individuale: '#26e0c4',
  gruppo: '#e07a3c',
  ignoto: 'var(--wa-muted)',
}

const STATUS_LABEL: Record<WaDiscoveredChat['status'], string> = {
  nuovo: 'Nuovo',
  promosso: 'Promosso',
  scartato: 'Scartato',
}

const STATUS_COLORE: Record<WaDiscoveredChat['status'], string> = {
  nuovo: '#5b9dd9',
  promosso: '#26e0c4',
  scartato: 'var(--wa-muted)',
}

// Righe per pagina lato client: stesso ordine di grandezza di RIGHE_PER_PAGINA
// in campagne/[id]/page.tsx (50), qui un po' piu' larga perche' i 273 record
// reali di Primero (Fase A) sono il volume di riferimento del collaudo.
const RIGHE_PER_PAGINA = 100

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

const selectStyle: React.CSSProperties = {
  backgroundColor: 'transparent',
  borderColor: 'var(--wa-border)',
  color: '#e7f3ef',
}

function formatData(iso: string | null): string {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('it-IT', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

// Spiegazione SOLO informativa per l'operatore: riusa la stessa logica di
// regole.promuovibile in modo approssimato (non e' la fonte di verita', lo
// e' il backend), giusto per non lasciare una checkbox scurita senza dire
// perche'.
// Testo umano per il `motivo` che il backend calcola (regole.py). Non
// ri-deriva la regola: si limita a tradurre il codice gia' deciso — l'ordine
// dei controlli vive in un solo posto (review finale di branch: prima
// esisteva una copia client-side con l'ordine invertito rispetto al
// backend, gruppo controllato prima di status invece che dopo).
const MOTIVO_LABEL: Record<string, string> = {
  gruppo: "Gruppo: mai promuovibile. Anche se selezionata, il backend la scarta comunque.",
  senza_numero: 'Nessun numero leggibile per questa chat.',
  gia_promosso: 'Gia\' promossa in precedenza.',
  scartato: 'Scartata in precedenza.',
}

function motivoNonPromuovibile(riga: WaDiscoveredChat): string | null {
  if (!riga.motivo) return null
  return MOTIVO_LABEL[riga.motivo] ?? `Non promuovibile (${riga.motivo}).`
}

export default function ScopertiPage() {
  // ---- Selettore numero -------------------------------------------------
  const { data: numeriData, error: erroreNumeri } = useSWR('wa-numeri-scoperti', () => waApi.numeri.list())
  const [numberId, setNumberId] = useState('')

  // ---- Filtri -------------------------------------------------------------
  const [filtroStatus, setFiltroStatus] = useState<'nuovo' | 'promosso' | 'scartato'>('nuovo')
  // 'tutti' (mai il default nascosto): i gruppi restano visibili finche'
  // l'operatore non sceglie esplicitamente di filtrarli via.
  const [filtroTipoChat, setFiltroTipoChat] = useState<'tutti' | WaDiscoveredChat['tipo_chat']>('tutti')
  const [filtroHaNumero, setFiltroHaNumero] = useState<'tutti' | 'si' | 'no'>('tutti')
  const [offset, setOffset] = useState(0)

  // Cambiare numero o filtro invalida selezione e report in corso: mostrare
  // un report di promozione di un altro numero/filtro sarebbe fuorviante.
  useEffect(() => {
    setOffset(0)
    setSelectedIds(new Set())
    setReportPromozione(null)
    setReportArruolamento(null)
    setCampagnaSelezionata('')
  }, [numberId, filtroStatus, filtroTipoChat, filtroHaNumero])

  const {
    data: scopertiData, error: erroreScoperti, isLoading: caricandoScoperti, mutate: refreshScoperti,
  } = useSWR(
    numberId
      ? ['wa-scoperti', numberId, filtroStatus, filtroTipoChat, filtroHaNumero, offset]
      : null,
    () => waApi.scoperti.list(numberId, {
      status: filtroStatus,
      tipoChat: filtroTipoChat === 'tutti' ? undefined : filtroTipoChat,
      haNumero: filtroHaNumero === 'tutti' ? undefined : filtroHaNumero === 'si',
      limit: RIGHE_PER_PAGINA,
      offset,
    }),
    { keepPreviousData: true },
  )

  const righe = scopertiData?.chat ?? []

  // ---- Selezione multipla --------------------------------------------------
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  function toggleId(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // "Seleziona tutti" opera solo sulle righe promuovibili della pagina
  // corrente: un bulk-select che raccoglie anche i gruppi confonderebbe
  // l'operatore, anche se il backend li scarterebbe comunque.
  const promuovibiliVisibili = righe.filter((r) => r.promuovibile)
  const tuttiSelezionati = promuovibiliVisibili.length > 0
    && promuovibiliVisibili.every((r) => selectedIds.has(r.id))

  function toggleSelezionaTutti() {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (tuttiSelezionati) {
        promuovibiliVisibili.forEach((r) => next.delete(r.id))
      } else {
        promuovibiliVisibili.forEach((r) => next.add(r.id))
      }
      return next
    })
  }

  // "Seleziona tutti i filtrati (tutte le pagine)": la checkbox d'intestazione
  // opera solo sulla pagina corrente (RIGHE_PER_PAGINA=100) -- con centinaia
  // di righe selezionare pagina per pagina e' impraticabile (richiesto dopo
  // il primo giro reale su Primero, 273 righe). Pagina lato server con lo
  // stesso limite massimo che l'API gia' accetta (500, vedi wa_discover.py),
  // accumulando SOLO gli id promuovibili -- un gruppo o una riga gia'
  // promossa non finiscono comunque in selezione, coerente con "seleziona
  // tutti" della pagina singola qui sopra.
  const LIMITE_FETCH_TUTTI = 500
  const [caricandoSelezioneTotale, setCaricandoSelezioneTotale] = useState(false)

  async function selezionaTuttiFiltrati() {
    if (!numberId) return
    setCaricandoSelezioneTotale(true)
    try {
      const tuttiIds = new Set<string>()
      let offsetFetch = 0
      while (true) {
        const pagina = await waApi.scoperti.list(numberId, {
          status: filtroStatus,
          tipoChat: filtroTipoChat === 'tutti' ? undefined : filtroTipoChat,
          haNumero: filtroHaNumero === 'tutti' ? undefined : filtroHaNumero === 'si',
          limit: LIMITE_FETCH_TUTTI,
          offset: offsetFetch,
        })
        pagina.chat.forEach((r) => { if (r.promuovibile) tuttiIds.add(r.id) })
        if (pagina.chat.length < LIMITE_FETCH_TUTTI) break
        offsetFetch += LIMITE_FETCH_TUTTI
      }
      setSelectedIds(tuttiIds)
      toast.success(`${tuttiIds.size} righe selezionate (tutte le pagine, filtri correnti).`)
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : 'Errore nel selezionare tutte le righe.')
    } finally {
      setCaricandoSelezioneTotale(false)
    }
  }

  // ---- Promozione -----------------------------------------------------------
  const [promuovendo, setPromuovendo] = useState(false)
  const [erroreCompletamento, setErroreCompletamento] = useState<string | null>(null)
  const [reportPromozione, setReportPromozione] = useState<ReportPromozione | null>(null)
  const [dettagliScartiPromozione, setDettagliScartiPromozione] = useState(false)

  async function handlePromuovi() {
    if (!numberId || selectedIds.size === 0) return
    setErroreCompletamento(null)
    setPromuovendo(true)
    try {
      const risultato = await waApi.scoperti.promote(numberId, Array.from(selectedIds))
      setReportPromozione(risultato)
      setDettagliScartiPromozione(false)
      setReportArruolamento(null)
      setCampagnaSelezionata('')
      setSelectedIds(new Set())
      // Ricarica la lista: le righe appena promosse spariscono dal filtro
      // status=nuovo di default (invariante: status non torna mai indietro).
      await refreshScoperti()
      toast.success(`${risultato.promossi} contatti promossi.`)
    } catch (err: unknown) {
      const messaggio = err instanceof Error ? err.message : 'Errore nella promozione.'
      setErroreCompletamento(messaggio)
      toast.error(messaggio)
    } finally {
      setPromuovendo(false)
    }
  }

  // ---- Arruolamento in campagna draft --------------------------------------
  const { data: campagneData } = useSWR(
    numberId ? ['wa-campagne-draft-scoperti', numberId] : null,
    () => waApi.campagne.list({ status: 'draft' }),
  )
  // Filtro client-side per wa_number_id, come da piano: l'API non offre un
  // filtro diretto per numero, solo per tenant/status.
  const campagneDraft = (campagneData?.campagne ?? []).filter((c) => c.wa_number_id === numberId)

  const [campagnaSelezionata, setCampagnaSelezionata] = useState('')
  const [arruolando, setArruolando] = useState(false)
  const [reportArruolamento, setReportArruolamento] = useState<ReportArruolamento | null>(null)

  async function handleArruola() {
    if (!campagnaSelezionata || !reportPromozione || reportPromozione.contatti_promossi_ids.length === 0) return
    setArruolando(true)
    try {
      const risultato = await waApi.contatti.enroll(campagnaSelezionata, reportPromozione.contatti_promossi_ids)
      setReportArruolamento(risultato)
      toast.success(`${risultato.arruolati} contatti aggiunti alla campagna.`)
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : "Errore nell'arruolamento.")
    } finally {
      setArruolando(false)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Scoperti</h1>
        <p className="mt-1 text-sm" style={{ color: 'var(--wa-muted)' }}>
          Chat trovate dallo scan auto-discover, in attesa di approvazione. Un gruppo compare
          sempre in lista (mai nascosto): solo la promozione lo blocca, non la visibilita&apos;.
        </p>
      </div>

      {erroreNumeri && (
        <Errore messaggio="Impossibile caricare i numeri WhatsApp. Avviare il server e ricaricare la pagina." />
      )}

      {/* ---- Filtri ---------------------------------------------------- */}
      <Riquadro>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium" style={{ color: 'var(--wa-muted)' }}>Numero WhatsApp</label>
            <select
              className="h-8 w-full rounded-lg border px-2.5 text-sm"
              style={selectStyle}
              value={numberId}
              onChange={(e) => setNumberId(e.target.value)}
            >
              <option value="">Seleziona un numero...</option>
              {(numeriData?.numeri ?? []).map((n) => (
                <option key={n.id} value={n.id}>{n.label}</option>
              ))}
            </select>
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium" style={{ color: 'var(--wa-muted)' }}>Stato</label>
            <select
              className="h-8 w-full rounded-lg border px-2.5 text-sm"
              style={selectStyle}
              value={filtroStatus}
              onChange={(e) => setFiltroStatus(e.target.value as typeof filtroStatus)}
            >
              <option value="nuovo">Nuovo</option>
              <option value="promosso">Promosso</option>
              <option value="scartato">Scartato</option>
            </select>
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium" style={{ color: 'var(--wa-muted)' }}>Tipo chat</label>
            <select
              className="h-8 w-full rounded-lg border px-2.5 text-sm"
              style={selectStyle}
              value={filtroTipoChat}
              onChange={(e) => setFiltroTipoChat(e.target.value as typeof filtroTipoChat)}
            >
              <option value="tutti">Tutti (inclusi i gruppi)</option>
              <option value="individuale">Individuale</option>
              <option value="gruppo">Gruppo</option>
              <option value="ignoto">Ignoto</option>
            </select>
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium" style={{ color: 'var(--wa-muted)' }}>Numero leggibile</label>
            <select
              className="h-8 w-full rounded-lg border px-2.5 text-sm"
              style={selectStyle}
              value={filtroHaNumero}
              onChange={(e) => setFiltroHaNumero(e.target.value as typeof filtroHaNumero)}
            >
              <option value="tutti">Tutti</option>
              <option value="si">Solo con numero</option>
              <option value="no">Solo senza numero</option>
            </select>
          </div>
        </div>
      </Riquadro>

      {numberId && <TestataScan numberId={numberId} onRiscansionato={refreshScoperti} />}

      {!numberId && (
        <p className="text-sm" style={{ color: 'var(--wa-muted)' }}>
          Seleziona un numero WhatsApp per vedere le chat scoperte.
        </p>
      )}

      {numberId && (
        <>
          {erroreScoperti && (
            <Errore messaggio="Errore nel caricamento delle chat scoperte." />
          )}

          {caricandoScoperti && !scopertiData && (
            <p style={{ color: 'var(--wa-muted)' }}>Caricamento...</p>
          )}

          {scopertiData && righe.length === 0 && (
            <div
              className="rounded-2xl border p-10 text-center"
              style={{ borderColor: 'var(--wa-border)', backgroundColor: 'var(--wa-surface)' }}
            >
              <p className="text-lg font-medium text-white">Nessuna chat trovata con questi filtri</p>
              <p className="mt-2 text-sm" style={{ color: 'var(--wa-muted)' }}>
                Prova a cambiare stato o tipo chat qui sopra.
              </p>
            </div>
          )}

          {scopertiData && righe.length > 0 && (
            <Riquadro>
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium text-white">
                    {righe.length} chat in questa pagina
                    {selectedIds.size > 0 && (
                      <span style={{ color: 'var(--wa-muted)' }}> -- {selectedIds.size} selezionate</span>
                    )}
                  </p>
                  {erroreCompletamento && <Errore messaggio={erroreCompletamento} />}
                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      disabled={caricandoSelezioneTotale || promuovendo}
                      onClick={selezionaTuttiFiltrati}
                      style={{ borderColor: 'var(--wa-border)', color: 'var(--wa-muted)' }}
                      title="Seleziona tutte le righe promuovibili che combaciano coi filtri, non solo questa pagina"
                    >
                      {caricandoSelezioneTotale ? 'Seleziono...' : 'Seleziona tutti i filtrati'}
                    </Button>
                    <Button
                      type="button"
                      disabled={selectedIds.size === 0 || promuovendo}
                      onClick={handlePromuovi}
                      style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
                    >
                      {promuovendo ? 'Promozione...' : `Promuovi selezionati (${selectedIds.size})`}
                    </Button>
                  </div>
                </div>

                <div className="overflow-hidden rounded-xl border" style={{ borderColor: 'var(--wa-border)' }}>
                  <table className="w-full text-sm">
                    <thead>
                      <tr style={{ backgroundColor: 'var(--wa-bg)', color: 'var(--wa-muted)' }}>
                        <th className="w-10 px-3 py-2">
                          <input
                            type="checkbox"
                            checked={tuttiSelezionati}
                            disabled={promuovibiliVisibili.length === 0}
                            onChange={toggleSelezionaTutti}
                            title="Seleziona tutte le righe promuovibili di questa pagina"
                          />
                        </th>
                        <th className="px-3 py-2 text-left font-medium">Chat</th>
                        <th className="px-3 py-2 text-left font-medium">Tipo</th>
                        <th className="px-3 py-2 text-left font-medium">Numero</th>
                        <th className="px-3 py-2 text-left font-medium">Stato</th>
                        <th className="px-3 py-2 text-left font-medium">Scoperta il</th>
                      </tr>
                    </thead>
                    <tbody>
                      {righe.map((riga) => {
                        const motivo = motivoNonPromuovibile(riga)
                        return (
                          <tr
                            key={riga.id}
                            style={{
                              borderTop: '1px solid var(--wa-border)',
                              opacity: riga.promuovibile ? 1 : 0.5,
                            }}
                          >
                            <td className="px-3 py-2">
                              <input
                                type="checkbox"
                                checked={selectedIds.has(riga.id)}
                                onChange={() => toggleId(riga.id)}
                                title={motivo ?? 'Seleziona per la promozione'}
                              />
                            </td>
                            <td className="px-3 py-2" style={{ color: '#e7f3ef' }}>
                              <div className="flex items-center gap-1.5">
                                {riga.tipo_chat === 'gruppo' && (
                                  <Users className="h-3.5 w-3.5 shrink-0" style={{ color: '#e07a3c' }} />
                                )}
                                <span>{riga.chat_title ?? riga.display_name ?? <span style={{ opacity: 0.6 }}>-</span>}</span>
                              </div>
                              {motivo && (
                                <div className="mt-0.5 flex items-start gap-1 text-xs" style={{ color: '#e07a3c' }}>
                                  <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                                  <span>{motivo}</span>
                                </div>
                              )}
                            </td>
                            <td className="px-3 py-2">
                              <span
                                className="rounded px-2 py-0.5 text-xs font-medium"
                                style={{ backgroundColor: 'var(--wa-accent-soft)', color: TIPO_CHAT_COLORE[riga.tipo_chat] }}
                              >
                                {TIPO_CHAT_LABEL[riga.tipo_chat] ?? riga.tipo_chat}
                              </span>
                            </td>
                            <td className="px-3 py-2 font-mono" style={{ color: 'var(--wa-muted)' }}>
                              {riga.numero_mascherato ?? <span style={{ opacity: 0.6 }}>-</span>}
                            </td>
                            <td className="px-3 py-2">
                              <span
                                className="rounded px-2 py-0.5 text-xs font-medium"
                                style={{ backgroundColor: 'var(--wa-accent-soft)', color: STATUS_COLORE[riga.status] }}
                              >
                                {STATUS_LABEL[riga.status] ?? riga.status}
                              </span>
                            </td>
                            <td className="px-3 py-2" style={{ color: 'var(--wa-muted)' }}>
                              {formatData(riga.discovered_at)}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>

                <div className="flex items-center justify-end gap-2 pt-1">
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
                    disabled={righe.length < RIGHE_PER_PAGINA}
                    onClick={() => setOffset(offset + RIGHE_PER_PAGINA)}
                    style={{ borderColor: 'var(--wa-border)', color: 'var(--wa-muted)' }}
                  >
                    Successiva <ChevronRight className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            </Riquadro>
          )}
        </>
      )}

      {/* ---- Report di promozione ---------------------------------------- */}
      {reportPromozione && (
        <Riquadro>
          <ReportPromozioneView
            report={reportPromozione}
            dettagli={dettagliScartiPromozione}
            setDettagli={setDettagliScartiPromozione}
          />
        </Riquadro>
      )}

      {/* ---- Arruolamento in campagna ------------------------------------ */}
      {reportPromozione && reportPromozione.contatti_promossi_ids.length > 0 && (
        <Riquadro>
          <div className="space-y-3">
            <p className="text-sm font-medium text-white">Aggiungi alla campagna</p>
            <p className="text-sm" style={{ color: 'var(--wa-muted)' }}>
              {reportPromozione.contatti_promossi_ids.length} contatti promossi pronti per essere
              arruolati in una campagna in bozza dello stesso numero.
            </p>

            {campagneDraft.length === 0 && (
              <div
                className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm"
                style={{ borderColor: '#7a5a2a', backgroundColor: 'rgba(224, 122, 60, 0.12)', color: '#f2c9a0' }}
              >
                <AlertTriangle className="h-4 w-4 shrink-0" />
                <span>Nessuna campagna in bozza per questo numero.</span>
                <Link
                  href="/wa/campagne/nuova"
                  className="ml-auto inline-flex items-center gap-1 font-medium underline"
                >
                  Crea una nuova campagna <ArrowRight className="h-3.5 w-3.5" />
                </Link>
              </div>
            )}

            {campagneDraft.length > 0 && (
              <div className="flex flex-wrap items-end gap-3">
                <div className="space-y-1.5">
                  <label className="text-sm font-medium" style={{ color: 'var(--wa-muted)' }}>Campagna (bozza)</label>
                  <select
                    className="h-8 min-w-64 rounded-lg border px-2.5 text-sm"
                    style={selectStyle}
                    value={campagnaSelezionata}
                    onChange={(e) => setCampagnaSelezionata(e.target.value)}
                  >
                    <option value="">Seleziona una campagna...</option>
                    {campagneDraft.map((c) => (
                      <option key={c.id} value={c.id}>{c.name}</option>
                    ))}
                  </select>
                </div>
                <Button
                  type="button"
                  disabled={!campagnaSelezionata || arruolando}
                  onClick={handleArruola}
                  style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
                >
                  {arruolando ? 'Aggiunta...' : 'Aggiungi'}
                </Button>
              </div>
            )}

            {reportArruolamento && <ReportArruolamentoView report={reportArruolamento} />}
          </div>
        </Riquadro>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Report di promozione: stesso stile di ReportIngestView in
// campagne/nuova/page.tsx (creati/aggiornati/esclusi/scarti sempre visibili,
// mai un solo numero azzeccato in silenzio).
// ---------------------------------------------------------------------------
function ReportPromozioneView({
  report, dettagli, setDettagli,
}: { report: ReportPromozione; dettagli: boolean; setDettagli: (v: boolean) => void }) {
  return (
    <div className="space-y-2 text-sm">
      <p className="font-medium text-white">
        {report.promossi} contatti promossi
        {(report.contatti_creati > 0 || report.contatti_riusati > 0) && (
          <span style={{ color: 'var(--wa-muted)' }}>
            {' '}({report.contatti_creati} nuovi, {report.contatti_riusati} gia&apos; noti)
          </span>
        )}
      </p>
      {report.gia_dnc > 0 && (
        <p style={{ color: 'var(--wa-muted)' }}>
          {report.gia_dnc} promossi ma gia&apos; in opt-out/do-not-contact: non verranno arruolati.
        </p>
      )}
      {report.scarti.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setDettagli(!dettagli)}
            className="flex items-center gap-1 font-medium"
            style={{ color: '#e0b83c' }}
          >
            {dettagli ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            {report.scarti.length} righe scartate
          </button>
          {dettagli && (
            <div className="mt-2 overflow-hidden rounded-lg border" style={{ borderColor: 'var(--wa-border)' }}>
              <table className="w-full text-xs">
                <thead>
                  <tr style={{ backgroundColor: 'var(--wa-bg)', color: 'var(--wa-muted)' }}>
                    <th className="px-3 py-2 text-left font-medium">Id</th>
                    <th className="px-3 py-2 text-left font-medium">Motivo</th>
                  </tr>
                </thead>
                <tbody>
                  {report.scarti.map((s) => (
                    <tr key={s.id} style={{ borderTop: '1px solid var(--wa-border)' }}>
                      <td className="px-3 py-2 font-mono">{s.id}</td>
                      <td className="px-3 py-2" style={{ color: 'var(--wa-muted)' }}>{s.motivo}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const MOTIVO_LABEL_SCAN: Record<string, string> = {
  in_corso: 'in corso',
  completato: 'completo',
  raccolta_parziale: 'raccolta parziale',
  fermato_dopo_stallo: 'fermato dopo stallo',
  sync_ignota: 'sincronizzazione ignota',
  sync_sotto_soglia: 'sincronizzazione incompleta',
  sidebar_coperta: 'lista coperta da un pannello',
  wa_halted: 'canale fermato',
  numero_non_attivo: 'numero non attivo',
  profilo_occupato: 'profilo occupato',
  sessione_non_loggata: 'sessione scaduta',
  errore_imprevisto: 'errore',
}

function TestataScan({ numberId, onRiscansionato }:
    { numberId: string; onRiscansionato: () => void }) {
  const [avvio, setAvvio] = useState(false)
  const [storicoAperto, setStoricoAperto] = useState(false)
  const { data, mutate } = useSWR(
    `wa-discover-${numberId}`,
    () => waApi.numeri.discoverStato(numberId),
    { refreshInterval: (ultimo) => (ultimo?.in_corso ?? true) ? 10_000 : 0 },
  )

  const ultima = data?.ultima
  const inCorso = data?.in_corso ?? false

  // Le chat nuove devono comparire senza che l'operatore ricarichi la
  // pagina: stesso useRef del Task 8, sulla transizione in-corso -> finita.
  const eraInCorso = useRef(false)
  useEffect(() => {
    if (eraInCorso.current && !inCorso) onRiscansionato()
    eraInCorso.current = inCorso
  }, [inCorso, onRiscansionato])

  async function riscansiona() {
    setAvvio(true)
    try {
      await waApi.numeri.discover(numberId)
      toast.info('Scansione avviata. Puo\' durare parecchi minuti.')
      await mutate()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setAvvio(false)
    }
  }

  return (
    <Riquadro>
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          {!ultima && (
            <p className="text-sm" style={{ color: 'var(--wa-muted)' }}>
              Questo numero non e&apos; mai stato scansionato.
            </p>
          )}
          {ultima && (
            <>
              <p className="text-sm text-white">
                Ultimo scan {formatData(ultima.finished_at ?? ultima.started_at)}
                {ultima.dichiarato !== null && (
                  <> — {ultima.salvate + ultima.aggiornate + ultima.saltate_gia_note} su{' '}
                    {ultima.dichiarato}
                    {ultima.copertura !== null && ` (${ultima.copertura}%)`}</>
                )}
                {' · '}{MOTIVO_LABEL_SCAN[ultima.motivo] ?? ultima.motivo}
              </p>
              {/* Il sospetto va detto accanto al risultato, non solo nei log:
                  una raccolta corta con la sincronizzazione ignota ha un
                  primo indiziato, e chi guarda la pagina deve saperlo. */}
              {ultima.sync_stato === 'ignota' && ultima.motivo !== 'completato' && (
                <p className="text-xs" style={{ color: '#e07a3c' }}>
                  Sincronizzazione ignota durante lo scan: e&apos; il primo indiziato
                  se la raccolta e&apos; corta.
                </p>
              )}
              {ultima.saltate_gia_note > 0 && (
                <p className="text-xs" style={{ color: 'var(--wa-muted)' }}>
                  {ultima.saltate_gia_note} chat gia&apos; note non sono state riaperte.
                </p>
              )}
            </>
          )}
        </div>
        <div className="flex items-center gap-2">
          {(data?.storico?.length ?? 0) > 0 && (
            <Button type="button" variant="ghost" onClick={() => setStoricoAperto((v) => !v)}
              style={{ color: 'var(--wa-muted)' }}>
              {storicoAperto ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
              Storico
            </Button>
          )}
          <Button type="button" disabled={avvio || inCorso} onClick={riscansiona}
            style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}>
            {avvio || inCorso ? 'Scansione in corso...' : 'Riscansiona'}
          </Button>
        </div>
      </div>

      {storicoAperto && (
        <table className="mt-4 w-full text-xs">
          <thead>
            <tr style={{ color: 'var(--wa-muted)' }}>
              <th className="py-1 text-left font-medium">Quando</th>
              <th className="py-1 text-left font-medium">Avviato da</th>
              <th className="py-1 text-right font-medium">Coperte</th>
              <th className="py-1 text-right font-medium">Nuove</th>
              <th className="py-1 text-right font-medium">Copertura</th>
              <th className="py-1 text-left font-medium">Esito</th>
            </tr>
          </thead>
          <tbody>
            {(data?.storico ?? []).map((r) => (
              <tr key={r.id} style={{ borderTop: '1px solid var(--wa-border)' }}>
                <td className="py-1" style={{ color: 'var(--wa-muted)' }}>
                  {formatData(r.finished_at ?? r.started_at)}
                </td>
                <td className="py-1" style={{ color: 'var(--wa-muted)' }}>{r.avviato_da}</td>
                <td className="py-1 text-right" style={{ color: 'var(--wa-muted)' }}>
                  {r.salvate + r.aggiornate + r.saltate_gia_note}
                </td>
                <td className="py-1 text-right" style={{ color: 'var(--wa-muted)' }}>{r.salvate}</td>
                <td className="py-1 text-right" style={{ color: 'var(--wa-muted)' }}>
                  {r.copertura !== null ? `${r.copertura}%` : '-'}
                </td>
                <td className="py-1" style={{ color: r.motivo === 'completato' ? 'var(--wa-muted)' : '#e07a3c' }}>
                  {MOTIVO_LABEL_SCAN[r.motivo] ?? r.motivo}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Riquadro>
  )
}

function ReportArruolamentoView({ report }: { report: ReportArruolamento }) {
  return (
    <div className="space-y-2 text-sm">
      <p className="font-medium text-white">{report.arruolati} contatti arruolati</p>
      {report.gia_presenti > 0 && (
        <p style={{ color: 'var(--wa-muted)' }}>{report.gia_presenti} gia&apos; presenti in questa campagna</p>
      )}
      {report.gia_dnc > 0 && (
        <p style={{ color: 'var(--wa-muted)' }}>{report.gia_dnc} esclusi: opt-out/do-not-contact</p>
      )}
      {report.scarti.length > 0 && (
        <div className="overflow-hidden rounded-lg border" style={{ borderColor: 'var(--wa-border)' }}>
          <table className="w-full text-xs">
            <thead>
              <tr style={{ backgroundColor: 'var(--wa-bg)', color: 'var(--wa-muted)' }}>
                <th className="px-3 py-2 text-left font-medium">Id</th>
                <th className="px-3 py-2 text-left font-medium">Motivo</th>
              </tr>
            </thead>
            <tbody>
              {report.scarti.map((s) => (
                <tr key={s.id} style={{ borderTop: '1px solid var(--wa-border)' }}>
                  <td className="px-3 py-2 font-mono">{s.id}</td>
                  <td className="px-3 py-2" style={{ color: 'var(--wa-muted)' }}>{s.motivo}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
