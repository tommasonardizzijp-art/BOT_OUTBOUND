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
