# Runbook operativo — canale WhatsApp

> Per chi opera una campagna (cliente o chi gestisce il canale per suo conto) e per chi risponde a un incidente. SDD §14 (M5), Q80.
>
> Ogni endpoint qui sotto e' stato provato contro l'app avviata: i path sono quelli veri, comprensivi del prefisso `/api` (i router WA sono montati con `prefix="/api"` in `backend/app/main.py`). Un path senza `/api` risponde **404**.

## Prima di tutto: come si chiamano gli endpoint

Tutti i router WA stanno dietro autenticazione (`Depends(get_current_user)`): **serve un Bearer token**, altrimenti si prende `401 Not authenticated` e sembra che l'endpoint non esista.

```bash
# 1. base URL dell'API (adatta host/porta al deploy in uso)
API=http://localhost:8000/api

# 2. token: si ottiene una volta e si riusa
TOKEN=$(curl -s -X POST $API/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"<la tua email>","password":"<la tua password>"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 3. da qui in poi ogni chiamata porta l'header
curl -s -H "Authorization: Bearer $TOKEN" $API/wa/ops/status
```

Tutti i comandi seguenti danno per scontato che `$API` e `$TOKEN` siano impostati.

## Deploy: le migrazioni PRIMA del riavvio

Le migrazioni non girano al boot, vanno lanciate a mano:

```bash
cd backend && python -m scripts.migrate
```

Da M5 questo ordine non e' piu' una buona pratica ma un vincolo: l'avvio
dell'applicazione legge una colonna introdotta dalla migrazione 028
(`wa_numbers.warmup_advanced_date`). Se il backend riparte **prima** di aver
migrato, Postgres risponde `UndefinedColumn` (42703) dentro il lifespan e
**uvicorn non parte affatto** — non e' un errore su una singola pagina, e'
il servizio che non sale. Prima di M5 lo stesso errore sarebbe emerso solo
alla prima richiesta sui numeri.

Il rimedio e' lanciare la migrazione e riavviare: non serve rollback.

## Stati di un numero — cosa significano

Li si legge in `GET $API/wa/numbers` (colonna "Stato" della pagina Numeri).

| Stato | Significato | Il worker manda? |
|---|---|---|
| `pending_qr` | Registrato, QR mai scansionato | No |
| `active` | Sessione WhatsApp Web viva | **Si'** |
| `qr_required` | La sessione e' caduta, serve un nuovo QR | No |
| `disconnected` | Il browser non raggiunge la sessione | No |
| `cooldown` | In pausa forzata dopo un segnale di rischio | No |
| `suspended` | Sospeso a mano da un operatore | No |
| `retired` | Ritirato definitivamente | No |

Solo `active` invia (`wa_worker.py`). `suspended` e `retired` li mette e li toglie **una persona**, mai il sistema.

## Il primo messaggio arriva ~15 minuti dopo l'avvio. E' giusto cosi'

La prima cosa che sorprende guardando una campagna partire: **non parte niente per un quarto d'ora.** Non e' un guasto ed e' l'unico comportamento accettabile.

Ogni mini-sessione di invio apre un browser nuovo, e WhatsApp Web dopo ogni connessione **risincronizza** la cronologia delle chat. Dentro quella finestra la guardia opt-out non leggerebbe un silenzio, leggerebbe il vuoto: una chat non ancora sincronizzata sembra non avere messaggi, quindi uno STOP scritto la settimana scorsa non esisterebbe. Il canale aspetta `WA_RESYNC_QUARANTINE_MIN` minuti (15 di default) prima del primo invio di ogni sessione, e nel log compare:

```
[WA] <id>: quarantena di risincronizzazione, attendo 15 min prima del primo invio
```

Se invece il primo messaggio parte **subito**, qualcosa non va: `WA_RESYNC_QUARANTINE_MIN` e' stata azzerata. Da controllare prima di lasciar continuare.

L'attesa vale per ogni mini-sessione, non solo per la prima: fra una sessione e l'altra c'e' un break di 20-40 minuti e il browser viene chiuso, quindi al giro dopo la sincronizzazione ricomincia. In pratica un numero manda a raffiche: ~15 minuti fermo, poi 8-15 messaggi con una novantina di secondi l'uno dall'altro, poi il break.

