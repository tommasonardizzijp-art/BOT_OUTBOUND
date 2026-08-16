'use client'

// Task 11: creazione campagna in tre passi su una pagina sola (Campagna ->
// Lista -> Messaggio). E' la schermata piu' importante di M2 (piano, Task
// 11): un ingest che dice "caricati 240 contatti" e tace sui 60 scartati
// sembra funzionare -- chi lo usa scopre il buco settimane dopo guardando i
// KPI. Il report qui sotto mostra SEMPRE creati/aggiornati/esclusi/duplicati/
// scartati, mai un solo numero azzeccato in silenzio.
//
// optout_enabled NON e' un campo che questa pagina invia in creazione: e'
// SEMPRE calcolato lato server da calcola_optout_enabled(tipo)
// (wa_campaign_service.py) e ignorato se il client lo manda -- bug bloccante
// trovato in review dedicata durante il backend (una campagna marketing
// poteva nascere senza CTA di opt-out). Qui si manda solo optout_cta (che
// serve per il gate "marketing senza CTA" del servizio) e si MOSTRA il
// valore che il server calcola e restituisce, mai un valore deciso qui.
//
// Nessun import da lib/api.ts: solo waApi.ts, come da regola del mondo /wa.
import { useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import useSWR from 'swr'
import { toast } from 'sonner'
import { Download, ChevronDown, ChevronRight } from 'lucide-react'
import {
  waApi,
  type WaCampaignType,
  type ReportIngest,
  type ScartoIngest,
  type AmbitoContatti,
  type ContattiDisponibili,
  type ReportArruolamento,
} from '@/lib/waApi'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

type Passo = 1 | 2 | 3

// Le due sorgenti del passo 2. "rubrica" e' il default: se i contatti sono
// gia' a DB (auto-discover), chiedere un file sarebbe chiedere di rifare a
// mano un lavoro gia' fatto.
type SorgenteLista = 'rubrica' | 'file'

// Contatti per richiesta di arruolamento. `arruola` costa ~750 ms per riga
// verso il pooler: 25 righe sono ~19 s, ben dentro qualunque timeout, e
// danno un avanzamento che si muove abbastanza spesso da non sembrare fermo.
const LOTTO_ARRUOLAMENTO = 25

// Testo segnaposto usato SOLO per soddisfare il vincolo "template_a
// obbligatorio" del POST di creazione (wa_campaigns.crea): non contiene
// graffe, quindi supera sempre validate_wa_template. Il messaggio vero si
// scrive nel passo 3 e sovrascrive questo con PUT /steps/0.
const TEMPLATE_SEGNAPOSTO_INIZIALE = 'Messaggio da scrivere: completalo nel passo 3.'

// Stesso testo di default usato da backend/tests/factories_wa.py per una
// campagna marketing: coerente con quello che un test/seed si aspetta gia'.
// In CORSIVO per default: gli underscore sono il markup di WhatsApp, e senza
// il disclaimer finale arriva con lo stesso peso visivo del messaggio. Il
// 16/08 la differenza fra due campagne era esattamente questa, e non c'era
// modo di capirlo dalla UI. Resta un default, si puo' riscrivere.
const CTA_DEFAULT_MARKETING = "_Scrivi STOP per non ricevere piu' messaggi._"

const ESEMPIO_CSV =
  'numero,nome,ultimo_ordine\n+393331112223,Marco,10/01/2026\n+393334445556,Anna,\n'

function scaricaTesto(nomeFile: string, contenuto: string) {
  const blob = new Blob([contenuto], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = nomeFile
  a.click()
  URL.revokeObjectURL(url)
}

// Mitigazione CSV injection sui file che genera QUESTA pagina (esempio e
// report scarti): se una cella comincia con =, +, - o @, Excel/Sheets puo'
// interpretarla come formula. Non e' il parser di ingresso (Task 2, quello
// e' server-side e ha il suo test dedicato) -- e' solo l'export che facciamo
// noi qui, e un valore mascherato o un motivo potrebbero comunque iniziare
// con uno di questi caratteri.
function celaCsv(valore: string): string {
  const v = valore.replaceAll('"', '""')
  return /^[=+\-@]/.test(v) ? `'${v}` : v
}

function scaricaReportScarti(scarti: ScartoIngest[]) {
  const righe = scarti.map(
    (s) => `${s.riga},"${celaCsv(s.motivo)}","${celaCsv(s.valore)}"`,
  )
  scaricaTesto('report_scarti_whatsapp.csv', ['riga,motivo,valore', ...righe].join('\n'))
}

// Estrazione delle colonne libere SOLO per popolare i chip dei segnaposto
// nel passo 3: e' un'anteprima lato client, non una validazione. La
// validazione vera resta server-side (validate_wa_template, contro le colonne
// EFFETTIVAMENTE persistite per la campagna), quindi un mismatch qui non
// puo' far salvare un template sbagliato -- al massimo manca un chip.
async function estraiAttributiDalFile(file: File): Promise<string[]> {
  const testo = await file.text()
  const primaRiga = (testo.split(/\r\n|\n/)[0] ?? '').replace(/^\uFEFF/, '')
  const puntoEVirgola = (primaRiga.match(/;/g) ?? []).length
  const virgola = (primaRiga.match(/,/g) ?? []).length
  const delimitatore = puntoEVirgola > virgola ? ';' : ','
  const colonne = primaRiga.split(delimitatore).map((c) => c.trim().toLowerCase())
  return colonne.filter((c) => c && c !== 'numero' && c !== 'nome')
}

function anteprimaMessaggio(testo: string, segnaposti: string[]): string {
  let out = testo.replace(/\{nome\}/gi, 'Marco')
  for (const s of segnaposti) {
    out = out.replaceAll(`{${s}}`, `esempio-${s}`)
  }
  return out
}

export default function NuovaCampagnaPage() {
  const router = useRouter()
  const [passo, setPasso] = useState<Passo>(1)

  // ---- Passo 1: dati campagna (bozza locale, prima della POST) ----
  const [tenantId, setTenantId] = useState('')
  const [numberId, setNumberId] = useState('')
  const [name, setName] = useState('')
  const [tipo, setTipo] = useState<WaCampaignType>('marketing')
  const [optoutCta, setOptoutCta] = useState(CTA_DEFAULT_MARKETING)
  const [creandoCampagna, setCreandoCampagna] = useState(false)
  const [erroreCampagna, setErroreCampagna] = useState<string | null>(null)

  const [campaignId, setCampaignId] = useState<string | null>(null)

  const { data: tenantsData } = useSWR('wa-tenants-nuova', () => waApi.tenants.list())
  const { data: numeriData } = useSWR(
    tenantId ? ['wa-numeri-nuova', tenantId] : null,
    () => waApi.numeri.list(tenantId),
  )
  const numeriAttivi = (numeriData?.numeri ?? []).filter((n) => n.status === 'active')

  // Fonte di verita' post-creazione: optout_enabled, optout_cta,
  // total_contacts e step_0 vengono SEMPRE da qui, mai ricalcolati in questo
  // componente.
  const { data: campagna, mutate: refreshCampagna } = useSWR(
    campaignId ? ['wa-campagna-nuova-detail', campaignId] : null,
    () => waApi.campagne.get(campaignId as string),
  )

  async function handleCreaCampagna(e: React.FormEvent) {
    e.preventDefault()
    setErroreCampagna(null)
    if (!tenantId || !numberId || !name.trim()) {
      setErroreCampagna('Nome, tenant e numero sono obbligatori.')
      return
    }
    if (tipo === 'marketing' && !optoutCta.trim()) {
      setErroreCampagna("La CTA di opt-out e' obbligatoria per le campagne marketing.")
      return
    }
    setCreandoCampagna(true)
    try {
      const creata = await waApi.campagne.create({
        tenant_id: tenantId,
        wa_number_id: numberId,
        name: name.trim(),
        campaign_type: tipo,
        template_a: TEMPLATE_SEGNAPOSTO_INIZIALE,
        // optout_enabled: MAI qui. Solo optout_cta, e solo per marketing.
        optout_cta: tipo === 'marketing' ? optoutCta.trim() : undefined,
      })
      setCampaignId(creata.id)
      setPasso(2)
      toast.success(`Campagna "${creata.name}" creata in bozza.`)
    } catch (err: unknown) {
      // Messaggio del backend, testuale (es. CTA mancante, numero di un altro
      // tenant): mai un "Errore 422" generico.
      setErroreCampagna(err instanceof Error ? err.message : 'Errore nella creazione della campagna.')
    } finally {
      setCreandoCampagna(false)
    }
  }

  // ---- Passo 2: la lista ----
  const [sorgente, setSorgente] = useState<SorgenteLista>('rubrica')
  const [fileSelezionato, setFileSelezionato] = useState<File | null>(null)
  const [caricando, setCaricando] = useState(false)
  const [erroreIngest, setErroreIngest] = useState<string | null>(null)
  const [report, setReport] = useState<ReportIngest | null>(null)
  const [placeholder, setPlaceholder] = useState<string[]>([])
  const [dettagliScarti, setDettagliScarti] = useState(false)

  // ---- Passo 2, sorgente "rubrica": contatti gia' a DB ----
  const [ambito, setAmbito] = useState<AmbitoContatti>('numero')
  const [arruolando, setArruolando] = useState(false)
  const [progresso, setProgresso] = useState<{ fatti: number; totale: number } | null>(null)
  const [reportArruolamento, setReportArruolamento] = useState<ReportArruolamento | null>(null)
  const [erroreArruolamento, setErroreArruolamento] = useState<string | null>(null)

  const { data: disponibili, mutate: refreshDisponibili } = useSWR(
    campaignId && sorgente === 'rubrica'
      ? ['wa-contatti-disponibili', campaignId, ambito] : null,
    () => waApi.contatti.disponibili(campaignId as string, ambito, { limit: 20 }),
  )

  async function handleArruola() {
    if (!campaignId || !disponibili) return
    setErroreArruolamento(null)
    setReportArruolamento(null)
    setArruolando(true)
    const totale = disponibili.totale_disponibili
    setProgresso({ fatti: 0, totale })
    const somma: ReportArruolamento = {
      arruolati: 0, gia_presenti: 0, gia_dnc: 0, scarti: [],
    }
    try {
      // Gli id si raccolgono a pagine da 500 (il tetto che l'API accetta):
      // con 666 contatti una pagina sola ne lascerebbe fuori un terzo.
      const ids: string[] = []
      for (let off = 0; off < totale; off += 500) {
        const p = await waApi.contatti.disponibili(campaignId, ambito, { limit: 500, offset: off })
        ids.push(...p.contatti.map((c) => c.id))
        if (p.contatti.length === 0) break
      }

      // A LOTTI, e non e' un vezzo: arruola impiega ~750 ms per riga verso il
      // pooler, quindi 666 contatti in una sola richiesta sono ~500 secondi e
      // il browser molla prima -- restituendo un 500 che NON e' un errore (il
      // lavoro a DB riesce comunque). Con i lotti si vede l'avanzamento, e
      // ogni lotto e' idempotente: se qualcosa si interrompe, rilanciare
      // ricomincia da dove era, perche' i gia' arruolati tornano
      // "gia_presenti" invece di duplicarsi.
      for (let i = 0; i < ids.length; i += LOTTO_ARRUOLAMENTO) {
        const lotto = ids.slice(i, i + LOTTO_ARRUOLAMENTO)
        const r = await waApi.contatti.enroll(campaignId, lotto)
        somma.arruolati += r.arruolati
        somma.gia_presenti += r.gia_presenti
        somma.gia_dnc += r.gia_dnc
        somma.scarti.push(...r.scarti)
        setProgresso({ fatti: Math.min(i + lotto.length, totale), totale })
        setReportArruolamento({ ...somma })
      }
      await Promise.all([refreshCampagna(), refreshDisponibili()])
      toast.success(`${somma.arruolati} contatti aggiunti alla campagna.`)
    } catch (err: unknown) {
      // Il parziale resta a schermo di proposito: dice quanti sono passati
      // prima dell'inciampo, e rilanciare riprende da li'.
      setErroreArruolamento(err instanceof Error ? err.message : "Errore nell'arruolamento.")
      await Promise.all([refreshCampagna(), refreshDisponibili()])
    } finally {
      setArruolando(false)
    }
  }

  async function handleUpload() {
    if (!campaignId || !fileSelezionato) return
    setErroreIngest(null)
    setCaricando(true)
    try {
      const risultato = await waApi.contatti.ingest(campaignId, fileSelezionato)
      setReport(risultato)
      setDettagliScarti(false)
      setPlaceholder(await estraiAttributiDalFile(fileSelezionato))
      await refreshCampagna()
    } catch (err: unknown) {
      // 422 = file rifiutato in blocco: il messaggio e' quello scritto per un
      // umano dal parser server-side (es. "Colonna 'numero' obbligatoria e
      // assente. Colonne trovate: telefono, nome."), mostrato testuale.
      setErroreIngest(err instanceof Error ? err.message : 'Errore nel caricamento del file.')
    } finally {
      setCaricando(false)
    }
  }

  // ---- Passo 3: messaggio ----
  const [testoMessaggio, setTestoMessaggio] = useState<string | null>(null)
  const [salvandoMessaggio, setSalvandoMessaggio] = useState(false)
  const [erroreMessaggio, setErroreMessaggio] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Inizializza la textarea dal template a DB una volta sola: se e' ancora
  // il segnaposto di creazione, parte da vuoto invece di far cancellare a
  // mano il testo finto a chi arriva al passo 3.
  useEffect(() => {
    if (testoMessaggio === null && campagna) {
      const attuale = campagna.step_0.template_a
      setTestoMessaggio(attuale === TEMPLATE_SEGNAPOSTO_INIZIALE ? '' : attuale)
    }
  }, [campagna, testoMessaggio])

  // Verita' server-side (step_0 non e' il segnaposto di creazione) PIU' il
  // confronto col testo che l'operatore sta guardando ORA nella textarea:
  // senza questo secondo controllo, un utente che salva, poi corregge una
  // parola SENZA ricliccare "Salva messaggio", vedeva ancora "salvato" e
  // poteva avviare la campagna col template VECCHIO -- un cliente reale
  // avrebbe ricevuto un testo diverso da quello a schermo, in silenzio.
  // Trovato in review dedicata.
  const messaggioSalvato =
    !!campagna?.step_0.template_a &&
    campagna.step_0.template_a.trim().length > 0 &&
    campagna.step_0.template_a !== TEMPLATE_SEGNAPOSTO_INIZIALE &&
    (testoMessaggio ?? '').trim() === campagna.step_0.template_a.trim()

  function inserisciSegnaposto(nomeSegnaposto: string) {
    const testoAttuale = testoMessaggio ?? ''
    const inserimento = `{${nomeSegnaposto}}`
    const ta = textareaRef.current
    const inizio = ta?.selectionStart ?? testoAttuale.length
    const fine = ta?.selectionEnd ?? testoAttuale.length
    const nuovoTesto = testoAttuale.slice(0, inizio) + inserimento + testoAttuale.slice(fine)
    setTestoMessaggio(nuovoTesto)
    if (ta) {
      requestAnimationFrame(() => {
        ta.focus()
        const posizione = inizio + inserimento.length
        ta.setSelectionRange(posizione, posizione)
      })
    }
  }

  async function handleSalvaMessaggio() {
    if (!campaignId) return
    setErroreMessaggio(null)
    setSalvandoMessaggio(true)
    try {
      await waApi.campagne.updateStep0(campaignId, { template_a: testoMessaggio ?? '' })
      await refreshCampagna()
      toast.success('Messaggio salvato.')
    } catch (err: unknown) {
      // 422 = placeholder ignoto: il messaggio del backend elenca ESATTAMENTE
      // quale (es. "Placeholder non disponibili nella lista contatti:
      // {citta}. Aggiungi la colonna al CSV oppure togli il segnaposto.").
      setErroreMessaggio(err instanceof Error ? err.message : 'Errore nel salvataggio del messaggio.')
    } finally {
      setSalvandoMessaggio(false)
    }
  }

  // ---- Avvio campagna ----
  const [avviando, setAvviando] = useState(false)
  const contattiCaricati = campagna?.total_contacts ?? 0

  const motiviAvvioBloccato: string[] = []
  if (contattiCaricati === 0) motiviAvvioBloccato.push('serve almeno un contatto caricato')
  if (!messaggioSalvato) motiviAvvioBloccato.push('salva prima il messaggio')

  async function handleAvvia() {
    if (!campaignId) return
    setAvviando(true)
    try {
      await waApi.campagne.start(campaignId)
      toast.success('Campagna avviata.')
      router.push(`/wa/campagne/${campaignId}`)
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : "Errore nell'avvio della campagna.")
    } finally {
      setAvviando(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Nuova campagna</h1>
        <p className="mt-1 text-sm" style={{ color: 'var(--wa-muted)' }}>
          Tre passi: campagna, lista contatti, messaggio.
        </p>
      </div>

      <Stepper passo={passo} />

      {passo === 1 && (
        <PassoCampagna
          tenants={tenantsData?.tenants ?? []}
          numeriAttivi={numeriAttivi}
          tenantId={tenantId}
          setTenantId={setTenantId}
          numberId={numberId}
          setNumberId={setNumberId}
          name={name}
          setName={setName}
          tipo={tipo}
          setTipo={setTipo}
          optoutCta={optoutCta}
          setOptoutCta={setOptoutCta}
          erroreCampagna={erroreCampagna}
          creandoCampagna={creandoCampagna}
          onSubmit={handleCreaCampagna}
        />
      )}

      {passo === 2 && campaignId && (
        <PassoLista
          sorgente={sorgente}
          setSorgente={setSorgente}
          etichettaNumero={
            numeriAttivi.find((n) => n.id === numberId)?.label ?? 'questo numero'
          }
          ambito={ambito}
          setAmbito={setAmbito}
          disponibili={disponibili}
          arruolando={arruolando}
          progresso={progresso}
          reportArruolamento={reportArruolamento}
          erroreArruolamento={erroreArruolamento}
          onArruola={handleArruola}
          contattiInCampagna={campagna?.total_contacts ?? 0}
          fileSelezionato={fileSelezionato}
          setFileSelezionato={setFileSelezionato}
          caricando={caricando}
          erroreIngest={erroreIngest}
          report={report}
          dettagliScarti={dettagliScarti}
          setDettagliScarti={setDettagliScarti}
          onUpload={handleUpload}
          onAvanti={() => setPasso(3)}
        />
      )}

      {passo === 3 && campaignId && (
        <PassoMessaggio
          campagna={campagna ?? null}
          placeholder={placeholder}
          testoMessaggio={testoMessaggio ?? ''}
          setTestoMessaggio={setTestoMessaggio}
          textareaRef={textareaRef}
          inserisciSegnaposto={inserisciSegnaposto}
          erroreMessaggio={erroreMessaggio}
          salvandoMessaggio={salvandoMessaggio}
          messaggioSalvato={messaggioSalvato}
          onSalva={handleSalvaMessaggio}
          onIndietro={() => setPasso(2)}
          contattiCaricati={contattiCaricati}
          motiviAvvioBloccato={motiviAvvioBloccato}
          avviando={avviando}
          onAvvia={handleAvvia}
        />
      )}
    </div>
  )
}

function Stepper({ passo }: { passo: Passo }) {
  const etichette: Record<Passo, string> = { 1: 'Campagna', 2: 'Contatti', 3: 'Messaggio' }
  return (
    <div className="flex items-center gap-2 text-sm">
      {([1, 2, 3] as Passo[]).map((n, i) => (
        <div key={n} className="flex items-center gap-2">
          <span
            className="flex h-7 w-7 items-center justify-center rounded-full text-xs font-semibold"
            style={
              n === passo
                ? { backgroundColor: 'var(--wa-accent)', color: '#04120e' }
                : n < passo
                  ? { backgroundColor: 'var(--wa-accent-soft)', color: 'var(--wa-accent)' }
                  : { backgroundColor: 'var(--wa-surface)', color: 'var(--wa-muted)' }
            }
          >
            {n}
          </span>
          <span style={{ color: n === passo ? '#fff' : 'var(--wa-muted)' }}>{etichette[n]}</span>
          {i < 2 && <ChevronRight className="h-4 w-4" style={{ color: 'var(--wa-muted)' }} />}
        </div>
      ))}
    </div>
  )
}

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

function Campo({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium" style={{ color: 'var(--wa-muted)' }}>{label}</label>
      {children}
    </div>
  )
}

const selectStyle: React.CSSProperties = {
  backgroundColor: 'transparent',
  borderColor: 'var(--wa-border)',
  color: '#e7f3ef',
}

// ---------------------------------------------------------------------------
// Passo 1: dati campagna
// ---------------------------------------------------------------------------
function PassoCampagna(props: {
  tenants: { id: string; name: string }[]
  numeriAttivi: { id: string; label: string }[]
  tenantId: string
  setTenantId: (v: string) => void
  numberId: string
  setNumberId: (v: string) => void
  name: string
  setName: (v: string) => void
  tipo: WaCampaignType
  setTipo: (v: WaCampaignType) => void
  optoutCta: string
  setOptoutCta: (v: string) => void
  erroreCampagna: string | null
  creandoCampagna: boolean
  onSubmit: (e: React.FormEvent) => void
}) {
  const {
    tenants, numeriAttivi, tenantId, setTenantId, numberId, setNumberId,
    name, setName, tipo, setTipo, optoutCta, setOptoutCta,
    erroreCampagna, creandoCampagna, onSubmit,
  } = props

  return (
    <Riquadro>
      <form onSubmit={onSubmit} className="space-y-4">
        {erroreCampagna && <Errore messaggio={erroreCampagna} />}

        <Campo label="Nome campagna">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Es. Promo autunno 2026" required />
        </Campo>

        <Campo label="Tenant">
          <select
            className="h-8 w-full rounded-lg border px-2.5 text-sm"
            style={selectStyle}
            value={tenantId}
            onChange={(e) => { setTenantId(e.target.value); setNumberId('') }}
            required
          >
            <option value="">Seleziona un tenant...</option>
            {tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          {tenants.length === 0 && (
            <p className="text-xs" style={{ color: 'var(--wa-muted)' }}>Nessun tenant configurato.</p>
          )}
        </Campo>

        <Campo label="Numero WhatsApp (solo attivi)">
          <select
            className="h-8 w-full rounded-lg border px-2.5 text-sm"
            style={selectStyle}
            value={numberId}
            onChange={(e) => setNumberId(e.target.value)}
            disabled={!tenantId}
            required
          >
            <option value="">Seleziona un numero...</option>
            {numeriAttivi.map((n) => <option key={n.id} value={n.id}>{n.label}</option>)}
          </select>
          {tenantId && numeriAttivi.length === 0 && (
            <p className="text-xs" style={{ color: '#e07a3c' }}>
              Nessun numero attivo per questo tenant: serve completare il login QR nella pagina Numeri
              prima di poter creare una campagna.
            </p>
          )}
        </Campo>

        <Campo label="Tipo campagna">
          <div className="flex gap-3">
            {(['marketing', 'followup'] as WaCampaignType[]).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTipo(t)}
                className="flex-1 rounded-lg border px-3 py-2 text-sm font-medium"
                style={
                  tipo === t
                    ? { borderColor: 'var(--wa-accent)', backgroundColor: 'var(--wa-accent-soft)', color: 'var(--wa-accent)' }
                    : { borderColor: 'var(--wa-border)', color: 'var(--wa-muted)' }
                }
              >
                {t === 'marketing' ? 'Marketing' : 'Follow-up'}
              </button>
            ))}
          </div>
          {/* Anteprima informativa: il valore VERO arriva dopo la creazione,
              dalla risposta del server (calcola_optout_enabled). Questa
              riga non calcola nulla che venga mandato, spiega solo la
              regola che il server applichera'. */}
          <p className="text-xs" style={{ color: 'var(--wa-muted)' }}>
            {tipo === 'marketing'
              ? "Opt-out: si attiva sempre in automatico per le campagne marketing (richiede la CTA sotto)."
              : "Opt-out: resta disattivato per le campagne di follow-up."}
          </p>
        </Campo>

        {tipo === 'marketing' && (
          <Campo label="CTA di opt-out (obbligatoria per marketing)">
            <Textarea
              value={optoutCta}
              onChange={(e) => setOptoutCta(e.target.value)}
              placeholder="Scrivi STOP per non ricevere piu' messaggi."
              required
            />
          </Campo>
        )}

        <div className="flex justify-end">
          <Button type="submit" disabled={creandoCampagna} style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}>
            {/* non piu' "carica la lista": dal passo 2 il file e' una delle
                due strade, e l'altra non carica niente. */}
            {creandoCampagna ? 'Creazione...' : 'Avanti: scegli i contatti'}
          </Button>
        </div>
      </form>
    </Riquadro>
  )
}

