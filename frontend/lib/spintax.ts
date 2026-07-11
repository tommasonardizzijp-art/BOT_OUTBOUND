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
 *  li rifiuta al rendering — segnalali nel form. Copre anche graffe/quadre
 *  spaiate (es. spintax con un gruppo mai chiuso: "{Ciao|Hey Marco"): quelle
 *  non matchano ne' SPINTAX_RE ne' il pattern "ben formato" sotto e prima
 *  passavano la validazione a zero segnali, finendo letterali nel DM. */
export function findUnknownPlaceholders(template: string): string[] {
  const cleaned = template.replace(SPINTAX_RE, 'x').replace(NAME_RE, 'x')
  const found = cleaned.match(/[{[][^{}[\]]{0,40}[}\]]/g) ?? []
  if (found.length > 0) return found

  const orphan = cleaned.match(/[{}[\]]/)
  if (orphan && orphan.index !== undefined) {
    return [cleaned.slice(orphan.index, orphan.index + 20)]
  }
  return found
}
