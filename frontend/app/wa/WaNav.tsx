'use client'

// Nav propria del mondo WhatsApp: nessun riferimento ai componenti di
// navigazione Instagram (components/layout/Sidebar.tsx), nessun import da
// lib/api.ts. Il colore (var(--wa-accent), definito in layout.tsx) e' quello
// che dice a colpo d'occhio "sei nel mondo WhatsApp".
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { ArrowLeftRight, LayoutGrid, Phone, PlusCircle } from 'lucide-react'

const navItems = [
  { href: '/wa', label: 'Campagne', icon: LayoutGrid },
  { href: '/wa/numeri', label: 'Numeri', icon: Phone },
  { href: '/wa/campagne/nuova', label: 'Nuova campagna', icon: PlusCircle },
]

export default function WaNav() {
  const pathname = usePathname()

  return (
    <aside
      className="flex w-64 flex-shrink-0 flex-col py-5"
      style={{ backgroundColor: 'var(--wa-surface)', borderRight: '1px solid var(--wa-border)' }}
    >
      <div className="mb-7 flex items-center gap-2 px-5">
        <span
          className="flex h-7 w-7 items-center justify-center rounded-full text-sm font-bold"
          style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
        >
          W
        </span>
        <span className="text-base font-semibold tracking-tight text-white">WhatsApp</span>
      </div>

      <nav className="flex-1 space-y-1 px-3">
        {navItems.map(({ href, label, icon: Icon }) => {
          const attivo = pathname === href || (href !== '/wa' && (pathname ?? '').startsWith(href))
          return (
            <Link
              key={href}
              href={href}
              className="flex items-center gap-3 rounded-lg px-4 py-2.5 text-sm font-medium transition-colors"
              style={attivo
                ? { backgroundColor: 'var(--wa-accent)', color: '#04120e' }
                : { color: 'var(--wa-muted)' }}
            >
              <Icon className="h-5 w-5" />
              {label}
            </Link>
          )
        })}
      </nav>

      <div className="mt-2 px-3">
        <Link
          href="/canale"
          className="flex items-center gap-2 rounded-lg px-4 py-2 text-xs font-medium transition-colors hover:text-white"
          style={{ color: 'var(--wa-muted)' }}
        >
          <ArrowLeftRight className="h-3.5 w-3.5" />
          Cambia canale
        </Link>
      </div>

      <div className="px-5 py-2 text-xs" style={{ color: 'var(--wa-muted)' }}>
        Canale WhatsApp
      </div>
    </aside>
  )
}
