'use client'

import { usePathname } from 'next/navigation'
import Sidebar from '@/components/layout/Sidebar'
import AuthGuard from '@/components/AuthGuard'

const FULLSCREEN_PATHS = ['/login']

// Il mondo WhatsApp (Task 9, SDD 6.3) e' un mondo a se': nessuna pagina
// condivisa con Instagram, sidebar propria (frontend/app/wa/WaNav.tsx). Il
// picker di canale (/canale) sta "fra" i due mondi, prima di sceglierne uno:
// niente sidebar Instagram nemmeno li'. Restano sotto AuthGuard entrambi
// (a differenza di /login): sono pagine post-login.
const NO_SIDEBAR_PATHS = ['/wa', '/canale']

export default function LayoutShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const isFullscreen = FULLSCREEN_PATHS.some(p => pathname?.startsWith(p))
  const isNoSidebar = NO_SIDEBAR_PATHS.some(p => pathname?.startsWith(p))

  if (isFullscreen) {
    return <main className="h-full w-full">{children}</main>
  }

  if (isNoSidebar) {
    return (
      <AuthGuard>
        <main className="h-full w-full overflow-y-auto">{children}</main>
      </AuthGuard>
    )
  }

  return (
    <AuthGuard>
      <div className="flex h-full">
        <Sidebar />
        <main className="flex-1 overflow-y-auto p-8">{children}</main>
      </div>
    </AuthGuard>
  )
}
