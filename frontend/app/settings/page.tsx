'use client'

import useSWR from 'swr'
import Link from 'next/link'
import { api } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { CheckCircle, XCircle, AlertCircle, BookOpen } from 'lucide-react'
import type { HealthStatus } from '@/lib/types'

export default function SettingsPage() {
  const { data: health } = useSWR<HealthStatus>('health', api.health.check, { refreshInterval: 15000 })

  const services = [
    { name: 'Database', status: health?.database, description: 'SQLite locale' },
    { name: 'Redis', status: health?.redis, description: 'Task queue e cache' },
    { name: 'Ollama', status: health?.ollama, description: 'LLM locale per AI messaggi' },
  ]

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h1 className="text-2xl font-bold text-white">Impostazioni</h1>
        <p className="text-gray-400 text-sm mt-1">Stato del sistema e configurazione</p>
      </div>

      {/* System Health */}
      <Card className="bg-gray-900 border-gray-800">
        <CardHeader className="pb-3">
          <CardTitle className="text-base text-gray-100 flex items-center gap-2">
            Stato sistema
            {health?.status === 'ok' ? (
              <Badge className="bg-green-700 text-white text-xs">Tutto OK</Badge>
            ) : (
              <Badge className="bg-yellow-700 text-white text-xs">Degradato</Badge>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {services.map(s => (
            <div key={s.name} className="flex items-center justify-between">
              <div>
                <p className="text-sm text-gray-200 font-medium">{s.name}</p>
                <p className="text-xs text-gray-500">{s.description}</p>
              </div>
              <ServiceStatus status={s.status} />
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Link to full guide */}
      <Link href="/guide">
        <Card className="bg-gray-900 border-gray-800 hover:border-purple-700 transition-colors cursor-pointer">
          <CardContent className="py-4 flex items-center gap-3">
            <BookOpen className="w-5 h-5 text-purple-400 flex-shrink-0" />
            <div>
              <p className="text-sm font-medium text-gray-200">Guida completa</p>
              <p className="text-xs text-gray-500">Configurazione, uso, template, troubleshooting e altro</p>
            </div>
          </CardContent>
        </Card>
      </Link>
    </div>
  )
}

function ServiceStatus({ status }: { status?: string }) {
  if (!status) return <AlertCircle className="w-4 h-4 text-gray-500" />
  if (status === 'ok') return <CheckCircle className="w-4 h-4 text-green-400" />
  return (
    <div className="flex items-center gap-1.5">
      <XCircle className="w-4 h-4 text-red-400" />
      <span className="text-xs text-red-400">{status}</span>
    </div>
  )
}
