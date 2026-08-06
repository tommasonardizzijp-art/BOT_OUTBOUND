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
    throw new Error(detail?.detail ?? `Errore ${res.status}`)
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

export type ScartoIngest = { riga: number; motivo: string; valore: string }

export type ReportIngest = {
  creati: number
  aggiornati: number
  gia_dnc: number
  duplicati_nel_file: number
  scarti: ScartoIngest[]
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
    kpi: (id: string) => req<WaCampaignKpi>(`/wa/campaigns/${id}/kpi`),
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
  },
}
