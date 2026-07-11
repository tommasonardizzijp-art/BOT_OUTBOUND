'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { api } from '@/lib/api'
import { renderPreview, findUnknownPlaceholders } from '@/lib/spintax'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { toast } from 'sonner'
import { ArrowLeft, Loader2, ChevronDown, ChevronRight } from 'lucide-react'
import Link from 'next/link'

// BUG-NEW-10: Instagram username: letters, numbers, periods, underscores, 1-30 chars
const IG_USERNAME_RE = /^[a-zA-Z0-9._]{1,30}$/

export default function NewCampaignPage() {
  const router = useRouter()
  const [loading, setLoading] = useState(false)
  const [sourceType, setSourceType] = useState<'scrape' | 'import'>('scrape')
  const [importFile, setImportFile] = useState<File | null>(null)
  const [form, setForm] = useState({
    name: '',
    target_username: '',
    scrape_mode: 'followers' as 'followers' | 'following' | 'dm_threads',
    base_message_template: '',
    message_template_b: '',
    message_template_c: '',
    ai_prompt_context: '',
    ai_enabled: false,
    ai_system_prompt: '',
    daily_limit: '',
    scrape_daily_limit: '',
    require_approval: false,
    approval_sample_size: '5',
  })
  // Inbox: unico motore reale = API (il browser DOM-listing è stato rimosso, no-op lato BE).
  const [inboxEngine, setInboxEngine] = useState<'browser' | 'api'>('api')
  const [bioEngine, setBioEngine] = useState<'api' | 'browser'>('api')
  const [messagingEnabled, setMessagingEnabled] = useState(true)
  const [advancedConfig, setAdvancedConfig] = useState({
    scrape_session_size: '250',
    scrape_break_minutes_min: '30',
    scrape_break_minutes_max: '45',
    bio_fetch_delay_min: '5',
    bio_fetch_delay_max: '8',
  })
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [showTemplateB, setShowTemplateB] = useState(false)
  const [showTemplateC, setShowTemplateC] = useState(false)
  const [previews, setPreviews] = useState<string[]>([])

  const validate = () => {
    const errs: Record<string, string> = {}
    if (!form.name.trim()) errs.name = 'Il nome è obbligatorio'
    if (sourceType === 'scrape' && form.scrape_mode !== 'dm_threads') {
      const username = form.target_username.replace(/^@/, '').trim()
      if (!username) {
        errs.target_username = "L'username è obbligatorio"
      } else if (!IG_USERNAME_RE.test(username)) {
        errs.target_username = 'Username non valido. Solo lettere, numeri, punti e underscore (max 30 caratteri)'
      }
    } else if (sourceType !== 'scrape' && !importFile) {
      errs.import_file = 'Carica un file con i profili da contattare'
    }
    if (messagingEnabled && !form.base_message_template.trim()) {
      errs.base_message_template = 'Il template è obbligatorio'
    } else if (messagingEnabled) {
      const unknownA = findUnknownPlaceholders(form.base_message_template)
      if (unknownA.length > 0) errs.base_message_template = `Placeholder sconosciuto: ${unknownA[0]} — usa solo {nome} o gruppi {a|b}`
    }
    if (messagingEnabled && showTemplateB && form.message_template_b.trim()) {
      const unknownB = findUnknownPlaceholders(form.message_template_b)
      if (unknownB.length > 0) errs.message_template_b = `Placeholder sconosciuto: ${unknownB[0]} — usa solo {nome} o gruppi {a|b}`
    }
    if (messagingEnabled && showTemplateC && form.message_template_c.trim()) {
      const unknownC = findUnknownPlaceholders(form.message_template_c)
      if (unknownC.length > 0) errs.message_template_c = `Placeholder sconosciuto: ${unknownC[0]} — usa solo {nome} o gruppi {a|b}`
    }
    if (form.daily_limit && Number(form.daily_limit) < 1) errs.daily_limit = 'Il limite deve essere almeno 1'
    if (form.scrape_daily_limit && Number(form.scrape_daily_limit) < 1) errs.scrape_daily_limit = 'Il cap deve essere almeno 1'
    return errs
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const errs = validate()
    if (Object.keys(errs).length > 0) { setErrors(errs); return }
    setErrors({})
    setLoading(true)
    try {
      const campaign = await api.campaigns.create({
        name: form.name.trim(),
        source_type: sourceType,
        target_username: (sourceType === 'scrape' && form.scrape_mode !== 'dm_threads')
          ? form.target_username.replace(/^@/, '').trim()
          : null,
        scrape_mode: form.scrape_mode,
        ...(form.scrape_mode === 'dm_threads' ? { inbox_engine: inboxEngine } : {}),
        bio_engine: bioEngine,
        messaging_enabled: messagingEnabled,
        base_message_template: messagingEnabled ? form.base_message_template : null,
        message_template_b: messagingEnabled && showTemplateB && form.message_template_b.trim() ? form.message_template_b : null,
        message_template_c: messagingEnabled && showTemplateC && form.message_template_c.trim() ? form.message_template_c : null,
        ai_prompt_context: messagingEnabled && form.ai_prompt_context ? form.ai_prompt_context : undefined,
        ai_enabled: messagingEnabled ? form.ai_enabled : false,
        ai_system_prompt: messagingEnabled && form.ai_enabled && form.ai_system_prompt.trim() ? form.ai_system_prompt : undefined,
        daily_limit: form.daily_limit ? Number(form.daily_limit) : null,
        scrape_daily_limit: form.scrape_daily_limit ? Number(form.scrape_daily_limit) : null,
        require_approval: form.require_approval,
        approval_sample_size: form.approval_sample_size ? Number(form.approval_sample_size) : 5,
        scrape_session_size: Number(advancedConfig.scrape_session_size) || 250,
        scrape_break_minutes_min: Number(advancedConfig.scrape_break_minutes_min) || 30,
        scrape_break_minutes_max: Number(advancedConfig.scrape_break_minutes_max) || 45,
        bio_fetch_delay_min: Number(advancedConfig.bio_fetch_delay_min) || 5,
        bio_fetch_delay_max: Number(advancedConfig.bio_fetch_delay_max) || 8,
      })
      if (sourceType === 'import' && importFile) {
        const res = await api.campaigns.importProfiles(campaign.id, importFile)
        toast.success(`Campagna creata! ${res.inserted} profili importati`)
      } else {
        toast.success('Campagna creata!')
      }
      router.push(`/campaigns/${campaign.id}`)
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore nella creazione')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="max-w-2xl">
      <div className="flex items-center gap-3 mb-6">
        <Link href="/campaigns">
          <Button variant="ghost" size="sm" className="text-gray-400">
            <ArrowLeft className="w-4 h-4" />
          </Button>
        </Link>
        <h1 className="text-2xl font-bold text-white">Nuova Campagna</h1>
      </div>

      <form onSubmit={handleSubmit}>
        <Card className="bg-gray-900 border-gray-800">
          <CardHeader>
            <CardTitle className="text-base text-gray-100">Configurazione campagna</CardTitle>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-2">
              <label className="text-sm text-gray-300 font-medium">Sorgente profili</label>
              <div className="flex gap-3">
                <button type="button" onClick={() => setSourceType('scrape')}
                  className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors ${sourceType === 'scrape' ? 'bg-purple-600/20 border-purple-500 text-purple-300' : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'}`}>
                  Scraping pagina
                  <span className="block text-xs font-normal mt-0.5 opacity-70">Follower/following di una pagina target</span>
                </button>
                <button type="button" onClick={() => setSourceType('import')}
                  className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors ${sourceType === 'import' ? 'bg-purple-600/20 border-purple-500 text-purple-300' : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'}`}>
                  Lista importata
                  <span className="block text-xs font-normal mt-0.5 opacity-70">File di URL/username</span>
                </button>
              </div>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">Nome campagna *</label>
              <Input
                placeholder="Es. Outreach Moda Estate 2026"
                value={form.name}
                onChange={e => { setForm(f => ({ ...f, name: e.target.value })); setErrors(er => ({ ...er, name: '' })) }}
                className={`bg-gray-800 border-gray-700 text-white ${errors.name ? 'border-red-600' : ''}`}
              />
              {errors.name && <p className="text-xs text-red-400">{errors.name}</p>}
            </div>

            {sourceType === 'scrape' && (<>
            <div className="space-y-2">
              <label className="text-sm text-gray-300 font-medium">Modalità raccolta profili</label>
              <div className="flex gap-3">
                <button
                  type="button"
                  onClick={() => setForm(f => ({ ...f, scrape_mode: 'followers' }))}
                  className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors ${
                    form.scrape_mode === 'followers'
                      ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                      : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                  }`}
                >
                  Follower
                  <span className="block text-xs font-normal mt-0.5 opacity-70">Chi segue la pagina target</span>
                </button>
                <button
                  type="button"
                  onClick={() => setForm(f => ({ ...f, scrape_mode: 'following' }))}
                  className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors ${
                    form.scrape_mode === 'following'
                      ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                      : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                  }`}
                >
                  Following
                  <span className="block text-xs font-normal mt-0.5 opacity-70">Chi viene seguito dalla pagina target</span>
                </button>
                <button
                  type="button"
                  onClick={() => setForm(f => ({ ...f, scrape_mode: 'dm_threads' }))}
                  className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors ${
                    form.scrape_mode === 'dm_threads'
                      ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                      : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                  }`}
                >
                  DM già avviati (inbox)
                  <span className="block text-xs font-normal mt-0.5 opacity-70">Thread DM esistenti nell&apos;account</span>
                </button>
              </div>
            </div>

            {form.scrape_mode !== 'dm_threads' && (
            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">Pagina target (username) *</label>
              <Input
                placeholder="@nomepagina o nomepagina"
                value={form.target_username}
                onChange={e => { setForm(f => ({ ...f, target_username: e.target.value })); setErrors(er => ({ ...er, target_username: '' })) }}
                className={`bg-gray-800 border-gray-700 text-white ${errors.target_username ? 'border-red-600' : ''}`}
              />
              {errors.target_username
                ? <p className="text-xs text-red-400">{errors.target_username}</p>
                : <p className="text-xs text-gray-500">
                    {form.scrape_mode === 'following'
                      ? 'I profili seguiti da questa pagina verranno contattati'
                      : 'I follower di questa pagina verranno contattati'}
                  </p>
              }
            </div>
            )}

            {form.scrape_mode === 'dm_threads' && (
            <div className="space-y-2 rounded-lg border border-gray-700/50 bg-gray-800/30 p-3">
              <label className="text-sm text-gray-300 font-medium block">Engine estrazione lista</label>
              <label className="flex items-center gap-2 opacity-50 cursor-not-allowed">
                <input
                  type="radio"
                  name="inboxEngine"
                  value="browser"
                  checked={false}
                  disabled
                  className="accent-purple-500"
                />
                <span className="text-sm text-gray-400">
                  🛡️ Browser (non disponibile)
                  <span className="ml-1 text-xs text-gray-500">— deprecato: l&apos;inbox usa sempre l&apos;API</span>
                </span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="inboxEngine"
                  value="api"
                  checked={inboxEngine === 'api'}
                  onChange={() => setInboxEngine('api')}
                  className="accent-purple-500"
                />
                <span className="text-sm text-gray-300">
                  ⚡ API
                  <span className="ml-1 text-xs text-gray-500">— unico motore supportato per l&apos;inbox</span>
                </span>
              </label>
            </div>
            )}
            </>)}

            {sourceType === 'import' && (
              <div className="space-y-1.5">
                <label className="text-sm text-gray-300 font-medium">File profili (.txt / .csv) *</label>
                <Input type="file" accept=".txt,.csv"
                  onChange={e => { setImportFile(e.target.files?.[0] ?? null); setErrors(er => ({ ...er, import_file: '' })) }}
                  className={`bg-gray-800 border-gray-700 text-white ${errors.import_file ? 'border-red-600' : ''}`} />
                {errors.import_file
                  ? <p className="text-xs text-red-400">{errors.import_file}</p>
                  : <p className="text-xs text-gray-500">Un profilo per riga (URL Instagram o username). Serve un account con ruolo scraping/both assegnato per recuperare le bio.</p>}
              </div>
            )}

            {/* Messaging toggle — quando OFF la campagna raccoglie solo lead */}
            <div className="space-y-2 rounded-lg border border-gray-700/50 bg-gray-800/30 p-3">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-gray-300 font-medium">Invia messaggi</p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {messagingEnabled
                      ? 'I profili raccolti riceveranno un DM personalizzato'
                      : 'Campagna solo raccolta lead — nessun messaggio verrà inviato'}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => { setMessagingEnabled(v => !v); setErrors(er => ({ ...er, base_message_template: '' })) }}
                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full transition-colors ${messagingEnabled ? 'bg-purple-600' : 'bg-gray-600'}`}
                >
                  <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform mt-0.5 ${messagingEnabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
                </button>
              </div>
            </div>

            {messagingEnabled && (<>
            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">Template messaggio base *</label>
              <Textarea
                placeholder="Ciao! Ho visto che segui @nomepagina e volevo presentarti..."
                value={form.base_message_template}
                onChange={e => { setForm(f => ({ ...f, base_message_template: e.target.value })); setErrors(er => ({ ...er, base_message_template: '' })) }}
                rows={4}
                className={`bg-gray-800 border-gray-700 text-white resize-none ${errors.base_message_template ? 'border-red-600' : ''}`}
              />
              {errors.base_message_template
                ? <p className="text-xs text-red-400">{errors.base_message_template}</p>
                : <p className="text-xs text-gray-500">
                    {'{nome}'} = nome del destinatario · {'{Ciao|Hey|Salve}'} = il bot sceglie una variante a caso per ogni DM
                  </p>
              }
              <button type="button" className="text-xs text-blue-400 hover:text-blue-300"
                onClick={() => setPreviews([1, 2, 3].map(() => renderPreview(form.base_message_template)))}>
                ⚡ Anteprima varianti
              </button>
              {previews.length > 0 && (
                <div className="space-y-1">
                  {previews.map((p, i) => (
                    <p key={i} className="text-xs text-gray-400 bg-gray-800 rounded p-2 whitespace-pre-wrap">{p}</p>
                  ))}
                </div>
              )}
            </div>

            {/* M10: A/B testing — optional second template */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <label className="text-sm text-gray-300 font-medium">Template B (A/B test)</label>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="text-xs text-purple-400 hover:text-purple-300 h-auto p-0"
                  onClick={() => setShowTemplateB(v => !v)}
                >
                  {showTemplateB ? '— Rimuovi template B' : '+ Aggiungi template B'}
                </Button>
              </div>
              {showTemplateB && (
                <>
                  <Textarea
                    placeholder="Variante B del messaggio — il bot sceglierà a caso tra i template attivi per ogni DM"
                    value={form.message_template_b}
                    onChange={e => { setForm(f => ({ ...f, message_template_b: e.target.value })); setErrors(er => ({ ...er, message_template_b: '' })) }}
                    rows={4}
                    className={`bg-gray-800 border-gray-700 text-white resize-none ${errors.message_template_b ? 'border-red-600' : ''}`}
                  />
                  {errors.message_template_b
                    ? <p className="text-xs text-red-400">{errors.message_template_b}</p>
                    : <p className="text-xs text-gray-500">
                        Se compilato, il bot sceglie a caso tra i template attivi per ogni DM.
                        I risultati sono visibili nel dettaglio campagna.
                      </p>
                  }
                </>
              )}
            </div>

            {/* Template mode: optional third variant — A/B/C rendering locale (spintax), nessun costo AI */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <label className="text-sm text-gray-300 font-medium">Template C</label>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="text-xs text-purple-400 hover:text-purple-300 h-auto p-0"
                  onClick={() => setShowTemplateC(v => !v)}
                >
                  {showTemplateC ? '— Rimuovi template C' : '+ Aggiungi template C'}
                </Button>
              </div>
              {showTemplateC && (
                <>
                  <Textarea
                    placeholder="Variante C del messaggio — il bot sceglierà a caso tra i template attivi per ogni DM"
                    value={form.message_template_c}
                    onChange={e => { setForm(f => ({ ...f, message_template_c: e.target.value })); setErrors(er => ({ ...er, message_template_c: '' })) }}
                    rows={4}
                    className={`bg-gray-800 border-gray-700 text-white resize-none ${errors.message_template_c ? 'border-red-600' : ''}`}
                  />
                  {errors.message_template_c
                    ? <p className="text-xs text-red-400">{errors.message_template_c}</p>
                    : <p className="text-xs text-gray-500">
                        I follower riceveranno a caso uno dei template attivi (A/B/C). Risultati visibili nel dettaglio campagna.
                      </p>
                  }
                </>
              )}
            </div>

            <div className="flex items-center justify-between rounded-lg border border-gray-700 p-3">
              <div>
                <p className="text-sm text-gray-200 font-medium">Personalizza con AI</p>
                <p className="text-xs text-gray-500">
                  OFF (default): il template parte così com&apos;è, con le varianti {'{a|b}'} — zero quota AI.
                  ON: l&apos;AI riscrive il messaggio sulla bio del destinatario.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setForm(f => ({ ...f, ai_enabled: !f.ai_enabled }))}
                className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full transition-colors ${form.ai_enabled ? 'bg-purple-600' : 'bg-gray-600'}`}
              >
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform mt-0.5 ${form.ai_enabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
              </button>
            </div>
            {form.ai_enabled && (
              <>
                <div className="space-y-1.5">
                  <label className="text-sm text-gray-300 font-medium">Contesto aggiuntivo per l&apos;AI</label>
                  <Textarea
                    placeholder="Es. Siamo un brand di moda sostenibile. Vogliamo un tono amichevole e non commerciale."
                    value={form.ai_prompt_context}
                    onChange={e => setForm(f => ({ ...f, ai_prompt_context: e.target.value }))}
                    rows={3}
                    className="bg-gray-800 border-gray-700 text-white resize-none"
                  />
                  <p className="text-xs text-gray-500">Opzionale. Aiuta l&apos;AI a capire il contesto del brand e il tono desiderato</p>
                </div>
                <div className="space-y-2">
                  <label className="text-sm text-gray-300 font-medium">Istruzioni AI (opzionale)</label>
                  <Textarea rows={3} value={form.ai_system_prompt}
                    onChange={e => setForm(f => ({ ...f, ai_system_prompt: e.target.value }))}
                    placeholder="Sovrascrive le istruzioni globali solo per questa campagna. Es: tono informale, max 3 frasi, niente emoji."
                    className="bg-gray-800 border-gray-700 text-white resize-none" />
                </div>
              </>
            )}
            </>)}

            <div className="space-y-1.5">
              <label className="text-sm text-gray-300 font-medium">Limite DM giornaliero campagna</label>
              <Input
                type="number"
                placeholder="Es. 50 (lascia vuoto = nessun limite)"
                value={form.daily_limit}
                onChange={e => setForm(f => ({ ...f, daily_limit: e.target.value }))}
                min={1}
                max={500}
                className="bg-gray-800 border-gray-700 text-white"
              />
              <p className="text-xs text-gray-500">
                Opzionale. Numero massimo di DM inviabili al giorno sommando tutti gli account assegnati.
                Ogni account ha anche il proprio limite individuale.
              </p>
            </div>

            {/* M15 rev: approval sampling toggle */}
            <div className="space-y-2 rounded-lg border border-gray-700/50 bg-gray-800/30 p-3">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-gray-300 font-medium">Approvazione messaggi</p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Dopo la pre-generazione, campiona N messaggi per la tua revisione prima dell&apos;invio
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setForm(f => ({ ...f, require_approval: !f.require_approval }))}
                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full transition-colors ${form.require_approval ? 'bg-purple-600' : 'bg-gray-600'}`}
                >
                  <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform mt-0.5 ${form.require_approval ? 'translate-x-4' : 'translate-x-0.5'}`} />
                </button>
              </div>
              {form.require_approval && (
                <div className="flex items-center gap-3 mt-2">
                  <label className="text-xs text-gray-400 whitespace-nowrap">Messaggi da revisionare:</label>
                  <Input
                    type="number"
                    value={form.approval_sample_size}
                    onChange={e => setForm(f => ({ ...f, approval_sample_size: e.target.value }))}
                    min={1}
                    max={50}
                    className="bg-gray-800 border-gray-700 text-white h-7 text-xs w-20"
                  />
                </div>
              )}
            </div>

            {/* Advanced scraping config */}
            <div className="rounded-lg border border-gray-700/50 bg-gray-800/30">
              <button
                type="button"
                onClick={() => setShowAdvanced(v => !v)}
                className="w-full flex items-center justify-between p-3 text-sm text-gray-400 hover:text-gray-300"
              >
                <span>Configurazione avanzata scraping</span>
                {showAdvanced ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
              </button>
              {showAdvanced && (
                <div className="px-3 pb-3 space-y-3 border-t border-gray-700/50 pt-3">
                  <p className="text-xs text-gray-500">Controlla velocità e pause durante la raccolta profili (anti-ban)</p>
                  <div className="space-y-1">
                    <label className="text-xs text-gray-400">Cap lookup/giorno per account (anti-ban)</label>
                    <Input type="number" placeholder="Vuoto = nessun cap"
                      value={form.scrape_daily_limit}
                      onChange={e => { setForm(f => ({ ...f, scrape_daily_limit: e.target.value })); setErrors(er => ({ ...er, scrape_daily_limit: '' })) }}
                      min={1} max={5000}
                      className={`bg-gray-800 border-gray-700 text-white h-8 text-xs ${errors.scrape_daily_limit ? 'border-red-600' : ''}`} />
                    {errors.scrape_daily_limit
                      ? <p className="text-xs text-red-400">{errors.scrape_daily_limit}</p>
                      : <p className="text-xs text-gray-600">Opzionale. Numero massimo di profili risolti/giorno per ogni account scraping.</p>}
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs text-gray-400">Motore Fase Bio</label>
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() => setBioEngine('api')}
                        className={`flex-1 py-1.5 px-2 rounded border text-xs font-medium transition-colors ${
                          bioEngine === 'api'
                            ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                            : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                        }`}
                      >
                        ⚡ API (veloce)
                      </button>
                      <button
                        type="button"
                        onClick={() => setBioEngine('browser')}
                        className={`flex-1 py-1.5 px-2 rounded border text-xs font-medium transition-colors ${
                          bioEngine === 'browser'
                            ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                            : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                        }`}
                      >
                        🛡️ Browser (prudente)
                      </button>
                    </div>
                    <p className="text-xs text-gray-600">
                      {bioEngine === 'browser'
                        ? 'Apre ogni profilo in un browser reale — più lento, nessun consumo del cap API.'
                        : 'Usa l’API instagrapi — più veloce, consuma il cap di lookup/giorno.'}
                    </p>
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <label className="text-xs text-gray-400">Profili per sessione</label>
                      <Input type="number" value={advancedConfig.scrape_session_size}
                        onChange={e => setAdvancedConfig(c => ({ ...c, scrape_session_size: e.target.value }))}
                        min={10} max={5000} className="bg-gray-800 border-gray-700 text-white h-8 text-xs" />
                      <p className="text-xs text-gray-600">Default: 250</p>
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-gray-400">Pausa sessione (min)</label>
                      <div className="flex gap-1 items-center">
                        <Input type="number" value={advancedConfig.scrape_break_minutes_min}
                          onChange={e => setAdvancedConfig(c => ({ ...c, scrape_break_minutes_min: e.target.value }))}
                          min={5} max={240} className="bg-gray-800 border-gray-700 text-white h-8 text-xs" />
                        <span className="text-gray-500 text-xs">–</span>
                        <Input type="number" value={advancedConfig.scrape_break_minutes_max}
                          onChange={e => setAdvancedConfig(c => ({ ...c, scrape_break_minutes_max: e.target.value }))}
                          min={5} max={240} className="bg-gray-800 border-gray-700 text-white h-8 text-xs" />
                      </div>
                      <p className="text-xs text-gray-600">Default: 30–45 min</p>
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-gray-400">Delay bio fetch (sec)</label>
                      <div className="flex gap-1 items-center">
                        <Input type="number" value={advancedConfig.bio_fetch_delay_min}
                          onChange={e => setAdvancedConfig(c => ({ ...c, bio_fetch_delay_min: e.target.value }))}
                          min={1} max={60} step={0.5} className="bg-gray-800 border-gray-700 text-white h-8 text-xs" />
                        <span className="text-gray-500 text-xs">–</span>
                        <Input type="number" value={advancedConfig.bio_fetch_delay_max}
                          onChange={e => setAdvancedConfig(c => ({ ...c, bio_fetch_delay_max: e.target.value }))}
                          min={1} max={120} step={0.5} className="bg-gray-800 border-gray-700 text-white h-8 text-xs" />
                      </div>
                      <p className="text-xs text-gray-600">Default: 5–8 sec</p>
                    </div>
                    <p className="col-span-2 text-xs text-amber-500/90 bg-amber-950/30 border border-amber-800/40 rounded px-2 py-1.5 mt-1">
                      ⚠️ <strong>Questi tempi valgono per OGNI lead estratto, condivisi tra tutti gli account scraping.</strong>{' '}
                      Con più account il delay si applica tra un account e il successivo: ogni singolo account aspetta circa
                      (n° account × questo valore) tra i suoi lead. Esempio: 2 account e vuoi ~6–10s per account → imposta <strong>3–5s</strong>.
                    </p>
                  </div>
                </div>
              )}
            </div>

            <Button type="submit" disabled={loading} className="w-full bg-purple-600 hover:bg-purple-700">
              {loading ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : null}
              Crea campagna
            </Button>
          </CardContent>
        </Card>
      </form>
    </div>
  )
}
