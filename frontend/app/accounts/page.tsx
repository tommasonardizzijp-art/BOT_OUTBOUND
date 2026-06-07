'use client'

import { useState, useEffect } from 'react'
import useSWR from 'swr'
import { api } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { toast } from 'sonner'
import {
  Plus, Trash2, Loader2, ShieldAlert, Clock, Ban, CheckCircle,
  LogIn, Globe, Pencil, Power, PowerOff, BarChart2, ChevronDown, ChevronUp,
  Inbox, RefreshCw, Eraser, AlertTriangle, Wifi, Smartphone
} from 'lucide-react'
import type { Account, AccountStatus, AccountMetrics, DMCount, ProxyTestResult } from '@/lib/types'
import { formatDistanceToNow, formatTime } from '@/lib/dateUtils'
import { Skeleton } from '@/components/ui/skeleton'

const STATUS_ICON: Record<AccountStatus, React.ReactNode> = {
  active: <CheckCircle className="w-4 h-4 text-green-400" />,
  warming_up: <Clock className="w-4 h-4 text-blue-400" />,
  cooldown: <Clock className="w-4 h-4 text-yellow-400" />,
  banned: <Ban className="w-4 h-4 text-red-400" />,
  challenge_required: <ShieldAlert className="w-4 h-4 text-orange-400" />,
  disabled: <Ban className="w-4 h-4 text-gray-500" />,
}

const STATUS_LABEL: Record<AccountStatus, string> = {
  active: 'Attivo',
  warming_up: 'Warm-up',
  cooldown: 'Cooldown',
  banned: 'Bannato',
  challenge_required: 'Challenge',
  disabled: 'Disabilitato',
}

function maskProxyUrl(url: string): string {
  // Mask password in proxy URL: http://user:pass@host:port -> http://user:****@host:port
  try {
    const u = new URL(url)
    const host = `${u.hostname}${u.port ? ':' + u.port : ''}`
    if (u.username) {
      const pwMask = u.password ? ':****' : ''
      return `${u.protocol}//${u.username}${pwMask}@${host}`
    }
    return `${u.protocol}//${host}`
  } catch {
    return url.replace(/:([^:@/]+)@/, ':****@')
  }
}