**Il browser si apre solo se c'e' davvero qualcosa da mandare.** Prima di aprirlo il worker controlla, con due query, che ci sia una campagna running, che l'ora sia dentro la finestra, che il cap non sia esaurito e che esista almeno un contatto pronto. Senza quel controllo un numero col cap gia' finito avrebbe aperto WhatsApp Web, tenuto il lucchetto del profilo per un quarto d'ora senza mandare niente, chiuso, e ricominciato dopo il break — tutta la notte, occupando il profilo che servirebbe all'health-check e allo scan delle risposte.

## Cosa deve/non deve fare il cliente durante una campagna attiva

**Deve:**
- Tenere il telefono con WhatsApp acceso e connesso a internet (il numero e' collegato via WhatsApp Web/dispositivo collegato: se il telefono resta offline a lungo, la sessione web puo' cadere).
- Non rispondere lui stesso, dal telefono, ai contatti che sono DENTRO una campagna attiva — il reply-watcher e la guardia pre-invio leggono le risposte per fermare la sequenza (opt-out/`replied`); una risposta "rubata" dal cliente prima che il sistema la veda non cambia il comportamento del bot, ma confonde la lettura di cosa ha risposto chi.

**Non deve:**
- Rimuovere il dispositivo collegato (WhatsApp → Impostazioni → Dispositivi collegati) mentre una campagna e' `running` — la sessione cade, le campagne su quel numero vanno in pausa automaticamente (vedi sotto), e va rifatto lo scan del QR.
- Disinstallare/cambiare WhatsApp sul telefono, cambiare numero, o farlo scansionare da un altro dispositivo — stessa conseguenza.

## Incidente: sessione WhatsApp Web persa

**Sintomo**: arriva un messaggio Telegram tipo `WhatsApp: numero <id> -> <stato>. Campagne in pausa. Serve un nuovo QR (lo scansiona il cliente).` Origine: cron `wa_session_healthcheck` (`backend/app/workers/cron_worker.py`), gira ai minuti 0 e 30 dalle **9 alle 19** (`hour=set(range(9, 20))`: l'ultimo giro e' alle 19:30, non alle 20).

**Cosa succede automaticamente**: il cron rileva che la sessione non e' piu' `active`, mette in `paused` tutte le campagne `running` su quel numero (query diretta su `wa_campaigns`; le righe `wa_campaign_contacts` non vengono riposizionate — restano ferme dove sono, nessuna perdita di coda) e avvisa via Telegram. Il worker di invio non prova piu' a mandare messaggi su un numero non-`active`.

**Cosa fare**:

