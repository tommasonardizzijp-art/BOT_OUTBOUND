// Mirror of backend app/utils/roles.py. The 'inbox' capability (DM-thread
// listing, max 1 account/campaign) is combinable with scraping/dm.
import type { AccountRole } from './types'

export const SCRAPE_ROLES: AccountRole[] = ['scraping', 'both', 'inbox_scraping', 'inbox_both']
export const DM_ROLES: AccountRole[] = ['dm', 'both', 'inbox_dm', 'inbox_both']
export const INBOX_ROLES: AccountRole[] = ['inbox', 'inbox_scraping', 'inbox_dm', 'inbox_both']

export const canScrape = (role?: AccountRole | null) => SCRAPE_ROLES.includes((role ?? 'both') as AccountRole)
export const canDm = (role?: AccountRole | null) => DM_ROLES.includes((role ?? 'both') as AccountRole)
export const isInbox = (role?: AccountRole | null) => INBOX_ROLES.includes((role ?? 'both') as AccountRole)

// Capability singola dedicata (esclude 'both'/'inbox_both'): usati dalla guard
// "2 profili distinti" per scraping+DM in parallelo (vedi backend app/utils/roles.py).
export const SCRAPE_ONLY_ROLES: AccountRole[] = ['scraping', 'inbox_scraping']
export const DM_ONLY_ROLES: AccountRole[] = ['dm', 'inbox_dm']

export const isScrapeOnly = (role?: AccountRole | null) => SCRAPE_ONLY_ROLES.includes((role ?? 'both') as AccountRole)
export const isDmOnly = (role?: AccountRole | null) => DM_ONLY_ROLES.includes((role ?? 'both') as AccountRole)
