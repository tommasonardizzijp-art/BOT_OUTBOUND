'use client'

import useSWR from 'swr'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BadgeCheck,
  Download,
  Filter,
  Loader2,
  Play,
  RefreshCw,
  Save,
  Sparkles,
  Target,
} from 'lucide-react'
import { api } from '@/lib/api'
import type {
  Account,
  Campaign,
  CompiledLeadRules,
  CompileProfileResponse,
  LeadQualificationFilters,
  LeadQualificationResultList,
  LeadQualificationRun,
  LeadTargetProfile,
} from '@/lib/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { formatDateTime } from '@/lib/dateUtils'

const PAGE_SIZE = 50

function todayLocal(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function defaultDescription(): string {
  return ''
}

function safeJson(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

export default function LeadQualificationPage() {
  const [selectedProfileId, setSelectedProfileId] = useState('')
  const [description, setDescription] = useState(defaultDescription())
  const [profileName, setProfileName] = useState('')
  const [rulesText, setRulesText] = useState('')
  const [compiled, setCompiled] = useState<CompileProfileResponse | null>(null)
  const [compileLoading, setCompileLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [estimateLoading, setEstimateLoading] = useState(false)
  const [starting, setStarting] = useState(false)
  const [estimate, setEstimate] = useState<null | {
    candidate_count: number
    already_qualified_same_rules: number
    will_process: number
    over_limit: boolean
    max_run_size: number
  }>(null)
  const [message, setMessage] = useState<string | null>(null)

  const [campaignIds, setCampaignIds] = useState<string[]>([])
  const [scrapingAccountIds, setScrapingAccountIds] = useState<string[]>([])
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState(todayLocal())
  const [hasPhone, setHasPhone] = useState(false)
  const [hasEmail, setHasEmail] = useState(false)
  const [minFollowers, setMinFollowers] = useState('')
  const [maxLeads, setMaxLeads] = useState('5000')
  const [skipExisting, setSkipExisting] = useState(true)

  const [resultStatus, setResultStatus] = useState('match')
  const [minScore, setMinScore] = useState('80')
  const [resultsPage, setResultsPage] = useState(1)

  const { data: profiles, mutate: mutateProfiles } = useSWR<LeadTargetProfile[]>(
    'lead-qualification-profiles',
    api.leadQualification.profiles.list,
    { refreshInterval: 60000 },
  )
  const selectedProfile = profiles?.find(p => p.id === selectedProfileId) ?? null
  const { data: campaigns } = useSWR<Campaign[]>('campaigns', api.campaigns.list, { refreshInterval: 60000 })
  const { data: accounts } = useSWR<Account[]>('accounts', api.accounts.list, { refreshInterval: 60000 })
  const { data: runs, mutate: mutateRuns } = useSWR<LeadQualificationRun[]>(
    ['lead-qualification-runs', selectedProfileId],
    () => api.leadQualification.runs.list(selectedProfileId || undefined),
    {
      refreshInterval: latestRuns => latestRuns?.some(r => r.status === 'queued' || r.status === 'running') ? 5000 : 30000,
    },
  )
  const { data: results, mutate: mutateResults } = useSWR<LeadQualificationResultList>(
    selectedProfileId
      ? ['lead-qualification-results', selectedProfileId, resultStatus, minScore, resultsPage]
      : null,
    () => api.leadQualification.results.list({
      target_profile_id: selectedProfileId,
      status: resultStatus || undefined,
      min_score: minScore ? Number(minScore) : undefined,
      page: resultsPage,
      page_size: PAGE_SIZE,
    }),
    { refreshInterval: 30000 },
  )

  useEffect(() => {
    if (!selectedProfile) return
    setProfileName(selectedProfile.name)
    setDescription(selectedProfile.description)
    setRulesText(safeJson(selectedProfile.compiled_rules))
    setCompiled({
      name_suggestion: selectedProfile.name,
      compiled_rules: selectedProfile.compiled_rules,
      pass_threshold: selectedProfile.pass_threshold,
      reject_threshold: selectedProfile.reject_threshold,
      ai_review_min_score: selectedProfile.ai_review_min_score,
      ai_review_max_score: selectedProfile.ai_review_max_score,
      max_run_size: selectedProfile.max_run_size,
    })
  }, [selectedProfile])

  const filters = useMemo<LeadQualificationFilters>(() => ({
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
    campaign_ids: campaignIds,
    scraping_account_ids: scrapingAccountIds,
    has_phone: hasPhone,
    has_email: hasEmail,
    min_followers: minFollowers ? Number(minFollowers) : undefined,
    max_leads: maxLeads ? Number(maxLeads) : 5000,
    skip_existing_same_rules: skipExisting,
  }), [dateFrom, dateTo, campaignIds, scrapingAccountIds, hasPhone, hasEmail, minFollowers, maxLeads, skipExisting])

  const handleCompile = useCallback(async () => {
    setCompileLoading(true)
    setMessage(null)
    try {
      const res = await api.leadQualification.profiles.compile(description)
      setCompiled(res)
      setProfileName(res.name_suggestion)
      setRulesText(safeJson(res.compiled_rules))
      setMessage('Criteri generati. Puoi modificarli e salvarli come target.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Generazione criteri fallita')
    } finally {
      setCompileLoading(false)
    }
  }, [description])

  const handleSaveProfile = useCallback(async () => {
    setSaving(true)
    setMessage(null)
    let rules: CompiledLeadRules
    try {
      rules = JSON.parse(rulesText) as CompiledLeadRules
    } catch {
      setMessage('JSON non valido: controlla la sintassi dei criteri')
      setSaving(false)
      return
    }
    try {
      const payload = {
        name: profileName.trim(),
        description: description.trim(),
        compiled_rules: rules,
        pass_threshold: compiled?.pass_threshold ?? 80,
        reject_threshold: compiled?.reject_threshold ?? 25,
        ai_review_min_score: compiled?.ai_review_min_score ?? 26,
        ai_review_max_score: compiled?.ai_review_max_score ?? 79,
        max_run_size: compiled?.max_run_size ?? 5000,
      }
      const saved = selectedProfileId
        ? await api.leadQualification.profiles.update(selectedProfileId, payload)
        : await api.leadQualification.profiles.create(payload)
      setSelectedProfileId(saved.id)
      await mutateProfiles()
      setMessage('Target salvato.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Salvataggio target fallito')
    } finally {
      setSaving(false)
    }
  }, [compiled, description, mutateProfiles, profileName, rulesText, selectedProfileId])

  const handleEstimate = useCallback(async () => {
    if (!selectedProfileId) return
    setEstimateLoading(true)
    setMessage(null)
    try {
      const res = await api.leadQualification.runs.estimate({ target_profile_id: selectedProfileId, filters })
      setEstimate(res)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Stima fallita')
    } finally {
      setEstimateLoading(false)
    }
  }, [filters, selectedProfileId])

  const handleStart = useCallback(async () => {
    if (!selectedProfileId) return
    setStarting(true)
    setMessage(null)
    try {
      await api.leadQualification.runs.create({ target_profile_id: selectedProfileId, filters })
      setEstimate(null)
      await mutateRuns()
      setMessage('Classificazione accodata.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Avvio classificazione fallito')
    } finally {
      setStarting(false)
    }
  }, [filters, mutateRuns, selectedProfileId])

  const handleCancelRun = useCallback(async (runId: string) => {
    try {
      await api.leadQualification.runs.cancel(runId)
      await mutateRuns()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Annullamento run fallito')
    }
  }, [mutateRuns])

  const handleExport = useCallback(async () => {
    if (!selectedProfileId) return
    try {
      const blob = await api.leadQualification.results.exportBlob({
        target_profile_id: selectedProfileId,
        status: resultStatus || undefined,
        min_score: minScore ? Number(minScore) : undefined,
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'lead_qualification.csv'
      a.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 10000)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Esportazione CSV fallita')
    }
  }, [minScore, resultStatus, selectedProfileId])

  const totalPages = results ? Math.max(1, Math.ceil(results.total / PAGE_SIZE)) : 1

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-white flex items-center gap-2">
            <Target className="w-7 h-7 text-purple-400" />
            Qualifica lead
          </h1>
          <p className="text-gray-400 text-base mt-1">
            Crea target, filtra i contatti globali e classifica solo il sottoinsieme utile.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="border-gray-700 text-gray-300 hover:text-white"
          onClick={() => { mutateRuns(); mutateResults() }}
        >
          <RefreshCw className="w-4 h-4 mr-2" />
          Aggiorna
        </Button>
      </div>

      {message && (
        <div className="rounded-md border border-gray-800 bg-gray-900 px-4 py-3 text-sm text-gray-300">
          {message}
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1.05fr)_minmax(420px,0.95fr)] gap-5">
        <Card className="bg-gray-900 border-gray-800">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm text-gray-300 flex items-center gap-2">
              <Sparkles className="w-4 h-4 text-purple-400" />
              Target e criteri
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <select
                value={selectedProfileId}
                onChange={e => { setSelectedProfileId(e.target.value); setEstimate(null); setResultsPage(1) }}
                className="h-9 text-sm bg-gray-800 border border-gray-700 text-gray-300 rounded-md px-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
              >
                <option value="">Nuovo target</option>
                {profiles?.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
              <Input
                value={profileName}
                onChange={e => setProfileName(e.target.value)}
                placeholder="Nome target"
                className="bg-gray-800 border-gray-700 text-white h-9"
              />
            </div>

            <Textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              rows={5}
              className="bg-gray-800 border-gray-700 text-white text-sm"
              placeholder="Descrivi cosa includere, cosa escludere, retail/B2B/ecommerce/local business, lingua o mercato."
            />

            <div className="flex flex-wrap gap-2">
              <Button onClick={handleCompile} disabled={compileLoading || description.trim().length < 20}>
                {compileLoading ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Sparkles className="w-4 h-4 mr-2" />}
                Genera criteri
              </Button>
              <Button
                variant="outline"
                className="border-gray-700 text-gray-300"
                onClick={handleSaveProfile}
                disabled={saving || !profileName.trim() || !rulesText.trim()}
              >
                {saving ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Save className="w-4 h-4 mr-2" />}
                Salva target
              </Button>
            </div>

            <div>
              <div className="text-xs text-gray-500 mb-1">Criteri JSON modificabili</div>
              <Textarea
                value={rulesText}
                onChange={e => setRulesText(e.target.value)}
                rows={13}
                className="bg-gray-950 border-gray-800 text-gray-200 text-xs font-mono"
                placeholder="Genera o incolla qui le regole compilate."
              />
            </div>
          </CardContent>
        </Card>

        <Card className="bg-gray-900 border-gray-800">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm text-gray-300 flex items-center gap-2">
              <Filter className="w-4 h-4 text-purple-400" />
              Filtri run
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <Input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} className="bg-gray-800 border-gray-700 text-white h-9" />
              <Input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} className="bg-gray-800 border-gray-700 text-white h-9" />
              <MultiSelect
                label="Campagne"
                placeholder="Tutte le campagne"
                options={(campaigns ?? []).map(c => ({ value: c.id, label: c.name }))}
                selected={campaignIds}
                onChange={setCampaignIds}
              />
              <MultiSelect
                label="Account"
                placeholder="Tutti gli account"
                options={(accounts ?? []).map(a => ({ value: a.id, label: `@${a.username}` }))}
                selected={scrapingAccountIds}
                onChange={setScrapingAccountIds}
              />
              <Input
                type="number"
                min={0}
                placeholder="Min followers"
                value={minFollowers}
                onChange={e => setMinFollowers(e.target.value)}
                className="bg-gray-800 border-gray-700 text-white h-9"
              />
              <Input
                type="number"
                min={1}
                max={5000}
                placeholder="Max lead"
                value={maxLeads}
                onChange={e => setMaxLeads(e.target.value)}
                className="bg-gray-800 border-gray-700 text-white h-9"
              />
            </div>

            <div className="flex flex-wrap gap-4 text-sm text-gray-400">
              <Checkbox label="Solo con telefono" checked={hasPhone} onChange={setHasPhone} />
              <Checkbox label="Solo con email" checked={hasEmail} onChange={setHasEmail} />
              <Checkbox label="Salta gia qualificati" checked={skipExisting} onChange={setSkipExisting} />
            </div>

            <div className="flex gap-2">
              <Button onClick={handleEstimate} disabled={!selectedProfileId || estimateLoading}>
                {estimateLoading ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <BadgeCheck className="w-4 h-4 mr-2" />}
                Stima
              </Button>
              <Button
                variant="outline"
                className="border-gray-700 text-gray-300"
                onClick={handleStart}
                disabled={!selectedProfileId || starting || !estimate || estimate.over_limit || estimate.will_process <= 0}
              >
                {starting ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Play className="w-4 h-4 mr-2" />}
                Avvia classificazione
              </Button>
            </div>

            {estimate && (
              <div className={`rounded-md border px-3 py-3 text-sm ${estimate.over_limit ? 'border-red-800 bg-red-950/30 text-red-200' : 'border-gray-800 bg-gray-950 text-gray-300'}`}>
                <div className="grid grid-cols-2 gap-2">
                  <Stat label="Candidati" value={estimate.candidate_count.toLocaleString()} />
                  <Stat label="Gia qualificati" value={estimate.already_qualified_same_rules.toLocaleString()} />
                  <Stat label="Da processare" value={estimate.will_process.toLocaleString()} />
                  <Stat label="Limite" value={estimate.max_run_size.toLocaleString()} />
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-3">
          <CardTitle className="text-sm text-gray-300">Run recenti</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {(runs ?? []).length === 0 ? (
            <div className="text-sm text-gray-500 py-4">Nessuna run ancora avviata.</div>
          ) : (
            runs?.map(run => <RunRow key={run.id} run={run} onCancel={handleCancelRun} />)
          )}
        </CardContent>
      </Card>

      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between gap-3">
            <CardTitle className="text-sm text-gray-300">Risultati qualificati</CardTitle>
            <Button
              variant="outline"
              size="sm"
              className="border-gray-700 text-gray-300"
              onClick={handleExport}
              disabled={!selectedProfileId}
            >
              <Download className="w-4 h-4 mr-2" />
              Esporta CSV
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <select
              value={resultStatus}
              onChange={e => { setResultStatus(e.target.value); setResultsPage(1) }}
              className="h-9 text-sm bg-gray-800 border border-gray-700 text-gray-300 rounded-md px-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
            >
              <option value="match">Solo match</option>
              <option value="match,ambiguous">Match + Ambigui</option>
              <option value="ambiguous">Solo ambigui</option>
              <option value="no_match">Solo no match</option>
              <option value="">Tutti</option>
            </select>
            <Input
              type="number"
              min={0}
              max={100}
              value={minScore}
              onChange={e => { setMinScore(e.target.value); setResultsPage(1) }}
              placeholder="Confidenza minima"
              className="bg-gray-800 border-gray-700 text-white h-9"
            />
            <div className="text-sm text-gray-500 flex items-center px-1">
              {results ? `${results.total.toLocaleString()} risultati` : 'Seleziona un target'}
            </div>
          </div>

          <p className="text-xs text-gray-500 -mt-1">
            Lo stato e la confidenza minima filtrano sia la lista qui sotto sia l&apos;<strong>Esporta CSV</strong>.
            Per i tuoi lead usa <strong>Match + Ambigui</strong> e confidenza minima vuota (0).
          </p>

          <div className="divide-y divide-gray-800">
            {results?.items.length === 0 && <div className="py-8 text-center text-gray-500">Nessun risultato</div>}
            {results?.items.map(item => (
              <div key={item.id} className="py-3 flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-white">@{item.username ?? '—'}</span>
                    {item.full_name && <span className="text-xs text-gray-400">{item.full_name}</span>}
                    <Badge className={item.status === 'match' ? 'bg-green-700 text-white' : item.status === 'ambiguous' ? 'bg-yellow-700 text-white' : 'bg-gray-700 text-gray-200'}>
                      {item.status}
                    </Badge>
                    <span className="text-xs text-purple-300">{item.confidence_score}%</span>
                    {item.ai_used && <span className="text-xs text-blue-300">AI</span>}
                  </div>
                  {item.biography && <p className="text-xs text-gray-500 mt-1 line-clamp-1">{item.biography}</p>}
                  {item.reason && <p className="text-xs text-blue-400/70 mt-1 line-clamp-2 italic">{item.reason}</p>}
                  <div className="text-xs text-gray-500 mt-1 flex gap-3 flex-wrap">
                    {item.phone && <span>{item.phone}</span>}
                    {item.email && <span>{item.email}</span>}
                    {item.whatsapp && <span>{item.whatsapp}</span>}
                    {item.first_seen_at && <span>{formatDateTime(item.first_seen_at)}</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>

          {results && totalPages > 1 && (
            <div className="flex items-center justify-between pt-2">
              <span className="text-xs text-gray-500">Pagina {resultsPage} di {totalPages}</span>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" className="border-gray-700 text-gray-400" disabled={resultsPage <= 1} onClick={() => setResultsPage(p => p - 1)}>Precedente</Button>
                <Button size="sm" variant="outline" className="border-gray-700 text-gray-400" disabled={resultsPage >= totalPages} onClick={() => setResultsPage(p => p + 1)}>Successiva</Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function Checkbox({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        onChange={e => onChange(e.target.checked)}
        className="rounded border-gray-600 bg-gray-800 accent-purple-500"
      />
      {label}
    </label>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-gray-500">{label}</div>
      <div className="text-lg font-semibold text-white">{value}</div>
    </div>
  )
}

function RunRow({ run, onCancel }: { run: LeadQualificationRun; onCancel?: (id: string) => void }) {
  const progress = run.total_candidates > 0
    ? Math.round((run.processed_count / Math.max(1, run.total_candidates - run.skipped_existing)) * 100)
    : 0
  const color = run.status === 'completed' ? 'bg-green-700' : run.status === 'failed' ? 'bg-red-700' : run.status === 'cancelled' ? 'bg-gray-600' : 'bg-purple-700'
  const cancellable = run.status === 'queued' || run.status === 'running'
  return (
    <div className="rounded-md border border-gray-800 bg-gray-950 px-3 py-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-white">{run.target_profile_name ?? run.target_profile_id}</span>
            <Badge className={`${color} text-white`}>{run.status}</Badge>
          </div>
          <div className="text-xs text-gray-500 mt-1">
            {run.created_at ? formatDateTime(run.created_at) : ''} · processati {run.processed_count}/{Math.max(0, run.total_candidates - run.skipped_existing)}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-right text-xs text-gray-400">
            <div>match {run.matched_count} · ambigui {run.ambiguous_count}</div>
            <div>AI {run.ai_reviewed_count} · errori {run.error_count}</div>
          </div>
          {cancellable && onCancel && (
            <Button
              size="sm"
              variant="ghost"
              className="text-gray-500 hover:text-red-400 h-7 px-2"
              onClick={() => onCancel(run.id)}
            >
              Annulla
            </Button>
          )}
        </div>
      </div>
      <div className="mt-2 h-1.5 rounded bg-gray-800 overflow-hidden">
        <div className="h-full bg-purple-500" style={{ width: `${Math.min(100, progress)}%` }} />
      </div>
    </div>
  )
}

function MultiSelect({
  label,
  placeholder,
  options,
  selected,
  onChange,
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
    const onClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  const summary = selected.length === 0
    ? placeholder
    : selected.length === 1
      ? options.find(o => o.value === selected[0])?.label ?? `${label}: 1`
      : `${label}: ${selected.length}`

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full h-9 text-sm bg-gray-800 border border-gray-700 text-gray-300 rounded-md px-2 flex items-center justify-between gap-2 focus:outline-none focus:ring-1 focus:ring-purple-500"
      >
        <span className={`truncate ${selected.length === 0 ? 'text-gray-500' : ''}`}>{summary}</span>
      </button>
      {open && (
        <div className="absolute z-30 mt-1 w-full max-h-60 overflow-auto rounded-md border border-gray-700 bg-gray-800 shadow-lg py-1">
          {options.length === 0 ? (
            <div className="px-3 py-2 text-xs text-gray-500">Nessuna opzione</div>
          ) : options.map(option => (
            <label key={option.value} className="flex items-center gap-2 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700/60 cursor-pointer">
              <input
                type="checkbox"
                checked={selected.includes(option.value)}
                onChange={() => {
                  onChange(selected.includes(option.value)
                    ? selected.filter(v => v !== option.value)
                    : [...selected, option.value])
                }}
                className="rounded border-gray-600 bg-gray-900 accent-purple-500"
              />
              <span className="truncate">{option.label}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
