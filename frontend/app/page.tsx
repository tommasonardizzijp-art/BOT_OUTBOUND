'use client'

import useSWR from 'swr'
import Link from 'next/link'
import { useState } from 'react'
import { api } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { Separator } from '@/components/ui/separator'
import {
  Users, Megaphone, MessageSquare, TrendingUp,
  CheckCircle, XCircle, Clock, AlertTriangle,
  Database, Server, Brain, Activity
} from 'lucide-react'
import type { DashboardStats, ActivityLog, HealthStatus, Account, Campaign, TimelineResponse, AccountStatus } from '@/lib/types'
import { formatDistanceToNow } from '@/lib/dateUtils'
import { Skeleton } from '@/components/ui/skeleton'

type TimelinePeriod = '24h' | '7d' | '30d' | '6m'

const PERIOD_LABELS: Record<TimelinePeriod, string> = {
  '24h': '24 ore',
  '7d': '7 giorni',
  '30d': '30 giorni',
  '6m': '6 mesi',
}

export default function DashboardPage() {
  const [timelinePeriod, setTimelinePeriod] = useState<TimelinePeriod>('24h')

  // BUG-NEW-11: include error for backend-offline detection
  const { data: stats, error: statsError } = useSWR<DashboardStats>('dashboard-stats', () => api.dashboard.stats(), { refreshInterval: 8000 })
  const { data: activity } = useSWR('dashboard-activity', () => api.dashboard.activity(50), { refreshInterval: 10000 })
  const { data: health } = useSWR<HealthStatus>('health', api.health.check, { refreshInterval: 15000 })
  const { data: timeline } = useSWR<TimelineResponse>(
    ['dashboard-timeline', timelinePeriod],
    () => api.dashboard.timeline(timelinePeriod),
    { refreshInterval: 30000 }
  )
  const { data: accounts } = useSWR('accounts', api.accounts.list, { refreshInterval: 10000 })
  const { data: campaigns } = useSWR('campaigns', api.campaigns.list, { refreshInterval: 10000 })

  const activeCampaigns = campaigns?.filter(c => ['running', 'paused', 'scraping'].includes(c.status)) ?? []

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Dashboard</h1>
        <p className="text-gray-400 text-base mt-1">Panoramica dell&apos;attivit&agrave; di outreach</p>
      </div>

      {/* BUG-NEW-11: backend error banner */}
      {statsError && (
        <div className="flex items-center gap-2 text-red-400 text-sm rounded-lg border border-red-800/50 bg-red-900/10 px-4 py-3">
          <AlertTriangle className="w-4 h-4 flex-shrink-0" />
          Backend non raggiungibile. Avviare il server e ricaricare la pagina.
        </div>
      )}

      {/* Stats grid */}
      {!stats && !statsError ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {[1, 2, 3, 4].map(i => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-2">
              <Skeleton className="h-3 w-24" />
              <Skeleton className="h-8 w-12" />
              <Skeleton className="h-3 w-32" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            title="Account Attivi"
            value={stats?.active_accounts ?? '—'}
            sub={`${stats?.accounts_in_cooldown ?? 0} in cooldown · ${stats?.accounts_banned ?? 0} bannati`}
            icon={<Users className="w-5 h-5 text-blue-400" />}
          />
          <StatCard
            title="Campagne Running"
            value={stats?.running_campaigns ?? '—'}
            sub={`${stats?.total_campaigns ?? 0} totali`}
            icon={<Megaphone className="w-5 h-5 text-purple-400" />}
          />
          <StatCard
            title="DM Inviati Oggi"
            value={stats?.messages_sent_today ?? '—'}
            sub={`${stats?.messages_sent_total ?? 0} totali`}
            icon={<MessageSquare className="w-5 h-5 text-green-400" />}
          />
          <StatCard
            title="Tasso Successo"
            value={stats ? `${stats.success_rate}%` : '—'}
            sub={`${stats?.messages_failed_total ?? 0} falliti`}
            icon={<TrendingUp className="w-5 h-5 text-yellow-400" />}
          />
        </div>
      )}

      {/* System health strip */}
      <Card className="bg-gray-900 border-gray-800">
        <CardContent className="py-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-6">
              <div className="flex items-center gap-2 text-sm text-gray-400">
                <Activity className="w-4 h-4" />
                <span className="font-medium text-gray-300">Stato Sistema</span>
              </div>
              <HealthDot label="Database" status={health?.database} icon={<Database className="w-3.5 h-3.5" />} />
              <HealthDot label="Redis" status={health?.redis} icon={<Server className="w-3.5 h-3.5" />} />
              <HealthDot label="Ollama" status={health?.ollama} icon={<Brain className="w-3.5 h-3.5" />} />
            </div>
            {health && (
              <Badge className={`text-xs ${health.status === 'ok' ? 'bg-green-700' : 'bg-yellow-700'} text-white`}>
                {health.status === 'ok' ? 'Tutto OK' : 'Degradato'}
              </Badge>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Message timeline chart */}
      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-lg text-gray-100">Messaggi inviati</CardTitle>
            <div className="flex gap-1">
              {(Object.keys(PERIOD_LABELS) as TimelinePeriod[]).map(p => (
                <Button
                  key={p}
                  size="sm"
                  variant={timelinePeriod === p ? 'default' : 'ghost'}
                  className={`h-6 px-2 text-xs ${timelinePeriod === p ? 'bg-purple-600 text-white' : 'text-gray-500 hover:text-gray-300'}`}
                  onClick={() => setTimelinePeriod(p)}
                >
                  {PERIOD_LABELS[p]}
                </Button>
              ))}
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <TimelineChart data={timeline?.data} period={timelinePeriod} />
        </CardContent>
      </Card>

      {/* Two-column layout: Account overview + Active campaigns */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Account health overview */}
        <Card className="bg-gray-900 border-gray-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-lg text-gray-100 flex items-center gap-2">
              Panoramica Account
              <Badge variant="outline" className="text-xs border-gray-700 text-gray-500">
                {accounts?.length ?? 0}
              </Badge>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {accounts && accounts.length > 0 ? (
              <AccountHealthOverview accounts={accounts} />
            ) : (
              <p className="text-gray-500 text-sm py-4 text-center">Nessun account configurato</p>
            )}
          </CardContent>
        </Card>

        {/* Active campaigns */}
        <Card className="bg-gray-900 border-gray-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-lg text-gray-100 flex items-center gap-2">
              Campagne Attive
              <Badge variant="outline" className="text-xs border-gray-700 text-gray-500">
                {activeCampaigns.length}
              </Badge>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {activeCampaigns.length > 0 ? (
              <div className="space-y-3">
                {activeCampaigns.map(c => {
                  const total = c.total_followers || (c.messages_sent + c.messages_failed + c.messages_pending) || 1
                  const progress = Math.min(100, Math.round((c.messages_sent / total) * 100))
                  return (
                    <div key={c.id} className="space-y-1.5">
                      <div className="flex items-center justify-between">
                        <Link href={`/campaigns/${c.id}`} className="text-base font-medium text-white hover:text-purple-300 truncate">
                          {c.name}
                        </Link>
                        <Badge className={`text-xs ${CAMPAIGN_STATUS_COLORS[c.status]} text-white`}>
                          {CAMPAIGN_STATUS_LABELS[c.status]}
                        </Badge>
                      </div>
                      <Progress value={progress} className="h-3" />
                      <div className="flex justify-between text-xs text-gray-500">
                        <span>{c.messages_sent} inviati · {c.messages_failed} falliti</span>
                        <span>{progress}%</span>
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="text-gray-500 text-sm py-4 text-center">Nessuna campagna attiva</p>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Activity feed */}
      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-3">
          <CardTitle className="text-lg text-gray-100">Attivit&agrave; Recente</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1">
          {activity?.items.length === 0 && (
            <p className="text-gray-500 text-sm py-4 text-center">Nessuna attivit&agrave; ancora</p>
          )}
          {activity?.items.map((log: ActivityLog) => (
            <div key={log.id} className="flex items-center gap-3 py-2">
              <ActionIcon action={log.action} />
              <div className="flex-1 min-w-0">
                <span className="text-sm text-gray-300">{formatAction(log.action)}</span>
                {log.details && (
                  <span className="text-xs text-gray-500 ml-2">{formatDetails(log.details)}</span>
                )}
              </div>
              <span className="text-xs text-gray-600 flex-shrink-0">
                {formatDistanceToNow(log.created_at)}
              </span>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}

/* ---------- Sub-components ---------- */

function StatCard({ title, value, sub, icon }: { title: string; value: number | string; sub: string; icon: React.ReactNode }) {
  return (
    <Card className="bg-gray-900 border-gray-800">
      <CardContent className="pt-5">
        <div className="flex items-start justify-between">
          <div>
            <p className="text-sm text-gray-400 uppercase tracking-wide">{title}</p>
            <p className="text-3xl font-bold text-white mt-1">{value}</p>
            <p className="text-sm text-gray-500 mt-1">{sub}</p>
          </div>
          {icon}
        </div>
      </CardContent>
    </Card>
  )
}

function HealthDot({ label, status, icon }: { label: string; status?: string; icon: React.ReactNode }) {
  const isOk = status === 'ok'
  return (
    <div className="flex items-center gap-1.5">
      <span className={`w-2 h-2 rounded-full ${!status ? 'bg-gray-600' : isOk ? 'bg-green-400' : 'bg-red-400'}`} />
      <span className={`flex items-center gap-1 text-xs ${isOk ? 'text-gray-400' : 'text-red-400'}`}>
        {icon} {label}
      </span>
    </div>
  )
}

function TimelineChart({ data, period = '24h' }: { data?: { hour: string; count: number }[]; period?: string }) {
  if (!data || data.length === 0) {
    return <p className="text-gray-500 text-sm py-6 text-center">Nessun messaggio nel periodo selezionato</p>
  }

  // Format label based on period
  const formatLabel = (key: string): string => {
    if (period === '24h') {
      // key = "2026-04-17T14:00" → show local hour "14"
      const d = new Date(key + ':00Z')
      return String(d.getHours()).padStart(2, '0')
    }
    if (period === '7d') {
      // key = "2026-04-17" → "Apr 17" or weekday
      const d = new Date(key + 'T00:00:00Z')
      return d.toLocaleDateString('it-IT', { weekday: 'short', timeZone: 'UTC' })
    }
    // 30d and 6m: key = "2026-04-17" → "17/04" or "W15"
    if (period === '30d') {
      const parts = key.split('-')
      return `${parts[2]}/${parts[1]}`
    }
    // 6m: key = week start "2026-04-14"
    const parts = key.split('-')
    return `${parts[2]}/${parts[1]}`
  }

  const maxCount = Math.max(...data.map(d => d.count), 1)
  const total = data.reduce((s, d) => s + d.count, 0)

  // For 24h, skip empty-only entries at ends (keep middle zeros for shape)
  const points = data

  // Decide label density: show every N-th label based on count
  const n = points.length
  const labelEvery = n <= 12 ? 1 : n <= 30 ? 2 : n <= 60 ? 5 : 7

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm font-medium text-gray-200">{total.toLocaleString()} messaggi totali nel periodo</span>
      </div>
      <div className="overflow-x-auto">
        <div className="flex items-end gap-1" style={{ minWidth: Math.max(400, n * 14) + 'px', height: '100px' }}>
          {points.map((p, i) => (
            <div key={i} className="flex-1 flex flex-col items-center justify-end h-full group relative">
              {p.count > 0 && (
                <span className="text-[10px] text-gray-500 mb-0.5 opacity-0 group-hover:opacity-100 transition-opacity absolute -top-5">
                  {p.count}
                </span>
              )}
              <div
                className="w-full rounded-t bg-purple-500 min-w-[8px] transition-all cursor-default"
                style={{ height: p.count > 0 ? `${(p.count / maxCount) * 100}%` : '2px', opacity: p.count > 0 ? 1 : 0.12 }}
                title={`${p.hour}: ${p.count} messaggi`}
              />
            </div>
          ))}
        </div>
        <div className="flex gap-1 mt-1" style={{ minWidth: Math.max(400, n * 14) + 'px' }}>
          {points.map((p, i) => (
            <div key={i} className="flex-1 text-center">
              {i % labelEvery === 0 && (
                <span className="text-[10px] text-gray-600">{formatLabel(p.hour)}</span>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

const ACCOUNT_STATUS_CONFIG: Record<AccountStatus, { color: string; label: string }> = {
  active: { color: 'bg-green-500', label: 'Attivi' },
  warming_up: { color: 'bg-blue-500', label: 'Warm-up' },
  cooldown: { color: 'bg-yellow-500', label: 'Cooldown' },
  banned: { color: 'bg-red-500', label: 'Bannati' },
  challenge_required: { color: 'bg-orange-500', label: 'Challenge' },
  disabled: { color: 'bg-gray-600', label: 'Disabilitati' },
}

function AccountHealthOverview({ accounts }: { accounts: Account[] }) {
  const counts: Partial<Record<AccountStatus, number>> = {}
  for (const acc of accounts) {
    counts[acc.status] = (counts[acc.status] ?? 0) + 1
  }

  const total = accounts.length
  const entries = Object.entries(counts) as [AccountStatus, number][]

  return (
    <div className="space-y-3">
      {/* Stacked bar */}
      <div className="flex rounded-full overflow-hidden h-3">
        {entries.map(([status, count]) => (
          <div
            key={status}
            className={`${ACCOUNT_STATUS_CONFIG[status].color} transition-all`}
            style={{ width: `${(count / total) * 100}%` }}
          />
        ))}
      </div>
      {/* Legend */}
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {entries.map(([status, count]) => (
          <div key={status} className="flex items-center gap-1.5">
            <span className={`w-2.5 h-2.5 rounded-full ${ACCOUNT_STATUS_CONFIG[status].color}`} />
            <span className="text-xs text-gray-400">
              {count} {ACCOUNT_STATUS_CONFIG[status].label}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

const CAMPAIGN_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-gray-600',
  scraping: 'bg-blue-600',
  ready: 'bg-cyan-600',
  running: 'bg-green-600',
  paused: 'bg-yellow-600',
  completed: 'bg-purple-600',
  error: 'bg-red-600',
}

const CAMPAIGN_STATUS_LABELS: Record<string, string> = {
  draft: 'Bozza',
  scraping: 'Scraping...',
  ready: 'Pronta',
  running: 'In corso',
  paused: 'In pausa',
  completed: 'Completata',
  error: 'Errore',
}

function ActionIcon({ action }: { action: string }) {
  if (action.includes('sent') || action.includes('completed')) return <CheckCircle className="w-4 h-4 text-green-400 flex-shrink-0" />
  if (action.includes('failed') || action.includes('banned') || action.includes('error')) return <XCircle className="w-4 h-4 text-red-400 flex-shrink-0" />
  if (action.includes('cooldown') || action.includes('challenge')) return <AlertTriangle className="w-4 h-4 text-yellow-400 flex-shrink-0" />
  return <Clock className="w-4 h-4 text-gray-400 flex-shrink-0" />
}

const ACTION_LABELS: Record<string, string> = {
  dm_sent: 'DM inviato',
  dm_failed: 'DM fallito',
  account_created: 'Account aggiunto',
  account_banned: 'Account BANNATO',
  cooldown_start: 'Cooldown iniziato',
  cooldown_end: 'Cooldown terminato',
  challenge_code_submitted: 'Codice challenge inviato',
  campaign_created: 'Campagna creata',
  campaign_started: 'Campagna avviata',
  campaign_paused: 'Campagna in pausa',
  campaign_resumed: 'Campagna ripresa',
  campaign_stopped: 'Campagna fermata',
  campaign_completed: 'Campagna completata',
  scrape_started: 'Scraping follower avviato',
  scrape_completed: 'Scraping follower completato',
}

function formatAction(action: string): string {
  return ACTION_LABELS[action] || action.replace(/_/g, ' ')
}

function formatDetails(details: string): string {
  try {
    const d = JSON.parse(details)
    if (d.follower) return `→ @${d.follower}`
    if (d.username) return `@${d.username}`
    if (d.name) return d.name
    if (d.total) return `${d.total} follower`
    return ''
  } catch {
    return ''
  }
}