1. **Trovare quali campagne sono in pausa** (`GET $API/wa/ops/status` da' solo il conteggio dei `running`, non la lista):
   ```bash
   curl -s -H "Authorization: Bearer $TOKEN" "$API/wa/campaigns?status=paused"
   ```
2. Il cliente (o chi ha il telefono) riapre WhatsApp Web/collega di nuovo il dispositivo e scansiona il nuovo QR:
   ```bash
   curl -s -X POST -H "Authorization: Bearer $TOKEN" $API/wa/numbers/<id>/login
   ```
3. **Forzare il ricontrollo della sessione.** Passaggio necessario e facile da saltare: dopo una riattivazione manuale il numero e' in `pending_qr`, e l'health-check **esclude** `pending_qr` (oltre a `retired` e `suspended`). Senza questa chiamata il numero non tornera' `active` da solo, per quanto si aspetti:
   ```bash
   curl -s -X POST -H "Authorization: Bearer $TOKEN" $API/wa/numbers/<id>/check
   ```
4. Verificare che sia tornato `active`:
   ```bash
   curl -s -H "Authorization: Bearer $TOKEN" $API/wa/numbers/<id>
   curl -s -H "Authorization: Bearer $TOKEN" $API/wa/ops/status   # campo numeri_attivi
   ```
5. Riprendere ciascuna campagna che era `paused`:
   ```bash
   curl -s -X POST -H "Authorization: Bearer $TOKEN" $API/wa/ops/campaigns/<id>/resume
   ```
   **Importante**: l'endpoint controlla che il numero sia gia' `active` prima di flippare lo stato — chiamandolo PRIMA dello scan del QR risponde `{"resumed": false, "motivo": "numero in stato ..., non active"}` senza fare danni, non crea una campagna "running fantasma".
6. Se dopo il resume la campagna non riparte da sola:
   ```bash
   curl -s -X POST -H "Authorization: Bearer $TOKEN" $API/wa/ops/campaigns/<id>/kick
   ```
   Riaccoda il worker. Da M5.1 e' davvero il caso raro che questa riga
   dichiarava: avviare o riprendere una campagna accoda il job da solo, e il
   cron `wa_campaign_supervisor` (minuti 10/25/40/55, ore attive) riaccoda
   entro un quarto d'ora qualunque campagna `running` sia rimasta senza worker.
   **Prima di M5.1 non era vero**: `POST /wa/campaigns/{id}/start` scriveva
   `running` e non accodava niente, quindi `kick` era l'unico modo di far
   partire davvero una campagna e nessuno lo sapeva.

**Nota**: `resume_campaign` (M3, eccezione dichiarata al contratto M2-M3 §4.1, vedi commento nel codice) non ristampa `next_action_at` sulle righe contatto come farebbe un resume "vero" — le righe con appuntamento futuro restano ferme fino al loro turno, quelle gia' in coda ripartono. Non serve altro per questo scenario.

## Incidente: un numero resta bloccato in `cooldown`

Il timer di cooldown **non e' a DB**: non esiste una colonna `cooldown_until`, il timer vive in Redis con un TTL (`wa_number_manager`). Conseguenza operativa: **se Redis e' giu' o e' stato svuotato, un numero puo' restare `cooldown` senza che niente lo segnali e senza che nessun cron lo liberi.** Se un numero e' fermo in `cooldown` piu' a lungo del previsto, controllare prima che Redis sia raggiungibile.

⚠️ **Da M5.1, `POST /wa/numbers/<id>/check` NON toglie piu' il cooldown** — e prima non doveva toglierlo comunque. L'health-check girava ogni 30 minuti, vedeva la sessione viva e rimetteva il numero `active`: il cooldown di 4 ore imposto dopo tre guasti consecutivi durava mezz'ora, e nessuno se ne accorgeva. Ora `check` aggiorna la diagnosi (un cooldown che ha anche perso la sessione diventa `disconnected`) ma non promuove: quello lo fa solo la scadenza del timer.

La regola esatta è **"nessuna lettura automatica toglie un cooldown"**, non "niente lo toglie". Le due uscite volute restano:

| Come | Effetto |
|---|---|
| Scadenza del timer Redis | `release_expired_wa_cooldowns`, dentro l'health-check, rimette `active` |
| `POST /wa/numbers/<id>/login` (riscansione del QR) | Toglie il cooldown: è un atto esplicito di un operatore davanti allo schermo, non la lettura di un DOM |
| `redis-cli DEL wa:cooldown:<id>` + `POST .../check` | Scorciatoia manuale, quando si è già verificata e risolta la causa |

## Incidente: una campagna e' finita in `error`

Ci finisce solo per FM2: tre guasti NOSTRI consecutivi su chat diverse (DOM cambiato, selettore rotto, pagina in stato inatteso). I contatti **non** sono bruciati — restano `queued`, e' il punto di quel meccanismo.

Prima si guarda perche' (log `[WA]`, alert Telegram), poi si recupera. Il recupero e' in due passi apposta:

```bash
# 1. error -> paused, con il motivo (obbligatorio, resta nei log e negli eventi)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"motivo":"selettore ROW aggiornato, verificato su chat di prova"}' \
  $API/wa/campaigns/<id>/recover

# 2. paused -> running, con tutte le validazioni di avvio
curl -s -X POST -H "Authorization: Bearer $TOKEN" $API/wa/campaigns/<id>/resume
```

Il primo passo non fa ripartire niente da solo: `resume` rivalida che il numero sia `active`, che non ci sia un'altra campagna in corso sullo stesso numero, ristampa `next_action_at` e accoda il worker. Prima di M5.1 il passo 1 non esisteva e da `error` si poteva solo `stop`.

⏳ **Il passo 2 non funziona nelle prime 4 ore.** FM2 mette insieme la campagna in `error` **e** il numero in `cooldown` per quattro ore: finché quel timer non scade, `resume` risponde 422 dicendo che il numero non è attivo. Non è un secondo guasto ed è per questo che il passo 1 ora risponde con `stato_numero` e un `prossimo_passo` che lo dice esplicitamente. Se hai già verificato e risolto la causa e non vuoi aspettare, si toglie la chiave Redis del cooldown e si forza il ricontrollo:

```bash
redis-cli DEL wa:cooldown:<number_id>          # oppure memurai-cli
curl -s -X POST -H "Authorization: Bearer $TOKEN" $API/wa/numbers/<number_id>/check
```

In alternativa, e senza toccare Redis: **riscansionare il QR** (`POST /wa/numbers/<id>/login`) toglie il cooldown, perché è un atto esplicito di un operatore e non una lettura automatica. Rifare il QR solo per questo è però sproporzionato — la sessione è viva.

## Kill-switch — fermare TUTTO il canale WhatsApp

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason":"<motivo>"}' $API/wa/ops/halt
```

Ferma l'invio su OGNI numero/campagna, indipendentemente dal loro stato — e' il kill-switch di canale (`bot_state_service.halt_wa`, separato dal kill-switch generale del bot Instagram: un incidente WhatsApp non ferma Instagram e viceversa). Ha effetto **immediato**, e' uno stato a DB.

Per far ripartire (nessun body), e verificare che `wa_halted` sia tornato `false`:
```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $API/wa/ops/resume
curl -s -H "Authorization: Bearer $TOKEN" $API/wa/ops/status
```

**Dopo il resume gli invii non ripartono all'istante.** Il kill-switch fa uscire il job di invio senza rischedularlo (`wa_halted` e' un motivo terminale per `wa_send_task`): la campagna resta `running` ma il suo worker e' morto. Da M5.1 lo rimette in piedi il cron `wa_campaign_supervisor`, quindi l'attesa e' al massimo un quarto d'ora. Per non aspettare: `POST $API/wa/ops/campaigns/<id>/kick`.

**Quando usarlo**: sospetto di ban/warning WhatsApp su piu' numeri contemporaneamente, comportamento anomalo del bot non spiegabile con un solo numero, richiesta esplicita di fermare tutto mentre si indaga. Non serve per un singolo numero con sessione caduta (basta aspettare il cron o gestirlo come sopra).

**Da non confondere con `WA_SEND_ENABLED=false`**, che e' il "Cancello 0" a monte di tutto: e' una variabile d'ambiente, **richiede un restart** per cambiare, e a spento il worker non manda nulla in nessun caso. Il kill-switch e' la leva da usare durante un incidente; `WA_SEND_ENABLED` e' la leva che tiene spento il canale finche' non si decide di accenderlo.

## Rampa volume — chi guarda i warning, chi decide di fermarla

La rampa si **ferma al primo warning**: non e' un traguardo da raggiungere a tutti i costi.

I gradini configurati oggi (`WA_WARMUP_STEPS`) sono, in messaggi al giorno:

| Giorno | 1 | 2 | 3 | 4 | 5 | 6 | 7+ |
|---|---|---|---|---|---|---|---|
| Cap | 20 | 20 | 30 | 40 | 60 | 80 | 100 |

Un gradino al giorno, poi plateau a 100. La SDD §14/BT12 cita una sequenza piu' corta (10 → 30 → 60 → 100): la lista qui sopra e' quella che comanda davvero, ed e' piu' prudente (parte da 20 e ci arriva in sette giorni invece che in quattro). Per cambiarla si modifica `WA_WARMUP_STEPS`, **non** il passo: `WA_WARMUP_ADVANCE_STEPS_PER_DAY` e' in gradini al giorno e deve restare 1 — alzarlo significa saltare gradini, non mandare piu' messaggi.

**Attenzione ai nomi.** Il campo si chiama `daily_cap` su `wa_numbers` (`PATCH $API/wa/numbers/{id}`) ma `daily_limit` su `wa_campaigns` (`PATCH $API/wa/campaigns/{id}`): non e' lo stesso nome sulle due tabelle. Il cap effettivo di invio (`wa_number_manager.effective_wa_daily_cap`) e' il **minimo fra tre valori**: `daily_cap` del numero, `daily_limit` della campagna (se impostato) e il gradino di warmup del numero. Se `warmup_day` e' basso, alzare solo `daily_cap`/`daily_limit` non sposta il tetto reale.

**Come funziona il gradino di warmup.** `warmup_day` e' un **indice** nella lista `WA_WARMUP_STEPS` (non un contatore di messaggi): `warmup_day = 3` significa "terzo valore della lista". Il valore in messaggi si legge senza fare conti dal campo `warmup_cap` di `GET $API/wa/numbers/{id}`, ed e' la colonna "Giorno rampa" della pagina Numeri.

Da M5 il gradino **sale da solo**: `advance_wa_warmup_if_needed` gira al boot dell'app e dal cron giornaliero, e avanza di un gradino al giorno i numeri `active`, fermandosi (plateau) sull'ultimo gradino della lista. Due conseguenze da tenere a mente:

- **Un riavvio dell'applicazione conta come un avanzamento** se quel numero non e' ancora stato avanzato quel giorno.
- **Abbassare `warmup_day` a mano NON e' una frenata durevole**: al prossimo avanzamento il numero risale partendo dal valore impostato. Dopo un warning, la leva che regge nel tempo e' `daily_cap`.

`warmup_day = 0` **non e' il valore piu' prudente**: significa "fuori warmup", quindi il gradino sparisce dal `min()` (resta solo `daily_cap`) e il numero non viene piu' avanzato in automatico. Non usarlo per frenare.

**Alzare `daily_limit` di una campagna gia' avviata oggi NON e' possibile**: `PATCH $API/wa/campaigns/{id}` risponde **409** su qualunque stato diverso da `draft` — e nemmeno mettere la campagna in pausa aiuta, perche' `paused` non e' `draft`. Durante la rampa la leva praticabile e' quindi `daily_cap` sul numero.

**Quanto e' stato inviato davvero.** Il campo `inviati_oggi` di `GET $API/wa/ops/status` e' un totale **globale** (tutti i numeri, tutti i tenant), mentre il cap effettivo e' per-numero: confrontarli fa credere di aver sforato quando non e' vero. Il contatore giusto per un singolo numero e' `sent_today`:
```bash
curl -s -H "Authorization: Bearer $TOKEN" $API/wa/numbers/<id>   # campi sent_today, warmup_cap, daily_cap
```

**Responsabilita'**: la salita e' automatica, la decisione umana e' **fermarla**. Chi ha acceso `WA_SEND_ENABLED` e avviato la campagna reale ha il compito di guardare i segnali e abbassare `daily_cap` (o usare il kill-switch) al primo segnale ambiguo, non solo a un errore conclamato. Nessuno deve fare qualcosa perche' il volume salga: succede da solo.

## Richieste GDPR

**Revoca di un opt-out** (un contatto che chiede di essere ricontattato dopo essersi disiscritto). La nota e' obbligatoria: e' la traccia del perche' la revoca e' legittima.
```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"note":"<chi ha chiesto la revoca, quando, come>"}' \
  $API/wa/ops/contacts/<contact_id>/revoke-optout
