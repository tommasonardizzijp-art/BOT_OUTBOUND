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
   Riaccoda il worker: serve quando un job e' andato perso, non e' il caso normale.

**Nota**: `resume_campaign` (M3, eccezione dichiarata al contratto M2-M3 §4.1, vedi commento nel codice) non ristampa `next_action_at` sulle righe contatto come farebbe un resume "vero" — le righe con appuntamento futuro restano ferme fino al loro turno, quelle gia' in coda ripartono. Non serve altro per questo scenario.

## Incidente: un numero resta bloccato in `cooldown`

Il timer di cooldown **non e' a DB**: non esiste una colonna `cooldown_until`, il timer vive in Redis con un TTL (`wa_number_manager`). Conseguenza operativa: **se Redis e' giu' o e' stato svuotato, un numero puo' restare `cooldown` senza che niente lo segnali e senza che nessun cron lo liberi.** Se un numero e' fermo in `cooldown` piu' a lungo del previsto, controllare prima che Redis sia raggiungibile, poi forzare il ricontrollo con `POST $API/wa/numbers/<id>/check`.

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

**Responsabilita'**: chi ha acceso `WA_SEND_ENABLED` e avviato la campagna reale decide se e quando salire di gradino, e decide di fermarsi al primo segnale ambiguo (non solo a un errore conclamato).

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
