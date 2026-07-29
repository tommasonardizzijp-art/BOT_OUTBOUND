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
    // Lettura del token da localStorage al mount: e' esattamente il caso
    // "sincronizza con un sistema esterno" per cui gli effetti esistono, e non
    // e' il cascading render che la regola vuole prevenire (gira una volta
    // sola, dipendenze vuote). Non si puo' spostare in un lazy initializer di
    // useState: il server non ha localStorage e renderizzerebbe uno stato
    // diverso dal client, con hydration mismatch. La riscrittura pulita
    // (useSyncExternalStore) tocca la logica di auth in produzione e va fatta
    // con l'app davanti, non dentro una PR che ripara la CI.
    // eslint-disable-next-line react-hooks/set-state-in-effect
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
