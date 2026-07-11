// Rispecchia backend/app/services/template_renderer.py (solo per anteprima UI).
const SPINTAX_RE = /\{([^{}|]*(?:\|[^{}|]*)+)\}/g

export function resolveSpintax(text: string): string {
  return text.replace(SPINTAX_RE, (_, group: string) => {
    const options = group.split('|')
    return options[Math.floor(Math.random() * options.length)]
  })
}

const NAME_RE = /\{nome\}|\[nome\]|\{name\}|\[name\]/gi

export function renderPreview(template: string, sampleName = 'Marco'): string {
  return resolveSpintax(template).replace(NAME_RE, sampleName)
}

/** Placeholder sconosciuti rimasti dopo spintax+nome (es. {azienda}): il backend
 *  li rifiuta al rendering — segnalali nel form. */
export function findUnknownPlaceholders(template: string): string[] {
  const cleaned = template.replace(SPINTAX_RE, 'x').replace(NAME_RE, 'x')
  return cleaned.match(/[{[][^{}[\]]{0,40}[}\]]/g) ?? []
}
