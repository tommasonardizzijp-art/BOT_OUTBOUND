'use client'

// Picker di canale post-login (Task 9, SDD 6.3): due mondi separati, non una
// vista mista. La preferenza si ricorda in localStorage cosi' chi usa un
// canale solo non paga un click in piu' ad ogni accesso (letta da
// app/page.tsx). Il link "cambia canale" resta sempre visibile nella shell
// di entrambi i mondi (WaNav.tsx e components/layout/Sidebar.tsx).
import { useRouter } from 'next/navigation'
import { Megaphone, MessageCircle } from 'lucide-react'
import { salvaCanalePreferito, type CanalePreferito } from '@/lib/canale'

export default function CanalePage() {
  const router = useRouter()

  function scegli(canale: CanalePreferito) {
    salvaCanalePreferito(canale)
    router.push(canale === 'instagram' ? '/campaigns' : '/wa')
  }

  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-10 bg-gray-950 px-6 py-16 text-gray-100">
      <div className="text-center">
        <h1 className="text-3xl font-bold text-white">Quale canale vuoi gestire?</h1>
        <p className="mt-2 text-base text-gray-400">
          Puoi cambiare canale in qualunque momento dal menu laterale.
        </p>
      </div>

      <div className="grid w-full max-w-3xl grid-cols-1 gap-6 sm:grid-cols-2">
        <button
          type="button"
          onClick={() => scegli('instagram')}
          className="group flex flex-col items-start gap-4 rounded-2xl border border-gray-800 bg-gray-900 p-8 text-left transition-colors hover:border-purple-500/60 hover:bg-gray-900/80"
        >
          <span className="flex h-12 w-12 items-center justify-center rounded-xl bg-purple-600/20 text-purple-400">
            <Megaphone className="h-6 w-6" />
          </span>
          <div>
            <h2 className="text-xl font-semibold text-white">Instagram</h2>
            <p className="mt-1 text-sm text-gray-400">Campagne DM, account, lead e follower.</p>
          </div>
        </button>

        <button
          type="button"
          onClick={() => scegli('whatsapp')}
          className="group flex flex-col items-start gap-4 rounded-2xl border border-gray-800 bg-gray-900 p-8 text-left transition-colors hover:border-[#128C7E]/60 hover:bg-gray-900/80"
        >
          <span
            className="flex h-12 w-12 items-center justify-center rounded-xl"
            style={{ backgroundColor: 'rgba(18, 140, 126, 0.16)', color: '#26e0c4' }}
          >
            <MessageCircle className="h-6 w-6" />
          </span>
          <div>
            <h2 className="text-xl font-semibold text-white">WhatsApp</h2>
            <p className="mt-1 text-sm text-gray-400">Numeri, campagne e liste contatti.</p>
          </div>
        </button>
      </div>
    </div>
  )
}