export default function AccountsPage() {
  const { data: accounts, mutate } = useSWR('accounts', api.accounts.list, { refreshInterval: 8000 })
  const [open, setOpen] = useState(false)
  const [metricsOpenId, setMetricsOpenId] = useState<string | null>(null)
  const [dmOpenId, setDmOpenId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [form, setForm] = useState({ username: '', password: '', proxy: '', daily_message_limit: 20, notes: '' })
  const [testResult, setTestResult] = useState<Record<string, ProxyTestResult | 'loading'>>({})

  const handleTest = async (acc: Account) => {
    setTestResult(r => ({ ...r, [acc.id]: 'loading' }))
    try {
      const res = await api.accounts.testConnection(acc.id)
      setTestResult(r => ({ ...r, [acc.id]: res }))
      if (res.ok) {
        toast.success(`@${acc.username}: ${res.egress_ip}${res.mobile ? ' · mobile' : ''}`)
      } else {
        toast.error(`@${acc.username}: ${res.error}`)
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Errore'
      setTestResult(r => ({
        ...r,
        [acc.id]: {
          ok: false, via: acc.proxy ? 'proxy' : 'direct', proxy: acc.proxy ?? null,
          account_id: acc.id, username: acc.username, error: msg,
        },
      }))
      toast.error(msg)
    }
  }

  const isValidProxy = (url: string) =>
    /^https?:\/\/([^@]+@)?[^:]+:\d+$/.test(url)

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (form.proxy && !isValidProxy(form.proxy)) {
      toast.error('Formato proxy non valido', { description: 'Usa: http://host:porta oppure http://user:pass@host:porta' })
      return
    }
    setLoading(true)
    try {
      await api.accounts.create({
        username: form.username,
        password: form.password,
        proxy: form.proxy || undefined,
        daily_message_limit: form.daily_message_limit,
        notes: form.notes || undefined,
      })
      toast.success(`Account @${form.username} aggiunto!`)
      setOpen(false)
      setForm({ username: '', password: '', proxy: '', daily_message_limit: 20, notes: '' })
      await mutate()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  const handleDelete = async (id: string, username: string) => {
    if (!confirm(`Eliminare account @${username}?`)) return
    try {
      await api.accounts.delete(id)
      toast.success('Account eliminato')
      await mutate()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    }
  }

  const handleToggleStatus = async (acc: Account) => {
    const newStatus = (acc.status === 'disabled') ? 'active' : 'disabled'
    try {
      await api.accounts.update(acc.id, { status: newStatus })
      toast.success(newStatus === 'disabled' ? `@${acc.username} disabilitato` : `@${acc.username} riattivato`)
      await mutate()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Account Instagram</h1>
          <p className="text-gray-400 text-base mt-1">{accounts?.length ?? 0} account configurati</p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger
            render={<Button className="bg-purple-600 hover:bg-purple-700" type="button" />}
          >
            <Plus className="w-4 h-4 mr-2" /> Aggiungi account
          </DialogTrigger>
          <DialogContent className="bg-gray-900 border-gray-800 text-white">
            <DialogHeader>
              <DialogTitle>Aggiungi account Instagram</DialogTitle>
            </DialogHeader>
            <form onSubmit={handleCreate} className="space-y-4">
              <div className="space-y-1.5">
                <label className="text-sm text-gray-300">Username *</label>
                <Input placeholder="username_instagram" value={form.username}
                  onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
                  required className="bg-gray-800 border-gray-700 text-white" />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm text-gray-300">Password *</label>
                <Input type="password" placeholder="••••••••" value={form.password}
                  onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                  required className="bg-gray-800 border-gray-700 text-white" />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm text-gray-300">Proxy (opzionale)</label>
                <Input placeholder="http://user:pass@host:porta" value={form.proxy}
                  onChange={e => setForm(f => ({ ...f, proxy: e.target.value }))}
                  className="bg-gray-800 border-gray-700 text-white" />
                <p className="text-xs text-gray-600">Formato: <code>http://host:porta</code> oppure <code>http://user:pass@host:porta</code></p>
              </div>
              <div className="space-y-1.5">
                <label className="text-sm text-gray-300">Limite DM/giorno</label>
                <Input type="number" min={1} max={100} value={form.daily_message_limit}
                  onChange={e => setForm(f => ({ ...f, daily_message_limit: Number(e.target.value) }))}
                  className="bg-gray-800 border-gray-700 text-white" />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm text-gray-300">Note</label>
                <Input placeholder="Note opzionali" value={form.notes}
                  onChange={e => setForm(f => ({ ...f, notes: e.target.value }))}
                  className="bg-gray-800 border-gray-700 text-white" />
              </div>
              <Button type="submit" disabled={loading} className="w-full bg-purple-600 hover:bg-purple-700">
                {loading && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
                Aggiungi account
              </Button>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        {!accounts && (
          <>
            {[1, 2].map(i => (
              <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl p-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <Skeleton className="w-4 h-4 rounded-full" />
                    <div className="space-y-1.5">
                      <Skeleton className="h-4 w-36" />
                      <Skeleton className="h-3 w-24" />
                    </div>
                  </div>
                  <Skeleton className="h-8 w-28" />
                </div>
              </div>
            ))}
          </>
        )}
        {accounts?.map((acc: Account) => (
          <Card key={acc.id} className="bg-gray-900 border-gray-800">
            <CardContent className="py-4 space-y-3">
              {/* Header: status + name + status badges */}
              <div className="flex items-center gap-3 min-w-0">
                <span className="flex-shrink-0">{STATUS_ICON[acc.status]}</span>
                <span className="font-semibold text-lg text-white truncate min-w-0" title={`@${acc.username}`}>
                  @{acc.username}
                </span>
                <div className="flex items-center gap-1.5 flex-shrink-0 flex-wrap">
                  <Badge variant="outline" className="text-xs border-gray-700 text-gray-400">
                    {STATUS_LABEL[acc.status]}
                  </Badge>
                  {acc.warmup_day > 0 && (
                    <Badge variant="outline" className="text-xs border-blue-800 text-blue-400">
                      Warm-up g.{acc.warmup_day}
                    </Badge>
                  )}
                </div>
              </div>

              {/* Stats grid: full-width, fixed columns, no wrap inside cell */}
              <div className="grid grid-cols-3 gap-x-4 text-sm">
                <div className="flex flex-col min-w-0">
                  <span className="text-xs text-gray-500 uppercase tracking-wide">DM oggi</span>
                  <span className="text-gray-200 font-medium truncate">
                    {acc.daily_message_count}/{acc.daily_message_limit}
                  </span>
                </div>
                <div className="flex flex-col min-w-0">
                  <span className="text-xs text-gray-500 uppercase tracking-wide">Totali</span>
                  <span className="text-gray-200 font-medium truncate">{acc.total_messages_sent}</span>
                </div>
                <div className="flex flex-col min-w-0">
                  <span className="text-xs text-gray-500 uppercase tracking-wide">Ultima attività</span>
                  <span className="text-gray-200 font-medium truncate" title={acc.last_activity_at ?? ''}>
                    {acc.last_activity_at ? formatDistanceToNow(acc.last_activity_at) : '—'}
                  </span>
                </div>
              </div>

              {acc.cooldown_until && acc.status === 'cooldown' && (
                <p className="text-xs text-yellow-500">
                  Cooldown fino alle {formatTime(acc.cooldown_until)}
                </p>
              )}
              {acc.proxy && (
                <p className="text-xs text-gray-600 truncate" title="Proxy configurato">
                  Proxy: {maskProxyUrl(acc.proxy)}
                </p>
              )}
              {acc.notes && <p className="text-xs text-gray-600 truncate">Note: {acc.notes}</p>}

              {/* Actions row: full width below stats, wraps cleanly */}
              <div className="flex items-center gap-1 flex-wrap pt-2 border-t border-gray-800/60">
                <div className="flex items-center gap-1 flex-wrap flex-1 min-w-0">
                  {/* M8 lite: DM inbox count toggle — only for accounts with session */}
                  {acc.last_login_at && acc.status !== 'banned' && (
                    <Button size="sm" variant="ghost"
                      className="text-gray-500 hover:text-blue-400 hover:bg-gray-800"
                      onClick={() => setDmOpenId(dmOpenId === acc.id ? null : acc.id)}
                      title="Visualizza notifiche DM">
                      <Inbox className="w-4 h-4" />
                    </Button>
                  )}

                  {/* Metrics toggle */}
                  <Button size="sm" variant="ghost"
                    className="text-gray-500 hover:text-gray-300 hover:bg-gray-800"
                    onClick={() => setMetricsOpenId(metricsOpenId === acc.id ? null : acc.id)}
                    title="Metriche account">
                    {metricsOpenId === acc.id
                      ? <ChevronUp className="w-4 h-4" />
                      : <BarChart2 className="w-4 h-4" />
                    }
                  </Button>

                  {/* Edit button */}
                  <EditAccountDialog account={acc} onSuccess={mutate} />

                  {/* Toggle enable/disable */}
                  {(acc.status === 'active' || acc.status === 'warming_up') && (
                    <Button size="sm" variant="ghost" className="text-gray-500 hover:text-gray-300 hover:bg-gray-800"
                      onClick={() => handleToggleStatus(acc)} title="Disabilita">
                      <PowerOff className="w-4 h-4" />
                    </Button>
                  )}
                  {acc.status === 'cooldown' && (
                    <ForceCancelCooldownButton accountId={acc.id} username={acc.username} onSuccess={mutate} />
                  )}
                  {acc.status === 'disabled' && (
                    <Button size="sm" variant="ghost" className="text-green-500 hover:text-green-400 hover:bg-green-900/20"
                      onClick={() => handleToggleStatus(acc)} title="Riattiva">
                      <Power className="w-4 h-4" />
                    </Button>
                  )}

                  {/* Login buttons */}
                  {(acc.status === 'active' || acc.status === 'challenge_required') && (
                    <ManualLoginButton
                      accountId={acc.id}
                      username={acc.username}
                      hasSession={!!acc.last_login_at}
                      onSuccess={mutate}
                    />
                  )}
                  {!acc.last_login_at && acc.status === 'active' && (
                    <ApiLoginButton accountId={acc.id} username={acc.username} onSuccess={mutate} />
                  )}
                  {acc.status === 'active' && acc.last_login_at && (
                    <BrowseSessionButton accountId={acc.id} username={acc.username} />
                  )}

                  {/* Testa connessione: IP/egress reale via proxy (o WiFi se nessun proxy) */}
                  <Button size="sm" variant="outline" className="border-cyan-800 text-cyan-400 hover:bg-cyan-900/20"
                    onClick={() => handleTest(acc)} disabled={testResult[acc.id] === 'loading'}
                    title="Verifica l'IP di uscita reale di questo account (proxy o WiFi)">
                    {testResult[acc.id] === 'loading'
                      ? <><Loader2 className="w-4 h-4 mr-1 animate-spin" />Test...</>
                      : <><Wifi className="w-4 h-4 mr-1" />Testa IP</>}
                  </Button>

                  {/* Reset session — wipe browser profile + instagrapi session */}
                  <ResetSessionButton
                    accountId={acc.id}
                    username={acc.username}
                    accountStatus={acc.status}
                    hasSession={!!acc.last_login_at}
                    onSuccess={mutate}
                  />

                  {/* Delete */}
                  <Button size="sm" variant="ghost" className="text-red-500 hover:text-red-400 hover:bg-red-900/20"
                    onClick={() => handleDelete(acc.id, acc.username)}>
                    <Trash2 className="w-4 h-4" />
                  </Button>
                </div>
              </div>

              {acc.status === 'challenge_required' && (
                <div className="mt-3 p-3 bg-orange-900/20 border border-orange-800 rounded-lg">
                  <p className="text-sm text-orange-400 font-medium">Instagram richiede verifica</p>
                  <p className="text-xs text-orange-200/80 mt-1.5 leading-relaxed">
                    Se l&apos;errore è <code>ufac_www_bloks</code> (UFAC challenge): il codice SMS NON
                    risolve. Usa il bottone <strong>Reset</strong> per pulire la sessione, poi rifai
                    Login Browser. Per altre challenge classiche usa il codice qui sotto.
                  </p>
                  <p className="text-xs text-gray-400 mt-2">
                    Controlla l&apos;email/SMS associato all&apos;account e inserisci il codice ricevuto
                  </p>
                  <ChallengeForm accountId={acc.id} onSuccess={mutate} />
                </div>
              )}

              {/* M8 lite: DM inbox panel */}
              {dmOpenId === acc.id && (
                <DMCountPanel accountId={acc.id} />
              )}

              {/* M9: Metrics panel */}
              {metricsOpenId === acc.id && (
                <AccountMetricsPanel accountId={acc.id} />
              )}

              {/* Risultato test connessione */}
              {testResult[acc.id] && testResult[acc.id] !== 'loading' && (
                <TestResultPanel result={testResult[acc.id] as ProxyTestResult} />
              )}
            </CardContent>
          </Card>
        ))}

        {accounts?.length === 0 && (
          <Card className="bg-gray-900 border-gray-800">
            <CardContent className="py-12 text-center">
              <p className="text-gray-500">Nessun account configurato.</p>
              <p className="text-gray-600 text-sm mt-1">Aggiungi almeno un account per iniziare.</p>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  )
}

/* ---------- Edit Account Dialog ---------- */

function EditAccountDialog({ account, onSuccess }: { account: Account; onSuccess: () => void }) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [form, setForm] = useState({
    proxy: account.proxy ?? '',
    daily_message_limit: account.daily_message_limit,
    notes: account.notes ?? '',
  })

  // Reset form when dialog opens
  const handleOpenChange = (isOpen: boolean) => {
    setOpen(isOpen)
    if (isOpen) {
      setForm({
        proxy: account.proxy ?? '',
        daily_message_limit: account.daily_message_limit,
        notes: account.notes ?? '',
      })
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    try {
      // Send empty strings (not undefined) so backend knows to clear the field.
      // Backend distinguishes "field absent" (keep) from "explicit empty" (clear).
      await api.accounts.update(account.id, {
        proxy: form.proxy.trim(),
        daily_message_limit: form.daily_message_limit,
        notes: form.notes.trim(),
      })
      toast.success(`@${account.username} aggiornato`)
      setOpen(false)
      onSuccess()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger
        render={<Button size="sm" variant="ghost" className="text-gray-400 hover:text-gray-200 hover:bg-gray-800" type="button" title="Modifica" />}
      >
        <Pencil className="w-4 h-4" />
      </DialogTrigger>
      <DialogContent className="bg-gray-900 border-gray-800 text-white">
        <DialogHeader>
          <DialogTitle>Modifica @{account.username}</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-sm text-gray-300">Proxy</label>
            <Input placeholder="http://user:pass@host:port" value={form.proxy}
              onChange={e => setForm(f => ({ ...f, proxy: e.target.value }))}
              className="bg-gray-800 border-gray-700 text-white" />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm text-gray-300">Limite DM/giorno</label>
            <Input type="number" min={1} max={200} value={form.daily_message_limit}
              onChange={e => setForm(f => ({ ...f, daily_message_limit: Number(e.target.value) }))}
              className="bg-gray-800 border-gray-700 text-white" />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm text-gray-300">Note</label>
            <Input placeholder="Note opzionali" value={form.notes}
              onChange={e => setForm(f => ({ ...f, notes: e.target.value }))}
              className="bg-gray-800 border-gray-700 text-white" />
          </div>
          <Button type="submit" disabled={loading} className="w-full bg-purple-600 hover:bg-purple-700">
            {loading && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
            Salva modifiche
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/* ---------- Login buttons ---------- */

function ManualLoginButton({ accountId, username, hasSession, onSuccess }: { accountId: string; username: string; hasSession: boolean; onSuccess: () => void }) {
  const [loading, setLoading] = useState(false)

  const handleLogin = async () => {
    setLoading(true)
    try {
      // Fast pre-check (~2-3s) — skip browser if session still valid
      const check = await api.accounts.checkSession(accountId)
      if (check.valid) {
        toast.success(`Sessione già attiva per @${username} — nessun login necessario.`)
        onSuccess()
        return
      }
      // Session expired — open browser for manual re-login
      toast.info('Sessione scaduta. Browser in apertura... Effettua il login su Instagram.', { duration: 8000 })
      await api.accounts.manualLogin(accountId)
      toast.success(`Sessione rinnovata per @${username}!`)
      onSuccess()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Login fallito'
      if (msg.includes('aborted') || msg.includes('abort')) {
        toast.error('Timeout: login non completato in tempo. Riprova.')
      } else {
        toast.error(msg)
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <Button size="sm" variant="outline" className="border-green-800 text-green-400 hover:bg-green-900/20"
      onClick={handleLogin} disabled={loading}>
      {loading
        ? <><Loader2 className="w-4 h-4 mr-1 animate-spin" />In attesa...</>
        : <><Globe className="w-4 h-4 mr-1" />{hasSession ? 'Rinnova sessione' : 'Login Browser'}</>
      }
    </Button>
  )
}

function BrowseSessionButton({ accountId, username }: { accountId: string; username: string }) {
  const [loading, setLoading] = useState(false)

  const handleOpen = async () => {
    if (!confirm(
      `Apre browser per attività manuale su @${username}.\n\n` +
      `Il browser usa stesso profilo + proxy + fingerprint del bot — IG vede device coerente.\n` +
      `Naviga, metti like, scrolla feed. Chiudi il browser quando hai finito.\n\n` +
      `Utile per: warm-up account dormienti, accumulo segnali organici, completamento challenge.\n\n` +
      `Continuare?`
    )) return
    setLoading(true)
    try {
      toast.info(`Browser in apertura per @${username}. Chiudi la finestra quando hai finito.`, { duration: 8000 })
      const res = await api.accounts.browseSession(accountId, 60)
      const mins = Math.floor(res.duration_seconds / 60)
      const secs = res.duration_seconds % 60
      toast.success(`Sessione browse @${username}: ${mins}m ${secs}s (${res.closed_by === 'user' ? 'chiusa da te' : 'timeout'})`)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Sessione fallita'
      toast.error(msg)
    } finally {
      setLoading(false)
    }
  }

  return (
    <Button size="sm" variant="outline" className="border-blue-800 text-blue-400 hover:bg-blue-900/20"
      onClick={handleOpen} disabled={loading}>
      {loading
        ? <><Loader2 className="w-4 h-4 mr-1 animate-spin" />Browser aperto...</>
        : <><Globe className="w-4 h-4 mr-1" />Apri browser</>
      }
    </Button>
  )
}

function ApiLoginButton({ accountId, username, onSuccess }: { accountId: string; username: string; onSuccess: () => void }) {
  const [loading, setLoading] = useState(false)

  const handleLogin = async () => {
    if (!confirm('Il login via API è rischioso (possibile ban IP). Usalo solo se il login browser non funziona. Continuare?')) return
    setLoading(true)
    try {
      await api.accounts.login(accountId)
      toast.success(`Login riuscito per @${username}! Sessione salvata.`)
      onSuccess()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Login fallito')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Button size="sm" variant="outline" className="border-yellow-800 text-yellow-400 hover:bg-yellow-900/20"
      onClick={handleLogin} disabled={loading}>
      {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <><LogIn className="w-4 h-4 mr-1" />Login API</>}
    </Button>
  )
}

/* ---------- Reset Session Button ---------- */

function ResetSessionButton({
  accountId, username, accountStatus, hasSession, onSuccess,
}: {
  accountId: string
  username: string
  accountStatus: AccountStatus
  hasSession: boolean
  onSuccess: () => void
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)

  // Suggested when account is in a flagged state
  const isRecommended =
    accountStatus === 'challenge_required' || accountStatus === 'banned'

  // Hide when there's nothing to reset
  if (!hasSession && accountStatus !== 'challenge_required' && accountStatus !== 'banned') {
    return null
  }

  const handleConfirm = async () => {
    setLoading(true)
    try {
      await api.accounts.resetSession(accountId)
      toast.success(`Sessione di @${username} resettata. Rifare 'Login Browser' per riusarlo.`)
      setOpen(false)
      onSuccess()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Reset fallito')
    } finally {
      setLoading(false)
    }
  }

  const triggerColor = isRecommended
    ? 'border-orange-700 text-orange-400 hover:bg-orange-900/20'
    : 'border-gray-700 text-gray-400 hover:bg-gray-800'

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button
            size="sm"
            variant="outline"
            className={triggerColor}
            type="button"
            title="Reset sessione browser + instagrapi"
          />
        }
      >
        <Eraser className="w-4 h-4 mr-1" />
        Reset
      </DialogTrigger>
      <DialogContent className="bg-gray-900 border-gray-800 text-white max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="w-5 h-5 text-orange-400" />
            Reset sessione di @{username}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4 text-sm">
          <div className="p-3 bg-orange-900/20 border border-orange-800/50 rounded-lg">
            <p className="text-orange-300 font-medium mb-1">Operazione distruttiva irreversibile</p>
            <p className="text-gray-300 text-xs leading-relaxed">
              Cancella il profilo browser (cookie, localStorage, cache, fingerprint persistito)
              e azzera la sessione instagrapi. Account dovrà rifare login browser.
              <strong className="text-orange-300"> Trust accumulato con Instagram: perso.</strong>
            </p>
          </div>

          <div>
            <p className="text-green-400 font-medium mb-2">✓ Quando USARLO</p>
            <ul className="text-gray-300 text-xs space-y-1.5 list-disc list-inside">
              <li>Account in stato <code className="text-orange-300">challenge_required</code> dopo errore UFAC (<code>ufac_www_bloks</code>)</li>
              <li>Account in <code className="text-red-300">banned</code> appena recuperato manualmente via app IG</li>
              <li>Cambio proxy/IP per questo account (device + IP coerenti = nuovo)</li>
              <li>Login Browser fallito 2+ volte di fila</li>
              <li>Sospetto che cookie siano stati flaggati dopo un errore</li>
            </ul>
          </div>

          <div>
            <p className="text-red-400 font-medium mb-2">✗ Quando NON usarlo</p>
            <ul className="text-gray-300 text-xs space-y-1.5 list-disc list-inside">
              <li>Account funzionante in produzione → perdi trust accumulato (anni di storia)</li>
              <li>Durante una campagna attiva → distrugge sessione mid-flight (bloccato comunque dal backend)</li>
              <li>&quot;Pulizia preventiva&quot; periodica → device che cambia troppo = pattern sospetto</li>
              <li>Senza piano di re-login + warm-up successivo</li>
            </ul>
          </div>

          <div className="p-3 bg-gray-800/50 border border-gray-700 rounded-lg">
            <p className="text-gray-300 text-xs font-medium mb-1.5">Cosa succede dopo il reset</p>
            <ol className="text-gray-400 text-xs space-y-1 list-decimal list-inside">
              <li>Status account → <code>active</code> (se era challenge/banned)</li>
              <li>Bottone &quot;Login Browser&quot; per rifare login (flow nuovo, no UFAC)</li>
              <li>Aspetta 30+ min prima di avviare campagne (bake time mobile API)</li>
              <li>Per account vecchio re-loggato: warm-up graduale, niente burst DM</li>
            </ol>
          </div>

          {isRecommended && (
            <div className="p-2 bg-orange-900/30 border border-orange-700/50 rounded text-xs text-orange-200">
              Reset <strong>raccomandato</strong> per questo account (stato: {accountStatus}).
            </div>
          )}
        </div>

        <div className="flex gap-2 justify-end pt-2 border-t border-gray-800">
          <Button
            variant="outline"
            className="border-gray-700 text-gray-300 hover:bg-gray-800"
            onClick={() => setOpen(false)}
            disabled={loading}
          >
            Annulla
          </Button>
          <Button
            className="bg-red-600 hover:bg-red-700 text-white"
            onClick={handleConfirm}
            disabled={loading}
          >
            {loading && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
            <Eraser className="w-4 h-4 mr-2" />
            Reset definitivo
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

/* ---------- Test Connection Result Panel ---------- */

function TestResultPanel({ result }: { result: ProxyTestResult }) {
  if (!result.ok) {
    return (
      <div className="mt-3 p-3 bg-red-900/15 border border-red-800/50 rounded-lg text-xs">
        <p className="text-red-400 font-medium flex items-center gap-1">
          <AlertTriangle className="w-3 h-3" />Connessione fallita ({result.via === 'proxy' ? 'via proxy' : 'diretta'})
        </p>
        <p className="text-red-200/80 mt-1 leading-relaxed">{result.error}</p>
      </div>
    )
  }
  return (
    <div className="mt-3 p-3 bg-cyan-900/10 border border-cyan-800/40 rounded-lg text-xs space-y-1.5">
      <p className="text-cyan-400 font-medium flex items-center gap-1.5">
        {result.mobile ? <Smartphone className="w-3 h-3" /> : <Wifi className="w-3 h-3" />}
        Egress: <span className="font-mono text-white">{result.egress_ip}</span>
        {result.via === 'proxy'
          ? <Badge variant="outline" className="text-[10px] border-cyan-700 text-cyan-300">via proxy</Badge>
          : <Badge variant="outline" className="text-[10px] border-gray-700 text-gray-400">diretta (WiFi)</Badge>}
        {result.mobile === true && <Badge variant="outline" className="text-[10px] border-green-700 text-green-400">mobile</Badge>}
      </p>
      <div className="text-gray-400">
        {result.isp && <span>{result.isp}</span>}
        {result.asn && <span className="text-gray-600"> · {result.asn}</span>}
        {(result.city || result.country) && (
          <span className="text-gray-600"> · {[result.city, result.country].filter(Boolean).join(', ')}</span>
        )}
      </div>
    </div>
  )
}

/* ---------- Account Metrics Panel ---------- */

function AccountMetricsPanel({ accountId }: { accountId: string }) {
  const { data, error } = useSWR<AccountMetrics>(
    `metrics-${accountId}`,
    () => api.accounts.metrics(accountId),
    { refreshInterval: 30000 }
  )

  if (error) return (
    <div className="mt-3 p-3 bg-red-900/10 border border-red-800/50 rounded-lg text-xs text-red-400">
      Errore nel caricamento metriche.
    </div>
  )
  if (!data) return (
    <div className="mt-3 flex items-center gap-2 text-xs text-gray-500">
      <Loader2 className="w-3 h-3 animate-spin" />Caricamento metriche...
    </div>
  )

  const successRateColor = data.success_rate >= 90 ? 'text-green-400'
    : data.success_rate >= 70 ? 'text-yellow-400'
    : 'text-red-400'

  return (
    <div className="mt-3 p-3 bg-gray-800/50 border border-gray-700/50 rounded-lg">
      <p className="text-xs text-gray-500 font-medium mb-2 flex items-center gap-1">
        <BarChart2 className="w-3 h-3" />Metriche
      </p>
      <div className="grid grid-cols-3 gap-3">
        <div className="text-center">
          <div className="text-base font-semibold text-white">{data.today_sent}</div>
          <div className="text-xs text-gray-500">oggi / {data.today_limit}</div>
        </div>
        <div className="text-center">
          <div className="text-base font-semibold text-white">{data.total_sent.toLocaleString()}</div>
          <div className="text-xs text-gray-500">totale inviati</div>
        </div>
        <div className="text-center">
          <div className={`text-base font-semibold ${successRateColor}`}>{data.success_rate}%</div>
          <div className="text-xs text-gray-500">success rate</div>
        </div>
      </div>
      {(data.ban_events > 0 || data.challenge_events > 0 || data.total_failed > 0) && (
        <div className="flex gap-4 mt-2 pt-2 border-t border-gray-700/50 text-xs">
          {data.total_failed > 0 && <span className="text-red-400">{data.total_failed} falliti</span>}
          {data.ban_events > 0 && <span className="text-red-400">{data.ban_events} ban</span>}
          {data.challenge_events > 0 && <span className="text-orange-400">{data.challenge_events} challenge</span>}
          {data.warmup_day > 0 && <span className="text-blue-400">warm-up giorno {data.warmup_day}</span>}
        </div>
      )}
    </div>
  )
}


/* ---------- M8 lite: DM Count Panel ---------- */

function DMCountPanel({ accountId }: { accountId: string }) {
  const [data, setData] = useState<DMCount | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchCount = async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.accounts.dmCount(accountId)
      setData(result)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  // Auto-fetch on mount
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { fetchCount() }, [])

  return (
    <div className="mt-3 p-3 bg-blue-900/10 border border-blue-800/40 rounded-lg">
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs text-blue-400 font-medium flex items-center gap-1">
          <Inbox className="w-3 h-3" />Notifiche DM
        </p>
        <button
          onClick={fetchCount}
          disabled={loading}
          className="text-blue-600 hover:text-blue-400 disabled:opacity-50"
          title="Aggiorna">
          <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>
      {loading && !data && (
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <Loader2 className="w-3 h-3 animate-spin" />Recupero notifiche Instagram...
        </div>
      )}
      {error && (
        <p className="text-xs text-red-400">{error}</p>
      )}
      {data && (
        <div className="flex items-center gap-6">
          <div className="text-center">
            <div className="text-lg font-semibold text-white">{data.unread_count}</div>
            <div className="text-xs text-gray-500">messaggi non letti</div>
          </div>
          <div className="text-center">
            <div className="text-lg font-semibold text-yellow-400">{data.pending_count}</div>
            <div className="text-xs text-gray-500">richieste in attesa</div>
          </div>
          <div className="text-xs text-gray-600 ml-auto">
            {new Date(data.checked_at).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })}
          </div>
        </div>
      )}
    </div>
  )
}

function ForceCancelCooldownButton({ accountId, username, onSuccess }: { accountId: string; username: string; onSuccess: () => void }) {
  const [loading, setLoading] = useState(false)

  const handleCancel = async () => {
    if (!confirm(`Annullare il cooldown per @${username}? L'account tornerà attivo immediatamente.`)) return
    setLoading(true)
    try {
      await api.accounts.forceCancelCooldown(accountId)
      toast.success(`Cooldown annullato per @${username}`)
      onSuccess()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Button size="sm" variant="ghost"
      className="text-yellow-500 hover:text-yellow-400 hover:bg-yellow-900/20"
      onClick={handleCancel} disabled={loading}
      title="Annulla cooldown">
      {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
    </Button>
  )
}

function ChallengeForm({ accountId, onSuccess }: { accountId: string; onSuccess: () => void }) {
  const [code, setCode] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    try {
      await api.accounts.verifyChallenge(accountId, code)
      toast.success('Codice inviato! Il bot riproverà il login.')
      onSuccess()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={submit} className="flex gap-2 mt-2">
      <Input placeholder="Codice verifica" value={code} onChange={e => setCode(e.target.value)}
        className="bg-gray-800 border-gray-700 text-white h-8 text-sm" required />
      <Button type="submit" size="sm" disabled={loading} className="bg-orange-600 hover:bg-orange-700">
        {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Invia'}
      </Button>
    </form>
  )
}
