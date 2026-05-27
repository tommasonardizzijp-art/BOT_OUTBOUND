import type {
  Account, AccountCreate, AccountMetrics, DMCount, Campaign, CampaignCreate, ABStats,
  ApprovalQueue, WorkerEventsResponse,
  CampaignAccount, CampaignAccountAssign, CampaignAccountUpdate,
  FollowerListResponse, MessageListResponse, MessageStats, DashboardStats,
  ActivityLogListResponse, TimelineResponse, HealthStatus,
  LeadListResponse, AccountRole, BotState,
} from './types'

// Re-export for consumers
export type { AccountRole }

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api'

const TOKEN_STORAGE_KEY = 'bot_outbound_token'

export function getAuthToken(): string | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY)
  } catch {
    return null
  }
}

export function setAuthToken(token: string | null): void {
  if (typeof window === 'undefined') return
  try {
    if (token) window.localStorage.setItem(TOKEN_STORAGE_KEY, token)
    else window.localStorage.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    /* ignore */
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getAuthToken()
  const authHeaders: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {}
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders,
      ...options?.headers,
    },
    ...options,
  })
  if (res.status === 401) {
    // Token missing/expired/invalid → drop it and bounce to login.
    setAuthToken(null)
    if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
      const next = encodeURIComponent(window.location.pathname + window.location.search)
      window.location.href = `/login?next=${next}`
    }
    throw new Error('Sessione scaduta — accedi di nuovo')
  }
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(error.detail || `HTTP ${res.status}`)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

