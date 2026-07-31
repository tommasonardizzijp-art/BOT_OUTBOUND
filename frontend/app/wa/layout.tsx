import type { Metadata } from 'next'
import WaNav from './WaNav'

export const metadata: Metadata = {
  title: 'WhatsApp - BOT OUTBOUND',
}

// Shell del mondo WhatsApp (Task 9, SDD 6.3): tema verde scuro isolato a
// questo sottoalbero via variabili CSS, cosi' il colore stesso dice "sei nel
// mondo WhatsApp" senza bisogno di un banner. Nessuna pagina sotto /wa deve
// importare da lib/api.ts (che resta di Instagram): solo waApi.ts.
//
// La sidebar Instagram (components/layout/Sidebar.tsx) e' saltata a monte in
// components/LayoutShell.tsx per i path che iniziano con /wa: qui c'e' gia'
// il body pieno per WaNav + contenuto, senza sovrapposizioni.
export default function WaLayout({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="flex min-h-full"
      style={{
        // Proposta del piano: #128C7E. Le altre variabili sono derivate per
        // superfici/bordi/testo attenuato dello stesso sottoalbero.
        ['--wa-accent' as string]: '#128C7E',
        ['--wa-accent-soft' as string]: 'rgba(18, 140, 126, 0.16)',
        ['--wa-bg' as string]: '#0a1512',
        ['--wa-surface' as string]: '#0f1f1b',
        ['--wa-border' as string]: '#1b332c',
        ['--wa-muted' as string]: '#8fb3aa',
        backgroundColor: 'var(--wa-bg)',
        color: '#e7f3ef',
      }}
    >
      <WaNav />
      <main className="flex-1 overflow-y-auto p-8">{children}</main>
    </div>
  )
}
