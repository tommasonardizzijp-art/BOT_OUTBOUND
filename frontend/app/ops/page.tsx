'use client'

import useSWR from 'swr'
import { Activity, AlertTriangle } from 'lucide-react'
import { api } from '@/lib/api'

function JsonTable({ title, data }: { title: string; data: { count: number; items: Array<Record<string, string | number | null>> } }) {
  const keys = data.items[0] ? Object.keys(data.items[0]) : []
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-white">{title} <span className="text-gray-500">({data.count})</span></h2>
      <div className="overflow-hidden rounded-lg border border-gray-800">
        <table className="w-full text-sm">
          <thead className="bg-gray-900 text-gray-400">
            <tr>
              {keys.length ? keys.map(k => <th key={k} className="px-3 py-2 text-left font-medium">{k}</th>) : <th className="px-3 py-2 text-left font-medium">Stato</th>}
            </tr>
          </thead>
          <tbody>
            {data.items.length ? data.items.map((row, idx) => (
              <tr key={idx} className="border-t border-gray-800">
                {keys.map(k => <td key={k} className="px-3 py-2 text-gray-300">{String(row[k] ?? '-')}</td>)}
              </tr>
            )) : (
              <tr className="border-t border-gray-800">
                <td className="px-3 py-3 text-gray-500">Nessun elemento critico</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export default function OpsPage() {
  const { data, error } = useSWR('ops-summary', api.ops.summary, { refreshInterval: 30000 })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-3xl font-bold text-white">
          <Activity className="h-7 w-7 text-purple-400" />
          Ops
        </h1>
        <p className="mt-1 text-gray-400">Diagnostica operativa dei job, lock e reservation.</p>
      </div>

      {error && (
        <div className="flex items-center gap-2 rounded-lg border border-red-800/50 bg-red-900/10 px-4 py-3 text-sm text-red-400">
          <AlertTriangle className="h-4 w-4" />
          {error instanceof Error ? error.message : 'Impossibile caricare la diagnostica'}
        </div>
      )}

      {!data && !error && <div className="text-gray-500">Caricamento diagnostica...</div>}

      {data && (
        <>
          <div className="rounded-lg border border-gray-800 bg-gray-900/50 px-4 py-3 text-sm text-gray-300">
            Generato: {new Date(data.generated_at).toLocaleString()}
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            {Object.entries(data.accounts_by_status).map(([status, count]) => (
              <div key={status} className="rounded-lg border border-gray-800 bg-gray-900/50 p-4">
                <div className="text-xs uppercase text-gray-500">{status}</div>
                <div className="mt-1 text-2xl font-semibold text-white">{count}</div>
              </div>
            ))}
          </div>
          <JsonTable title="Messaggi sending stale" data={data.sending_stale} />
          <JsonTable title="Reservation scadute" data={data.expired_reservations} />
          <JsonTable title="Follower lock stale" data={data.stale_follower_locks} />
          <JsonTable title="Campagne stale" data={data.stale_campaigns} />
        </>
      )}
    </div>
  )
}
