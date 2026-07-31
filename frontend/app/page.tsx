'use client'

// Prima di questo cambio, "/" era la Dashboard aggregata di Instagram
// (stats, health, timeline, activity feed): verificato leggendo il file
// prima di toccarlo. Con due mondi separati (Task 9, SDD 6.3) "/" diventa
// solo lo smistamento post-login verso il picker di canale, o verso il
// canale gia' preferito. La Dashboard aggregata Instagram NON e' stata
// eliminata: e' stata spostata verbatim in app/dashboard/page.tsx, e il nav
// Sidebar ("Dashboard") punta li' -- nessun contenuto Instagram perso, solo
// spostato di un livello per fare posto al picker.
import { useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { leggiCanalePreferito } from '@/lib/canale'

export default function Home() {
  const router = useRouter()

  useEffect(() => {
    const canale = leggiCanalePreferito()
    if (canale === 'instagram') router.replace('/campaigns')
    else if (canale === 'whatsapp') router.replace('/wa')
    else router.replace('/canale')
  }, [router])

  return null
}
