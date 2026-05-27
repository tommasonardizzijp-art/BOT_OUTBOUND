'use client'

import { useEffect, useState } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { getAuthToken } from '@/lib/api'

const PUBLIC_PATHS = ['/login']

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const router = useRouter()
  const [token, setToken] = useState<string | null>(null)
  const [checked, setChecked] = useState(false)

  const isPublic = PUBLIC_PATHS.some(p => pathname?.startsWith(p))

  useEffect(() => {
    setToken(getAuthToken())
    setChecked(true)
  }, [])

  useEffect(() => {
    if (checked && !isPublic && !token) {
      const next = encodeURIComponent(pathname || '/')
      router.replace(`/login?next=${next}`)
    }
  }, [pathname, isPublic, router, token, checked])

  if (isPublic) {
    return <>{children}</>
  }

  if (!checked || !token) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 text-sm">
        Caricamento...
      </div>
    )
  }
  return <>{children}</>
}
