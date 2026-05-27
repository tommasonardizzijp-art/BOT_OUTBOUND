// Backend stores UTC datetimes without 'Z' suffix.
// Without 'Z', JS treats them as local time → 2h offset in Italy (UTC+2).
function parseUTC(dateStr: string): Date {
  if (!/Z$|[+-]\d{2}:\d{2}$/.test(dateStr)) {
    return new Date(dateStr + 'Z')
  }
  return new Date(dateStr)
}

export function formatDistanceToNow(dateStr: string): string {
  const now = new Date()
  const date = parseUTC(dateStr)
  const diffMs = now.getTime() - date.getTime()
  const diffSec = Math.floor(diffMs / 1000)
  const diffMin = Math.floor(diffSec / 60)
  const diffHour = Math.floor(diffMin / 60)
  const diffDay = Math.floor(diffHour / 24)

  if (diffSec < 60) return 'adesso'
  if (diffMin < 60) return `${diffMin}m fa`
  if (diffHour < 24) return `${diffHour}h fa`
  return `${diffDay}g fa`
}

export function formatDateTime(dateStr: string): string {
  return parseUTC(dateStr).toLocaleString('it-IT', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit'
  })
}

export function formatTime(dateStr: string): string {
  return parseUTC(dateStr).toLocaleTimeString('it-IT', {
    hour: '2-digit', minute: '2-digit'
  })
}
