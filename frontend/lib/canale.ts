// Chiave localStorage condivisa fra il picker (app/canale/page.tsx) e il
// redirect di app/page.tsx. Modulo a se' (non un export da un file page.tsx)
// per non importare un componente di pagina come se fosse una libreria.
export const CHANNEL_STORAGE_KEY = 'bot_outbound_canale'

export type CanalePreferito = 'instagram' | 'whatsapp'

export function leggiCanalePreferito(): CanalePreferito | null {
  if (typeof window === 'undefined') return null
  try {
    const v = window.localStorage.getItem(CHANNEL_STORAGE_KEY)
    return v === 'instagram' || v === 'whatsapp' ? v : null
  } catch {
    return null
  }
}

export function salvaCanalePreferito(canale: CanalePreferito): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(CHANNEL_STORAGE_KEY, canale)
  } catch {
    // localStorage non disponibile: si naviga comunque, si ridomandera'
    // semplicemente al prossimo accesso.
  }
}
