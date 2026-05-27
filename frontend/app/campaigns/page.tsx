'use client'

import useSWR from 'swr'
import Link from 'next/link'
import { api } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Plus, Play, Pause, Square, Loader2, Trash2, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'
import type { Campaign, CampaignStatus } from '@/lib/types'
import { useState } from 'react'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Skeleton } from '@/components/ui/skeleton'

const STATUS_COLORS: Record<CampaignStatus, string> = {
  draft: 'bg-gray-600',
  scraping: 'bg-blue-600',
  scraping_break: 'bg-amber-600',
  scraping_and_running: 'bg-teal-600',
  ready: 'bg-cyan-600',
  running: 'bg-green-600',
  paused: 'bg-yellow-600',
  completed: 'bg-purple-600',
  error: 'bg-red-600',
}

const STATUS_LABELS: Record<CampaignStatus, string> = {
  draft: 'Bozza',
  scraping: 'Scraping...',
  scraping_break: 'Pausa sessione',
  scraping_and_running: 'Scraping + DM',
  ready: 'Pronta',
  running: 'In corso',
  paused: 'In pausa',
  completed: 'Completata',
  error: 'Errore',
}

export default function CampaignsPage() {
  const { data: campaigns, mutate } = useSWR('campaigns', api.campaigns.list, { refreshInterval: 6000 })
  const [loadingId, setLoadingId] = useState<string | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean; title: string; description: string
    confirmLabel: string; variant: 'destructive' | 'warning' | 'default'
    onConfirm: () => void
  }>({ open: false, title: '', description: '', confirmLabel: 'Conferma', variant: 'destructive', onConfirm: () => {} })

  const openConfirm = (
    title: string, description: string, confirmLabel: string,
    onConfirm: () => void, variant: 'destructive' | 'warning' | 'default' = 'destructive'
  ) => setConfirmDialog({ open: true, title, description, confirmLabel, variant, onConfirm })

  const action = async (id: string, fn: () => Promise<Campaign>) => {
    setLoadingId(id)
    try {
      await fn()
      await mutate()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Errore'
      if (msg.includes('account') && msg.includes('assegna')) {
        toast.error(msg, {
          description: 'Apri la campagna e usa la sezione "Account assegnati".',
          action: { label: 'Apri', onClick: () => window.location.href = `/campaigns/${id}` },
          duration: 8000,
        })
      } else {
        toast.error(msg)
      }
    } finally {
      setLoadingId(null)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Campagne</h1>
          <p className="text-gray-400 text-base mt-1">{campaigns?.length ?? 0} campagne totali</p>
        </div>
        <Link href="/campaigns/new">
          <Button className="bg-purple-600 hover:bg-purple-700">
            <Plus className="w-4 h-4 mr-2" /> Nuova campagna
          </Button>
        </Link>
      </div>

      {!campaigns && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[1, 2, 3].map(i => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-3">
              <div className="flex items-center justify-between">
                <div className="space-y-2">
                  <Skeleton className="h-5 w-48" />
                  <Skeleton className="h-4 w-32" />
                </div>
                <Skeleton className="h-8 w-24" />
              </div>
            </div>
          ))}
        </div>
      )}

      {campaigns?.length === 0 && (
        <Card className="bg-gray-900 border-gray-800">
          <CardContent className="py-12 text-center">
            <p className="text-gray-500">Nessuna campagna ancora.</p>
            <Link href="/campaigns/new">
              <Button className="mt-4 bg-purple-600 hover:bg-purple-700">
                <Plus className="w-4 h-4 mr-2" /> Crea la prima campagna
              </Button>
            </Link>
          </CardContent>
        </Card>
      )}

      <ConfirmDialog
        open={confirmDialog.open}
        onOpenChange={open => setConfirmDialog(d => ({ ...d, open }))}
        title={confirmDialog.title}
        description={confirmDialog.description}
        confirmLabel={confirmDialog.confirmLabel}
        variant={confirmDialog.variant}
        onConfirm={confirmDialog.onConfirm}
      />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {campaigns?.map((c: Campaign) => {
          const total = c.total_followers || 1
          const progress = Math.min(100, Math.round((c.messages_sent / total) * 100))
          const isLoading = loadingId === c.id

          return (
            <Card key={c.id} className="bg-gray-900 border-gray-800">
              <CardContent className="py-5">
                <div className="flex flex-col gap-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1 flex-wrap">
                        <Link href={`/campaigns/${c.id}`} className="font-semibold text-lg text-white hover:text-purple-300 truncate">
                          {c.name}
                        </Link>
                        <Badge className={`${STATUS_COLORS[c.status]} text-white text-xs`}>
                          {STATUS_LABELS[c.status]}
                        </Badge>
                      </div>
                      <p className="text-gray-400 text-sm">@{c.target_username}</p>
                    </div>
                  </div>

                    {c.total_followers > 0 && (
                      <div>
                        <div className="flex justify-between text-sm text-gray-500 mb-1.5">
                          <span>{c.messages_sent} inviati · {c.messages_failed} falliti · {c.messages_pending} in coda</span>
                          <span>{progress}%</span>
                        </div>
                        <div className="h-3 w-full bg-gray-800 rounded-full overflow-hidden">
                          <div className="h-full bg-amber-600 rounded-full transition-all" style={{ width: `${progress}%` }} />
                        </div>
                        {c.messages_sent > 0 && (
                          <div className="flex items-center gap-2 mt-1.5 text-sm">
                            <span className={
                              c.reply_rate >= 0.15 ? 'text-emerald-400'
                              : c.reply_rate >= 0.05 ? 'text-yellow-400'
                              : 'text-red-400'
                            }>
                              💬 {c.messages_replied} risposte
                            </span>
                            <span className="text-gray-500">·</span>
                            <span className={
                              c.reply_rate >= 0.15 ? 'text-emerald-400 font-medium'
                              : c.reply_rate >= 0.05 ? 'text-yellow-400 font-medium'
                              : 'text-red-400 font-medium'
                            }>
                              tasso {(c.reply_rate * 100).toFixed(1)}%
                            </span>
                          </div>
                        )}
                      </div>
                    )}

                  {/* Actions */}
                  <div className="flex items-center gap-2 flex-wrap">
                    {c.status === 'draft' && (
                      <Button size="sm" variant="outline" className="border-gray-700 text-gray-300"
                        onClick={() => action(c.id, () => api.campaigns.startScrape(c.id))} disabled={isLoading}>
                        {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Scraping'}
                      </Button>
                    )}
                    {c.status === 'ready' && (
                      <Button size="sm" className="bg-green-600 hover:bg-green-700"
                        onClick={() => action(c.id, () => api.campaigns.start(c.id))} disabled={isLoading}>
                        {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Play className="w-4 h-4 mr-1" />Avvia</>}
                      </Button>
                    )}
                    {c.status === 'running' && (
                      <Button size="sm" variant="outline" className="border-yellow-600 text-yellow-400"
                        onClick={() => action(c.id, () => api.campaigns.pause(c.id))} disabled={isLoading}>
                        {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Pause className="w-4 h-4 mr-1" />Pausa</>}
                      </Button>
                    )}
                    {c.status === 'paused' && (
                      <Button size="sm" className="bg-green-600 hover:bg-green-700"
                        onClick={() => action(c.id, () => api.campaigns.resume(c.id))} disabled={isLoading}>
                        {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Play className="w-4 h-4 mr-1" />Riprendi</>}
                      </Button>
                    )}
                    {(c.status === 'running' || c.status === 'paused') && (
                      <Button size="sm" variant="outline" className="border-red-800 text-red-400"
                        onClick={() => action(c.id, () => api.campaigns.stop(c.id))} disabled={isLoading}>
                        <Square className="w-4 h-4" />
                      </Button>
                    )}
                    {(c.status === 'error' || c.status === 'completed' || c.status === 'scraping') && (
                      <Button size="sm" variant="outline" className="border-cyan-700 text-cyan-400 hover:bg-cyan-900/20"
                        onClick={() => openConfirm(
                          'Reset campagna',
                          `Tutti i messaggi di "${c.name}" verranno cancellati e i follower reimpostati. Dovrai riavviare la campagna da zero.`,
                          'Reset',
                          () => action(c.id, () => api.campaigns.reset(c.id)),
                          'warning'
                        )}
                        disabled={isLoading}>
                        {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RotateCcw className="w-4 h-4" />}
                      </Button>
                    )}
                    {(c.status === 'draft' || c.status === 'error' || c.status === 'completed' || c.status === 'scraping') && (
                      <Button size="sm" variant="outline" className="border-red-800 text-red-400 hover:bg-red-900/20"
                        onClick={() => openConfirm(
                          'Elimina campagna',
                          `Tutti i dati di "${c.name}" (follower, messaggi, statistiche) verranno eliminati definitivamente.`,
                          'Elimina',
                          async () => {
                            setLoadingId(c.id)
                            try {
                              await api.campaigns.delete(c.id)
                              toast.success('Campagna eliminata')
                              await mutate()
                            } catch (e: unknown) {
                              toast.error(e instanceof Error ? e.message : 'Errore')
                            } finally {
                              setLoadingId(null)
                            }
                          }
                        )}
                        disabled={isLoading}>
                        {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                      </Button>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>
    </div>
  )
}
