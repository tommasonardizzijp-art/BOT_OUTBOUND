'use client'

import { useState, Suspense } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { api, setAuthToken } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Loader2, LogIn } from 'lucide-react'
import { toast } from 'sonner'

function LoginForm() {
  const router = useRouter()
  const params = useSearchParams()
  const next = params.get('next') || '/'

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    try {
      const res = await api.auth.login(email, password)
      setAuthToken(res.access_token)
      toast.success(`Benvenuto, ${res.user.email}`)
      router.replace(next.startsWith('/') ? next : '/')
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Errore'
      toast.error(msg)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex h-full w-full items-center justify-center bg-gray-950 p-4">
      <Card className="w-full max-w-sm bg-gray-900 border-gray-800">
        <CardHeader>
          <CardTitle className="text-xl text-white text-center">BOT OUTBOUND</CardTitle>
          <p className="text-sm text-gray-500 text-center">Accedi per continuare</p>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="block text-sm text-gray-400 mb-1.5">Email</label>
              <Input
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={e => setEmail(e.target.value)}
                disabled={loading}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1.5">Password</label>
              <Input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={e => setPassword(e.target.value)}
                disabled={loading}
              />
            </div>
            <Button type="submit" className="w-full bg-purple-600 hover:bg-purple-700" disabled={loading}>
              {loading
                ? <><Loader2 className="w-4 h-4 mr-2 animate-spin" />Accesso...</>
                : <><LogIn className="w-4 h-4 mr-2" />Accedi</>
              }
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

export default function LoginPage() {
  return (
    <Suspense fallback={
      <div className="flex h-full items-center justify-center text-gray-500 text-sm">Caricamento...</div>
    }>
      <LoginForm />
    </Suspense>
  )
}
