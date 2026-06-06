'use client'

import useSWR from 'swr'
import { useState, useCallback, useRef, useEffect } from 'react'
import { api } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Database, Download, XCircle, ChevronLeft, ChevronRight,
  CheckCircle2, Users, BadgeCheck, ExternalLink,
  ChevronDown, ChevronUp, Phone, Mail, MessageCircle, Link2,
} from 'lucide-react'
import type { Lead, LeadListResponse, Campaign, Account } from '@/lib/types'
import { formatDateTime, formatDistanceToNow } from '@/lib/dateUtils'
import { Skeleton } from '@/components/ui/skeleton'

const PAGE_SIZE = 50

function formatCount(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

export default function LeadsPage() {
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [campaignFilter, setCampaignFilter] = useState('')
  const [campaignIds, setCampaignIds] = useState<string[]>([])
  const [scrapingAccountIds, setScrapingAccountIds] = useState<string[]>([])
  const [hasPhone, setHasPhone] = useState(false)
  const [hasEmail, setHasEmail] = useState(false)
  const [repliedFilter, setRepliedFilter] = useState<'' | 'true' | 'false'>('')
  const [verifiedOnly, setVerifiedOnly] = useState(false)
  const [minFollowers, setMinFollowers] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [expandedId, setExpandedId] = useState<number | null>(null)

  // Build shared filter params for both list and export (must match so CSV respects selection).
  const buildFilters = useCallback(() => ({
    search: search || undefined,
    campaign_id: campaignFilter || undefined,
    campaign_ids: campaignIds.length ? campaignIds : undefined,
    scraping_account_ids: scrapingAccountIds.length ? scrapingAccountIds : undefined,
    has_phone: hasPhone || undefined,
    has_email: hasEmail || undefined,
    has_replied: repliedFilter === '' ? undefined : repliedFilter === 'true',
    verified_only: verifiedOnly || undefined,
    min_followers: minFollowers ? Number(minFollowers) : undefined,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
  }), [search, campaignFilter, campaignIds, scrapingAccountIds, hasPhone, hasEmail, repliedFilter, verifiedOnly, minFollowers, dateFrom, dateTo])

  const swrKey = [
    'leads', page, search, campaignFilter, campaignIds.join(','), scrapingAccountIds.join(','),
    hasPhone, hasEmail, repliedFilter, verifiedOnly, minFollowers, dateFrom, dateTo
  ]

  const { data, error } = useSWR<LeadListResponse>(
    swrKey,
    () => api.leads.list({ ...buildFilters(), page, page_size: PAGE_SIZE }),
    { refreshInterval: 30000 }
  )

  const { data: campaigns } = useSWR<Campaign[]>('campaigns', api.campaigns.list, { refreshInterval: 60000 })
  const { data: accounts } = useSWR<Account[]>('accounts', api.accounts.list, { refreshInterval: 60000 })

  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 1

  const handleFilterChange = useCallback(() => { setPage(1) }, [])

  const handleExport = useCallback(async () => {
    const blob = await api.leads.exportBlob(buildFilters())
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'leads.csv'
    a.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  }, [buildFilters])

  const ins = data?.insights

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white flex items-center gap-2">
            <Database className="w-7 h-7 text-purple-400" />
            Leads
          </h1>
          <p className="text-gray-400 text-base mt-1">
            Tutti i contatti raggiunti — database per retargeting e analisi
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="border-gray-700 text-gray-300 hover:text-white"
          onClick={handleExport}
        >
          <Download className="w-4 h-4 mr-2" />
          Esporta CSV
        </Button>
      </div>

      {/* Error banner */}
      {error && (
        <div className="flex items-center gap-2 text-red-400 text-sm rounded-lg border border-red-800/50 bg-red-900/10 px-4 py-3">
          <XCircle className="w-4 h-4 flex-shrink-0" />
          Backend non raggiungibile.
        </div>
      )}

      {/* Insight cards — responsive to active filters */}
      {!data && !error ? (
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          {[1, 2, 3].map(i => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl p-4 space-y-2">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-7 w-16" />
              <Skeleton className="h-3 w-14" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          <InsightCard
            icon={<Users className="w-5 h-5 text-purple-400" />}
            label="Lead scrapati"
            value={ins ? ins.scraped_leads.toLocaleString() : '—'}
            sub={campaignFilter ? 'campagna selezionata' : 'tutte le campagne'}
          />
          <InsightCard
            icon={<Database className="w-5 h-5 text-green-400" />}
            label="Lead contattati"
            value={ins ? ins.total_leads.toLocaleString() : '—'}
            sub={campaignFilter || dateFrom || dateTo ? 'filtro attivo' : 'totale'}
            color="green"
          />
          <InsightCard
            icon={<CheckCircle2 className="w-5 h-5 text-blue-400" />}
            label="% Risposta"
            value={ins ? `${ins.reply_rate}%` : '—'}
            sub={ins ? `${ins.total_replied} risposte` : undefined}
            color="blue"
          />
        </div>
      )}

      {/* Filters */}
      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-3">
          <CardTitle className="text-sm text-gray-400 font-medium">Filtri</CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <Input
              placeholder="Cerca username, nome, bio..."
              value={search}
              onChange={e => { setSearch(e.target.value); handleFilterChange() }}
              className="bg-gray-800 border-gray-700 text-white text-sm h-8"
            />
            <select
              value={campaignFilter}
              onChange={e => { setCampaignFilter(e.target.value); handleFilterChange() }}
              className="h-8 text-xs bg-gray-800 border border-gray-700 text-gray-300 rounded-md px-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
            >
              <option value="">Tutte le campagne</option>
              {campaigns?.map(c => (
                <option key={c.id} value={c.id}>{c.name} ({c.source_type === 'import' ? 'lista importata' : `@${c.target_username}`})</option>
              ))}
            </select>
            <select
              value={repliedFilter}
              onChange={e => { setRepliedFilter(e.target.value as '' | 'true' | 'false'); handleFilterChange() }}
              className="h-8 text-xs bg-gray-800 border border-gray-700 text-gray-300 rounded-md px-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
            >
              <option value="">Tutti (con/senza risposta)</option>
              <option value="true">Solo chi ha risposto</option>
              <option value="false">Solo chi non ha risposto</option>
            </select>
            <MultiSelect
              label="Campagne"
              placeholder="Tutte le campagne"
              options={(campaigns ?? []).map(c => ({ value: String(c.id), label: c.name }))}
              selected={campaignIds}
              onChange={vals => { setCampaignIds(vals); handleFilterChange() }}
            />
            <MultiSelect
              label="Account scraping"
              placeholder="Tutti gli account"
              options={(accounts ?? []).map(a => ({ value: String(a.id), label: `@${a.username}` }))}
              selected={scrapingAccountIds}
              onChange={vals => { setScrapingAccountIds(vals); handleFilterChange() }}
            />
            <Input
              type="number"
              placeholder="Min followers (es. 1000)"
              value={minFollowers}
              onChange={e => { setMinFollowers(e.target.value); handleFilterChange() }}
              min={0}
              className="bg-gray-800 border-gray-700 text-white text-sm h-8"
            />
            <Input
              type="date"
              placeholder="Data da"
              value={dateFrom}
              onChange={e => { setDateFrom(e.target.value); handleFilterChange() }}
              className="bg-gray-800 border-gray-700 text-white text-sm h-8"
            />
            <Input
              type="date"
              placeholder="Data a"
              value={dateTo}
              onChange={e => { setDateTo(e.target.value); handleFilterChange() }}
              className="bg-gray-800 border-gray-700 text-white text-sm h-8"
            />
          </div>
          <div className="flex items-center gap-4 mt-3 flex-wrap">
            <label className="flex items-center gap-2 text-sm text-gray-400 cursor-pointer">
              <input
                type="checkbox"
                checked={verifiedOnly}
                onChange={e => { setVerifiedOnly(e.target.checked); handleFilterChange() }}
                className="rounded border-gray-600 bg-gray-800 accent-purple-500"
              />
              Solo account verificati
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-400 cursor-pointer">
              <input
                type="checkbox"
                checked={hasPhone}
                onChange={e => { setHasPhone(e.target.checked); handleFilterChange() }}
                className="rounded border-gray-600 bg-gray-800 accent-purple-500"
              />
              Solo con telefono
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-400 cursor-pointer">
              <input
                type="checkbox"
                checked={hasEmail}
                onChange={e => { setHasEmail(e.target.checked); handleFilterChange() }}
                className="rounded border-gray-600 bg-gray-800 accent-purple-500"
              />
              Solo con email
            </label>
            {(search || campaignFilter || campaignIds.length || scrapingAccountIds.length || hasPhone || hasEmail || repliedFilter || verifiedOnly || minFollowers || dateFrom || dateTo) && (
              <Button
                size="sm"
                variant="ghost"
                className="text-xs text-gray-500 hover:text-gray-300 h-6 px-2"
                onClick={() => {
                  setSearch(''); setCampaignFilter(''); setCampaignIds([]); setScrapingAccountIds([])
                  setHasPhone(false); setHasEmail(false); setRepliedFilter('')
                  setVerifiedOnly(false); setMinFollowers(''); setDateFrom(''); setDateTo('')
                  setPage(1)
                }}
              >
                Rimuovi filtri
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Table */}
      <Card className="bg-gray-900 border-gray-800">
        <CardContent className="p-0">
          {!data && !error && (
            <div className="divide-y divide-gray-800">
              {[1, 2, 3, 4, 5].map(i => (
                <div key={i} className="flex items-center gap-3 p-4">
                  <Skeleton className="w-8 h-8 rounded-full flex-shrink-0" />
                  <div className="flex-1 space-y-1.5">
                    <Skeleton className="h-4 w-36" />
                    <Skeleton className="h-3 w-56" />
                  </div>
                  <Skeleton className="h-4 w-20 flex-shrink-0" />
                </div>
              ))}
            </div>
          )}
          {data?.items.length === 0 && (
            <div className="py-12 text-center text-gray-500">
              Nessun lead trovato con i filtri selezionati
            </div>
          )}
          <div className="divide-y divide-gray-800">
            {data?.items.map((lead: Lead) => (
              <LeadRow
                key={lead.ig_user_id}
                lead={lead}
                expanded={expandedId === lead.ig_user_id}
                onToggle={() => setExpandedId(expandedId === lead.ig_user_id ? null : lead.ig_user_id)}
              />
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-gray-500">
            {data?.total.toLocaleString()} lead — pagina {page} di {totalPages}
          </p>
          <div className="flex gap-2">
            <Button
              size="sm" variant="outline"
              className="border-gray-700 text-gray-400"
              disabled={page === 1}
              onClick={() => setPage(p => p - 1)}
            >
              <ChevronLeft className="w-4 h-4" />
            </Button>
            <Button
              size="sm" variant="outline"
              className="border-gray-700 text-gray-400"
              disabled={page >= totalPages}
              onClick={() => setPage(p => p + 1)}
            >
              <ChevronRight className="w-4 h-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

/* ---------- Multi-select dropdown ---------- */

function MultiSelect({
  label, placeholder, options, selected, onChange,
}: {
  label: string
  placeholder: string
  options: { value: string; label: string }[]
  selected: string[]
  onChange: (values: string[]) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  const toggle = (value: string) => {
    onChange(selected.includes(value) ? selected.filter(v => v !== value) : [...selected, value])
  }

  const summary = selected.length === 0
    ? placeholder
    : selected.length === 1
      ? (options.find(o => o.value === selected[0])?.label ?? `${selected.length} selez.`)
      : `${label}: ${selected.length}`

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full h-8 text-xs bg-gray-800 border border-gray-700 text-gray-300 rounded-md px-2 flex items-center justify-between gap-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
      >
        <span className={`truncate ${selected.length === 0 ? 'text-gray-500' : ''}`}>{summary}</span>
        <ChevronDown className="w-3.5 h-3.5 flex-shrink-0 text-gray-500" />
      </button>
      {open && (
        <div className="absolute z-20 mt-1 w-full max-h-60 overflow-auto rounded-md border border-gray-700 bg-gray-800 shadow-lg py-1">
          {options.length === 0 ? (
            <div className="px-3 py-2 text-xs text-gray-500">Nessuna opzione</div>
          ) : (
            options.map(o => (
              <label
                key={o.value}
                className="flex items-center gap-2 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700/60 cursor-pointer"
              >
                <input
                  type="checkbox"
                  checked={selected.includes(o.value)}
                  onChange={() => toggle(o.value)}
                  className="rounded border-gray-600 bg-gray-900 accent-purple-500"
                />
                <span className="truncate">{o.label}</span>
              </label>
            ))
          )}
        </div>
      )}
    </div>
  )
}

/* ---------- Insight card ---------- */

function InsightCard({
  icon, label, value, sub, color = 'purple'
}: {
  icon: React.ReactNode
  label: string
  value: string
  sub?: string
  color?: 'purple' | 'green' | 'blue' | 'yellow'
}) {
  const border = {
    purple: 'border-purple-900/40',
    green: 'border-green-900/40',
    blue: 'border-blue-900/40',
    yellow: 'border-yellow-900/40',
  }[color]

  return (
    <Card className={`bg-gray-900 ${border} border`}>
      <CardContent className="py-4 px-4">
        <div className="flex items-center gap-2 mb-1">{icon}<span className="text-xs text-gray-400">{label}</span></div>
        <div className="text-2xl font-bold text-white">{value}</div>
        {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
      </CardContent>
    </Card>
  )
}

/* ---------- Lead row ---------- */

function LeadRow({ lead, expanded, onToggle }: { lead: Lead; expanded: boolean; onToggle: () => void }) {
  return (
    <div>
      <div
        className="flex items-start gap-3 p-4 cursor-pointer hover:bg-gray-800/40 transition-colors"
        onClick={onToggle}
      >
        {/* Avatar placeholder */}
        <div className="w-9 h-9 rounded-full bg-gray-700 flex-shrink-0 flex items-center justify-center text-xs text-gray-400 overflow-hidden">
          {lead.profile_pic_url
            ? <img src={lead.profile_pic_url} alt="" className="w-full h-full object-cover" />
            : (lead.username?.[0]?.toUpperCase() ?? '?')
          }
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-white text-sm">@{lead.username ?? '—'}</span>
            {lead.full_name && <span className="text-xs text-gray-400">{lead.full_name}</span>}
            {lead.is_verified && <BadgeCheck className="w-3.5 h-3.5 text-blue-400 flex-shrink-0" />}
            {lead.has_replied && (
              <Badge className="bg-green-700 text-white text-xs px-1.5 py-0">Risposto</Badge>
            )}
            {!lead.last_contacted_at && (
              <Badge className="bg-gray-700 text-gray-300 text-xs px-1.5 py-0">Non contattato</Badge>
            )}
          </div>
          {lead.biography && (
            <p className="text-xs text-gray-400 mt-0.5 line-clamp-1">{lead.biography}</p>
          )}

          {/* Contact columns */}
          {(lead.phone || lead.email || lead.whatsapp || lead.bio_links.length > 0) && (
            <div className="flex items-center gap-3 mt-1 text-xs flex-wrap">
              {lead.phone && (
                <span className="flex items-center gap-1 text-emerald-400">
                  <Phone className="w-3 h-3" />{lead.phone}
                </span>
              )}
              {lead.email && (
                <span className="flex items-center gap-1 text-sky-400">
                  <Mail className="w-3 h-3" />{lead.email}
                </span>
              )}
              {lead.whatsapp && (
                <span className="flex items-center gap-1 text-green-400">
                  <MessageCircle className="w-3 h-3" />{lead.whatsapp}
                </span>
              )}
              {lead.bio_links.length > 0 && (
                <span className="flex items-center gap-1 text-gray-400">
                  <Link2 className="w-3 h-3" />{lead.bio_links.length} link
                </span>
              )}
            </div>
          )}
          <div className="flex items-center gap-3 mt-1 text-xs text-gray-500">
            {lead.follower_count != null && (
              <span className="flex items-center gap-1">
                <Users className="w-3 h-3" />
                {formatCount(lead.follower_count)}
              </span>
            )}
            {lead.scrape_sources.length > 0 && (
              <span>da @{lead.scrape_sources.join(', @')}</span>
            )}
            {lead.last_contacted_at && (
              <span>contattato {formatDistanceToNow(lead.last_contacted_at)}</span>
            )}
            {lead.contacts_count > 1 && (
              <span>{lead.contacts_count}x contattato</span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {lead.external_url && (
            <a
              href={lead.external_url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={e => e.stopPropagation()}
              className="text-gray-500 hover:text-gray-300"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </a>
          )}
          {expanded
            ? <ChevronUp className="w-4 h-4 text-gray-500" />
            : <ChevronDown className="w-4 h-4 text-gray-500" />
          }
        </div>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="px-4 pb-4 pt-0 ml-12 space-y-3 border-t border-gray-800/60 bg-gray-800/20">
          <div className="flex items-center gap-4 pt-3 text-xs">
            <span className="text-gray-500">Contattato <span className="text-white font-medium">{lead.contacts_count}x</span></span>
            <span className="text-gray-500">Risposta <span className={`font-medium ${lead.has_replied ? 'text-green-400' : 'text-gray-400'}`}>{lead.has_replied ? 'Sì' : 'No'}</span></span>
          </div>

          {(lead.phone || lead.email || lead.whatsapp) && (
            <div className="flex items-center gap-4 text-xs flex-wrap">
              {lead.phone && <span className="text-gray-500">Tel: <span className="text-white">{lead.phone}</span></span>}
              {lead.email && <span className="text-gray-500">Email: <span className="text-white">{lead.email}</span></span>}
              {lead.whatsapp && <span className="text-gray-500">WhatsApp: <span className="text-white">{lead.whatsapp}</span></span>}
            </div>
          )}

          {lead.external_url && (
            <div className="text-xs">
              <span className="text-gray-500">Sito: </span>
              <a href={lead.external_url} target="_blank" rel="noopener noreferrer"
                className="text-purple-400 hover:underline break-all">
                {lead.external_url}
              </a>
            </div>
          )}

          {lead.bio_links.length > 0 && (
            <div className="text-xs">
              <span className="text-gray-500">Link bio:</span>
              <div className="mt-1 space-y-0.5">
                {lead.bio_links.map((bl, i) => (
                  <a key={i} href={bl.url} target="_blank" rel="noopener noreferrer"
                    className="block text-purple-400 hover:underline break-all">
                    {bl.title || bl.url}
                  </a>
                ))}
              </div>
            </div>
          )}

          {lead.biography && (
            <div className="text-xs">
              <span className="text-gray-500">Bio: </span>
              <span className="text-gray-300">{lead.biography}</span>
            </div>
          )}

          {lead.contact_history.length > 0 && (
            <div>
              <p className="text-xs text-gray-500 mb-1">Storico contatti:</p>
              <div className="space-y-1">
                {lead.contact_history.map((h, i) => (
                  <div key={i} className="text-xs text-gray-400 flex gap-2">
                    <span className="text-gray-600">{i + 1}.</span>
                    <span>{h.campaign_name ?? 'Campagna sconosciuta'}</span>
                    {h.account_username && <span className="text-gray-600">via @{h.account_username}</span>}
                    {h.contacted_at && <span className="text-gray-600">{formatDateTime(h.contacted_at)}</span>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function StatCell({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-gray-500">{label}</div>
      <div className="text-white font-medium">{value}</div>
    </div>
  )
}
