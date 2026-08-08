// Etichette del pulsante di avvio di una campagna ferma.
//
// Il pulsante chiama sempre POST /campaigns/{id}/start-scrape, ma cosa quell'endpoint
// faccia davvero dipende da due cose:
//   - source_type: 'import' -> risoluzione delle righe importate (apre i profili),
//                  'scrape'  -> Fase Lista (NON apre i profili);
//   - enrichment_level, ma solo sulle import: e' la risoluzione a leggere i dati.
//
// Perche' esiste questo file invece di una stringa inline: l'etichetta unica "Avvia
// scraping" faceva credere che su una campagna scrape quel pulsante aprisse i profili e
// potesse raccogliere i contatti. Non e' mai stato vero — quella e' la Fase Bio, che si
// avvia a parte — e non distingueva i tre livelli di arricchimento. Le stesse stringhe
// servono al dettaglio campagna e alla lista campagne: tenerle in un posto solo evita
// che le due pagine raccontino due storie diverse dello stesso pulsante.
import type { Campaign } from './types'

export type AvvioLabel = {
  /** Etichetta del pulsante nel dettaglio campagna. */
  label: string
  /** Etichetta compatta per la lista campagne, dove lo spazio e' poco. */
  breve: string
  /** Spiegazione completa, come `title` del pulsante. */
  title: string
}

const LISTA_TITLE =
  'Raccoglie la lista dei profili dal target. Nessuna visita ai profili: bio e contatti '
  + 'sono la Fase Bio, che si avvia a parte dal pannello a due fasi.'

/**
 * @param ripresa true quando la campagna e' in `error` e il pulsante riprende invece di
 *   avviare: cambia solo il verbo, non il significato.
 */
export function avvioLabel(campaign: Campaign, ripresa = false): AvvioLabel {
  if (campaign.source_type !== 'import') {
    return {
      label: ripresa ? 'Riavvia Fase Lista' : 'Avvia Fase Lista',
      breve: 'Fase Lista',
      title: LISTA_TITLE,
    }
  }

  const prefisso = ripresa ? 'Riprendi risoluzione' : 'Risolvi lista'
  switch (campaign.enrichment_level ?? 'none') {
    case 'contacts':
      return {
        label: `${prefisso} (bio + contatti)`,
        breve: 'Risolvi (bio + contatti)',
        title: 'Apre un profilo per riga e raccoglie bio, email e telefono.',
      }
    case 'bio':
      return {
        label: `${prefisso} (bio)`,
        breve: 'Risolvi (bio)',
        title: 'Apre un profilo per riga e raccoglie la bio. Nessuna chiamata a email/telefono.',
      }
    default:
      // Livello 'none' su import: la risoluzione apre comunque il profilo, perche' serve
      // l'ID Instagram per creare il contatto, e la bio arriva gratis dalla stessa
      // risposta. L'unica cosa che il livello spegne davvero, oggi, sono le chiamate a
      // email/telefono. Dirlo qui evita di promettere un risparmio che non c'e'.
      return {
        label: prefisso,
        breve: 'Risolvi lista',
        title:
          "Apre un profilo per riga per ottenere l'ID Instagram; la bio arriva dalla stessa "
          + 'risposta. Nessuna chiamata a email/telefono.',
      }
  }
}