// ---------------------------------------------------------------------------
// Passo 2: upload lista
// ---------------------------------------------------------------------------
function PassoLista(props: {
  sorgente: SorgenteLista
  setSorgente: (v: SorgenteLista) => void
  etichettaNumero: string
  ambito: AmbitoContatti
  setAmbito: (v: AmbitoContatti) => void
  disponibili: ContattiDisponibili | undefined
  arruolando: boolean
  progresso: { fatti: number; totale: number } | null
  reportArruolamento: ReportArruolamento | null
  erroreArruolamento: string | null
  onArruola: () => void
  contattiInCampagna: number
  fileSelezionato: File | null
  setFileSelezionato: (f: File | null) => void
  caricando: boolean
  erroreIngest: string | null
  report: ReportIngest | null
  dettagliScarti: boolean
  setDettagliScarti: (v: boolean) => void
  onUpload: () => void
  onAvanti: () => void
}) {
  const {
    sorgente, setSorgente, etichettaNumero, ambito, setAmbito, disponibili,
    arruolando, progresso, reportArruolamento, erroreArruolamento, onArruola,
    contattiInCampagna,
    fileSelezionato, setFileSelezionato, caricando, erroreIngest, report,
    dettagliScarti, setDettagliScarti, onUpload, onAvanti,
  } = props

  return (
    <div className="space-y-4">
      <Riquadro>
        <div className="space-y-3">
          <p className="text-sm font-medium text-white">Da dove prendo i contatti</p>
          <div className="flex flex-col gap-2 sm:flex-row">
            <SceltaSorgente
              attiva={sorgente === 'rubrica'}
              onClick={() => setSorgente('rubrica')}
              titolo="Contatti gia' in rubrica"
              sotto="Quelli trovati dall'auto-discover o gia' caricati in passato. Nessun file."
            />
            <SceltaSorgente
              attiva={sorgente === 'file'}
              onClick={() => setSorgente('file')}
              titolo="Carica un file CSV"
              sotto="Una lista esterna, con eventuali colonne extra da usare come segnaposto."
            />
          </div>
        </div>
      </Riquadro>

      {sorgente === 'rubrica' && (
        <PassoListaRubrica
          etichettaNumero={etichettaNumero}
          ambito={ambito}
          setAmbito={setAmbito}
          disponibili={disponibili}
          arruolando={arruolando}
          progresso={progresso}
          report={reportArruolamento}
          errore={erroreArruolamento}
          onArruola={onArruola}
        />
      )}

      {sorgente === 'file' && (
      <Riquadro>
        <div className="space-y-4">
          <div>
            <p className="text-sm font-medium text-white">Contratto del file</p>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm" style={{ color: 'var(--wa-muted)' }}>
              <li>colonna <code>numero</code> obbligatoria (con o senza <code>+</code>: senza prefisso si assume l&apos;Italia)</li>
              <li>colonna <code>nome</code> opzionale</li>
              <li>ogni altra colonna diventa un segnaposto utilizzabile nel messaggio, es. <code>{'{ultimo_ordine}'}</code></li>
            </ul>
            <button
              type="button"
              onClick={() => scaricaTesto('esempio_contatti_whatsapp.csv', ESEMPIO_CSV)}
              className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium"
              style={{ color: 'var(--wa-accent)' }}
            >
              <Download className="h-3.5 w-3.5" /> Scarica un file di esempio
            </button>
          </div>

          {erroreIngest && <Errore messaggio={erroreIngest} />}

          <div className="flex items-center gap-3">
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => setFileSelezionato(e.target.files?.[0] ?? null)}
              className="text-sm"
              style={{ color: 'var(--wa-muted)' }}
            />
            <Button
              type="button"
              disabled={!fileSelezionato || caricando}
              onClick={onUpload}
              style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
            >
              {caricando ? 'Caricamento...' : 'Carica file'}
            </Button>
          </div>
        </div>
      </Riquadro>
      )}

      {sorgente === 'file' && report && (
        <Riquadro>
          <ReportIngestView report={report} dettagli={dettagliScarti} setDettagli={setDettagliScarti} />
        </Riquadro>
      )}

      <div className="flex items-center justify-between gap-4">
        {/* La condizione di "Avanti" e' il conteggio VERO a DB, non l'esito
            dell'ultima azione: con due sorgenti, sbloccare su "esiste un
            report di upload" lascerebbe bloccato chi ha arruolato dalla
            rubrica -- e sbloccherebbe chi ha caricato un file da cui non e'
            entrato nemmeno un contatto. */}
        <p className="text-sm" style={{ color: 'var(--wa-muted)' }}>
          {contattiInCampagna > 0
            ? `${contattiInCampagna} contatti in campagna`
            : 'Nessun contatto in campagna: aggiungine prima di proseguire.'}
        </p>
        <Button
          type="button"
          disabled={contattiInCampagna === 0 || arruolando}
          onClick={onAvanti}
          style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
        >
          Avanti: scrivi il messaggio
        </Button>
      </div>
    </div>
  )
}

