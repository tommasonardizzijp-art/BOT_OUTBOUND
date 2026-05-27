'use client'

import useSWR from 'swr'
import { useState } from 'react'
import { api } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { toast } from 'sonner'
import { RotateCcw, CheckCircle, XCircle, Clock, RefreshCw, Send, TrendingUp, Reply } from 'lucide-react'
import type { Message, MessageStatus, MessageStats, Campaign, Account } from '@/lib/types'
import { formatDateTime } from '@/lib/dateUtils'
import { Skeleton } from '@/components/ui/skeleton'

const STATUS_ICON: Record<MessageStatus, React.ReactNode> = {
  pending: <Clock className="w-4 h-4 text-gray-400" />,
  sending: <Send className="w-4 h-4 text-blue-400" />,
  sent: <CheckCircle className="w-4 h-4 text-green-400" />,
  failed: <XCircle className="w-4 h-4 text-red-400" />,
  retry: <RefreshCw className="w-4 h-4 text-yellow-400" />,
}

const STATUS_COLORS: Record<MessageStatus, string> = {
  pending: 'bg-gray-700',
  sending: 'bg-blue-700',
  sent: 'bg-green-700',
  failed: 'bg-red-700',
  retry: 'bg-yellow-700',
}

type Period = '24h' | '7d' | '30d' | '6m' | 'custom'
type MessageFilter = MessageStatus | 'replied' | ''
const PERIOD_LABELS: Record<Exclude<Period, 'custom'>, string> = {
  '24h': '24 ore',
  '7d': '7 giorni',
  '30d': '30 giorni',
  '6m': '6 mesi',
}

