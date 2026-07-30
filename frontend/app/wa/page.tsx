'use client'

// Home del mondo WhatsApp: elenco campagne. Minimale di proposito (Task 9):
// i dettagli (KPI, azioni, contatti) arrivano nel Task 12. Nessun import da
// lib/api.ts: solo waApi.ts, come da regola del mondo /wa.
import Link from 'next/link'
import useSWR from 'swr'
import { waApi, type WaCampaignStatus } from '@/lib/waApi'

const STATO_LABEL: Record<WaCampaignStatus, string> = {
  draft: 'Bozza',
  running: 'In corso',
  paused: 'In pausa',
  stopped: 'Fermata',
  completed: 'Completata',
  error: 'Errore',
}

const TIPO_LABEL: Record<string, string> = {
  marketing: 'Marketing',
  followup: 'Follow-up',
}

export default function WaHomePage() {
  const { data, error, isLoading } = useSWR(
    'wa-campagne',
    () => waApi.campagne.list(),
    { refreshInterval: 10000 },
  )

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Campagne WhatsApp</h1>
          <p className="mt-1 text-sm" style={{ color: 'var(--wa-muted)' }}>
            Elenco delle campagne del canale WhatsApp.
          </p>
        </div>
        <Link
          href="/wa/campagne/nuova"
          className="rounded-lg px-4 py-2 text-sm font-medium"
          style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}
        >
          + Nuova campagna
        </Link>
      </div>

      {error && (
        <div
          className="rounded-lg border px-4 py-3 text-sm"
          style={{ borderColor: '#7a3a3a', backgroundColor: 'rgba(122, 58, 58, 0.15)', color: '#f2b8b8' }}
        >
          Backend non raggiungibile. Avviare il server e ricaricare la pagina.
        </div>
      )}

      {isLoading && !error && (
        <p style={{ color: 'var(--wa-muted)' }}>Caricamento...</p>
      )}

      {data && data.campagne.length === 0 && (
        <div
          className="rounded-2xl border p-10 text-center"
          style={{ borderColor: 'var(--wa-border)', backgroundColor: 'var(--wa-surface)' }}
        >
          <p className="text-lg font-medium text-white">Nessuna campagna ancora</p>
          <p className="mt-1 text-sm" style={{ color: 'var(--wa-muted)' }}>
            Crea la prima campagna per iniziare a caricare una lista contatti.
          </p>
        </div>
      )}

      {data && data.campagne.length > 0 && (
        <div className="overflow-hidden rounded-2xl border" style={{ borderColor: 'var(--wa-border)' }}>
          <table className="w-full text-sm">
            <thead>
              <tr style={{ backgroundColor: 'var(--wa-surface)', color: 'var(--wa-muted)' }}>
                <th className="px-4 py-3 text-left font-medium">Nome</th>
                <th className="px-4 py-3 text-left font-medium">Tipo</th>
                <th className="px-4 py-3 text-left font-medium">Stato</th>
                <th className="px-4 py-3 text-right font-medium">Contatti</th>
                <th className="px-4 py-3 text-right font-medium">Inviati</th>
              </tr>
            </thead>
            <tbody>
              {data.campagne.map((c) => (
                <tr key={c.id} style={{ borderTop: '1px solid var(--wa-border)' }}>
                  <td className="px-4 py-3">
                    <Link href={`/wa/campagne/${c.id}`} className="font-medium text-white hover:underline">
                      {c.name}
                    </Link>
                  </td>
                  <td className="px-4 py-3" style={{ color: 'var(--wa-muted)' }}>
                    {TIPO_LABEL[c.campaign_type] ?? c.campaign_type}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className="rounded px-2 py-0.5 text-xs font-medium"
                      style={{ backgroundColor: 'var(--wa-accent-soft)', color: '#26e0c4' }}
                    >
                      {STATO_LABEL[c.status] ?? c.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right" style={{ color: 'var(--wa-muted)' }}>
                    {c.total_contacts}
                  </td>
                  <td className="px-4 py-3 text-right" style={{ color: 'var(--wa-muted)' }}>
                    {c.sent}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