// ---- Accounts ----
export const api = {
  accounts: {
    list: () => request<Account[]>('/accounts'),
    create: (data: AccountCreate) => request<Account>('/accounts', {
      method: 'POST', body: JSON.stringify(data)
    }),
    update: (id: string, data: Partial<AccountCreate & { status: string }>) =>
      request<Account>(`/accounts/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
    delete: (id: string) => request<void>(`/accounts/${id}`, { method: 'DELETE' }),
    verifyChallenge: (id: string, code: string) =>
      request<Account>(`/accounts/${id}/verify-challenge`, {
        method: 'POST', body: JSON.stringify({ code })
      }),
    metrics: (id: string) => request<AccountMetrics>(`/accounts/${id}/metrics`),
    dmCount: (id: string) => request<DMCount>(`/accounts/${id}/dm-count`),
    login: (id: string) =>
      request<Account>(`/accounts/${id}/login`, { method: 'POST' }),
    forceCancelCooldown: (id: string) =>
      request<Account>(`/accounts/${id}/force-cancel-cooldown`, { method: 'POST' }),
    checkSession: (id: string) =>
      request<{ valid: boolean; username: string }>(`/accounts/${id}/check-session`),
    manualLogin: (id: string) => {
      // 6 minute timeout — browser stays open while user logs in manually
      const controller = new AbortController()
      const timeout = setTimeout(() => controller.abort(), 360_000)
      return request<Account>(`/accounts/${id}/manual-login`, {
        method: 'POST',
        signal: controller.signal,
      }).finally(() => clearTimeout(timeout))
    },
    resetSession: (id: string) =>
      request<Account>(`/accounts/${id}/reset-session`, { method: 'POST' }),
    browseSession: (id: string, maxMinutes = 60) => {
      // Browser stays open until user closes it or maxMinutes elapse.
      // Add 60s slack to client timeout so server can close cleanly first.
      const controller = new AbortController()
      const timeout = setTimeout(() => controller.abort(), (maxMinutes * 60 + 60) * 1000)
      return request<{ duration_seconds: number; closed_by: string }>(
        `/accounts/${id}/browse-session?max_minutes=${maxMinutes}`,
        { method: 'POST', signal: controller.signal },
      ).finally(() => clearTimeout(timeout))
    },
  },

  campaigns: {
    list: () => request<Campaign[]>('/campaigns'),
    create: (data: CampaignCreate) => request<Campaign>('/campaigns', {
      method: 'POST', body: JSON.stringify(data)
    }),
    get: (id: string) => request<Campaign>(`/campaigns/${id}`),
    update: (id: string, data: Partial<CampaignCreate>) =>
      request<Campaign>(`/campaigns/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
    delete: (id: string) => request<void>(`/campaigns/${id}`, { method: 'DELETE' }),
    startScrape: (id: string) => request<Campaign>(`/campaigns/${id}/start-scrape`, { method: 'POST' }),
    start: (id: string) => request<Campaign>(`/campaigns/${id}/start`, { method: 'POST' }),
    pause: (id: string) => request<Campaign>(`/campaigns/${id}/pause`, { method: 'POST' }),
    resume: (id: string) => request<Campaign>(`/campaigns/${id}/resume`, { method: 'POST' }),
    stop: (id: string) => request<Campaign>(`/campaigns/${id}/stop`, { method: 'POST' }),
    reset: (id: string) => request<Campaign>(`/campaigns/${id}/reset`, { method: 'POST' }),
    preGenerate: (id: string) => request<Campaign>(`/campaigns/${id}/pre-generate`, { method: 'POST' }),
    retryFailed: (id: string) => request<Campaign>(`/campaigns/${id}/retry-failed`, { method: 'POST' }),
    abStats: (id: string) => request<ABStats>(`/campaigns/${id}/ab-stats`),
    approvalQueue: (id: string) => request<ApprovalQueue>(`/campaigns/${id}/approval-queue`),
    approveMessage: (id: string, followerId: string) =>
      request<{ ok: boolean }>(`/campaigns/${id}/approve-message`, { method: 'POST', body: JSON.stringify({ follower_id: followerId }) }),
    rejectMessage: (id: string, followerId: string) =>
      request<{ ok: boolean }>(`/campaigns/${id}/reject-message`, { method: 'POST', body: JSON.stringify({ follower_id: followerId }) }),
    approvePreview: (id: string) =>
      request<{ ok: boolean; approved: number }>(`/campaigns/${id}/approve-preview`, { method: 'POST' }),
    rejectPreview: (id: string) =>
      request<{ ok: boolean; reset: number }>(`/campaigns/${id}/reject-preview`, { method: 'POST' }),
    events: (id: string, sinceId = 0) =>
      request<WorkerEventsResponse>(`/campaigns/${id}/events?since_id=${sinceId}`),
    startDmAuto: (id: string) =>
      request<Campaign>(`/campaigns/${id}/start-dm-auto`, { method: 'POST' }),
    resumeBreak: (id: string) =>
      request<Campaign>(`/campaigns/${id}/resume-break`, { method: 'POST' }),
  },

  campaignAccounts: {
    list: (campaignId: string) =>
      request<CampaignAccount[]>(`/campaigns/${campaignId}/accounts`),
    assign: (campaignId: string, data: CampaignAccountAssign, force = false) =>
      request<CampaignAccount>(`/campaigns/${campaignId}/accounts${force ? '?force=true' : ''}`, {
        method: 'POST', body: JSON.stringify(data)
      }),
    update: (campaignId: string, accountId: string, data: CampaignAccountUpdate) =>
      request<CampaignAccount>(`/campaigns/${campaignId}/accounts/${accountId}`, {
        method: 'PUT', body: JSON.stringify(data)
      }),
    unassign: (campaignId: string, accountId: string) =>
      request<void>(`/campaigns/${campaignId}/accounts/${accountId}`, { method: 'DELETE' }),
  },

  followers: {
    list: (campaignId: string, params?: { status?: string; page?: number; page_size?: number; sort_by?: string }) => {
      const q = new URLSearchParams()
      if (params?.status) q.set('status', params.status)
      if (params?.page) q.set('page', String(params.page))
      if (params?.page_size) q.set('page_size', String(params.page_size))
      if (params?.sort_by) q.set('sort_by', params.sort_by)
      return request<FollowerListResponse>(`/campaigns/${campaignId}/followers?${q}`)
    },
    skip: (campaignId: string, followerId: string) =>
      request(`/campaigns/${campaignId}/followers/${followerId}/skip`, { method: 'POST' }),
    regenerate: (campaignId: string, followerId: string) =>
      request(`/campaigns/${campaignId}/followers/${followerId}/regenerate`, { method: 'POST' }),
    requeue: (campaignId: string, followerId: string) =>
      request(`/campaigns/${campaignId}/followers/${followerId}/requeue`, { method: 'POST' }),
  },

  messages: {
    list: (params?: { campaign_id?: string; account_id?: string; status?: string; replied_only?: boolean; page?: number; page_size?: number }) => {
      const q = new URLSearchParams()
      if (params?.campaign_id) q.set('campaign_id', params.campaign_id)
      if (params?.account_id) q.set('account_id', params.account_id)
      if (params?.status) q.set('status', params.status)
      if (params?.replied_only) q.set('replied_only', 'true')
      if (params?.page) q.set('page', String(params.page))
      if (params?.page_size) q.set('page_size', String(params.page_size))
      return request<MessageListResponse>(`/messages?${q}`)
    },
    retry: (id: string) => request(`/messages/${id}/retry`, { method: 'POST' }),
    stats: (params?: { period?: string; date_from?: string; date_to?: string; campaign_id?: string; account_id?: string }) => {
      const q = new URLSearchParams()
      if (params?.period) q.set('period', params.period)
      if (params?.date_from) q.set('date_from', params.date_from)
      if (params?.date_to) q.set('date_to', params.date_to)
      if (params?.campaign_id) q.set('campaign_id', params.campaign_id)
      if (params?.account_id) q.set('account_id', params.account_id)
      return request<MessageStats>(`/messages/stats?${q}`)
    },
  },

  leads: {
    list: (params?: {
      search?: string
      campaign_id?: string
      has_replied?: boolean
      verified_only?: boolean
      min_followers?: number
      date_from?: string
      date_to?: string
      page?: number
      page_size?: number
    }) => {
      const q = new URLSearchParams()
      if (params?.search) q.set('search', params.search)
      if (params?.campaign_id) q.set('campaign_id', params.campaign_id)
      if (params?.has_replied !== undefined) q.set('has_replied', String(params.has_replied))
      if (params?.verified_only) q.set('verified_only', 'true')
      if (params?.min_followers !== undefined) q.set('min_followers', String(params.min_followers))
      if (params?.date_from) q.set('date_from', params.date_from)
      if (params?.date_to) q.set('date_to', params.date_to)
      if (params?.page) q.set('page', String(params.page))
      if (params?.page_size) q.set('page_size', String(params.page_size))
      return request<LeadListResponse>(`/leads?${q}`)
    },
    exportBlob: async (params?: {
      search?: string; campaign_id?: string; has_replied?: boolean
      verified_only?: boolean; min_followers?: number
      date_from?: string; date_to?: string
    }) => {
      const q = new URLSearchParams()
      if (params?.search) q.set('search', params.search)
      if (params?.campaign_id) q.set('campaign_id', params.campaign_id)
      if (params?.has_replied !== undefined) q.set('has_replied', String(params.has_replied))
      if (params?.verified_only) q.set('verified_only', 'true')
      if (params?.min_followers !== undefined) q.set('min_followers', String(params.min_followers))
      if (params?.date_from) q.set('date_from', params.date_from)
      if (params?.date_to) q.set('date_to', params.date_to)
      const token = getAuthToken()
      const res = await fetch(`${BASE_URL}/leads/export?${q}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!res.ok) throw new Error(`Export failed: HTTP ${res.status}`)
      return res.blob()
    },
  },

  dashboard: {
    stats: () => request<DashboardStats>('/dashboard/stats'),
    activity: (limit = 50) => request<ActivityLogListResponse>(`/dashboard/activity?limit=${limit}`),
    timeline: (period: '24h' | '7d' | '30d' | '6m' = '24h') => request<TimelineResponse>(`/dashboard/timeline?period=${period}`),
  },

  health: {
    check: () => request<HealthStatus>('/health'),
  },

  ops: {
    summary: () => request<{
      generated_at: string
      sending_stale: { count: number; items: Array<Record<string, string | number | null>> }
      expired_reservations: { count: number; items: Array<Record<string, string | number | null>> }
      stale_follower_locks: { count: number; items: Array<Record<string, string | number | null>> }
      stale_campaigns: { count: number; items: Array<Record<string, string | number | null>> }
      accounts_by_status: Record<string, number>
    }>('/ops/summary'),
  },

  admin: {
    state: () => request<BotState>('/admin/state'),
    halt: (reason: string, kind?: string) =>
      request<BotState>('/admin/halt', { method: 'POST', body: JSON.stringify({ reason, kind }) }),
    resume: () => request<BotState>('/admin/resume', { method: 'POST' }),
  },

  auth: {
    login: (email: string, password: string) =>
      request<{
        access_token: string
        token_type: string
        expires_in: number
        user: { id: string; email: string; role: string; is_active: boolean; created_at: string; last_login_at: string | null }
      }>('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
    me: () => request<{ id: string; email: string; role: string; is_active: boolean; created_at: string; last_login_at: string | null }>('/auth/me'),
  },

  users: {
    list: () => request<Array<{ id: string; email: string; role: string; is_active: boolean; created_at: string; last_login_at: string | null }>>('/users'),
    create: (data: { email: string; password: string; role?: 'admin' | 'operator' }) =>
      request<{ id: string; email: string; role: string; is_active: boolean; created_at: string; last_login_at: string | null }>('/users', {
        method: 'POST', body: JSON.stringify(data),
      }),
    update: (id: string, data: { role?: 'admin' | 'operator'; is_active?: boolean; password?: string }) =>
      request<{ id: string; email: string; role: string; is_active: boolean; created_at: string; last_login_at: string | null }>(`/users/${id}`, {
        method: 'PATCH', body: JSON.stringify(data),
      }),
    delete: (id: string) => request<void>(`/users/${id}`, { method: 'DELETE' }),
  },
}