```

**Cancellazione totale dei dati di un cliente** ("il cliente X chiude il rapporto"): script `backend/scripts/wa_purge_tenant.py`. Cancella ogni riga `wa_*` di quel tenant, il tenant stesso e il profilo browser di ogni suo numero. **Irreversibile.**
```bash
cd backend
python -m scripts.wa_purge_tenant --tenant-id <uuid>          # dry-run: conta e basta, NON cancella
python -m scripts.wa_purge_tenant --tenant-id <uuid> --yes    # cancellazione REALE
```
Senza `--yes` non cancella nulla; `--dry-run` insieme a `--yes` vince il dry-run. Lanciarlo **dalla stessa directory da cui gira il backend**: `BROWSER_PROFILES_DIR` puo' essere un path relativo, e da un'altra directory lo script non troverebbe i profili da rimuovere (li elenca a schermo con il path assoluto, controllare quell'elenco). Uscita `2` = DB pulito ma almeno un profilo browser non rimosso, serve pulizia manuale della cartella indicata.

## Riferimenti

- Contratto M2-M3: `docs/whatsapp/contratto-M2-M3.md`
- SDD: `docs/whatsapp/SDD-whatsapp-channel.md` §14
- Endpoint operativi: `backend/app/api/wa_ops.py` · numeri: `backend/app/api/wa_numbers.py`
- Cron: `backend/app/workers/cron_worker.py` (`wa_session_healthcheck`, `wa_reply_scan`)
- Cap e rampa: `backend/app/services/wa_number_manager.py`
- Purge GDPR: `backend/scripts/wa_purge_tenant.py`
