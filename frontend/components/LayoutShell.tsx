'use client'

import { usePathname } from 'next/navigation'
import Sidebar from '@/components/layout/Sidebar'
import AuthGuard from '@/components/AuthGuard'

const FULLSCREEN_PATHS = ['/login']

export default function LayoutShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const isFullscreen = FULLSCREEN_PATHS.some(p => pathname?.startsWith(p))

  if (isFullscreen) {
    return <main className="h-full w-full">{children}</main>
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
