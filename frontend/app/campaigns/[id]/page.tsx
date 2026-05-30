'use client'

import { use, useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import useSWR from 'swr'
import Link from 'next/link'
import { api } from '@/lib/api'
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
  ThumbsUp, ThumbsDown, MessageSquare, Activity, ArrowUpDown, MinusCircle, Filter
} from 'lucide-react'
import type { Campaign, Follower, FollowerStatus, CampaignAccount, Account, AccountStatus, ABStats, ApprovalQueueItem, ApprovalQueue, WorkerEvent, AccountRole, ImportStatusResponse } from '@/lib/types'

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
  const [remaining, setRemaining] = useState('')

  useEffect(() => {
    const tick = () => {
      const diff = Math.max(0, new Date(breakUntil).getTime() - Date.now())
      const m = Math.floor(diff / 60000)
      const s = Math.floor((diff % 60000) / 1000)
      setRemaining(diff === 0 ? 'Ripresa...' : `${m}:${s.toString().padStart(2, '0')}`)
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [breakUntil])

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

  // M3: edit template (draft/ready only)
  const [editTemplateOpen, setEditTemplateOpen] = useState(false)
  const [editTemplateValue, setEditTemplateValue] = useState('')
  const [editTemplateBValue, setEditTemplateBValue] = useState('')
  const [editContextValue, setEditContextValue] = useState('')
  const [savingTemplate, setSavingTemplate] = useState(false)

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
    setSavingTemplate(true)
    try {
      await api.campaigns.update(id, {
        base_message_template: editTemplateValue,
        ai_prompt_context: editContextValue || undefined,
        message_template_b: editTemplateBValue.trim() || null,
      })
      toast.success('Template aggiornato')
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
              campaign.status === 'scraping_break' ? 'bg-amber-700/80 text-amber-100 border-0' : ''
            }`}>
              {campaign.status === 'scraping_and_running' ? '⚡ Scraping + DM' :
               campaign.source_type === 'import' && campaign.status === 'scraping' ? 'Risoluzione profili' :
               campaign.source_type === 'import' && campaign.status === 'scraping_break' ? '⏸ Pausa risoluzione' :
               campaign.status === 'scraping_break' ? '⏸ Pausa sessione' :
               campaign.status}
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
                <p className="text-gray-400 text-base">@{campaign.target_username}</p>
                <Badge variant="outline" className="text-xs text-gray-500 border-gray-700 py-0">
                  {campaign.scrape_mode === 'following' ? 'following' : 'follower'}
                </Badge>
              </>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-2 flex-shrink-0">
          {campaign.status === 'draft' && (
            <Button size="sm" variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => action(() => api.campaigns.startScrape(id))} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : (campaign.source_type === 'import' ? 'Avvia risoluzione' : 'Avvia scraping')}
            </Button>
          )}
          {campaign.status === 'ready' && (
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
          {(campaign.status === 'paused' || (campaign.status === 'completed' && campaign.messages_pending > 0)) && (
            <Button size="sm" className="bg-green-600 hover:bg-green-700"
              onClick={() => action(() => api.campaigns.resume(id))} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Play className="w-4 h-4 mr-1" />{campaign.scrape_completed_at ? 'Riprendi' : 'Riprendi scraping'}</>}
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
          {campaign.source_type !== 'import' && campaign.status === 'scraping' && !campaign.scrape_completed_at && (campaignAccounts?.some(ca => ca.is_active && (ca.role === 'dm' || ca.role === 'both')) ?? false) && (
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
          {(campaign.status === 'ready' || campaign.status === 'paused') && (
            <Button size="sm" variant="outline" className="border-blue-700 text-blue-400 hover:bg-blue-900/20"
              onClick={handlePreGenerate} disabled={pregenLoading || loadingAction}
              title="Pre-genera messaggi AI per tutti i follower prima di avviare">
              {pregenLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <><RefreshCw className="w-4 h-4 mr-1" />Pre-genera</>}
            </Button>
          )}
          {(campaign.status === 'error' || campaign.status === 'completed' || campaign.status === 'paused' || campaign.status === 'scraping' || campaign.status === 'scraping_and_running' || campaign.status === 'scraping_break') && (
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
          {(campaign.status === 'draft' || campaign.status === 'error' || campaign.status === 'completed' || campaign.status === 'scraping') && (
            <Button size="sm" variant="outline" className="border-red-800 text-red-400 hover:bg-red-900/20"
              onClick={handleDelete} disabled={loadingAction}>
              {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Trash2 className="w-4 h-4 mr-1" />Elimina</>}
            </Button>
          )}
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
            <div className="grid grid-cols-6 gap-4 pt-1">
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
                <p className="text-sm text-gray-500 mt-1">In coda</p>
              </div>
              <div className="text-center">
                <div className="flex items-center justify-center gap-1 text-gray-400 font-semibold text-lg">
                  <Users className="w-4 h-4" />{campaign.total_followers}
                </div>
                <p className="text-sm text-gray-500 mt-1">Scrappati</p>
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
              {campaign.message_template_b && (
                <Badge variant="outline" className="ml-2 text-xs border-purple-700 text-purple-400">A/B</Badge>
              )}
            </CardTitle>
            {(campaign.status === 'draft' || campaign.status === 'ready') && (
              <button
                className="text-gray-600 hover:text-gray-300 flex items-center gap-1 text-xs"
                onClick={() => {
                  setEditTemplateValue(campaign.base_message_template)
                  setEditTemplateBValue(campaign.message_template_b ?? '')
                  setEditContextValue(campaign.ai_prompt_context ?? '')
                  setEditTemplateOpen(true)
                }}
              >
                <Pencil className="w-3 h-3" />Modifica
              </button>
            )}
          </div>
        </CardHeader>
        <CardContent>
          {campaign.message_template_b && (
            <p className="text-xs text-gray-500 mb-1">Template A</p>
          )}
          <p className="text-sm text-gray-300 whitespace-pre-wrap">{campaign.base_message_template}</p>
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
      {(campaign.status === 'running' || campaign.status === 'scraping' || campaign.status === 'scraping_and_running' || campaign.status === 'paused' || liveEvents.length > 0) && (
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
              </select>
              <p className="text-xs text-gray-500">
                Scraping = solo bio fetch. DM = solo invio messaggi. Entrambi = comportamento classico.
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

      {/* ── M3: Edit Template Dialog ────────────────────────────────── */}
      <Dialog open={editTemplateOpen} onOpenChange={setEditTemplateOpen}>
        <DialogContent className="bg-gray-900 border-gray-700 text-white max-w-lg">
          <DialogHeader>
            <DialogTitle className="text-white">Modifica template messaggio</DialogTitle>
          </DialogHeader>

          <div className="space-y-4 py-2">
            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">Template base *</label>
              <textarea
                value={editTemplateValue}
                onChange={e => setEditTemplateValue(e.target.value)}
                rows={5}
                className="w-full bg-gray-800 border border-gray-700 text-white text-sm rounded-md px-3 py-2 resize-none focus:outline-none focus:ring-1 focus:ring-purple-500"
                placeholder="Template messaggio..."
              />
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
              <p className="text-xs text-gray-600">50% dei follower riceveranno B, 50% A. Lascia vuoto per disattivare.</p>
            </div>
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
          </div>

          <DialogFooter>
            <Button variant="outline" className="border-gray-700 text-gray-300"
              onClick={() => setEditTemplateOpen(false)}>
              Annulla
            </Button>
            <Button
              className="bg-purple-600 hover:bg-purple-700"
              onClick={handleSaveTemplate}
              disabled={!editTemplateValue.trim() || savingTemplate}
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