function SceltaSorgente({ attiva, onClick, titolo, sotto }: {
  attiva: boolean; onClick: () => void; titolo: string; sotto: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex-1 rounded-lg border px-4 py-3 text-left transition-colors"
      style={{
        borderColor: attiva ? 'var(--wa-accent)' : 'var(--wa-border)',
        backgroundColor: attiva ? 'rgba(38, 224, 196, 0.08)' : 'transparent',
      }}
    >
      <span className="block text-sm font-medium"
        style={{ color: attiva ? 'var(--wa-accent)' : '#e7f3ef' }}>{titolo}</span>
      <span className="mt-1 block text-xs" style={{ color: 'var(--wa-muted)' }}>{sotto}</span>
    </button>
  )
}

function PassoListaRubrica(props: {
  etichettaNumero: string
  ambito: AmbitoContatti
  setAmbito: (v: AmbitoContatti) => void
  disponibili: ContattiDisponibili | undefined
  arruolando: boolean
  progresso: { fatti: number; totale: number } | null
  report: ReportArruolamento | null
  errore: string | null
  onArruola: () => void
}) {
  const {
    etichettaNumero, ambito, setAmbito, disponibili, arruolando, progresso,
    report, errore, onArruola,
  } = props

  const totale = disponibili?.totale_disponibili ?? 0
  const esclusi = disponibili?.esclusi

  return (
    <Riquadro>
      <div className="space-y-4">
        <div className="space-y-2">
          <label className="block text-sm font-medium text-white">Quali contatti</label>
          <select
            value={ambito}
            onChange={(e) => setAmbito(e.target.value as AmbitoContatti)}
            disabled={arruolando}
            className="w-full rounded-lg border px-3 py-2 text-sm"
            style={{ backgroundColor: 'transparent', borderColor: 'var(--wa-border)', color: '#e7f3ef' }}
          >
            <option value="numero">Solo quelli scoperti su {etichettaNumero}</option>
            <option value="tutti">Tutti i contatti in rubrica</option>
          </select>
          {/* Detto qui e non in un tooltip: e' il motivo per cui i due numeri
              possono essere molto diversi, e senza saperlo si sceglie a caso. */}
          <p className="text-xs" style={{ color: 'var(--wa-muted)' }}>
            Un contatto e&apos; legato a un numero solo se e&apos; arrivato dall&apos;auto-discover
            di quel numero. Chi e&apos; entrato da un CSV non e&apos; legato a nessun numero:
            compare solo in &quot;tutti&quot;.
          </p>
        </div>

        {errore && <Errore messaggio={errore} />}

        <div className="rounded-lg border px-4 py-3" style={{ borderColor: 'var(--wa-border)' }}>
          {disponibili === undefined ? (
            <p className="text-sm" style={{ color: 'var(--wa-muted)' }}>Conteggio in corso...</p>
          ) : (
            <>
              <p className="text-sm text-white">
                <strong>{totale}</strong> contatti da aggiungere
              </p>
              {esclusi && (esclusi.gia_in_campagna > 0 || esclusi.opt_out_o_dnc > 0) && (
                <p className="mt-1 text-xs" style={{ color: 'var(--wa-muted)' }}>
                  Esclusi:{' '}
                  {esclusi.gia_in_campagna > 0 && <>{esclusi.gia_in_campagna} gia&apos; in campagna</>}
                  {esclusi.gia_in_campagna > 0 && esclusi.opt_out_o_dnc > 0 && <> · </>}
                  {esclusi.opt_out_o_dnc > 0 && <>{esclusi.opt_out_o_dnc} in opt-out o do-not-contact</>}
                </p>
              )}
              {totale > 0 && (
                <ul className="mt-3 space-y-1 text-xs" style={{ color: 'var(--wa-muted)' }}>
                  {disponibili.contatti.slice(0, 8).map((c) => (
                    <li key={c.id} className="flex gap-2">
                      <span className="font-mono">{c.numero}</span>
                      <span>{c.nome ?? c.chat_title ?? '-'}</span>
                    </li>
                  ))}
                  {totale > disponibili.contatti.length && (
                    <li>...e altri {totale - disponibili.contatti.length}</li>
                  )}
                </ul>
              )}
            </>
          )}
        </div>

        {progresso && (
          <div className="space-y-1">
            <div className="h-2 w-full overflow-hidden rounded-full"
              style={{ backgroundColor: 'var(--wa-border)' }}>
              <div className="h-full transition-all"
                style={{
                  width: `${progresso.totale ? Math.round((progresso.fatti / progresso.totale) * 100) : 0}%`,
                  backgroundColor: 'var(--wa-accent)',
                }} />
            </div>
            <p className="text-xs" style={{ color: 'var(--wa-muted)' }}>
              {progresso.fatti} di {progresso.totale}
              {arruolando && <> — puo&apos; durare qualche minuto, non chiudere la pagina.</>}
            </p>
          </div>
        )}

        {report && (
          <div className="text-sm">
            <p className="font-medium text-white">{report.arruolati} contatti aggiunti</p>
            {(report.gia_presenti > 0 || report.gia_dnc > 0) && (
              <p className="text-xs" style={{ color: 'var(--wa-muted)' }}>
                {report.gia_presenti > 0 && <>{report.gia_presenti} erano gia&apos; in campagna. </>}
                {report.gia_dnc > 0 && <>{report.gia_dnc} esclusi per opt-out o do-not-contact.</>}
              </p>
            )}
          </div>
        )}

        <Button
          type="button"
          disabled={arruolando || totale === 0}
          onClick={onArruola}
          style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
        >
          {arruolando ? 'Aggiunta in corso...' : `Aggiungi ${totale} contatti alla campagna`}
        </Button>
      </div>
    </Riquadro>
  )
}

