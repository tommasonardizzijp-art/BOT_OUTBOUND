# Runbook operativo — canale WhatsApp

> 1 pagina. Per chi opera una campagna (cliente o chi gestisce il canale per suo conto) e per chi risponde a un incidente. SDD §14 (M5), Q80.

## Cosa deve/non deve fare il cliente durante una campagna attiva

**Deve:**
- Tenere il telefono con WhatsApp acceso e connesso a internet (il numero e' collegato via WhatsApp Web/dispositivo collegato: se il telefono resta offline a lungo, la sessione web puo' cadere).
- Non rispondere lui stesso, dal telefono, ai contatti che sono DENTRO una campagna attiva — il reply-watcher e la guardia pre-invio leggono le risposte per fermare la sequenza (opt-out/`replied`); una risposta "rubata" dal cliente prima che il sistema la veda non cambia il comportamento del bot, ma confonde la lettura di cosa ha risposto chi.

**Non deve:**
- Rimuovere il dispositivo collegato (WhatsApp → Impostazioni → Dispositivi collegati) mentre una campagna e' `running` — la sessione cade, le campagne su quel numero vanno in pausa automaticamente (vedi sotto), e va rifatto lo scan del QR.
- Disinstallare/cambiare WhatsApp sul telefono, cambiare numero, o farlo scansionare da un altro dispositivo — stessa conseguenza.

## Incidente: sessione WhatsApp Web persa

**Sintomo**: arriva un messaggio Telegram tipo `WhatsApp: numero <id> -> <stato>. Campagne in pausa. Serve un nuovo QR (lo scansiona il cliente).` Origine: cron `wa_session_healthcheck` (`backend/app/workers/cron_worker.py`), gira ogni 30 minuti nelle ore attive (9-20).

**Cosa succede automaticamente**: il cron rileva che la sessione non e' piu' `active`, mette in `paused` tutte le campagne `running` su quel numero (query diretta su `wa_campaigns`, non tocca le righe `wa_campaign_contacts` — restano ferme dove sono, nessuna perdita di coda), e avvisa via Telegram. Il worker di invio non prova piu' a mandare messaggi su un numero non-`active`.

**Cosa fare**:
1. Il cliente (o chi ha il telefono) riapre WhatsApp Web/collega di nuovo il dispositivo, scansiona il nuovo QR.
2. Verificare che il numero sia tornato `active`: `GET /wa/ops/status` (endpoint gia' esistente, `backend/app/api/wa_ops.py`) mostra `numeri_attivi`.
3. Riprendere manualmente ciascuna campagna che era `paused`: `POST /wa/ops/campaigns/{id}/resume`. **Importante**: questo endpoint controlla che il numero sia gia' `active` prima di flippare lo stato — se lo chiami PRIMA che il QR sia stato riscansionato, risponde `{"resumed": false, "motivo": "numero in stato ..., non active"}` senza fare danni, non crea una campagna "running fantasma".
4. Se dopo il resume la campagna non riparte da sola: `POST /wa/ops/campaigns/{id}/kick` riaccoda il worker (serve quando un job e' andato perso, non e' il caso normale).

**Nota**: `resume_campaign` (M3, eccezione dichiarata al contratto M2-M3 §4.1, vedi commento nel codice) non ristampa `next_action_at` sulle righe contatto come farebbe un resume "vero" — le righe con appuntamento futuro restano ferme fino al loro turno, quelle gia' in coda ripartono. Non serve altro per questo scenario.

## Kill-switch — fermare TUTTO il canale WhatsApp

Per un incidente che riguarda l'intero canale (non un solo numero): `POST /wa/ops/halt` con body `{"reason": "<motivo>"}`. Ferma l'invio su OGNI numero/campagna, indipendentemente dal loro stato — e' il kill-switch di canale (`bot_state_service.halt_wa`, separato dal kill-switch generale del bot Instagram: un incidente WhatsApp non ferma Instagram e viceversa).

Per far ripartire: `POST /wa/ops/resume` (nessun body). Verificare `GET /wa/ops/status` — campo `wa_halted` deve tornare `false`.

**Quando usarlo**: sospetto di ban/warning WhatsApp su piu' numeri contemporaneamente, comportamento anomalo del bot non spiegabile con un solo numero, richiesta esplicita di fermare tutto mentre si indaga. Non serve per un singolo numero con sessione caduta (basta aspettare il cron o gestirlo come sopra).

## Rampa volume — chi guarda i warning, chi decide di fermarla

La rampa (10 → 30 → 60 → 100 messaggi/giorno, prima campagna vera, SDD §14/BT12) e' un protocollo operativo, non un meccanismo automatico: il cap si alza A MANO, un gradino alla volta, e si **ferma al primo warning** — non e' un traguardo da raggiungere a tutti i costi.

**Attenzione ai nomi**: il campo si chiama `daily_cap` su `wa_numbers` (PATCH `/wa/numbers/{id}`) ma `daily_limit` su `wa_campaigns` (PATCH `/wa/campaigns/{id}`) — non e' lo stesso nome sulle due tabelle. Il cap effettivo di invio (`wa_number_manager.effective_wa_daily_cap`) e' il MINIMO fra tre valori: `daily_cap` del numero, `daily_limit` della campagna (se impostato) e il gradino di warmup del numero (`warmup_day` + `WA_WARMUP_STEPS`, default `20,20,30,40,60,80,100`) — se `warmup_day` e' ancora basso, alzare solo `daily_cap`/`daily_limit` non sposta il tetto reale. `warmup_day` pero' NON e' scrivibile via API (e' escluso a proposito da `CAMPI_MODIFICABILI` in `wa_numbers.py`, di proprieta' M3 a runtime) e oggi (M5) non c'e' in `wa_number_manager.py` alcun incremento automatico giornaliero equivalente a quello degli account Instagram (`advance_warmup_if_needed`): resta a 1 dalla creazione/riattivazione in poi, salvo intervento diretto sul DB. Verificare quindi il valore corrente di `warmup_day` (`GET /wa/numbers/{id}`) prima di assumere che alzare `daily_cap` basti a far salire il volume inviato.

**Responsabilita'**: chi ha acceso `WA_SEND_ENABLED` e avviato la campagna reale decide se e quando salire di gradino, e decide di fermarsi al primo segnale ambiguo (non solo a un errore conclamato). Oggi (M5) non esiste un endpoint dedicato "stato rampa" — lo stato si legge da `GET /wa/ops/status` (`inviati_oggi`) confrontato col cap effettivo corrente (vedi sopra) della campagna/numero.

## Riferimenti

- Contratto M2-M3: `docs/whatsapp/contratto-M2-M3.md`
- SDD: `docs/whatsapp/SDD-whatsapp-channel.md` §14
- Endpoint operativi: `backend/app/api/wa_ops.py`
- Cron: `backend/app/workers/cron_worker.py` (`wa_session_healthcheck`, `wa_reply_scan`)
