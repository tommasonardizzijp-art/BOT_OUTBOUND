// Client REST del mondo WhatsApp. Separato da lib/api.ts di proposito: i due
// canali non condividono pagine ne' dati (SDD 6.3), e un client unico
// diventerebbe il primo punto in cui tornano a mescolarsi.
//
// I path e le forme di risposta qui sotto sono presi 1:1 dai router gia'
// scritti e testati in questo cantiere (backend/app/api/tenants.py,
// wa_numbers.py, wa_campaigns.py, wa_contacts.py), non dallo schizzo del
// piano: dove divergevano, ha vinto il backend (e' la fonte di verita').
import { getAuthToken } from './api'

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000/api'

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getAuthToken()
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  })
  if (!res.ok) {
    // Il backend risponde 422/409 con un messaggio scritto per un umano
    // (righe scartate, placeholder mancanti, stato macchina non valido):
    // mostrarlo e' meglio di un generico "Errore 422".
    const detail = await res.json().catch(() => null)
    const grezzo = detail?.detail
    const messaggio = typeof grezzo === 'string'
      ? grezzo
      // I 409 del discover mandano {codice, messaggio}: senza questo ramo
      // l'utente vedrebbe "[object Object]".
      : (grezzo?.messaggio ?? `Errore ${res.status}`)
    throw new Error(messaggio)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

// ---- Tipi, presi dai serializzatori reali dei router (non dal piano) ----

export type WaNumberStatus =
  | 'pending_qr' | 'active' | 'qr_required' | 'disconnected'
  | 'cooldown' | 'suspended' | 'retired'

export type WaCampaignStatus =
  | 'draft' | 'running' | 'paused' | 'stopped' | 'completed' | 'error'

export type WaCampaignType = 'marketing' | 'followup'

export type WaContactStatus =
  | 'queued' | 'in_sequence' | 'replied' | 'completed' | 'opted_out' | 'skipped' | 'failed'

export type Tenant = {
  id: string
  name: string
  status: string
  settings: Record<string, unknown> | null
  created_at: string | null
}

// wa_numbers._serializza: il numero torna SEMPRE mascherato (P12), mai in
// chiaro -- nessun campo "phone"/"numero_intero" esiste in questa forma.
export type WaNumber = {
  id: string
  tenant_id: string
  label: string
  numero: string
  status: WaNumberStatus
  proxy_url: string | null
  daily_cap: number
  warmup_day: number
  // Derivati dal backend (non scrivibili): warmup_day da solo e' un indice
  // senza significato per chi guarda la pagina -- warmup_cap dice quanti
  // messaggi sono davvero, ed e' null quando la rampa non pone alcun tetto.
  warmup_cap: number | null
  warmup_advanced_date: string | null
  sent_today: number
  sent_date: string | null
  session_checked_at: string | null
  notes: string | null
  created_at: string | null
}

export type WaSequenceStep = {
  step_index: number
  template_a: string
  template_b: string | null
  template_c: string | null
  template_d: string | null
}

export type WaCampaign = {
  id: string
  tenant_id: string
  wa_number_id: string
  name: string
  campaign_type: WaCampaignType
  status: WaCampaignStatus
  daily_limit: number | null
  optout_enabled: boolean
  optout_cta: string | null
  active_hours_start: string | null
  active_hours_end: string | null
  total_contacts: number
  sent: number
  replied: number
  opted_out: number
  failed: number
  created_at: string | null
  started_at: string | null
  completed_at: string | null
}

export type WaCampaignDetail = WaCampaign & { step_0: WaSequenceStep }

// wa_campaigns.kpi(): campi in italiano, presi 1:1 dal backend (Task 8).
export type WaCampaignKpi = {
  stato: WaCampaignStatus
  caricati: number
  inviati: number
  da_inviare: number
  risposti: number
  optout: number
  falliti: number
  tasso_risposta: number
  tasso_optout: number
  allarme_optout: boolean
  nota: string
}

// wa_contacts.lista_contatti(): il numero torna mascherato, mai intero.
export type WaCampaignContactRow = {
  id: string
  numero: string
  nome: string | null
  stato: WaContactStatus
  tentativi_falliti: number
  ultimo_errore: string | null
  opted_out: boolean
  in_lavorazione: boolean
}

// wa_ops.wa_ops_status(): striscia di stato del canale (G3). motivo_stop e
// cap_effettivo li aggiunge in parallelo l'agente sul backend (stesso design
// 08/08, PR separata): possono arrivare assenti o null a seconda dell'ordine
// di merge, vanno trattati come opzionali anche lato tipo.
export type WaOpsStatus = {
  wa_halted: boolean
  send_enabled: boolean
  numeri_attivi: number
  campagne_running: number
  inviati_oggi: number
  motivo_stop?: string | null
  cap_effettivo?: number | null
}

export type WaRecoverResult = {
  recovered: boolean
  status: WaCampaignStatus
  stato_numero: WaNumberStatus | null
  prossimo_passo: string
}

export type ScartoIngest = { riga: number; motivo: string; valore: string }

export type ReportIngest = {
  creati: number
  aggiornati: number
  gia_dnc: number
  duplicati_nel_file: number
  scarti: ScartoIngest[]
}

// wa_discover._serializza: numero_mascherato mai il numero intero (P12,
// stesso vincolo di wa_contacts.lista_contatti). promuovibile riusa
// regole.promuovibile lato backend -- un gruppo compare comunque nella
// lista, solo marcato non promuovibile, mai nascosto.
export type WaDiscoveredChat = {
  id: string
  chat_title: string | null
  display_name: string | null
  tipo_chat: 'individuale' | 'gruppo' | 'ignoto'
  numero_leggibile: boolean
  numero_mascherato: string | null
  status: 'nuovo' | 'promosso' | 'scartato'
  promuovibile: boolean
  // Il motivo VERO calcolato da regole.py quando promuovibile e' false
  // (null altrimenti) -- mai ri-derivarlo lato client, l'ordine dei
  // controlli vive in un solo posto (review finale di branch).
  motivo: string | null
  discovered_at: string | null
}

// wa_numbers._serializza_run: una riga di wa_discover_runs. `motivo` sono i
// valori del motore (completato, raccolta_parziale, fermato_dopo_stallo,
// sync_ignota, sync_sotto_soglia, sidebar_coperta, wa_halted,
// numero_non_attivo, profilo_occupato, sessione_non_loggata,
// errore_imprevisto, in_corso): non ri-derivarli lato client.
export type WaDiscoverRun = {
  id: string
  stato: 'running' | 'done' | 'failed'
  avviato_da: 'manuale' | 'cron'
  started_at: string | null
  finished_at: string | null
  salvate: number
  aggiornate: number
  saltate_gia_note: number
  non_verificate: number
  dichiarato: number | null
  copertura: number | null
  motivo: string
  sync_stato: 'letta' | 'assente' | 'ignota'
  errore: string | null
}

export type WaDiscoverStato = {
  ultima: WaDiscoverRun | null
  storico: WaDiscoverRun[]
  in_corso: boolean
}

export type ScartoPromozione = { id: string; motivo: string }

// wa_discover.promote(): risposta di POST /wa/discovered-chats/promote,
// stesso stile di ReportIngest (chiavi del dataclass, scarti come lista di dict).
export type ReportPromozione = {
  promossi: number
  contatti_creati: number
  contatti_riusati: number
  gia_dnc: number
  scarti: ScartoPromozione[]
  contatti_promossi_ids: string[]
}

// wa_contacts.enroll(): risposta di POST /wa/contacts/enroll.
export type ReportArruolamento = {
  arruolati: number
  gia_presenti: number
  gia_dnc: number
  scarti: { id: string; motivo: string }[]
}

export type WaNumberCreate = {
  tenant_id: string
  label: string
  numero: string
  proxy_url?: string | null
  daily_cap?: number
}

export type WaCampaignCreate = {
  tenant_id: string
  wa_number_id: string
  name: string
  campaign_type: WaCampaignType
  template_a: string
  optout_enabled?: boolean
  optout_cta?: string | null
  daily_limit?: number
}

export const waApi = {
  tenants: {
    list: () => req<{ tenants: Tenant[] }>('/tenants'),
    create: (data: { name: string; settings?: Record<string, unknown> }) =>
      req<Tenant>('/tenants', { method: 'POST', body: JSON.stringify(data) }),
    get: (id: string) => req<Tenant>(`/tenants/${id}`),
  },

  numeri: {
    list: (tenantId?: string) =>
      req<{ numeri: WaNumber[] }>(`/wa/numbers${tenantId ? `?tenant_id=${tenantId}` : ''}`),
    get: (id: string) => req<WaNumber>(`/wa/numbers/${id}`),
    create: (data: WaNumberCreate) =>
      req<WaNumber>('/wa/numbers', { method: 'POST', body: JSON.stringify(data) }),
    update: (id: string, data: Partial<{ label: string; proxy_url: string | null; daily_cap: number; notes: string; warmup_day: number }>) =>
      req<WaNumber>(`/wa/numbers/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
    delete: (id: string) => req<{ eliminato: boolean }>(`/wa/numbers/${id}`, { method: 'DELETE' }),
    // Apre un browser VISIBILE sulla macchina del backend: va premuto solo
    // con qualcuno davanti allo schermo (contratto §7.6).
    avviaLoginQr: (id: string) => req<{ status: string }>(`/wa/numbers/${id}/login`, { method: 'POST' }),
    verificaSessione: (id: string) => req<{ status: string }>(`/wa/numbers/${id}/check`, { method: 'POST' }),
    riattiva: (id: string, motivo: string) =>
      req<{ status: string; prossimo_passo: string }>(`/wa/numbers/${id}/riattiva`, {
        method: 'POST', body: JSON.stringify({ motivo }),
      }),
    // Apre un browser sulla macchina del backend e blocca gli invii su TUTTI
    // i numeri finche' non finisce: la conferma in UI deve dirlo.
    discover: (id: string) =>
      req<{ run_id: string; queued: boolean }>(`/wa/numbers/${id}/discover`, { method: 'POST' }),
    discoverStato: (id: string) => req<WaDiscoverStato>(`/wa/numbers/${id}/discover`),
  },

  campagne: {
    list: (params?: { tenantId?: string; status?: WaCampaignStatus }) => {
      const q = new URLSearchParams()
      if (params?.tenantId) q.set('tenant_id', params.tenantId)
      if (params?.status) q.set('status', params.status)
      const qs = q.toString()
      return req<{ campagne: WaCampaign[] }>(`/wa/campaigns${qs ? `?${qs}` : ''}`)
    },
    get: (id: string) => req<WaCampaignDetail>(`/wa/campaigns/${id}`),
    create: (data: WaCampaignCreate) =>
      req<WaCampaign>('/wa/campaigns', { method: 'POST', body: JSON.stringify(data) }),
    update: (id: string, data: Partial<{
      name: string; daily_limit: number; optout_cta: string | null
      active_hours_start: string; active_hours_end: string; optout_enabled: boolean
    }>) => req<WaCampaign>(`/wa/campaigns/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
    updateStep0: (id: string, data: {
      template_a: string; template_b?: string | null
      template_c?: string | null; template_d?: string | null
    }) => req<WaSequenceStep>(`/wa/campaigns/${id}/steps/0`, { method: 'PUT', body: JSON.stringify(data) }),
    start: (id: string) => req<WaCampaign>(`/wa/campaigns/${id}/start`, { method: 'POST' }),
    pause: (id: string) => req<WaCampaign>(`/wa/campaigns/${id}/pause`, { method: 'POST' }),
    resume: (id: string) => req<WaCampaign>(`/wa/campaigns/${id}/resume`, { method: 'POST' }),
    stop: (id: string) => req<WaCampaign>(`/wa/campaigns/${id}/stop`, { method: 'POST' }),
    // error -> paused (mai -> running: lo stesso "due click" di riattiva). Il
    // motivo non ha una colonna dove finire, resta nel log e nell'evento.
    recover: (id: string, motivo: string) =>
      req<WaRecoverResult>(`/wa/campaigns/${id}/recover`, { method: 'POST', body: JSON.stringify({ motivo }) }),
    kpi: (id: string) => req<WaCampaignKpi>(`/wa/campaigns/${id}/kpi`),
  },

  // Operativita' di CANALE (kill-switch WhatsApp), non di singola campagna.
  // Punta a /wa/ops/*, non a /admin/halt di lib/api.ts: quello e' l'interruttore
  // Instagram (bot_state_service.halt, colonna diversa) e non ferma WhatsApp.
  ops: {
    status: () => req<WaOpsStatus>('/wa/ops/status'),
    halt: (reason: string) =>
      req<{ wa_halted: boolean; reason: string }>('/wa/ops/halt', {
        method: 'POST', body: JSON.stringify({ reason }),
      }),
    resume: () => req<{ wa_halted: boolean }>('/wa/ops/resume', { method: 'POST' }),
  },

  contatti: {
    list: (campaignId: string, params?: { limit?: number; offset?: number }) => {
      const q = new URLSearchParams({ campaign_id: campaignId })
      if (params?.limit) q.set('limit', String(params.limit))
      if (params?.offset) q.set('offset', String(params.offset))
      return req<{ contatti: WaCampaignContactRow[] }>(`/wa/contacts?${q}`)
    },
    // multipart/form-data: niente Content-Type esplicito, il boundary lo
    // mette fetch (stesso motivo di api.campaigns.importProfiles).
    ingest: (campaignId: string, file: File) => {
      const fd = new FormData()
      fd.append('campaign_id', campaignId)
      fd.append('file', file)
      return req<ReportIngest>('/wa/contacts/ingest', { method: 'POST', body: fd })
    },
    rimuovi: (campaignContactId: string) =>
      req<{ rimosso: boolean }>(`/wa/contacts/${campaignContactId}`, { method: 'DELETE' }),
    // wa_contacts.enroll(): arruola WaContact gia' esistenti (usciti da
    // waApi.scoperti.promote) in una campagna in bozza.
    enroll: (campaignId: string, contactIds: string[]) =>
      req<ReportArruolamento>('/wa/contacts/enroll', {
        method: 'POST',
        body: JSON.stringify({ campaign_id: campaignId, contact_ids: contactIds }),
      }),
  },

  // wa_discover.py: staging dello scan auto-discover (Fase B). number_id e'
  // obbligatorio sia in GET che in POST -- e' la chiave con cui il backend
  // risolve il tenant_id corretto da WaNumber, mai da un campo scritto dal
  // client (barriera IDOR, vedi docstring del router).
  scoperti: {
    list: (numberId: string, filtri?: {
      status?: 'nuovo' | 'promosso' | 'scartato'
      tipoChat?: 'individuale' | 'gruppo' | 'ignoto'
      haNumero?: boolean
      limit?: number
      offset?: number
    }) => {
      const q = new URLSearchParams({ number_id: numberId })
      if (filtri?.status) q.set('status', filtri.status)
      if (filtri?.tipoChat) q.set('tipo_chat', filtri.tipoChat)
      if (filtri?.haNumero !== undefined) q.set('ha_numero', String(filtri.haNumero))
      if (filtri?.limit) q.set('limit', String(filtri.limit))
      if (filtri?.offset) q.set('offset', String(filtri.offset))
      return req<{ chat: WaDiscoveredChat[] }>(`/wa/discovered-chats?${q}`)
    },
    promote: (numberId: string, ids: string[]) =>
      req<ReportPromozione>('/wa/discovered-chats/promote', {
        method: 'POST',
        body: JSON.stringify({ number_id: numberId, ids }),
      }),
  },
}