function ReportIngestView({
  report, dettagli, setDettagli,
}: { report: ReportIngest; dettagli: boolean; setDettagli: (v: boolean) => void }) {
  const totaleCaricati = report.creati + report.aggiornati
  return (
    <div className="space-y-2 text-sm">
      <p className="font-medium text-white">
        {totaleCaricati} contatti caricati
        {report.aggiornati > 0 && (
          <span style={{ color: 'var(--wa-muted)' }}> (di cui {report.aggiornati} gia&apos; presenti, aggiornati)</span>
        )}
      </p>
      {report.gia_dnc > 0 && (
        <p style={{ color: 'var(--wa-muted)' }}>
          {report.gia_dnc} esclusi: hanno detto STOP o sono in do-not-contact
        </p>
      )}
      {report.duplicati_nel_file > 0 && (
        <p style={{ color: 'var(--wa-muted)' }}>{report.duplicati_nel_file} duplicati nel file</p>
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
            <div className="mt-2 space-y-2">
              <div className="overflow-hidden rounded-lg border" style={{ borderColor: 'var(--wa-border)' }}>
                <table className="w-full text-xs">
                  <thead>
                    <tr style={{ backgroundColor: 'var(--wa-bg)', color: 'var(--wa-muted)' }}>
                      <th className="px-3 py-2 text-left font-medium">Riga</th>
                      <th className="px-3 py-2 text-left font-medium">Numero</th>
                      <th className="px-3 py-2 text-left font-medium">Motivo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.scarti.map((s, i) => (
                      <tr key={i} style={{ borderTop: '1px solid var(--wa-border)' }}>
                        <td className="px-3 py-2">{s.riga}</td>
                        {/* s.valore arriva gia' mascherato dal backend
                            (P12): non lo tocchiamo, non lo ricostruiamo. */}
                        <td className="px-3 py-2 font-mono">{s.valore}</td>
                        <td className="px-3 py-2" style={{ color: 'var(--wa-muted)' }}>{s.motivo}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <button
                type="button"
                onClick={() => scaricaReportScarti(report.scarti)}
                className="inline-flex items-center gap-1.5 text-xs font-medium"
                style={{ color: 'var(--wa-accent)' }}
              >
                <Download className="h-3.5 w-3.5" /> Scarica il report scarti (CSV)
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Passo 3: messaggio
// ---------------------------------------------------------------------------
function PassoMessaggio(props: {
  campagna: { optout_enabled: boolean; optout_cta: string | null } | null
  placeholder: string[]
  testoMessaggio: string
  setTestoMessaggio: (v: string) => void
  textareaRef: React.RefObject<HTMLTextAreaElement | null>
  inserisciSegnaposto: (nome: string) => void
  erroreMessaggio: string | null
  salvandoMessaggio: boolean
  messaggioSalvato: boolean
  onSalva: () => void
  onIndietro: () => void
  contattiCaricati: number
  motiviAvvioBloccato: string[]
  avviando: boolean
  onAvvia: () => void
}) {
  const {
    campagna, placeholder, testoMessaggio, setTestoMessaggio, textareaRef,
    inserisciSegnaposto, erroreMessaggio, salvandoMessaggio, messaggioSalvato,
    onSalva, onIndietro, contattiCaricati, motiviAvvioBloccato, avviando, onAvvia,
  } = props

  const chip = ['nome', ...placeholder]

  return (
    <div className="space-y-4">
      {campagna && (
        <div className="rounded-lg border px-4 py-2 text-xs" style={{ borderColor: 'var(--wa-border)', color: 'var(--wa-muted)' }}>
          Opt-out: <strong style={{ color: campagna.optout_enabled ? 'var(--wa-accent)' : 'var(--wa-muted)' }}>
            {campagna.optout_enabled ? 'attivo' : 'disattivato'}
          </strong>
          {campagna.optout_enabled && campagna.optout_cta && <> -- CTA: &quot;{campagna.optout_cta}&quot;</>}
          {' '}(valore calcolato e restituito dal server, non modificabile da questa pagina)
        </div>
      )}

      <Riquadro>
        <div className="space-y-4">
          {erroreMessaggio && <Errore messaggio={erroreMessaggio} />}

          <div>
            <p className="text-sm font-medium" style={{ color: 'var(--wa-muted)' }}>Segnaposto disponibili</p>
            <div className="mt-2 flex flex-wrap gap-2">
              {chip.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => inserisciSegnaposto(c)}
                  className="rounded-full border px-3 py-1 text-xs font-mono"
                  style={{ borderColor: 'var(--wa-accent)', color: 'var(--wa-accent)' }}
                >
                  {`{${c}}`}
                </button>
              ))}
            </div>
          </div>

          <Campo label="Messaggio">
            <Textarea
              ref={textareaRef}
              value={testoMessaggio}
              onChange={(e) => setTestoMessaggio(e.target.value)}
              placeholder="Ciao {nome}, ..."
              rows={5}
            />
          </Campo>

          <div>
            <p className="text-sm font-medium" style={{ color: 'var(--wa-muted)' }}>Anteprima (valori di esempio)</p>
            <div className="mt-1 whitespace-pre-wrap rounded-lg border px-3 py-2 text-sm" style={{ borderColor: 'var(--wa-border)' }}>
              {anteprimaMessaggio(testoMessaggio, placeholder) || <span style={{ color: 'var(--wa-muted)' }}>(vuoto)</span>}
            </div>
          </div>

          <div className="flex justify-between">
            <Button type="button" variant="outline" onClick={onIndietro} style={{ borderColor: 'var(--wa-border)', color: 'var(--wa-muted)' }}>
              Indietro
            </Button>
            <Button
              type="button"
              disabled={salvandoMessaggio || !testoMessaggio.trim()}
              onClick={onSalva}
              style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
            >
              {salvandoMessaggio ? 'Salvataggio...' : 'Salva messaggio'}
            </Button>
          </div>
        </div>
      </Riquadro>

      <Riquadro>
        <div className="flex items-center justify-between">
          <div className="text-sm" style={{ color: 'var(--wa-muted)' }}>
            {contattiCaricati} contatti caricati -- messaggio {messaggioSalvato ? 'salvato' : 'non ancora salvato'}
          </div>
          <div className="flex items-center gap-3">
            {motiviAvvioBloccato.length > 0 && (
              <span className="text-xs" style={{ color: '#e07a3c' }}>
                Non avviabile: {motiviAvvioBloccato.join('; ')}.
              </span>
            )}
            <Button
              type="button"
              disabled={avviando || motiviAvvioBloccato.length > 0}
              onClick={onAvvia}
              style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
            >
              {avviando ? 'Avvio...' : 'Avvia campagna'}
            </Button>
          </div>
        </div>
      </Riquadro>
    </div>
  )
}