export default function MessagesPage() {
  const [messageFilter, setMessageFilter] = useState<MessageFilter>('')
  const [campaignFilter, setCampaignFilter] = useState('')
  const [accountFilter, setAccountFilter] = useState('')

  // Stats time selector
  const [statsPeriod, setStatsPeriod] = useState<Period>('7d')
  const [statsDateFrom, setStatsDateFrom] = useState('')
  const [statsDateTo, setStatsDateTo] = useState('')

  const effectivePeriod = statsPeriod === 'custom' ? undefined : statsPeriod
  const effectiveDateFrom = statsPeriod === 'custom' ? statsDateFrom : undefined
  const effectiveDateTo = statsPeriod === 'custom' ? statsDateTo : undefined

  const { data, error, mutate } = useSWR(
    ['messages', messageFilter, campaignFilter, accountFilter],
    () => api.messages.list({
      status: messageFilter && messageFilter !== 'replied' ? messageFilter : undefined,
      replied_only: messageFilter === 'replied' || undefined,
      campaign_id: campaignFilter || undefined,
      account_id: accountFilter || undefined,
      page_size: 100,
    }),
    { refreshInterval: 8000 }
  )

  const { data: stats, isLoading: statsLoading } = useSWR<MessageStats>(
    ['message-stats', statsPeriod, statsDateFrom, statsDateTo, campaignFilter, accountFilter],
    () => api.messages.stats({
      period: effectivePeriod,
      date_from: effectiveDateFrom || undefined,
      date_to: effectiveDateTo || undefined,
      campaign_id: campaignFilter || undefined,
      account_id: accountFilter || undefined,
    }),
    { refreshInterval: 30000 }
  )

  const { data: campaigns } = useSWR<Campaign[]>('campaigns', api.campaigns.list, { refreshInterval: 60000 })
  const { data: accounts } = useSWR<Account[]>('accounts', api.accounts.list, { refreshInterval: 60000 })

  const handleRetry = async (id: string) => {
    try {
      await api.messages.retry(id)
      toast.success('Messaggio in coda per retry')
      await mutate()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    }
  }

  const filters: Array<{ label: string; value: MessageFilter }> = [
    { label: 'Tutti', value: '' },
    { label: 'Risposte', value: 'replied' },
    { label: 'Inviati', value: 'sent' },
    { label: 'Falliti', value: 'failed' },
    { label: 'In attesa', value: 'pending' },
    { label: 'Invio in corso', value: 'sending' },
  ]

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Messaggi</h1>
        <p className="text-gray-400 text-base mt-1">{data?.total ?? 0} messaggi totali</p>
      </div>

      {/* Stats mini-dashboard */}
      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <CardTitle className="text-sm text-gray-400 font-medium">Statistiche messaggi</CardTitle>
            {/* Period selector */}
            <div className="flex items-center gap-1 flex-wrap">
              {(Object.keys(PERIOD_LABELS) as Exclude<Period, 'custom'>[]).map(p => (
                <Button
                  key={p}
                  size="sm"
                  variant={statsPeriod === p ? 'default' : 'ghost'}
                  className={`h-6 px-2 text-xs ${statsPeriod === p ? 'bg-purple-600 text-white' : 'text-gray-500 hover:text-gray-300'}`}
                  onClick={() => setStatsPeriod(p)}
                >
                  {PERIOD_LABELS[p]}
                </Button>
              ))}
              <Button
                size="sm"
                variant={statsPeriod === 'custom' ? 'default' : 'ghost'}
                className={`h-6 px-2 text-xs ${statsPeriod === 'custom' ? 'bg-purple-600 text-white' : 'text-gray-500 hover:text-gray-300'}`}
                onClick={() => setStatsPeriod('custom')}
              >
                Personalizzato
              </Button>
            </div>
          </div>
          {statsPeriod === 'custom' && (
            <div className="flex items-center gap-2 mt-2 flex-wrap">
              <Input
                type="date"
                value={statsDateFrom}
                onChange={e => setStatsDateFrom(e.target.value)}
                className="h-7 text-xs bg-gray-800 border-gray-700 text-white w-36"
              />
              <span className="text-gray-500 text-xs">→</span>
              <Input
                type="date"
                value={statsDateTo}
                onChange={e => setStatsDateTo(e.target.value)}
                className="h-7 text-xs bg-gray-800 border-gray-700 text-white w-36"
              />
            </div>
          )}
        </CardHeader>
        <CardContent className="pt-0">
          {statsLoading ? (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {[1, 2, 3, 4].map(i => (
                <div key={i} className="space-y-2">
                  <Skeleton className="h-3 w-20" />
                  <Skeleton className="h-7 w-14" />
                </div>
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <StatCard
                icon={<Send className="w-4 h-4 text-green-400" />}
                label="Inviati"
                value={stats?.total_sent.toLocaleString() ?? '—'}
                color="green"
              />
              <StatCard
                icon={<TrendingUp className="w-4 h-4 text-purple-400" />}
                label="Tasso successo"
                value={stats ? `${stats.success_rate}%` : '—'}
                color="purple"
              />
              <StatCard
                icon={<Reply className="w-4 h-4 text-blue-400" />}
                label="% Risposta"
                value={stats ? `${stats.reply_rate}%` : '—'}
                color="blue"
              />
              <StatCard
                icon={<XCircle className="w-4 h-4 text-red-400" />}
                label="Falliti"
                value={stats?.total_failed.toLocaleString() ?? '—'}
                color="red"
              />
            </div>
          )}
        </CardContent>
      </Card>

      {error && (
        <div className="flex items-center gap-2 text-red-400 text-sm rounded-lg border border-red-800/50 bg-red-900/10 px-4 py-3">
          <XCircle className="w-4 h-4 flex-shrink-0" />
          Backend non raggiungibile. Assicurarsi che il server sia in esecuzione.
        </div>
      )}

      {/* Filter tabs */}
      <div className="flex flex-wrap gap-2 items-center">
        {filters.map(f => (
          <Button
            key={f.value}
            size="sm"
            variant={messageFilter === f.value ? 'default' : 'outline'}
            className={messageFilter === f.value ? 'bg-purple-600' : 'border-gray-700 text-gray-400'}
            onClick={() => setMessageFilter(f.value)}
          >
            {f.label}
          </Button>
        ))}
        {campaigns && campaigns.length > 0 && (
          <select
            value={campaignFilter}
            onChange={e => setCampaignFilter(e.target.value)}
            className="h-8 text-xs bg-gray-800 border border-gray-700 text-gray-300 rounded-md px-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
          >
            <option value="">Tutte le campagne</option>
            {campaigns.map(c => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        )}
        {accounts && accounts.length > 0 && (
          <select
            value={accountFilter}
            onChange={e => setAccountFilter(e.target.value)}
            className="h-8 text-xs bg-gray-800 border border-gray-700 text-gray-300 rounded-md px-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
          >
            <option value="">Tutti gli account</option>
            {accounts.map(a => (
              <option key={a.id} value={a.id}>@{a.username}</option>
            ))}
          </select>
        )}
      </div>

      <Card className="bg-gray-900 border-gray-800">
        <CardContent className="p-0">
          {!data && !error && (
            <div className="divide-y divide-gray-800">
              {[1, 2, 3, 4].map(i => (
                <div key={i} className="flex items-start gap-3 p-4">
                  <Skeleton className="w-4 h-4 rounded-full mt-0.5 flex-shrink-0" />
                  <div className="flex-1 space-y-2">
                    <div className="flex items-center gap-2">
                      <Skeleton className="h-4 w-16" />
                      <Skeleton className="h-3 w-24" />
                    </div>
                    <Skeleton className="h-3 w-full" />
                    <Skeleton className="h-3 w-3/4" />
                  </div>
                </div>
              ))}
            </div>
          )}
          {data?.items.length === 0 && (
            <div className="py-12 text-center text-gray-500">
              {messageFilter === 'replied' ? 'Nessuna risposta trovata' : 'Nessun messaggio trovato'}
            </div>
          )}
          <div className="divide-y divide-gray-800">
            {data?.items.map((msg: Message) => (
              <div key={msg.id} className="flex items-start gap-3 p-4">
                <div className="mt-0.5 flex-shrink-0">
                  {STATUS_ICON[msg.status]}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <Badge className={`${STATUS_COLORS[msg.status]} text-white text-xs`}>
                      {msg.status}
                    </Badge>
                    {msg.has_reply && (
                      <Badge className="bg-blue-700 text-white text-xs">
                        Risposta ricevuta
                      </Badge>
                    )}
                    {msg.sent_at && (
                      <span className="text-xs text-gray-500">{formatDateTime(msg.sent_at)}</span>
                    )}
                    {msg.retry_count > 0 && (
                      <span className="text-xs text-yellow-600">{msg.retry_count}x retry</span>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-gray-500 mb-1">
                    {msg.follower_username && (
                      <span className="text-gray-300">@{msg.follower_username}</span>
                    )}
                    {msg.follower_full_name && <span>{msg.follower_full_name}</span>}
                    {msg.campaign_name && <span>Campagna {msg.campaign_name}</span>}
                    {msg.account_username && <span>Profilo @{msg.account_username}</span>}
                  </div>
                  <p className="text-sm text-gray-300 line-clamp-2">{msg.generated_text}</p>
                  {msg.error_message && (
                    <p className="text-xs text-red-400 mt-1">{msg.error_message}</p>
                  )}
                </div>
                {msg.status === 'failed' && (
                  <Button size="sm" variant="ghost" className="text-gray-400 flex-shrink-0"
                    onClick={() => handleRetry(msg.id)}>
                    <RotateCcw className="w-4 h-4" />
                  </Button>
                )}
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function StatCard({
  icon, label, value, color = 'purple'
}: {
  icon: React.ReactNode
  label: string
  value: string
  color?: 'purple' | 'green' | 'blue' | 'red'
}) {
  const border = {
    purple: 'border-purple-900/40',
    green: 'border-green-900/40',
    blue: 'border-blue-900/40',
    red: 'border-red-900/40',
  }[color]

  return (
    <div className={`rounded-lg border ${border} bg-gray-800/40 px-4 py-3`}>
      <div className="flex items-center gap-1.5 mb-1">{icon}<span className="text-xs text-gray-400">{label}</span></div>
      <div className="text-xl font-bold text-white">{value}</div>
    </div>
  )
}
