'use client'

import { use, useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import useSWR from 'swr'
import Link from 'next/link'
import { api } from '@/lib/api'
import { renderPreview, findUnknownPlaceholders } from '@/lib/spintax'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { Separator } from '@/components/ui/separator'
import { toast } from 'sonner'
import {
  ArrowLeft, Play, Pause, Square, Loader2, Users, CheckCircle,
  XCircle, Clock, Trash2, RotateCcw, SkipForward, RefreshCw,
  UserPlus, Pencil, X, AlertTriangle, Shield, Zap, ChevronLeft, ChevronRight,
  ThumbsUp, ThumbsDown, MessageSquare, Activity, ArrowUpDown, MinusCircle, Filter,
  Settings, FileText
} from 'lucide-react'
import type { Campaign, Follower, FollowerStatus, CampaignAccount, Account, AccountStatus, ABStats, ApprovalQueueItem, ApprovalQueue, WorkerEvent, AccountRole, ImportStatusResponse } from '@/lib/types'
import { canDm } from '@/lib/roles'

const FOLLOWER_STATUS_LABEL: Record<FollowerStatus, string> = {
  pending: 'In attesa',
  bio_scraped: 'Bio ottenuta',
  message_generated: 'Messaggio pronto',
  pending_approval: 'In revisione',
  sent: 'Inviato',
  failed: 'Fallito',
  skipped: 'Saltato',
  replied: 'Ha risposto',
}

const ACCOUNT_STATUS_COLORS: Record<AccountStatus, string> = {
  active: 'text-green-400',
  warming_up: 'text-blue-400',
  cooldown: 'text-yellow-400',
  banned: 'text-red-400',
  challenge_required: 'text-orange-400',
  disabled: 'text-gray-500',
}

const FOLLOWER_PAGE_SIZE = 50

function ScrapeBreakPanel({ breakUntil, onResume, loading }: { breakUntil: string; onResume: () => void; loading: boolean }) {
  const remaining = useCountdown(breakUntil)

  return (
    <div className="mx-6 mb-4 bg-amber-950/40 border border-amber-700/50 rounded-lg p-4 flex items-center justify-between gap-4">
      <div>
        <p className="text-sm font-medium text-amber-300">Pausa sessione scraping attiva</p>
        <p className="text-xs text-amber-400/70 mt-0.5">Ripresa automatica tra <span className="font-mono font-bold">{remaining}</span></p>
      </div>
      <Button size="sm" className="bg-amber-600 hover:bg-amber-500 text-white shrink-0" onClick={onResume} disabled={loading}>
        {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Play className="w-4 h-4 mr-1" />Riprendi subito</>}
      </Button>
    </div>
  )
}

// Live mm:ss countdown verso `until` (ISO). Mostra 'Ripresa...' a zero.
function useCountdown(until: string | null | undefined): string {
  const [remaining, setRemaining] = useState('')
  useEffect(() => {
    if (!until) { setRemaining(''); return }
    const tick = () => {
      const diff = Math.max(0, new Date(until).getTime() - Date.now())
      const m = Math.floor(diff / 60000)
      const s = Math.floor((diff % 60000) / 1000)
      setRemaining(diff === 0 ? 'Ripresa...' : `${m}:${s.toString().padStart(2, '0')}`)
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [until])
  return remaining
}

function TwoPhasePanel({ campaign, id, action, loadingAction }: {
  campaign: Campaign
  id: string
  action: (fn: () => Promise<Campaign>) => void
  loadingAction: boolean
}) {
  const [listTarget, setListTarget] = useState<string>('')
  const [bioTarget, setBioTarget] = useState<string>('')
  const lp = campaign.list_progress
  const bp = campaign.bio_progress
  const listing = campaign.status === 'listing' || campaign.status === 'listing_break'
  const bioing = campaign.status === 'scraping' || campaign.status === 'scraping_break'

  return (
    <div className="grid gap-4 md:grid-cols-2">
      {/* Fase 1 — Lista follower */}
      <Card className="bg-gray-900 border-gray-800">
        <CardContent className="pt-4 space-y-3">
          <div className="font-semibold text-sm text-gray-200">Fase 1 — Lista follower</div>
          <div className="text-sm text-gray-400">
            Lista: {lp?.done ?? 0}{lp?.target ? ` / ${lp.target}` : ''}
            {campaign.status === 'listing_break' && <span className="ml-1 text-amber-400">(pausa)</span>}
            {campaign.status === 'listing' && <span className="ml-1 text-blue-400 animate-pulse">in corso…</span>}
          </div>
          <Input
            type="number"
            placeholder="Target (vuoto = tutta la lista)"
            value={listTarget}
            onChange={(e) => setListTarget(e.target.value)}
            disabled={listing}
            className="bg-gray-800 border-gray-700 text-white text-sm h-8 disabled:opacity-50"
          />
          {!listing ? (
            <Button
              size="sm"
              className="w-full bg-blue-600 hover:bg-blue-700 text-white"
              disabled={loadingAction}
              onClick={() => action(() => api.campaigns.startList(id, listTarget ? Number(listTarget) : null))}
            >
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin mr-1" /> : <Play className="w-4 h-4 mr-1" />}
              Avvia Fase Lista
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              className="w-full border-orange-700 text-orange-400"
              disabled={loadingAction}
              onClick={() => action(() => api.campaigns.stopList(id))}
            >
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin mr-1" /> : <Square className="w-4 h-4 mr-1" />}
              Ferma Fase Lista
            </Button>
          )}
        </CardContent>
      </Card>

      {/* Fase 2 — Scraping bio/contatti */}
      <Card className="bg-gray-900 border-gray-800">
        <CardContent className="pt-4 space-y-3">
          <div className="font-semibold text-sm text-gray-200">Fase 2 — Scraping bio/contatti</div>
          <div className="text-sm text-gray-400">
            Bio: {bp?.done ?? 0}{bp?.target ? ` / ${bp.target}` : ''}
            {campaign.status === 'scraping_break' && <span className="ml-1 text-amber-400">(pausa)</span>}
            {campaign.status === 'scraping' && <span className="ml-1 text-green-400 animate-pulse">in corso…</span>}
          </div>
          <Input
            type="number"
            placeholder="Target (vuoto = tutti i pending)"
            value={bioTarget}
            onChange={(e) => setBioTarget(e.target.value)}
            disabled={bioing}
            className="bg-gray-800 border-gray-700 text-white text-sm h-8 disabled:opacity-50"
          />
          {!bioing ? (
            <Button
              size="sm"
              className="w-full bg-green-600 hover:bg-green-700 text-white"
              disabled={loadingAction}
              onClick={() => action(() => api.campaigns.startBios(id, bioTarget ? Number(bioTarget) : null))}
            >
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin mr-1" /> : <Play className="w-4 h-4 mr-1" />}
              Avvia Fase Bio
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              className="w-full border-orange-700 text-orange-400"
              disabled={loadingAction}
              onClick={() => action(() => api.campaigns.stopBios(id))}
            >
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin mr-1" /> : <Square className="w-4 h-4 mr-1" />}
              Ferma Fase Bio
            </Button>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

export default function CampaignDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const [followerPage, setFollowerPage] = useState(1)
  const [followerSort, setFollowerSort] = useState<'updated_at_desc' | 'contact_order'>('updated_at_desc')
  const [followerFilter, setFollowerFilter] = useState<string>('all')

  // BUG-NEW-11: destructure error from all SWR hooks to show proper error UI
  const { data: campaign, error: campaignError, mutate: mutateCampaign } = useSWR<Campaign>(
    `campaign-${id}`, () => api.campaigns.get(id), { refreshInterval: 6000 }
  )
  const { data: followersData, error: followersError, mutate: mutateFollowers } = useSWR(
    `followers-${id}-${followerPage}-${followerSort}-${followerFilter}`,
    () => api.followers.list(id, {
      page_size: FOLLOWER_PAGE_SIZE,
      page: followerPage,
      sort_by: followerSort,
      ...(followerFilter !== 'all' ? { status: followerFilter } : {}),
    }),
    { refreshInterval: 15000, keepPreviousData: true, revalidateOnFocus: false }
  )
  const { data: campaignAccounts, error: campaignAccountsError, mutate: mutateCampaignAccounts } = useSWR<CampaignAccount[]>(
    `campaign-accounts-${id}`, () => api.campaignAccounts.list(id), { refreshInterval: 10000 }
  )
  const { data: allAccounts } = useSWR<Account[]>('accounts', api.accounts.list, { refreshInterval: 30000 })
  const { data: importStatus } = useSWR<ImportStatusResponse>(
    campaign?.source_type === 'import' ? `import-status-${id}` : null,
    () => api.campaigns.importStatus(id),
    { refreshInterval: 5000 }
  )

  // Countdown live verso la fine della pausa sessione (mostrato anche nel badge header)
  const breakRemaining = useCountdown((campaign?.status === 'scraping_break' || campaign?.status === 'listing_break') ? campaign?.scrape_break_until : null)

  const [loadingAction, setLoadingAction] = useState(false)
  const [loadingFollowerId, setLoadingFollowerId] = useState<string | null>(null)
  const [loadingAccountId, setLoadingAccountId] = useState<string | null>(null)

  // Add account dialog
  const [addDialogOpen, setAddDialogOpen] = useState(false)
  const [addAccountId, setAddAccountId] = useState('')
  const [addLimitOverride, setAddLimitOverride] = useState('')
  const [addRoleValue, setAddRoleValue] = useState<AccountRole>('both')
  const [addingAccount, setAddingAccount] = useState(false)

  // Edit limit inline
  const [editingLimitFor, setEditingLimitFor] = useState<string | null>(null) // account_id
  const [editLimitValue, setEditLimitValue] = useState('')

  // Edit campaign daily_limit
  const [editDailyLimitOpen, setEditDailyLimitOpen] = useState(false)
  const [editDailyLimitValue, setEditDailyLimitValue] = useState('')
  const [savingDailyLimit, setSavingDailyLimit] = useState(false)

  // Edit template/messaging toggle (also used to convert lead-only campaigns after scraping)
  const [editTemplateOpen, setEditTemplateOpen] = useState(false)
  const [editTemplateValue, setEditTemplateValue] = useState('')
  const [editTemplateBValue, setEditTemplateBValue] = useState('')
  const [editTemplateCValue, setEditTemplateCValue] = useState('')
  const [editContextValue, setEditContextValue] = useState('')
  const [editAiEnabled, setEditAiEnabled] = useState(false)
  const [editAiSystemPrompt, setEditAiSystemPrompt] = useState('')
  const [editMessagingEnabled, setEditMessagingEnabled] = useState(true)
  const [savingTemplate, setSavingTemplate] = useState(false)
  const [editPreviews, setEditPreviews] = useState<string[]>([])

  // Campaign settings modal
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsForm, setSettingsForm] = useState({
    name: '',
    daily_limit: '',
    scrape_session_size: '',
    scrape_break_minutes_min: '',
    scrape_break_minutes_max: '',
    bio_fetch_delay_min: '',
    bio_fetch_delay_max: '',
    scrape_daily_limit: '',
    messaging_enabled: true,
    base_message_template: '',
    ai_prompt_context: '',
    require_approval: false,
    approval_sample_size: '5',
  })
  const [savingSettings, setSavingSettings] = useState(false)

  // Inbox engine switch (solo campagne dm_threads in stato fermo)
  const [switchingEngine, setSwitchingEngine] = useState(false)
  const [switchingBioEngine, setSwitchingBioEngine] = useState(false)

  const openSettings = () => {
    if (!campaign) return
    setSettingsForm({
      name: campaign.name,
      daily_limit: campaign.daily_limit != null ? String(campaign.daily_limit) : '',
      scrape_session_size: String(campaign.scrape_session_size ?? 250),
      scrape_break_minutes_min: String(campaign.scrape_break_minutes_min ?? 30),
      scrape_break_minutes_max: String(campaign.scrape_break_minutes_max ?? 45),
      bio_fetch_delay_min: String(campaign.bio_fetch_delay_min ?? 5),
      bio_fetch_delay_max: String(campaign.bio_fetch_delay_max ?? 8),
      scrape_daily_limit: campaign.scrape_daily_limit != null ? String(campaign.scrape_daily_limit) : '',
      messaging_enabled: campaign.messaging_enabled,
      base_message_template: campaign.base_message_template ?? '',
      ai_prompt_context: campaign.ai_prompt_context ?? '',
      require_approval: campaign.require_approval,
      approval_sample_size: String(campaign.approval_sample_size ?? 5),
    })
    setSettingsOpen(true)
  }

  const saveSettings = async () => {
    if (!campaign) return
    setSavingSettings(true)
    try {
      type Payload = Parameters<typeof api.campaigns.update>[1]
      const payload: Payload = {
        name: settingsForm.name,
        daily_limit: settingsForm.daily_limit ? parseInt(settingsForm.daily_limit) : null,
        scrape_session_size: parseInt(settingsForm.scrape_session_size) || undefined,
        scrape_break_minutes_min: parseInt(settingsForm.scrape_break_minutes_min) || undefined,
        scrape_break_minutes_max: parseInt(settingsForm.scrape_break_minutes_max) || undefined,
        bio_fetch_delay_min: parseFloat(settingsForm.bio_fetch_delay_min) || undefined,
        bio_fetch_delay_max: parseFloat(settingsForm.bio_fetch_delay_max) || undefined,
        scrape_daily_limit: settingsForm.scrape_daily_limit ? parseInt(settingsForm.scrape_daily_limit) : null,
      }
      const msgEditable = ['draft', 'ready', 'paused', 'completed', 'error'].includes(campaign.status as string)
      if (msgEditable) {
        payload.messaging_enabled = settingsForm.messaging_enabled
        payload.base_message_template = settingsForm.base_message_template || null
        payload.ai_prompt_context = settingsForm.ai_prompt_context || undefined
        payload.require_approval = settingsForm.require_approval
        payload.approval_sample_size = parseInt(settingsForm.approval_sample_size) || 5
      }
      await api.campaigns.update(id, payload)
      await mutateCampaign()
      setSettingsOpen(false)
      toast.success('Impostazioni salvate')
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : 'Errore salvataggio')
    } finally {
      setSavingSettings(false)
    }
  }

  // M14: pre-generate messages
  const [pregenLoading, setPregenLoading] = useState(false)

  // Live worker log feed
  const [liveEvents, setLiveEvents] = useState<WorkerEvent[]>([])
  const lastEventIdRef = useRef(0) // ref avoids stale closure + prevents effect re-run on each batch
  const liveLogRef = useRef<HTMLDivElement>(null)

  // M6: confirm dialog state
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean; title: string; description: string
    confirmLabel: string; variant: 'destructive' | 'warning' | 'default'
    onConfirm: () => void
  }>({ open: false, title: '', description: '', confirmLabel: 'Conferma', variant: 'destructive', onConfirm: () => {} })

  const openConfirm = (
    title: string, description: string, confirmLabel: string,
    onConfirm: () => void, variant: 'destructive' | 'warning' | 'default' = 'destructive'
  ) => setConfirmDialog({ open: true, title, description, confirmLabel, variant, onConfirm })

  const router = useRouter()

  // M10: A/B stats (only poll if campaign has template_b)
  const { data: abStats } = useSWR<ABStats>(
    campaign?.message_template_b ? `ab-stats-${id}` : null,
    () => api.campaigns.abStats(id),
    { refreshInterval: 15000 }
  )

  // Approval preview queue — poll when require_approval is set
  const { data: approvalQueue, mutate: mutateApproval } = useSWR<ApprovalQueue>(
    campaign?.require_approval ? `approval-${id}` : null,
    () => api.campaigns.approvalQueue(id),
    { refreshInterval: 5000, revalidateOnFocus: true }
  )
  const [loadingApproveAll, setLoadingApproveAll] = useState(false)
  const [loadingRejectAll, setLoadingRejectAll] = useState(false)

  // Poll live events every 2s when campaign is running, every 10s otherwise
  useEffect(() => {
    if (!campaign) return
    const isActive = campaign.status === 'running'
      || campaign.status === 'scraping'
      || campaign.status === 'scraping_and_running'
      || campaign.status === 'listing'
    const interval = isActive ? 2000 : 10000

    const poll = async () => {
      try {
        const res = await api.campaigns.events(id, lastEventIdRef.current)
        if (res.events.length > 0) {
          lastEventIdRef.current = res.last_id
          setLiveEvents(prev => {
            const seen = new Set(prev.map(e => e.id))
            const newEvents = res.events.filter((e: WorkerEvent) => !seen.has(e.id))
            if (newEvents.length === 0) return prev
            return [...prev, ...newEvents].slice(-200) // keep last 200
          })
        }
      } catch {
        // silently ignore — backend might be temporarily unavailable
      }
    }

    poll() // immediate first poll
    const timer = setInterval(poll, interval)
    return () => clearInterval(timer)
  }, [campaign?.status, id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-scroll live log to bottom when new events arrive
  useEffect(() => {
    if (liveLogRef.current) {
      liveLogRef.current.scrollTop = liveLogRef.current.scrollHeight
    }
  }, [liveEvents.length])

  const action = async (fn: () => Promise<Campaign>) => {
    setLoadingAction(true)
    try {
      const updated = await fn()
      // Optimistic update — use response data immediately, skip extra GET
      mutateCampaign(updated, false)
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingAction(false)
    }
  }

  const doDelete = async () => {
    setLoadingAction(true)
    try {
      await api.campaigns.delete(id)
      toast.success('Campagna eliminata')
      router.push('/campaigns')
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
      setLoadingAction(false)
    }
  }

  const handleDelete = () => openConfirm(
    'Elimina campagna',
    'Tutti i dati (follower, messaggi, statistiche) verranno eliminati definitivamente. Questa azione non è reversibile.',
    'Elimina',
    doDelete,
  )

  const handleSkip = async (followerId: string, username: string) => {
    setLoadingFollowerId(followerId)
    try {
      await api.followers.skip(id, followerId)
      toast.success(`@${username} saltato`)
      await Promise.all([mutateFollowers(), mutateCampaign()])
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingFollowerId(null)
    }
  }

  const handleRegenerate = async (followerId: string, username: string) => {
    setLoadingFollowerId(followerId)
    try {
      await api.followers.regenerate(id, followerId)
      toast.success(`Messaggio rigenerato per @${username}`)
      await mutateFollowers()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingFollowerId(null)
    }
  }

  // ── Account assignment handlers ─────────────────────────────────────

  const handleAddAccount = async (force = false) => {
    if (!addAccountId) return
    setAddingAccount(true)
    try {
      await api.campaignAccounts.assign(id, {
        account_id: addAccountId,
        daily_limit_override: addLimitOverride ? Number(addLimitOverride) : null,
        role: addRoleValue,
      }, force)
      toast.success('Account assegnato')
      setAddDialogOpen(false)
      setAddAccountId('')
      setAddLimitOverride('')
      setAddRoleValue('both')
      await mutateCampaignAccounts()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Errore'
      if (msg.startsWith('ACCOUNT_IN_USE:')) {
        const campaigns = msg.replace('ACCOUNT_IN_USE:', '')
        openConfirm(
          'Account già in uso',
          `Questo account è già attivo in: ${campaigns}.\n\nUn account condiviso su più campagne running usa il browser in serie (non in parallelo) — la velocità non aumenta. Vuoi assegnarlo comunque?`,
          'Assegna comunque',
          () => handleAddAccount(true),
          'warning'
        )
      } else {
        toast.error(msg)
      }
    } finally {
      setAddingAccount(false)
    }
  }

  const handleUnassign = async (ca: CampaignAccount) => {
    if (!confirm(`Rimuovere @${ca.account_username} da questa campagna?`)) return
    setLoadingAccountId(ca.account_id)
    try {
      await api.campaignAccounts.unassign(id, ca.account_id)
      toast.success(`@${ca.account_username} rimosso`)
      await mutateCampaignAccounts()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingAccountId(null)
    }
  }

  const handleToggleActive = async (ca: CampaignAccount) => {
    setLoadingAccountId(ca.account_id)
    try {
      await api.campaignAccounts.update(id, ca.account_id, { is_active: !ca.is_active })
      toast.success(ca.is_active ? 'Account disabilitato per questa campagna' : 'Account riabilitato')
      await mutateCampaignAccounts()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingAccountId(null)
    }
  }

  const startEditLimit = (ca: CampaignAccount) => {
    setEditingLimitFor(ca.account_id)
    setEditLimitValue(ca.daily_limit_override != null ? String(ca.daily_limit_override) : '')
  }

  const handleSaveLimit = async (ca: CampaignAccount) => {
    setLoadingAccountId(ca.account_id)
    try {
      await api.campaignAccounts.update(id, ca.account_id, {
        daily_limit_override: editLimitValue ? Number(editLimitValue) : null,
      })
      toast.success('Limite aggiornato')
      setEditingLimitFor(null)
      await mutateCampaignAccounts()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingAccountId(null)
    }
  }

  const handleSaveTemplate = async () => {
    if (editMessagingEnabled && editTemplateValue.trim().length < 10) {
      toast.error('Inserisci un template di almeno 10 caratteri')
      return
    }
    if (editMessagingEnabled) {
      const unknownA = findUnknownPlaceholders(editTemplateValue)
      if (unknownA.length > 0) {
        toast.error(`Template A: placeholder sconosciuto ${unknownA[0]} — usa solo {nome} o gruppi {a|b}`)
        return
      }
      const unknownB = findUnknownPlaceholders(editTemplateBValue)
      if (unknownB.length > 0) {
        toast.error(`Template B: placeholder sconosciuto ${unknownB[0]} — usa solo {nome} o gruppi {a|b}`)
        return
      }
      const unknownC = findUnknownPlaceholders(editTemplateCValue)
      if (unknownC.length > 0) {
        toast.error(`Template C: placeholder sconosciuto ${unknownC[0]} — usa solo {nome} o gruppi {a|b}`)
        return
      }
    }
    setSavingTemplate(true)
    try {
      // messaging_enabled va nel payload SOLO se cambiato: non e' always_editable
      // lato backend, includerlo invariato farebbe fallire con 400 l'update dei
      // campi template/AI su una campagna running (che invece e' permesso).
      await api.campaigns.update(id, {
        ...(campaign && editMessagingEnabled !== campaign.messaging_enabled
          ? { messaging_enabled: editMessagingEnabled }
          : {}),
        base_message_template: editMessagingEnabled ? editTemplateValue : null,
        ai_prompt_context: editMessagingEnabled ? (editContextValue || undefined) : undefined,
        message_template_b: editMessagingEnabled ? (editTemplateBValue.trim() || null) : null,
        message_template_c: editMessagingEnabled ? (editTemplateCValue.trim() || null) : null,
        ai_enabled: editMessagingEnabled ? editAiEnabled : false,
        ai_system_prompt: editMessagingEnabled && editAiEnabled ? (editAiSystemPrompt.trim() || null) : null,
      })
      toast.success(editMessagingEnabled ? 'Messaggistica aggiornata' : 'Messaggistica disattivata')
      setEditTemplateOpen(false)
      await mutateCampaign()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setSavingTemplate(false)
    }
  }

  const handleApprove = async (followerId: string) => {
    try {
      await api.campaigns.approveMessage(id, followerId)
      toast.success('Messaggio approvato')
      await mutateApproval()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    }
  }

  const handleReject = async (followerId: string, username: string) => {
    try {
      await api.campaigns.rejectMessage(id, followerId)
      toast.success(`Messaggio rigenerato per @${username}`)
      await mutateApproval()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    }
  }

  const handleApproveAll = async () => {
    setLoadingApproveAll(true)
    try {
      const res = await api.campaigns.approvePreview(id)
      toast.success(`${res.approved} messaggi approvati — generazione batch avviata`)
      await mutateApproval()
      await mutateCampaign()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingApproveAll(false)
    }
  }

  const handleRejectAll = async () => {
    setLoadingRejectAll(true)
    try {
      await api.campaigns.rejectPreview(id)
      toast.success('Anteprima annullata — nuova anteprima in generazione')
      await mutateApproval()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingRejectAll(false)
    }
  }

  const handlePreGenerate = async () => {
    setPregenLoading(true)
    try {
      await api.campaigns.preGenerate(id)
      toast.success('Pre-generazione avviata. I messaggi appariranno qui tra qualche secondo.')
      // Force immediate refresh of followers + approval queue
      // so user sees results as soon as ARQ worker processes them
      setTimeout(() => { mutateFollowers(); mutateApproval?.() }, 3000)
      setTimeout(() => { mutateFollowers(); mutateApproval?.() }, 8000)
      setTimeout(() => { mutateFollowers(); mutateApproval?.() }, 15000)
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setPregenLoading(false)
    }
  }

  const handleRequeue = async (followerId: string, username: string) => {
    setLoadingFollowerId(followerId)
    try {
      await api.followers.requeue(id, followerId)
      toast.success(`@${username} rimesso in coda`)
      await Promise.all([mutateFollowers(), mutateCampaign()])
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingFollowerId(null)
    }
  }

  const handleRetryFailed = async () => {
    setLoadingAction(true)
    try {
      await api.campaigns.retryFailed(id)
      await mutateCampaign()
      await mutateFollowers()
      toast.success('Messaggi falliti rimessi in coda.')
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoadingAction(false)
    }
  }

  const handleSaveCampaignDailyLimit = async () => {
    setSavingDailyLimit(true)
    try {
      await api.campaigns.update(id, {
        daily_limit: editDailyLimitValue ? Number(editDailyLimitValue) : null,
      })
      toast.success('Limite campagna aggiornato')
      setEditDailyLimitOpen(false)
      await mutateCampaign()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setSavingDailyLimit(false)
    }
  }

  const handleInboxEngineSwitch = async (newEngine: 'browser' | 'api') => {
    if (!campaign) return
    const current = campaign.inbox_engine ?? 'browser'
    if (newEngine === current) return
    setSwitchingEngine(true)
    try {
      await api.campaigns.update(id, { inbox_engine: newEngine })
      await mutateCampaign()
      toast.success(`Engine cambiato a ${newEngine === 'browser' ? 'Browser' : 'API'}`)
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore aggiornamento engine')
    } finally {
      setSwitchingEngine(false)
    }
  }

  const handleBioEngineSwitch = async (newEngine: 'api' | 'browser') => {
    if (!campaign) return
    const current = campaign.bio_engine ?? 'api'
    if (newEngine === current) return
    setSwitchingBioEngine(true)
    try {
      await api.campaigns.update(id, { bio_engine: newEngine })
      await mutateCampaign()
      toast.success(`Motore Fase Bio cambiato a ${newEngine === 'browser' ? 'Browser' : 'API'}`)
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore aggiornamento motore Fase Bio')
    } finally {
      setSwitchingBioEngine(false)
    }
  }

  // BUG-NEW-11: distinguish error (backend down) from loading (first fetch)
  if (campaignError) return (
    <div className="flex items-center gap-2 text-red-400 p-8">
      <XCircle className="w-5 h-5 flex-shrink-0" />
      <span>Backend non raggiungibile. Assicurarsi che il server sia in esecuzione, poi ricaricare la pagina.</span>
    </div>
  )
  if (!campaign) return (
    <div className="space-y-6">
      <div className="flex items-start gap-3">
        <Skeleton className="w-8 h-8 rounded-lg" />
        <div className="space-y-2 flex-1">
          <Skeleton className="h-6 w-64" />
          <Skeleton className="h-4 w-32" />
        </div>
      </div>
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-3">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-2 w-full rounded-full" />
        <div className="grid grid-cols-4 gap-4 pt-1">
          {[1, 2, 3, 4].map(i => <div key={i} className="space-y-1"><Skeleton className="h-5 w-10 mx-auto" /><Skeleton className="h-3 w-12 mx-auto" /></div>)}
        </div>
      </div>
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-3">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-3/4" />
      </div>
    </div>
  )

  // Progress bar: if daily_limit is set → today's usage vs limit; else → overall campaign progress
  const todaySent = campaign.messages_sent_today ?? 0
  const progress = campaign.daily_limit
    ? Math.min(100, Math.round((todaySent / campaign.daily_limit) * 100))
    : Math.min(100, Math.round((campaign.messages_sent / (campaign.total_followers || 1)) * 100))

  // Accounts not yet assigned to this campaign
  const assignedIds = new Set(campaignAccounts?.map(ca => ca.account_id) ?? [])
  const availableToAssign = allAccounts?.filter(a => !assignedIds.has(a.id)) ?? []
  const accountsLoaded = allAccounts !== undefined

  const hasAssignedAccounts = (campaignAccounts?.filter(ca => ca.is_active) ?? []).length > 0

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start gap-3">
        <Link href="/campaigns">
          <Button variant="ghost" size="sm" className="text-gray-400 mt-1">
            <ArrowLeft className="w-4 h-4" />
          </Button>
        </Link>
        <div className="flex-1">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-3xl font-bold text-white">{campaign.name}</h1>
            <Badge className={`text-sm ${
              campaign.status === 'scraping_and_running' ? 'bg-gradient-to-r from-blue-700 to-green-700 text-white border-0' :
              campaign.status === 'scraping_break' ? 'bg-amber-700/80 text-amber-100 border-0' :
              campaign.status === 'listing' ? 'bg-blue-800/80 text-blue-100 border-0' :
              campaign.status === 'listing_break' ? 'bg-amber-700/80 text-amber-100 border-0' : ''
            }`}>
              {campaign.status === 'scraping_and_running' ? '⚡ Scraping + DM' :
               campaign.status === 'listing' ? 'In lista' :
               campaign.status === 'listing_break' ? '⏸ Lista in pausa' :
               campaign.source_type === 'import' && campaign.status === 'scraping' ? 'Scraping lista' :
               campaign.source_type === 'import' && campaign.status === 'scraping_break' ? '⏸ Pausa scraping' :
               campaign.status === 'scraping_break' ? '⏸ Pausa sessione' :
               campaign.status}
              {(campaign.status === 'scraping_break' || campaign.status === 'listing_break') && breakRemaining && (
                <span className="ml-1.5 font-mono font-bold">· {breakRemaining}</span>
              )}
            </Badge>
          </div>
          <div className="flex items-center gap-2 mt-1">
            {campaign.source_type === 'import' ? (
              <>
                <p className="text-gray-400 text-base">Lista importata</p>
                <Badge variant="outline" className="text-xs text-gray-500 border-gray-700 py-0">
                  import
                </Badge>
              </>
            ) : (
              <>
                {campaign.scrape_mode !== 'dm_threads' && (
                  <p className="text-gray-400 text-base">@{campaign.target_username}</p>
                )}
                <Badge variant="outline" className="text-xs text-gray-500 border-gray-700 py-0">
                  {campaign.scrape_mode === 'following' ? 'following' : campaign.scrape_mode === 'dm_threads' ? 'dm inbox' : 'follower'}
                </Badge>
                {campaign.scrape_mode === 'dm_threads' && campaign.inbox_engine && (
                  <Badge variant="outline" className="text-xs text-gray-500 border-gray-700 py-0">
                    {campaign.inbox_engine === 'api' ? '⚡ api' : '🛡️ browser'}
                  </Badge>
                )}
              </>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-2 flex-shrink-0">
          {campaign.status === 'draft' && (
            <Button size="sm" variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => action(() => api.campaigns.startScrape(id))} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Avvia scraping'}
            </Button>
          )}
          {campaign.status === 'ready' && campaign.messaging_enabled && (
            <Button size="sm" className="bg-green-600 hover:bg-green-700"
              onClick={() => action(() => api.campaigns.start(id))} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Play className="w-4 h-4 mr-1" />Avvia</>}
            </Button>
          )}
          {campaign.status === 'running' && (
            <Button size="sm" variant="outline" className="border-yellow-600 text-yellow-400"
              onClick={() => action(() => api.campaigns.pause(id))} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Pause className="w-4 h-4 mr-1" />Pausa</>}
            </Button>
          )}
          {campaign.messaging_enabled && (campaign.status === 'paused' || (campaign.status === 'completed' && campaign.messages_pending > 0)) && (
            <Button size="sm" className="bg-green-600 hover:bg-green-700"
              onClick={() => action(() => api.campaigns.resume(id))} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Play className="w-4 h-4 mr-1" />{campaign.scrape_completed_at ? 'Riprendi' : 'Riprendi scraping'}</>}
            </Button>
          )}
          {/* Lista incompleta: invia DM ai profili GIA' raccolti senza riattivare lo
              scraping. Senza questo, da 'paused' l'unica strada era 'Riprendi
              scraping' -> i DM richiedevano per forza lo scraping attivo. */}
          {campaign.messaging_enabled && campaign.status === 'paused' && !campaign.scrape_completed_at
            && (campaignAccounts?.some(ca => ca.is_active && canDm(ca.role)) ?? false) && (
            <Button size="sm" className="bg-green-700 hover:bg-green-600 text-white"
              onClick={() => action(() => api.campaigns.start(id))} disabled={loadingAction}
              title="Invia i DM ai profili già raccolti, lasciando lo scraping fermo">
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Zap className="w-4 h-4 mr-1" />Avvia solo DM</>}
            </Button>
          )}
          {campaign.status === 'scraping' && (
            <Button size="sm" variant="outline" className="border-orange-700 text-orange-400"
              onClick={() => openConfirm(
                'Interrompi scraping',
                'Lo scraping verrà fermato al prossimo ciclo (entro 15 secondi). I profili già raccolti non verranno persi — potrai avviare la campagna con i dati già salvati oppure fare il Reset per ricominciare.',
                'Ferma scraping',
                () => action(() => api.campaigns.stop(id)),
                'warning'
              )} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Square className="w-4 h-4 mr-1" />Ferma scraping</>}
            </Button>
          )}
          {/* Avvia DM in parallelo mentre scraping gira (non per import: fase singola) */}
          {campaign.messaging_enabled && campaign.source_type !== 'import' && campaign.status === 'scraping' && !campaign.scrape_completed_at && (campaignAccounts?.some(ca => ca.is_active && canDm(ca.role)) ?? false) && (
            <Button size="sm" className="bg-green-700 hover:bg-green-600 text-white"
              onClick={() => action(() => api.campaigns.startDmAuto(id))} disabled={loadingAction}
              title="Avvia invio DM mentre lo scraping continua in background (auto-gen)">
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Zap className="w-4 h-4 mr-1" />Avvia DM ora</>}
            </Button>
          )}
          {/* Pausa sessione scraping */}
          {campaign.status === 'scraping_break' && (
            <Button size="sm" className="bg-amber-600 hover:bg-amber-500 text-white"
              onClick={() => action(() => api.campaigns.resumeBreak(id))} disabled={loadingAction}
              title="Interrompi la pausa e riprendi lo scraping adesso">
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Play className="w-4 h-4 mr-1" />Riprendi subito</>}
            </Button>
          )}
          {/* Pausa/Stop su stato composto */}
          {campaign.status === 'scraping_and_running' && (
            <Button size="sm" variant="outline" className="border-yellow-600 text-yellow-400"
              onClick={() => action(() => api.campaigns.pause(id))} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Pause className="w-4 h-4 mr-1" />Pausa</>}
            </Button>
          )}
          {(campaign.status === 'running' || campaign.status === 'paused' || campaign.status === 'scraping_and_running' || campaign.status === 'scraping_break') && (
            <Button size="sm" variant="outline" className="border-red-800 text-red-400"
              onClick={() => openConfirm(
                'Ferma campagna',
                'La campagna verrà fermata. I follower già scrapati e i messaggi già generati non verranno persi — potrai riprendere da dove ti sei fermato con il pulsante "Riprendi".',
                'Ferma',
                () => action(() => api.campaigns.stop(id)),
                'warning'
              )} disabled={loadingAction}>
              <Square className="w-4 h-4 mr-1" />Stop
            </Button>
          )}
          {/* Retry failed messages */}
          {(campaign.status === 'ready' || campaign.status === 'paused' || campaign.status === 'running') && campaign.messages_failed > 0 && (
            <Button size="sm" variant="outline" className="border-orange-700 text-orange-400 hover:bg-orange-900/20"
              onClick={handleRetryFailed} disabled={loadingAction}
              title={`Rimetti in coda ${campaign.messages_failed} messaggi falliti`}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><RotateCcw className="w-4 h-4 mr-1" />Ritenta falliti ({campaign.messages_failed})</>}
            </Button>
          )}
          {/* M14: pre-generate messages button — only for ready/paused */}
          {campaign.messaging_enabled && (campaign.status === 'ready' || campaign.status === 'paused') && (
            <Button size="sm" variant="outline" className="border-blue-700 text-blue-400 hover:bg-blue-900/20"
              onClick={handlePreGenerate} disabled={pregenLoading || loadingAction}
              title="Pre-genera messaggi AI per tutti i follower prima di avviare">
              {pregenLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <><RefreshCw className="w-4 h-4 mr-1" />Pre-genera</>}
            </Button>
          )}
          {/* Ripresa dopo errore: riavvia senza perdere il progresso già raccolto */}
          {campaign.status === 'error' && (
            <Button size="sm" className="bg-green-600 hover:bg-green-700"
              onClick={() => action(() => api.campaigns.startScrape(id))} disabled={loadingAction}
              title="Riprende la campagna dall'errore senza perdere i profili già raccolti (i profili non ancora risolti vengono ripresi)">
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Play className="w-4 h-4 mr-1" />{campaign.source_type === 'import' ? 'Riprendi risoluzione' : 'Riavvia scraping'}</>}
            </Button>
          )}
          {(campaign.status === 'error' || campaign.status === 'completed' || campaign.status === 'paused' || campaign.status === 'scraping' || campaign.status === 'scraping_and_running' || campaign.status === 'scraping_break' || campaign.status === 'listing' || campaign.status === 'listing_break') && (
            <Button size="sm" variant="outline" className="border-cyan-700 text-cyan-400 hover:bg-cyan-900/20"
              onClick={() => openConfirm(
                'Reset campagna',
                'Tutti i messaggi verranno cancellati e i follower reimpostati a bio_scraped. Dovrai riavviare la campagna da zero.',
                'Reset',
                () => action(() => api.campaigns.reset(id)),
                'warning'
              )}
              disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><RotateCcw className="w-4 h-4 mr-1" />Reset</>}
            </Button>
          )}
          {(campaign.status === 'draft' || campaign.status === 'error' || campaign.status === 'completed' || campaign.status === 'scraping' || campaign.status === 'listing') && (
            <Button size="sm" variant="outline" className="border-red-800 text-red-400 hover:bg-red-900/20"
              onClick={handleDelete} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Trash2 className="w-4 h-4 mr-1" />Elimina</>}
            </Button>
          )}
          <Button size="sm" variant="outline" className="border-gray-700 text-gray-400 hover:text-white hover:border-gray-500"
            onClick={openSettings} title="Impostazioni campagna">
            <Settings className="w-4 h-4" />
          </Button>
        </div>
      </div>

      {/* Scraping break panel */}
      {campaign.status === 'scraping_break' && campaign.scrape_break_until && (
        <ScrapeBreakPanel breakUntil={campaign.scrape_break_until} onResume={() => action(() => api.campaigns.resumeBreak(id))} loading={loadingAction} />
      )}

      {/* Import status panel (solo campagne import) */}
      {campaign.source_type === 'import' && importStatus && (
        <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 space-y-2">
          <h3 className="text-sm font-medium text-gray-200">Profili importati</h3>
          <div className="grid grid-cols-3 gap-2 text-sm">
            <div><span className="text-gray-400">Totale:</span> <span className="text-white">{importStatus.total}</span></div>
            <div><span className="text-gray-400">Da risolvere:</span> <span className="text-yellow-300">{importStatus.pending}</span></div>
            <div><span className="text-gray-400">Risolti:</span> <span className="text-green-400">{importStatus.resolved}</span></div>
            <div><span className="text-gray-400">Non trovati:</span> <span className="text-gray-300">{importStatus.not_found}</span></div>
            <div><span className="text-gray-400">Privati:</span> <span className="text-gray-300">{importStatus.private}</span></div>
            <div><span className="text-gray-400">Errori:</span> <span className="text-red-400">{importStatus.error}</span></div>
          </div>
        </div>
      )}

      {/* No account warning */}
      {(campaign.status === 'draft' || campaign.status === 'ready') && !hasAssignedAccounts && (
        <div className="flex items-start gap-3 rounded-lg border border-yellow-700/50 bg-yellow-900/10 px-4 py-3">
          <AlertTriangle className="w-4 h-4 text-yellow-400 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-yellow-300">
            Nessun account attivo assegnato. Assegna almeno un account prima di avviare la campagna.
          </p>
        </div>
      )}

      {/* Inbox engine switch — solo campagne dm_threads in stato fermo */}
      {campaign.scrape_mode === 'dm_threads' && (['draft', 'ready', 'paused', 'error'] as Campaign['status'][]).includes(campaign.status) && (
        <div className="rounded-lg border border-gray-700/50 bg-gray-800/30 px-4 py-3 space-y-3">
          <div>
            <p className="text-sm text-gray-300 font-medium">Engine estrazione inbox</p>
            <p className="text-xs text-gray-500 mt-0.5">
              L&apos;inbox dei DM già avviati si legge via API. Il motore browser è stato rimosso
              (la lista web dei DM non espone username/pk).
            </p>
          </div>
          <div className="flex gap-3">
            <button
              type="button"
              disabled={switchingEngine}
              onClick={() => handleInboxEngineSwitch('api')}
              className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors disabled:opacity-50 ${
                campaign.inbox_engine === 'api'
                  ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                  : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
              }`}
            >
              ⚡ API
              <span className="block text-xs font-normal mt-0.5 opacity-70">Unico motore supportato per l&apos;inbox</span>
            </button>
            <button
              type="button"
              disabled
              title="L'estrazione dell'inbox usa sempre l'API: il motore browser è stato rimosso."
              className="flex-1 py-2 px-3 rounded-lg border text-sm font-medium bg-gray-800 border-gray-700 text-gray-500 opacity-50 cursor-not-allowed"
            >
              🛡️ Browser (non disponibile)
              <span className="block text-xs font-normal mt-0.5 opacity-70">Deprecato — l&apos;inbox usa sempre l&apos;API</span>
            </button>
          </div>
          {switchingEngine && (
            <div className="flex items-center gap-2 text-xs text-gray-500">
              <Loader2 className="w-3 h-3 animate-spin" />Salvataggio...
            </div>
          )}
        </div>
      )}

      {/* Bio engine switch — campagne ferme (draft/ready/paused/error): si cambia solo a
          campagna NON in corso (un fan-out browser e un loop API attivi si pesterebbero).
          Vale sia per scrape (Fase Bio) sia per import (risoluzione lista). */}
      {['draft', 'ready', 'paused', 'error'].includes(campaign.status) && (
        <div className="rounded-lg border border-gray-700/50 bg-gray-800/30 px-4 py-3 space-y-3">
          <div>
            <p className="text-sm text-gray-300 font-medium">Motore {campaign.source_type === 'import' ? 'risoluzione' : 'Fase Bio'}</p>
            <p className="text-xs text-gray-500 mt-0.5">
              Motore per recuperare bio/contatti dei profili · API (veloce, consuma cap) o Browser (prudente, no cap)
            </p>
          </div>
          <div className="flex gap-3">
            <button
              type="button"
              disabled={switchingBioEngine}
              onClick={() => handleBioEngineSwitch('api')}
              className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors disabled:opacity-50 ${
                (campaign.bio_engine ?? 'api') === 'api'
                  ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                  : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
              }`}
            >
              ⚡ API (veloce)
              <span className="block text-xs font-normal mt-0.5 opacity-70">Consuma il cap lookup/giorno</span>
            </button>
            <button
              type="button"
              disabled={switchingBioEngine}
              onClick={() => handleBioEngineSwitch('browser')}
              className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors disabled:opacity-50 ${
                campaign.bio_engine === 'browser'
                  ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                  : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
              }`}
            >
              🛡️ Browser (prudente)
              <span className="block text-xs font-normal mt-0.5 opacity-70">Nessun consumo del cap API</span>
            </button>
          </div>
          {switchingBioEngine && (
            <div className="flex items-center gap-2 text-xs text-gray-500">
              <Loader2 className="w-3 h-3 animate-spin" />Salvataggio...
            </div>
          )}
        </div>
      )}

      {/* Two-phase scraping panel (Fase Lista + Fase Bio) — solo campagne scrape */}
      {campaign.source_type === 'scrape' && (
        <TwoPhasePanel campaign={campaign} id={id} action={action} loadingAction={loadingAction} />
      )}

      {/* Pre-approval workflow guide */}
      {campaign.status === 'ready' && campaign.require_approval && (
        <div className="rounded-lg border border-purple-700/50 bg-purple-900/10 px-4 py-3 space-y-2">
          <p className="text-sm text-purple-300 font-medium flex items-center gap-2">
            <MessageSquare className="w-4 h-4" />
            Approvazione messaggi attiva — segui questi passi prima di avviare
          </p>
          <ol className="text-xs text-gray-400 space-y-1 ml-6 list-decimal">
            <li>
              <span className="text-gray-300">Pre-genera i messaggi campione</span>
              {' — '}clicca il pulsante <span className="text-blue-400">Pre-genera</span> qui sopra
            </li>
            <li>
              <span className="text-gray-300">Revisiona e approva</span>
              {' — '}controlla la sezione <span className="text-purple-400">Anteprima messaggi</span> qui sotto
            </li>
            <li>
              <span className="text-gray-300">Avvia la campagna</span>
              {' — '}solo i messaggi approvati verranno inviati
            </li>
          </ol>
        </div>
      )}

      {/* Progress */}
      {campaign.total_followers > 0 && (
        <Card className="bg-gray-900 border-gray-800">
          <CardContent className="pt-5 space-y-3">
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">
                {campaign.daily_limit
                  ? <span>Oggi: <span className="text-white font-medium">{todaySent}</span> / {campaign.daily_limit} DM</span>
                  : 'Progresso campagna'
                }
              </span>
              <div className="flex items-center gap-3">
                {campaign.daily_limit != null && (
                  <span className="text-xs text-gray-500 flex items-center gap-1">
                    <Zap className="w-3 h-3" />
                    Limite campagna: {campaign.daily_limit} DM/giorno
                    <button
                      className="text-gray-600 hover:text-gray-400 ml-1"
                      onClick={() => { setEditDailyLimitValue(String(campaign.daily_limit ?? '')); setEditDailyLimitOpen(true) }}
                      title="Modifica limite"
                    >
                      <Pencil className="w-3 h-3" />
                    </button>
                  </span>
                )}
                {campaign.daily_limit == null && (
                  <button
                    className="text-xs text-gray-600 hover:text-gray-400 flex items-center gap-1"
                    onClick={() => { setEditDailyLimitValue(''); setEditDailyLimitOpen(true) }}
                  >
                    <Zap className="w-3 h-3" />Imposta limite campagna
                  </button>
                )}
                <span className="text-white font-medium">{progress}%</span>
              </div>
            </div>
            <Progress value={progress} className="h-3" />
            <div className="grid grid-cols-7 gap-4 pt-1">
              <div className="text-center">
                <div className="flex items-center justify-center gap-1 text-green-400 font-semibold text-lg">
                  <CheckCircle className="w-4 h-4" />{campaign.messages_sent}
                </div>
                <p className="text-sm text-gray-500 mt-1">Inviati</p>
              </div>
              <div className="text-center">
                <div className={`flex items-center justify-center gap-1 font-semibold text-lg ${
                  (campaign.reply_rate ?? 0) >= 0.15 ? 'text-emerald-400'
                  : (campaign.reply_rate ?? 0) >= 0.05 ? 'text-yellow-400'
                  : campaign.messages_sent > 0 ? 'text-red-400'
                  : 'text-gray-500'
                }`}>
                  <MessageSquare className="w-4 h-4" />{campaign.messages_replied ?? 0}
                </div>
                <p className="text-sm text-gray-500 mt-1">
                  Risposte
                  {campaign.messages_sent > 0 && (
                    <span className="ml-1 text-xs">({((campaign.reply_rate ?? 0) * 100).toFixed(1)}%)</span>
                  )}
                </p>
              </div>
              <div className="text-center">
                <div className="flex items-center justify-center gap-1 text-red-400 font-semibold text-lg">
                  <XCircle className="w-4 h-4" />{campaign.messages_failed}
                </div>
                <p className="text-sm text-gray-500 mt-1">Falliti</p>
              </div>
              <div className="text-center">
                <div className="flex items-center justify-center gap-1 text-gray-500 font-semibold text-lg">
                  <MinusCircle className="w-4 h-4" />{campaign.messages_skipped ?? 0}
                </div>
                <p className="text-sm text-gray-500 mt-1">Skippati</p>
              </div>
              <div className="text-center">
                <div className="flex items-center justify-center gap-1 text-yellow-400 font-semibold text-lg">
                  <Clock className="w-4 h-4" />{campaign.messages_pending}
                </div>
                <p className="text-sm text-gray-500 mt-1">In coda DM</p>
              </div>
              <div className="text-center">
                <div className="flex items-center justify-center gap-1 text-blue-400 font-semibold text-lg">
                  <Users className="w-4 h-4" />{campaign.list_progress?.done ?? campaign.total_followers}
                </div>
                <p className="text-sm text-gray-500 mt-1">In lista</p>
              </div>
              <div className="text-center">
                <div className="flex items-center justify-center gap-1 text-cyan-400 font-semibold text-lg">
                  <FileText className="w-4 h-4" />{campaign.bio_progress?.done ?? 0}
                </div>
                <p className="text-sm text-gray-500 mt-1">Bio estratte</p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Account assignment */}
      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base text-gray-300 flex items-center gap-2">
              <Shield className="w-4 h-4" />
              Account assegnati
              {campaignAccounts && (
                <Badge variant="outline" className="text-xs border-gray-700 text-gray-500">
                  {campaignAccounts.filter(ca => ca.is_active).length} attivi
                </Badge>
              )}
            </CardTitle>
            <Button
              size="sm"
              variant="outline"
              className="border-gray-700 text-gray-300 hover:text-white h-7 text-xs"
              onClick={() => setAddDialogOpen(true)}
              disabled={accountsLoaded && availableToAssign.length === 0}
              title={accountsLoaded && availableToAssign.length === 0 ? 'Tutti gli account sono già assegnati' : ''}
            >
              {!accountsLoaded ? <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" /> : <UserPlus className="w-3.5 h-3.5 mr-1" />}
              Aggiungi account
            </Button>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          {!campaignAccounts ? (
            <div className="flex items-center justify-center py-6 gap-2 text-gray-500">
              <Loader2 className="w-4 h-4 animate-spin" />
              <span className="text-sm">Caricamento...</span>
            </div>
          ) : campaignAccounts.length === 0 ? (
            <div className="text-center py-6">
              <Users className="w-8 h-8 text-gray-700 mx-auto mb-2" />
              <p className="text-sm text-gray-500">Nessun account assegnato.</p>
              <p className="text-xs text-gray-600 mt-1">Assegna almeno un account per poter avviare la campagna.</p>
            </div>
          ) : (
            <div className="space-y-2 mt-1">
              {campaignAccounts.map((ca) => {
                const isLoadingThis = loadingAccountId === ca.account_id
                const isEditingLimit = editingLimitFor === ca.account_id
                const accountInfo = allAccounts?.find(a => a.id === ca.account_id)

                return (
                  <div
                    key={ca.id}
                    className={`flex items-center gap-3 rounded-lg px-3 py-2.5 ${ca.is_active ? 'bg-gray-800/50' : 'bg-gray-900/50 opacity-60'}`}
                  >
                    {/* Status dot */}
                    <div className={`w-2 h-2 rounded-full flex-shrink-0 ${ca.is_active ? 'bg-green-400' : 'bg-gray-600'}`} />

                    {/* Username + account status */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-base text-white font-medium">@{ca.account_username}</span>
                        {accountInfo && (
                          <span className={`text-xs ${ACCOUNT_STATUS_COLORS[accountInfo.status]}`}>
                            {accountInfo.status}
                          </span>
                        )}
                        {!ca.is_active && (
                          <span className="text-xs text-gray-600">disabilitato</span>
                        )}
                      </div>

                      {/* Limit display / edit */}
                      <div className="mt-0.5">
                        {isEditingLimit ? (
                          <div className="flex items-center gap-1.5 mt-1">
                            <Input
                              type="number"
                              placeholder="Es. 15 (vuoto = default)"
                              value={editLimitValue}
                              onChange={e => setEditLimitValue(e.target.value)}
                              min={1}
                              max={200}
                              className="h-6 text-xs bg-gray-700 border-gray-600 text-white w-44 px-2"
                              autoFocus
                            />
                            <Button
                              size="sm"
                              className="h-6 text-xs bg-purple-600 hover:bg-purple-700 px-2"
                              onClick={() => handleSaveLimit(ca)}
                              disabled={isLoadingThis}
                            >
                              {isLoadingThis ? <Loader2 className="w-3 h-3 animate-spin" /> : 'Salva'}
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-6 text-xs text-gray-400 px-1.5"
                              onClick={() => setEditingLimitFor(null)}
                            >
                              Annulla
                            </Button>
                          </div>
                        ) : (
                          <div className="flex items-center gap-1.5">
                            <span className="text-xs text-gray-500">
                              {ca.daily_limit_override != null
                                ? `${ca.daily_limit_override} DM/giorno (override)`
                                : `default account${accountInfo ? ` (${accountInfo.daily_message_limit})` : ''}`
                              }
                            </span>
                            <button
                              className="text-gray-700 hover:text-gray-400"
                              onClick={() => startEditLimit(ca)}
                              title="Modifica limite"
                            >
                              <Pencil className="w-3 h-3" />
                            </button>
                          </div>
                        )}
                      </div>
                    </div>

                    {/* Role selector */}
                    <div className="flex-shrink-0">
                      <select
                        value={ca.role ?? 'both'}
                        onChange={async (e) => {
                          try {
                            await api.campaignAccounts.update(id, ca.account_id, { role: e.target.value as AccountRole })
                            mutateCampaignAccounts()
                          } catch { toast.error('Errore aggiornamento ruolo') }
                        }}
                        className="text-xs bg-gray-700 border border-gray-600 text-gray-300 rounded px-1.5 py-0.5 cursor-pointer hover:border-gray-500"
                        title="Ruolo account in questa campagna"
                      >
                        <option value="both">Scraping + DM</option>
                        <option value="scraping">Solo scraping</option>
                        <option value="dm">Solo DM</option>
                        {campaign.scrape_mode === 'dm_threads' && (
                          <optgroup label="Inbox DM (max 1 account)">
                            <option value="inbox">Solo inbox</option>
                            <option value="inbox_scraping">Inbox + scraping</option>
                            <option value="inbox_dm">Inbox + DM</option>
                            <option value="inbox_both">Inbox + tutto</option>
                          </optgroup>
                        )}
                      </select>
                    </div>

                    {/* Account daily progress */}
                    {accountInfo && (
                      <div className="text-xs text-gray-500 text-right flex-shrink-0 hidden sm:block">
                        <span className={accountInfo.daily_message_count >= accountInfo.daily_message_limit ? 'text-red-400' : ''}>
                          {accountInfo.daily_message_count}/{ca.daily_limit_override ?? accountInfo.daily_message_limit}
                        </span>
                        <span className="text-gray-600"> oggi</span>
                      </div>
                    )}

                    {/* Actions */}
                    <div className="flex items-center gap-1 flex-shrink-0">
                      <button
                        className={`text-xs px-1.5 py-0.5 rounded border ${ca.is_active ? 'border-yellow-700 text-yellow-600 hover:text-yellow-400' : 'border-green-700 text-green-600 hover:text-green-400'}`}
                        onClick={() => handleToggleActive(ca)}
                        disabled={isLoadingThis}
                        title={ca.is_active ? 'Disabilita' : 'Abilita'}
                      >
                        {isLoadingThis ? <Loader2 className="w-3 h-3 animate-spin" /> : (ca.is_active ? 'Disabilita' : 'Abilita')}
                      </button>
                      <button
                        className="text-gray-600 hover:text-red-400 p-1"
                        onClick={() => handleUnassign(ca)}
                        disabled={isLoadingThis}
                        title="Rimuovi dalla campagna"
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Message template */}
      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base text-gray-300">
              Template messaggio
              <span className={`ml-2 text-xs px-2 py-0.5 rounded ${campaign.ai_enabled ? 'bg-purple-900 text-purple-300' : 'bg-gray-800 text-gray-400'}`}>
                {campaign.ai_enabled ? '🤖 AI attiva' : '📋 Template'}
              </span>
              {campaign.message_template_b && (
                <Badge variant="outline" className="ml-2 text-xs border-purple-700 text-purple-400">A/B</Badge>
              )}
            </CardTitle>
            {/* Template/AI editabili in QUALSIASI stato (anche running): il backend li
                legge freschi a ogni generazione — i messaggi gia' generati restano,
                i prossimi seguono la nuova modalita' (decisione 11/07). */}
            <button
              className="text-gray-600 hover:text-gray-300 flex items-center gap-1 text-xs"
              onClick={() => {
                setEditTemplateValue(campaign.base_message_template ?? '')
                setEditTemplateBValue(campaign.message_template_b ?? '')
                setEditTemplateCValue(campaign.message_template_c ?? '')
                setEditContextValue(campaign.ai_prompt_context ?? '')
                setEditAiEnabled(campaign.ai_enabled)
                setEditAiSystemPrompt(campaign.ai_system_prompt ?? '')
                setEditMessagingEnabled(campaign.messaging_enabled)
                setEditPreviews([])
                setEditTemplateOpen(true)
              }}
            >
              <Pencil className="w-3 h-3" />{campaign.messaging_enabled ? 'Modifica' : 'Abilita messaggi'}
            </button>
          </div>
        </CardHeader>
        <CardContent>
          {campaign.message_template_b && (
            <p className="text-xs text-gray-500 mb-1">Template A</p>
          )}
          {campaign.messaging_enabled && campaign.base_message_template ? (
            <p className="text-sm text-gray-300 whitespace-pre-wrap">{campaign.base_message_template}</p>
          ) : (
            <p className="text-sm text-gray-500">Messaggistica disattivata. I lead restano disponibili per export o attivazione successiva.</p>
          )}
          {campaign.message_template_b && (
            <>
              <Separator className="my-3 bg-gray-800" />
              <p className="text-xs text-gray-500 mb-1">Template B</p>
              <p className="text-sm text-gray-300 whitespace-pre-wrap">{campaign.message_template_b}</p>
            </>
          )}
          {campaign.ai_prompt_context && (
            <>
              <Separator className="my-3 bg-gray-800" />
              <p className="text-xs text-gray-500 mb-1">Contesto AI</p>
              <p className="text-xs text-gray-400">{campaign.ai_prompt_context}</p>
            </>
          )}
        </CardContent>
      </Card>

      {/* M10: A/B stats — shown when template_b is active and messages exist */}
      {abStats && (abStats.variant_a || abStats.variant_b) && (() => {
        const aRate = abStats.variant_a?.reply_rate ?? 0
        const bRate = abStats.variant_b?.reply_rate ?? 0
        const aSent = abStats.variant_a?.sent ?? 0
        const bSent = abStats.variant_b?.sent ?? 0
        // Highlight a winner only when both variants have at least 5 sent messages
        const winner = aSent >= 5 && bSent >= 5
          ? (aRate > bRate ? 'a' : bRate > aRate ? 'b' : null)
          : null

        return (
          <Card className="bg-gray-900 border-gray-800">
            <CardHeader className="pb-2">
              <CardTitle className="text-base text-gray-300">Risultati A/B test</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-4">
                {[
                  { key: 'a' as const, label: 'Template A', data: abStats.variant_a },
                  { key: 'b' as const, label: 'Template B', data: abStats.variant_b },
                ].map(({ key, label, data }) => {
                  const isWinner = winner === key
                  return (
                    <div
                      key={label}
                      className={`bg-gray-800/60 rounded-lg p-3 space-y-1.5 ${
                        isWinner ? 'ring-2 ring-emerald-500/70' : ''
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <p className="text-xs text-gray-400 font-medium">{label}</p>
                        {isWinner && (
                          <Badge className="bg-emerald-700 text-white text-[10px] px-1.5 py-0">
                            Vincitore
                          </Badge>
                        )}
                      </div>
                      {data ? (
                        <>
                          <div className="flex justify-between text-sm">
                            <span className="text-green-400">{data.sent} inviati</span>
                            <span className="text-red-400">{data.failed} falliti</span>
                            <span className="text-yellow-400">{data.pending} in coda</span>
                          </div>
                          <div className="flex items-center justify-between pt-1 border-t border-gray-700/50">
                            <span className="text-xs text-gray-400">
                              💬 {data.replied} risposte / {data.sent} inviati
                            </span>
                            <span className={`text-sm font-semibold ${
                              data.reply_rate >= 0.15 ? 'text-emerald-400'
                              : data.reply_rate >= 0.05 ? 'text-yellow-400'
                              : data.sent > 0 ? 'text-red-400'
                              : 'text-gray-500'
                            }`}>
                              {(data.reply_rate * 100).toFixed(1)}%
                            </span>
                          </div>
                          <div className="text-xs text-gray-500">
                            {data.total > 0
                              ? `${Math.round((data.sent / data.total) * 100)}% success rate`
                              : 'Nessun messaggio'
                            }
                          </div>
                        </>
                      ) : (
                        <p className="text-xs text-gray-600">Nessun dato</p>
                      )}
                    </div>
                  )
                })}
              </div>
            </CardContent>
          </Card>
        )
      })()}

      {/* M15 rev: Approval queue — shown when require_approval=true and items pending */}
      {campaign.require_approval && (
        <Card className="bg-gray-900 border-purple-800/40">
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base text-gray-300 flex items-center gap-2">
                <MessageSquare className="w-4 h-4 text-purple-400" />
                Anteprima messaggi
                {approvalQueue && approvalQueue.total > 0 && (
                  <Badge className="bg-purple-700 text-white text-xs">{approvalQueue.total} in attesa</Badge>
                )}
              </CardTitle>
              <p className="text-xs text-gray-600">
                Revisiona la qualità prima di generare tutti i messaggi
              </p>
            </div>
          </CardHeader>
          <CardContent className="pt-0">
            {!approvalQueue && (
              <div className="flex items-center gap-2 py-4 text-xs text-gray-500">
                <Loader2 className="w-3 h-3 animate-spin" />Caricamento...
              </div>
            )}
            {approvalQueue?.total === 0 && (
              <div className="py-4 text-center text-sm text-gray-500">
                Nessun messaggio in anteprima.
                {campaign.status === 'ready' || campaign.status === 'paused'
                  ? ' Usa "Pre-genera" per vedere un campione.'
                  : ''}
              </div>
            )}
            {approvalQueue && approvalQueue.total > 0 && (
              <div className="space-y-3 mt-1">
                {approvalQueue.items.map((item: ApprovalQueueItem) => (
                  <div key={item.follower_id} className="rounded-lg border border-gray-700/60 bg-gray-800/40 p-3 space-y-2">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-base font-medium text-white">@{item.username}</span>
                      {item.full_name && <span className="text-xs text-gray-500">{item.full_name}</span>}
                      {item.template_variant && (
                        <Badge variant="outline" className="text-xs border-purple-800 text-purple-400">
                          Template {item.template_variant.toUpperCase()}
                        </Badge>
                      )}
                    </div>
                    {item.biography && (
                      <p className="text-xs text-gray-500 line-clamp-1">{item.biography}</p>
                    )}
                    {item.generated_text && (
                      <div className="bg-gray-900/60 rounded p-2 border-l-2 border-purple-700">
                        <p className="text-xs text-gray-300 leading-relaxed">{item.generated_text}</p>
                      </div>
                    )}
                  </div>
                ))}

                {/* Global approval actions */}
                <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-gray-800">
                  <Button
                    size="sm"
                    className="bg-green-700 hover:bg-green-600 text-white"
                    onClick={handleApproveAll}
                    disabled={loadingApproveAll || loadingRejectAll}
                  >
                    {loadingApproveAll
                      ? <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />
                      : <ThumbsUp className="w-3.5 h-3.5 mr-1.5" />}
                    Approva tutti — genera i rimanenti
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="border-gray-600 text-gray-400 hover:bg-gray-800"
                    onClick={handleRejectAll}
                    disabled={loadingApproveAll || loadingRejectAll}
                  >
                    {loadingRejectAll
                      ? <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />
                      : <RefreshCw className="w-3.5 h-3.5 mr-1.5" />}
                    Rigenera anteprima
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-gray-500 hover:text-gray-300"
                    onClick={() => setEditTemplateOpen(true)}
                    disabled={loadingApproveAll || loadingRejectAll}
                  >
                    <Pencil className="w-3.5 h-3.5 mr-1.5" />Modifica prompt
                  </Button>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Live worker log */}
      {(campaign.status === 'running' || campaign.status === 'scraping' || campaign.status === 'scraping_and_running' || campaign.status === 'paused' || campaign.status === 'listing' || campaign.status === 'listing_break' || liveEvents.length > 0) && (
        <Card className="bg-gray-900 border-gray-800">
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base text-gray-300 flex items-center gap-2">
                <Activity className="w-4 h-4 text-green-400" />
                Log worker in tempo reale
                {campaign.status === 'running' && (
                  <span className="inline-flex items-center gap-1 text-xs text-green-400">
                    <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
                    live
                  </span>
                )}
              </CardTitle>
              {liveEvents.length > 0 && (
                <button
                  className="text-xs text-gray-600 hover:text-gray-400"
                  onClick={() => setLiveEvents([])}
                >
                  Pulisci
                </button>
              )}
            </div>
          </CardHeader>
          <CardContent className="pt-0">
            {liveEvents.length === 0 ? (
              <p className="text-xs text-gray-600 py-3 text-center">
                {campaign.status === 'running' ? 'In attesa di eventi dal worker…' : 'Nessun evento recente.'}
              </p>
            ) : (
              <div
                ref={liveLogRef}
                className="font-mono text-xs space-y-0.5 max-h-64 overflow-y-auto pr-1"
                style={{ scrollbarWidth: 'thin' }}
              >
                {liveEvents.map(ev => (
                  <WorkerLogLine key={ev.id} event={ev} />
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Followers table */}
      {followersError && (
        <div className="flex items-center gap-2 text-red-400 text-sm px-1">
          <XCircle className="w-4 h-4 flex-shrink-0" />
          Errore nel caricamento dei follower. Riprovare tra qualche secondo.
        </div>
      )}
      {followersData && followersData.total > 0 && (
        <Card className="bg-gray-900 border-gray-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-base text-gray-300 flex items-center gap-2 flex-wrap">
              Follower
              <Badge variant="outline" className="text-xs border-gray-700 text-gray-500">
                {followersData.total}
              </Badge>
              <div className="ml-auto flex items-center gap-2">
                <select
                  value={followerFilter}
                  onChange={e => { setFollowerFilter(e.target.value); setFollowerPage(1) }}
                  className="text-xs bg-gray-800 border border-gray-700 text-gray-400 rounded px-1.5 py-0.5 cursor-pointer"
                  title="Filtra per stato"
                >
                  <option value="all">Tutti</option>
                  <option value="pending">In coda</option>
                  <option value="bio_scraped">Bio scraped</option>
                  <option value="message_generated">Msg creato</option>
                  <option value="pending_approval">In approvazione</option>
                  <option value="sent">Inviati</option>
                  <option value="replied">Risposto</option>
                  <option value="failed">Falliti</option>
                  <option value="skipped">Skippati</option>
                </select>
                <button
                  onClick={() => { setFollowerSort(s => s === 'updated_at_desc' ? 'contact_order' : 'updated_at_desc'); setFollowerPage(1) }}
                  className={`flex items-center gap-1 text-xs px-2 py-0.5 rounded border transition-colors ${followerSort === 'contact_order' ? 'border-purple-600 text-purple-400 bg-purple-900/20' : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'}`}
                  title="Ordina per ordine di contatto"
                >
                  <ArrowUpDown className="w-3 h-3" />
                  {followerSort === 'contact_order' ? 'Ordine contatto' : 'Recenti'}
                </button>
              </div>
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <div className="divide-y divide-gray-800">
              {followersData.items.map((f: Follower) => {
                const isLoading = loadingFollowerId === f.id
                const canSkip = ['pending', 'bio_scraped', 'message_generated', 'pending_approval'].includes(f.status)
                const canRegenerate = ['bio_scraped', 'message_generated', 'failed', 'pending_approval'].includes(f.status)

                return (
                  <div key={f.id} className="px-4 py-2.5">
                    <div className="flex items-center gap-3">
                      <FollowerStatusIcon status={f.status} />
                      <div className="flex-1 min-w-0">
                        <p className="text-base text-white font-medium">@{f.username}</p>
                        {f.biography && (
                          <p className="text-xs text-gray-500 truncate">{f.biography}</p>
                        )}
                      </div>
                      <div className="flex items-center gap-1 flex-shrink-0">
                        {(f.status === 'failed' || f.status === 'skipped') && (
                          <Button size="sm" variant="ghost"
                            className="text-orange-400 hover:text-orange-300 hover:bg-orange-900/20 h-7 w-7 p-0"
                            onClick={() => handleRequeue(f.id, f.username)}
                            disabled={isLoading}
                            title="Rimetti in coda">
                            {isLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RotateCcw className="w-3.5 h-3.5" />}
                          </Button>
                        )}
                        {canRegenerate && (
                          <Button size="sm" variant="ghost"
                            className="text-blue-400 hover:text-blue-300 hover:bg-blue-900/20 h-7 w-7 p-0"
                            onClick={() => handleRegenerate(f.id, f.username)}
                            disabled={isLoading}
                            title="Rigenera messaggio">
                            {isLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                          </Button>
                        )}
                        {canSkip && (
                          <Button size="sm" variant="ghost"
                            className="text-gray-400 hover:text-gray-200 hover:bg-gray-800 h-7 w-7 p-0"
                            onClick={() => handleSkip(f.id, f.username)}
                            disabled={isLoading}
                            title="Salta follower">
                            {isLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <SkipForward className="w-3.5 h-3.5" />}
                          </Button>
                        )}
                        <Badge variant="outline" className="text-xs border-gray-700 text-gray-500">
                          {FOLLOWER_STATUS_LABEL[f.status]}
                        </Badge>
                      </div>
                    </div>
                    {f.generated_text && (
                      <div className="ml-7 mt-1.5 rounded bg-gray-800/60 px-3 py-2">
                        <p className="text-xs text-gray-300 leading-relaxed">{f.generated_text}</p>
                        {f.template_variant && (
                          <span className="text-[10px] text-gray-600 mt-1 inline-block">Template {f.template_variant.toUpperCase()}</span>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
              {/* BUG-NEW-08: paginazione follower */}
              {followersData.total > FOLLOWER_PAGE_SIZE && (
                <div className="flex items-center justify-between px-4 py-3 border-t border-gray-800">
                  <Button
                    size="sm" variant="outline"
                    className="border-gray-700 text-gray-400 h-7 text-xs"
                    onClick={() => setFollowerPage(p => Math.max(1, p - 1))}
                    disabled={followerPage === 1}
                  >
                    <ChevronLeft className="w-3.5 h-3.5 mr-1" />Precedente
                  </Button>
                  <span className="text-xs text-gray-500">
                    Pagina {followerPage} di {Math.ceil(followersData.total / FOLLOWER_PAGE_SIZE)}
                    <span className="ml-1 text-gray-600">({followersData.total} totali)</span>
                  </span>
                  <Button
                    size="sm" variant="outline"
                    className="border-gray-700 text-gray-400 h-7 text-xs"
                    onClick={() => setFollowerPage(p => p + 1)}
                    disabled={followerPage >= Math.ceil(followersData.total / FOLLOWER_PAGE_SIZE)}
                  >
                    Successiva<ChevronRight className="w-3.5 h-3.5 ml-1" />
                  </Button>
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {/* ── Add Account Dialog ──────────────────────────────────────── */}
      <Dialog open={addDialogOpen} onOpenChange={setAddDialogOpen}>
        <DialogContent className="bg-gray-900 border-gray-700 text-white max-w-md">
          <DialogHeader>
            <DialogTitle className="text-white">Assegna account alla campagna</DialogTitle>
          </DialogHeader>

          <div className="space-y-4 py-2">
            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">Account Instagram *</label>
              {availableToAssign.length === 0 ? (
                <p className="text-sm text-gray-500">Tutti gli account sono già assegnati.</p>
              ) : (
                <select
                  value={addAccountId}
                  onChange={e => setAddAccountId(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
                >
                  <option value="">Seleziona account...</option>
                  {availableToAssign.map(a => (
                    <option key={a.id} value={a.id}>
                      @{a.username} — {a.status} ({a.daily_message_count}/{a.daily_message_limit} oggi)
                    </option>
                  ))}
                </select>
              )}
            </div>

            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">Limite DM/giorno per questa campagna</label>
              <Input
                type="number"
                placeholder={`Lascia vuoto = default account`}
                value={addLimitOverride}
                onChange={e => setAddLimitOverride(e.target.value)}
                min={1}
                max={200}
                className="bg-gray-800 border-gray-700 text-white"
              />
              <p className="text-xs text-gray-500">
                Override del limite giornaliero dell&apos;account, solo per questa campagna.
              </p>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">Ruolo account</label>
              <select
                value={addRoleValue}
                onChange={e => setAddRoleValue(e.target.value as AccountRole)}
                className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
              >
                <option value="both">Scraping + DM</option>
                <option value="scraping">Solo scraping</option>
                <option value="dm">Solo DM</option>
                {campaign.scrape_mode === 'dm_threads' && (
                  <optgroup label="Inbox DM (max 1 account)">
                    <option value="inbox">Solo inbox</option>
                    <option value="inbox_scraping">Inbox + scraping</option>
                    <option value="inbox_dm">Inbox + DM</option>
                    <option value="inbox_both">Inbox + tutto</option>
                  </optgroup>
                )}
              </select>
              <p className="text-xs text-gray-500">
                Scraping = solo bio fetch. DM = solo invio messaggi. Entrambi = comportamento classico.
                {campaign.scrape_mode === 'dm_threads' && ' Inbox = legge la lista DM (un solo account per campagna; può anche scrapare/inviare).'}
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => setAddDialogOpen(false)}>
              Annulla
            </Button>
            <Button
              className="bg-purple-600 hover:bg-purple-700"
              onClick={() => handleAddAccount()}
              disabled={!addAccountId || addingAccount}
            >
              {addingAccount ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <UserPlus className="w-4 h-4 mr-2" />}
              Assegna
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Edit Campaign Daily Limit Dialog ───────────────────────── */}
      <Dialog open={editDailyLimitOpen} onOpenChange={setEditDailyLimitOpen}>
        <DialogContent className="bg-gray-900 border-gray-700 text-white max-w-sm">
          <DialogHeader>
            <DialogTitle className="text-white">Limite DM giornaliero campagna</DialogTitle>
          </DialogHeader>

          <div className="space-y-3 py-2">
            <Input
              type="number"
              placeholder="Es. 50 (lascia vuoto = nessun limite)"
              value={editDailyLimitValue}
              onChange={e => setEditDailyLimitValue(e.target.value)}
              min={1}
              max={500}
              className="bg-gray-800 border-gray-700 text-white"
              autoFocus
            />
            <p className="text-xs text-gray-500">
              Numero massimo di DM inviabili al giorno da tutti gli account assegnati sommati.
              Lascia vuoto per rimuovere il limite.
            </p>
          </div>

          <DialogFooter>
            <Button variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => setEditDailyLimitOpen(false)}>
              Annulla
            </Button>
            <Button
              className="bg-purple-600 hover:bg-purple-700"
              onClick={handleSaveCampaignDailyLimit}
              disabled={savingDailyLimit}
            >
              {savingDailyLimit ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : null}
              Salva
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── M6: Confirm Dialog ─────────────────────────────────────── */}
      <ConfirmDialog
        open={confirmDialog.open}
        onOpenChange={open => setConfirmDialog(d => ({ ...d, open }))}
        title={confirmDialog.title}
        description={confirmDialog.description}
        confirmLabel={confirmDialog.confirmLabel}
        variant={confirmDialog.variant}
        onConfirm={confirmDialog.onConfirm}
      />

      {/* ── Settings Dialog ─────────────────────────────────────────── */}
      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="bg-gray-900 border-gray-700 text-white max-w-2xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-white flex items-center gap-2">
              <Settings className="w-4 h-4 text-gray-400" />
              Impostazioni campagna
            </DialogTitle>
          </DialogHeader>

          {campaign && (() => {
            const msgEditable = ['draft', 'ready', 'paused', 'completed', 'error'].includes(campaign.status as string)
            const sf = settingsForm
            const set = (k: keyof typeof settingsForm, v: string | boolean) =>
              setSettingsForm(f => ({ ...f, [k]: v }))

            return (
              <div className="space-y-6 py-2">

                {/* ── Sezione: Generale ── */}
                <div className="space-y-3">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 border-b border-gray-800 pb-1">Generale</h3>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="col-span-2 space-y-1">
                      <label className="text-xs text-gray-400">Nome campagna</label>
                      <Input value={sf.name} onChange={e => set('name', e.target.value)}
                        className="bg-gray-800 border-gray-700 text-white text-sm h-8" />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-gray-400">Limite DM/giorno <span className="text-gray-600">(vuoto = illimitato)</span></label>
                      <Input type="number" value={sf.daily_limit} onChange={e => set('daily_limit', e.target.value)}
                        placeholder="es. 20" min={1} max={500}
                        className="bg-gray-800 border-gray-700 text-white text-sm h-8" />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-gray-400">Cap lookup/giorno per account <span className="text-gray-600">(vuoto = default 180)</span></label>
                      <Input type="number" value={sf.scrape_daily_limit} onChange={e => set('scrape_daily_limit', e.target.value)}
                        placeholder="es. 180" min={1} max={2000}
                        className="bg-gray-800 border-gray-700 text-white text-sm h-8" />
                    </div>
                  </div>
                </div>

                {/* ── Sezione: Scraping ── */}
                <div className="space-y-3">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 border-b border-gray-800 pb-1">
                    Scraping
                    <span className="ml-2 normal-case font-normal text-green-600">modificabile anche durante scraping</span>
                  </h3>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="col-span-2 space-y-1">
                      <label className="text-xs text-gray-400">Profili per sessione</label>
                      <Input type="number" value={sf.scrape_session_size} onChange={e => set('scrape_session_size', e.target.value)}
                        min={10} max={5000}
                        className="bg-gray-800 border-gray-700 text-white text-sm h-8" />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-gray-400">Pausa sessione min (min)</label>
                      <Input type="number" value={sf.scrape_break_minutes_min} onChange={e => set('scrape_break_minutes_min', e.target.value)}
                        min={5} max={240}
                        className="bg-gray-800 border-gray-700 text-white text-sm h-8" />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-gray-400">Pausa sessione max (min)</label>
                      <Input type="number" value={sf.scrape_break_minutes_max} onChange={e => set('scrape_break_minutes_max', e.target.value)}
                        min={5} max={240}
                        className="bg-gray-800 border-gray-700 text-white text-sm h-8" />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-gray-400">Delay fetch bio min (sec)</label>
                      <Input type="number" step="0.5" value={sf.bio_fetch_delay_min} onChange={e => set('bio_fetch_delay_min', e.target.value)}
                        min={1} max={60}
                        className="bg-gray-800 border-gray-700 text-white text-sm h-8" />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-gray-400">Delay fetch bio max (sec)</label>
                      <Input type="number" step="0.5" value={sf.bio_fetch_delay_max} onChange={e => set('bio_fetch_delay_max', e.target.value)}
                        min={1} max={120}
                        className="bg-gray-800 border-gray-700 text-white text-sm h-8" />
                    </div>
                    <p className="col-span-2 text-xs text-amber-500/90 bg-amber-950/30 border border-amber-800/40 rounded px-2 py-1.5">
                      ⚠️ <strong>I delay valgono per OGNI lead, condivisi tra tutti gli account scraping.</strong>{' '}
                      Con N account ogni account aspetta circa N× questo valore tra i suoi lead. Con 2 account, per ~6–10s
                      effettivi per account imposta <strong>3–5s</strong>.
                    </p>
                  </div>
                </div>

                {/* ── Sezione: Messaggi ── */}
                <div className="space-y-3">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 border-b border-gray-800 pb-1">
                    Messaggi
                    {!msgEditable && <span className="ml-2 normal-case font-normal text-amber-600">metti in pausa per modificare</span>}
                  </h3>
                  <div className="flex items-center justify-between rounded-md border border-gray-700 bg-gray-800/40 px-3 py-2">
                    <div>
                      <p className="text-sm text-gray-300 font-medium">Invia messaggi</p>
                      <p className="text-xs text-gray-500 mt-0.5">
                        {sf.messaging_enabled ? 'Campagna genera e invia DM.' : 'Solo raccolta lead, nessun DM.'}
                      </p>
                    </div>
                    <button type="button" disabled={!msgEditable}
                      onClick={() => set('messaging_enabled', !sf.messaging_enabled)}
                      className={`relative inline-flex h-5 w-9 flex-shrink-0 rounded-full transition-colors ${sf.messaging_enabled ? 'bg-purple-600' : 'bg-gray-600'} ${!msgEditable ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}>
                      <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform mt-0.5 ${sf.messaging_enabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
                    </button>
                  </div>
                  {sf.messaging_enabled && (
                    <>
                      <div className="space-y-1">
                        <label className="text-xs text-gray-400">Template base *</label>
                        <textarea value={sf.base_message_template}
                          onChange={e => set('base_message_template', e.target.value)}
                          disabled={!msgEditable} rows={5}
                          className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 resize-none focus:outline-none focus:ring-1 focus:ring-purple-500 disabled:opacity-40 disabled:cursor-not-allowed"
                          placeholder="Template messaggio..." />
                      </div>
                      <div className="space-y-1">
                        <label className="text-xs text-gray-400">Contesto AI <span className="text-gray-600">(opzionale)</span></label>
                        <textarea value={sf.ai_prompt_context}
                          onChange={e => set('ai_prompt_context', e.target.value)}
                          disabled={!msgEditable} rows={2}
                          className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 resize-none focus:outline-none focus:ring-1 focus:ring-purple-500 disabled:opacity-40 disabled:cursor-not-allowed"
                          placeholder="Contesto opzionale..." />
                      </div>
                      <div className="flex items-center justify-between rounded-md border border-gray-700 bg-gray-800/40 px-3 py-2">
                        <div>
                          <p className="text-sm text-gray-300 font-medium">Approvazione messaggi</p>
                          <p className="text-xs text-gray-500 mt-0.5">
                            {sf.require_approval
                              ? `Revisione campione: ${sf.approval_sample_size} messaggi prima di iniziare.`
                              : 'Auto-invio senza revisione.'}
                          </p>
                        </div>
                        <button type="button" disabled={!msgEditable}
                          onClick={() => set('require_approval', !sf.require_approval)}
                          className={`relative inline-flex h-5 w-9 flex-shrink-0 rounded-full transition-colors ${sf.require_approval ? 'bg-purple-600' : 'bg-gray-600'} ${!msgEditable ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}>
                          <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform mt-0.5 ${sf.require_approval ? 'translate-x-4' : 'translate-x-0.5'}`} />
                        </button>
                      </div>
                      {sf.require_approval && (
                        <div className="space-y-1">
                          <label className="text-xs text-gray-400">Dimensione campione approvazione</label>
                          <Input type="number" value={sf.approval_sample_size}
                            onChange={e => set('approval_sample_size', e.target.value)}
                            disabled={!msgEditable} min={1} max={50}
                            className="bg-gray-800 border-gray-700 text-white text-sm h-8 w-24 disabled:opacity-40" />
                        </div>
                      )}
                    </>
                  )}
                </div>

                {/* ── Info sola lettura ── */}
                <div className="space-y-2 rounded-md border border-gray-800 bg-gray-800/20 px-3 py-2">
                  <p className="text-xs text-gray-600 font-medium uppercase tracking-wider">Impostato alla creazione (non modificabile)</p>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                    <span className="text-gray-500">Sorgente</span>
                    <span className="text-gray-400">{campaign.source_type === 'import' ? 'Lista importata' : `@${campaign.target_username}`}</span>
                    {campaign.source_type === 'scrape' && <>
                      <span className="text-gray-500">Modalità</span>
                      <span className="text-gray-400">
                        {campaign.scrape_mode === 'following' ? 'Following' : campaign.scrape_mode === 'dm_threads' ? 'DM inbox' : 'Follower'}
                      </span>
                    </>}
                    {campaign.scrape_mode === 'dm_threads' && <>
                      <span className="text-gray-500">Engine inbox</span>
                      <span className="text-gray-400">{campaign.inbox_engine ?? 'browser'}</span>
                    </>}
                  </div>
                </div>

              </div>
            )
          })()}

          <DialogFooter>
            <Button variant="outline" className="border-gray-700 text-gray-300" onClick={() => setSettingsOpen(false)}>
              Annulla
            </Button>
            <Button className="bg-purple-600 hover:bg-purple-700" onClick={saveSettings} disabled={savingSettings}>
              {savingSettings ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : null}
              Salva
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── M3: Edit Template Dialog ────────────────────────────────── */}
      <Dialog open={editTemplateOpen} onOpenChange={setEditTemplateOpen}>
        <DialogContent className="bg-gray-900 border-gray-700 text-white max-w-lg">
          <DialogHeader>
            <DialogTitle className="text-white">Modifica template messaggio</DialogTitle>
          </DialogHeader>

          <div className="space-y-4 py-2">
            {/* messaging_enabled si cambia solo a campagna ferma (gate backend:
                draft/ready/paused, + completed). Template e campi AI invece sono
                editabili in ogni stato — il toggle qui sotto resta quindi bloccato
                a campagna in corso, il resto del dialog no. */}
            {(() => {
              const messagingToggleLocked = !campaign || !['draft', 'ready', 'paused', 'completed'].includes(campaign.status as string)
              return (
            <div className="flex items-center justify-between rounded-md border border-gray-700 bg-gray-800/40 px-3 py-2">
              <div>
                <p className="text-sm text-gray-300 font-medium">Invia messaggi</p>
                <p className="text-xs text-gray-500 mt-0.5">
                  {messagingToggleLocked
                    ? 'Si cambia solo a campagna ferma (bozza/pronta/pausa/completata).'
                    : editMessagingEnabled
                      ? 'La campagna potra generare e inviare DM.'
                      : 'Modalita solo raccolta lead: nessun DM verra inviato.'}
                </p>
              </div>
              <button
                type="button"
                disabled={messagingToggleLocked}
                onClick={() => { if (!messagingToggleLocked) setEditMessagingEnabled(v => !v) }}
                className={`relative inline-flex h-5 w-9 flex-shrink-0 rounded-full transition-colors ${messagingToggleLocked ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'} ${editMessagingEnabled ? 'bg-purple-600' : 'bg-gray-600'}`}
              >
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform mt-0.5 ${editMessagingEnabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
              </button>
            </div>
              )
            })()}
            {editMessagingEnabled && (<>
            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">Template base *</label>
              <textarea
                value={editTemplateValue}
                onChange={e => setEditTemplateValue(e.target.value)}
                rows={5}
                className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 resize-none focus:outline-none focus:ring-1 focus:ring-purple-500"
                placeholder="Template messaggio..."
              />
              <p className="text-xs text-gray-600">
                {'{nome}'} = nome del destinatario · {'{Ciao|Hey|Salve}'} = il bot sceglie una variante a caso per ogni DM
              </p>
              <button type="button" className="text-xs text-blue-400 hover:text-blue-300"
                onClick={() => setEditPreviews([1, 2, 3].map(() => renderPreview(editTemplateValue)))}>
                ⚡ Anteprima varianti
              </button>
              {editPreviews.length > 0 && (
                <div className="space-y-1">
                  {editPreviews.map((p, i) => (
                    <p key={i} className="text-xs text-gray-400 bg-gray-800 rounded p-2 whitespace-pre-wrap">{p}</p>
                  ))}
                </div>
              )}
            </div>
            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">
                Template B
                <span className="ml-1 text-gray-600 font-normal">(A/B test — opzionale)</span>
              </label>
              <textarea
                value={editTemplateBValue}
                onChange={e => setEditTemplateBValue(e.target.value)}
                rows={4}
                className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 resize-none focus:outline-none focus:ring-1 focus:ring-purple-500"
                placeholder="Lascia vuoto per disattivare A/B testing..."
              />
              <p className="text-xs text-gray-600">Se compilato, il bot sceglie a caso tra i template attivi per ogni DM.</p>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">
                Template C
                <span className="ml-1 text-gray-600 font-normal">(opzionale)</span>
              </label>
              <textarea
                value={editTemplateCValue}
                onChange={e => setEditTemplateCValue(e.target.value)}
                rows={4}
                className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 resize-none focus:outline-none focus:ring-1 focus:ring-purple-500"
                placeholder="Lascia vuoto per disattivare la terza variante..."
              />
              <p className="text-xs text-gray-600">I follower riceveranno a caso uno dei template attivi (A/B/C).</p>
            </div>

            <div className="flex items-center justify-between rounded-md border border-gray-700 bg-gray-800/40 px-3 py-2">
              <div>
                <p className="text-sm text-gray-300 font-medium">Personalizza con AI</p>
                <p className="text-xs text-gray-500 mt-0.5">
                  OFF (default): il template parte così com&apos;è, con le varianti {'{a|b}'} — zero quota AI.
                  ON: l&apos;AI riscrive il messaggio sulla bio del destinatario.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setEditAiEnabled(v => !v)}
                className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full transition-colors ${editAiEnabled ? 'bg-purple-600' : 'bg-gray-600'}`}
              >
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform mt-0.5 ${editAiEnabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
              </button>
            </div>
            {editAiEnabled && (
              <>
                <div className="space-y-1.5">
                  <label className="text-sm text-gray-300 font-medium">Contesto AI</label>
                  <textarea
                    value={editContextValue}
                    onChange={e => setEditContextValue(e.target.value)}
                    rows={3}
                    className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 resize-none focus:outline-none focus:ring-1 focus:ring-purple-500"
                    placeholder="Contesto opzionale per l'AI..."
                  />
                </div>
                <div className="space-y-1.5">
                  <label className="text-sm text-gray-300 font-medium">Istruzioni AI</label>
                  <textarea
                    value={editAiSystemPrompt}
                    onChange={e => setEditAiSystemPrompt(e.target.value)}
                    rows={3}
                    className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 resize-none focus:outline-none focus:ring-1 focus:ring-purple-500"
                    placeholder="Sovrascrive le istruzioni globali solo per questa campagna. Es: tono informale, max 3 frasi, niente emoji."
                  />
                </div>
              </>
            )}
            </>)}
          </div>

          <DialogFooter>
            <Button variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => setEditTemplateOpen(false)}>
              Annulla
            </Button>
            <Button
              className="bg-purple-600 hover:bg-purple-700"
              onClick={handleSaveTemplate}
              disabled={(editMessagingEnabled && editTemplateValue.trim().length < 10) || savingTemplate}
            >
              {savingTemplate ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : null}
              Salva
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

const EVENT_COLORS: Record<string, string> = {
  dm_sent: 'text-green-400',
  worker_queued: 'text-cyan-400',
  worker_started: 'text-blue-400',
  worker_stopped: 'text-yellow-400',
  account_banned: 'text-red-400',
  account_challenge: 'text-orange-400',
  dm_failed: 'text-red-400',
  cooldown_started: 'text-yellow-400',
  daily_limit_reached: 'text-yellow-400',
  dm_restricted: 'text-gray-500',
  session_break: 'text-gray-500',
  no_followers_left: 'text-blue-400',
  campaign_completed: 'text-purple-400',
  pregen_started: 'text-blue-400',
  pregen_progress: 'text-blue-400',
  pregen_completed: 'text-green-400',
  pregen_error: 'text-red-400',
  approval_sampling: 'text-purple-400',
  worker_error: 'text-red-400',
}

function WorkerLogLine({ event }: { event: WorkerEvent }) {
  const color = event.level === 'error' ? 'text-red-400' : event.level === 'warn' ? 'text-yellow-400' : (EVENT_COLORS[event.action] ?? 'text-gray-400')
  const time = event.ts.slice(11, 19) // "HH:MM:SS"

  return (
    <div className="flex items-start gap-2 py-0.5 hover:bg-gray-800/30 px-1 rounded">
      <span className="text-gray-600 flex-shrink-0 select-none">{time}</span>
      <span className={`flex-1 leading-relaxed ${color}`}>{event.detail}</span>
    </div>
  )
}

function FollowerStatusIcon({ status }: { status: FollowerStatus }) {
  if (status === 'sent') return <CheckCircle className="w-4 h-4 text-green-400 flex-shrink-0" />
  if (status === 'replied') return <CheckCircle className="w-4 h-4 text-blue-400 flex-shrink-0" />
  if (status === 'failed') return <XCircle className="w-4 h-4 text-red-500 flex-shrink-0" />
  if (status === 'skipped') return <SkipForward className="w-4 h-4 text-yellow-600 flex-shrink-0" />
  if (status === 'message_generated') return <MessageSquare className="w-4 h-4 text-cyan-500 flex-shrink-0" />
  if (status === 'pending_approval') return <MessageSquare className="w-4 h-4 text-purple-400 flex-shrink-0" />
  return <Clock className="w-4 h-4 text-gray-500 flex-shrink-0" />
}
