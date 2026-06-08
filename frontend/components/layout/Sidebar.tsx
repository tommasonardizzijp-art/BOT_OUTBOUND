'use client'

import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import {
  LayoutDashboard,
  Megaphone,
  Users,
  MessageSquare,
  Database,
  Settings,
  BookOpen,
  Zap,
  LogOut,
  AlertTriangle,
  Play,
  PauseCircle,
  Activity,
  Target,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { api, getAuthToken, setAuthToken } from '@/lib/api'

const navItems = [
  { href: '/', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/campaigns', label: 'Campagne', icon: Megaphone },
  { href: '/accounts', label: 'Account IG', icon: Users },
  { href: '/messages', label: 'Messaggi', icon: MessageSquare },
  { href: '/leads', label: 'Leads', icon: Database },
  { href: '/lead-qualification', label: 'Qualifica lead', icon: Target },
  { href: '/ops', label: 'Ops', icon: Activity },
  { href: '/settings', label: 'Impostazioni', icon: Settings },
  { href: '/guide', label: 'Guida', icon: BookOpen },
]

export default function Sidebar() {
  const pathname = usePathname()
  const router = useRouter()
  const [me, setMe] = useState<{ email: string; role: string } | null>(null)
  const [botState, setBotState] = useState<{ halted: boolean; halted_reason: string | null } | null>(null)
  const [botActionLoading, setBotActionLoading] = useState(false)

  useEffect(() => {
    if (!getAuthToken()) return
    api.auth.me().then(u => setMe({ email: u.email, role: u.role })).catch(() => { /* ignore */ })
  }, [])

  useEffect(() => {
    let stopped = false
    const load = () => {
      api.admin.state()
        .then(s => {
          if (!stopped) setBotState({ halted: s.halted, halted_reason: s.halted_reason })
        })
        .catch(() => { /* ignore */ })
    }
    load()
    const id = window.setInterval(load, 10000)
    return () => {
      stopped = true
      window.clearInterval(id)
    }
  }, [])

  const logout = () => {
    setAuthToken(null)
    router.replace('/login')
  }

  const haltBot = async () => {
    const reason = window.prompt('Motivo del blocco globale?', 'Blocco manuale da dashboard')
    if (reason === null) return
    setBotActionLoading(true)
    try {
      const s = await api.admin.halt(reason.trim() || 'Blocco manuale da dashboard', 'web_halt')
      setBotState({ halted: s.halted, halted_reason: s.halted_reason })
    } catch (error) {
      window.alert(error instanceof Error ? error.message : 'Impossibile bloccare il bot')
    } finally {
      setBotActionLoading(false)
    }
  }

  const unhaltBot = async () => {
    setBotActionLoading(true)
    try {
      const s = await api.admin.resume()
      setBotState({ halted: s.halted, halted_reason: s.halted_reason })
    } catch (error) {
      window.alert(error instanceof Error ? error.message : 'Impossibile sbloccare il bot')
    } finally {
      setBotActionLoading(false)
    }
  }

  return (
    <aside className="w-64 bg-gray-900 border-r border-gray-800 flex flex-col py-5 flex-shrink-0">
      {/* Logo */}
      <div className="flex items-center gap-2 px-5 mb-7">
        <Zap className="w-6 h-6 text-purple-400" />
        <span className="font-bold text-white text-base tracking-tight">BOT OUTBOUND</span>
      </div>

      {me?.role === 'admin' && botState && (
        <div className={cn(
          'mx-3 mb-4 rounded-lg border px-3 py-2',
          botState.halted
            ? 'border-red-700/70 bg-red-950/50'
            : 'border-gray-700 bg-gray-800/50'
        )}>
          <div className={cn(
            'flex items-center gap-2 text-xs font-semibold',
            botState.halted ? 'text-red-200' : 'text-gray-300'
          )}>
            <AlertTriangle className="w-4 h-4" />
            {botState.halted ? 'Kill-switch attivo' : 'Kill-switch pronto'}
          </div>
          {botState.halted_reason && (
            <div className="mt-1 max-h-8 overflow-hidden text-[11px] text-red-300/80" title={botState.halted_reason}>
              {botState.halted_reason}
            </div>
          )}
          {botState.halted ? (
            <button
              onClick={unhaltBot}
              disabled={botActionLoading}
              className="mt-2 inline-flex items-center gap-1 rounded bg-red-700 px-2 py-1 text-[11px] font-medium text-white hover:bg-red-600 disabled:opacity-60"
              title="Sblocca kill-switch globale"
            >
              <Play className="w-3 h-3" /> Sblocca
            </button>
          ) : (
            <button
              onClick={haltBot}
              disabled={botActionLoading}
              className="mt-2 inline-flex items-center gap-1 rounded border border-red-800 px-2 py-1 text-[11px] font-medium text-red-200 hover:bg-red-950/70 disabled:opacity-60"
              title="Blocca tutto il bot con kill-switch globale"
            >
              <PauseCircle className="w-3 h-3" /> Blocca tutto
            </button>
          )}
        </div>
      )}

      {/* Nav */}
      <nav className="flex-1 px-3 space-y-1">
        {navItems.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className={cn(
              'flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium transition-colors',
              pathname === href || (href !== '/' && pathname.startsWith(href))
                ? 'bg-purple-600 text-white'
                : 'text-gray-400 hover:bg-gray-800 hover:text-gray-100'
            )}
          >
            <Icon className="w-5 h-5" />
            {label}
          </Link>
        ))}
      </nav>

      {me && (
        <div className="mx-3 mt-2 mb-1 rounded-lg bg-gray-800/60 px-3 py-2 space-y-1.5">
          <div className="text-xs text-gray-400 truncate" title={me.email}>{me.email}</div>
          <div className="flex items-center justify-between">
            <span className={cn(
              'text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded',
              me.role === 'admin' ? 'bg-purple-700 text-white' : 'bg-gray-700 text-gray-300'
            )}>
              {me.role}
            </span>
            <button
              onClick={logout}
              className="flex items-center gap-1 text-xs text-gray-500 hover:text-red-400"
              title="Esci"
            >
              <LogOut className="w-3 h-3" />Esci
            </button>
          </div>
        </div>
      )}

      <div className="px-5 py-2 text-xs text-gray-600">v0.1.0</div>
    </aside>
  )
}
