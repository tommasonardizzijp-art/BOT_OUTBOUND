// Gemello TypeScript della guardia backend `valida_ai_senza_bio`
// (backend/app/models/campaign.py). Il backend resta l'autorita': risponde 400 comunque.
// Questo serve solo a spegnere il pulsante e dire perche', invece di far scoprire il
// divieto dopo aver compilato il form.
//
// Se cambi la regola qui, cambiala anche di la': sono due copie della stessa decisione.

/** «Solo DM» non apre il profilo, quindi con l'AI accesa il messaggio si genererebbe
 *  senza bio: l'AI ricopierebbe il template spendendo una chiamata. */
export function soloDmVietato(aiEnabled: boolean): boolean {
  return aiEnabled
}

export const MOTIVO_SOLO_DM_VIETATO =
  'Con la personalizzazione AI attiva questo livello non ha dati su cui lavorare: '
  + 'la bio arriva solo aprendo il profilo prima di scrivere, e «Solo DM» non lo apre. '
  + 'Alza il livello a «Bio», oppure spegni la personalizzazione AI e usa i template.'

/** Una campagna gia' salvata in questa combinazione non puo' partire: da 22/08 la
 *  guardia vale anche all'AVVIO (`ensure_campaign_can_send_messages` lato backend),
 *  non solo alla creazione e alla modifica. Serve a spegnere i pulsanti di avvio
 *  invece di far scoprire il divieto DOPO il click, con un toast di errore. */
export function campagnaNonAvviabile(
  aiEnabled: boolean | null | undefined,
  enrichmentLevel: string | null | undefined,
): boolean {
  return soloDmVietato(aiEnabled ?? false) && (enrichmentLevel ?? 'none') === 'none'
}

export const MOTIVO_NON_AVVIABILE =
  'Questa campagna non puo' + '\u2019' + ' partire: ha la personalizzazione AI attiva '
  + 'con il livello «Solo DM», che non apre mai il profilo — l' + '\u2019' + 'AI genererebbe '
  + 'i messaggi senza la bio. Alza il livello a «Bio», oppure spegni la personalizzazione AI.'
