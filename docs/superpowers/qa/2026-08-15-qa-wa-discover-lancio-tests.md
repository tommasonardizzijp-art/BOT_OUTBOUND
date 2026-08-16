# Test manuali UI — Lancio dell'auto-discover WhatsApp (`/wa/numeri` + `/wa/scoperti`)

**ESEGUITI il 16/08**, dopo il merge della PR #85 e l'applicazione della migrazione `035` in produzione. Backend + worker ARQ + cron + frontend avviati, browser vero (Patchright, profilo temporaneo — mai i profili WhatsApp).

**Esito complessivo: 17 PASS · 0 FAIL aperti · 5 SKIP · 2 rilievi da decidere.**

Due difetti trovati qui sono stati corretti e riverificati nello stesso giro (PR #86 e #87), ciascuno con prova del nove. Restano aperti solo i casi che richiedono **il telefono collegato**, che il 16/08 non lo era.

Vincolo di sicurezza rispettato in tutto il giro: **nessuna scansione vera e' mai partita**. Ogni `POST /discover` e' stato fatto con una condizione di rifiuto gia' in piedi (lucchetto profilo su Redis, kill-switch, soglia RAM alzata), cosi' il gate rifiuta a monte di `apri_run` e nessun browser WhatsApp si apre. Verificato a DB dopo ogni blocco: zero run `running` residue.

Convenzioni: dati di test prefissati `QAWAD-`, cancellati in chiusura di ogni giro (`try/finally`). Lucchetto Redis e kill-switch rimessi come erano e **riletti** per confermarlo.

## `/wa/numeri` — colonna e bottone (1-10)

1. Numero `active` mai scansionato → colonna "Ultimo scan" mostra "Mai", bottone "Scansiona contatti" abilitato.
   - **PASS (16/08)** — su `PRIMERO TEST`, tabella vergine (`wa_discover_runs` appena creata dalla `035`).
2. Numero `active` GIA' scansionato in precedenza (una run `done` chiusa, nessuna `running`) → bottone ANCORA abilitato, un secondo giro parte normalmente.
   - **PASS (16/08)** — con due run chiuse a storico il bottone resta abilitato.
3. Click su "Scansiona contatti" → dialog di conferma con la frase "blocca gli invii su TUTTI i numeri finche' non finisce" visibile prima di confermare.
   - **PASS (16/08)** — frase presente nel testo del dialog.
4. Conferma il dialog → bottone passa a "Scansione in corso..." disabilitato, toast "Scansione avviata per **label**".
   - **PARZIALE (16/08)** — la meta' verificabile senza telefono e' PASS: con una run `running` a DB il bottone e' "Scansione in corso..." **e disabilitato** (caso 7). Il toast "Scansione avviata" NON e' stato visto, perche' ogni conferma di questo giro e' stata deliberatamente fatta sotto una condizione di rifiuto per non far partire scansioni vere. Da chiudere col telefono collegato.
5. A scan finito → "Ultimo scan" mostra data, contatori, percentuale, motivo leggibile; toast di esito **UNA** sola volta.
   - **PASS (16/08)** — cella: `15/15 (100%) · completo`, motivo tradotto e non grezzo. Toast: la transizione `running`→`done` e' stata provocata a DB mentre la pagina pollava, e i toast sono stati **campionati ogni 400 ms per 45 s** (oltre quattro giri di `refreshInterval`). Risultato: **una sola comparsa**, `"PRIMERO TEST: 10 chat coperte, 7 nuove."`. Nota di metodo: guardare i toast una volta sola dopo 15 s non prova nulla — durano ~4 s, e infatti al primo tentativo ne risultavano zero.
6. Doppio click ravvicinato su "Scansiona contatti" → solo UNA richiesta di rete parte.
   - **FAIL → PASS (16/08, PR #87)** — trovato ROSSO: **2** `POST /discover`. `ConfirmDialog` faceva `onOpenChange(false); onConfirm()`, e due click nello stesso tick girano entrambi prima che React ri-renderizzi. Corretto con una guardia in `useRef` (non in uno state: deve reggere dentro lo stesso tick). Riverificato: 1 richiesta. Prova del nove eseguita.
7. Ricarica la pagina mentre una scansione e' in corso → "In corso..." e bottone disabilitato.
   - **PASS (16/08)** — lo stato si rilegge dal backend, non dipende dal componente sopravvissuto al reload.
8. Bottone assente su un numero `pending_qr`/`qr_required`/`disconnected`/`retired`/`suspended`.
   - **SKIP (16/08)** — in produzione tutti e tre i numeri sono `active`. Non e' stato forzato uno stato diverso su un numero vero. Il gating a livello di API e' comunque gia' coperto per tutti e sei gli stati non-`active` dall'adversarial F.34.
9. Numero `active` non ancora sincronizzato → il motivo finale `sync_ignota`/`sync_sotto_soglia` scritto in chiaro.
   - **SKIP (16/08)** — richiede una scansione vera, quindi il telefono. La traduzione dei due motivi e' presente in `MOTIVO_LABEL` ed e' stata esercitata dal caso 11 (`sincronizzazione ignota` mostrata in testata).
10. Motivo NON presente in `MOTIVO_LABEL` → la cella mostra il codice grezzo, mai vuota o "undefined".
    - **PASS (16/08)** — forzato `motivo_inventato_qa` a DB su una run chiusa: la cella mostra il codice grezzo. Chiude anche l'adversarial C.24, che era SKIP verificato solo staticamente.

## `/wa/scoperti` — testata e storico (11-16)

11. Testata con "Ultimo scan **data**", contatori, motivo, e la frase sulla sincronizzazione ignota.
    - **PASS (16/08)** — `Ultimo scan 15/08, 23:57 — 57 su 100 (57%) · raccolta parziale` piu' la frase "Sincronizzazione ignota durante lo scan: e' il primo indiziato se la raccolta e' corta.", mostrata solo perche' il motivo non e' `completato`.
12. "Storico" visibile solo se ci sono almeno 2 run.
    - **RILIEVO (16/08)** — il bottone compare gia' con **UNA** run: `wa_discover_runs.storico()` include anche l'ultima, e la UI mostra il bottone su `storico.length > 0`. Non e' un difetto di correttezza (lo storico con una riga e' veritiero), ma non e' cio' che la lista si aspettava. **Decisione di Tommaso**: o si allinea la UI (`> 1`), o si allinea questa lista. Non toccato.
13. "Riscansiona" da `/wa/scoperti`: verificare la coerenza col bottone di `/wa/numeri`.
    - **INCOERENZA CONFERMATA (16/08)** — `/wa/numeri` chiede conferma con un dialog, `/wa/scoperti` fa partire la scansione **diretta** (`TestataScan.riscansiona`). La lista chiedeva di segnalarlo, non necessariamente un bug. **Decisione di Tommaso.** Non toccato — ma va notato che e' proprio la frase del dialog ("blocca gli invii su TUTTI i numeri") a essere l'informazione che qui non viene data.
14. Scansione avviata da `/wa/scoperti` che finisce → la lista delle chat si aggiorna DA SOLA.
    - **SKIP (16/08)** — richiede una scansione vera con raccolta di chat nuove, quindi il telefono.
15. Numero MAI scansionato → "Questo numero non e' mai stato scansionato.", nessun bottone Storico.
    - **PASS (16/08)** — entrambe le meta' verificate.
16. `saltate_gia_note > 0` → riga "N chat gia' note non sono state riaperte."
    - **FAIL → PASS (16/08, PR #87)** — trovato ROSSO: rendeva `"12chat gia' note..."`, **senza lo spazio**. Verificato nel DOM e non dedotto: due nodi di testo, `"12"` e `"chat gia'..."`, con lo spazio iniziale del secondo sparito nonostante il sorgente lo avesse. Corretto con `{' '}` esplicito. Riverificato: `"12 chat gia' note non sono state riaperte."`. Prova del nove eseguita.

## Guardie e messaggi (17-21)

17. Scan durante un invio sullo stesso numero → rifiutato `browser_occupato`, frase leggibile.
    - **PASS con riserva (16/08)** — il lucchetto e' stato preso su Redis **per davvero** (`wa:profile-lock:{number_id}`, stessa chiave e stesso formato `token:epoch` che usa `wa_profile_lock`), non mockato. Risposta: `409` con `{"codice":"browser_occupato","messaggio":"Il browser sulla macchina del backend e' gia' in uso..."}`, e la frase compare a schermo. **Riserva**: il lucchetto e' stato preso a mano e non da una mini-sessione d'invio vera, quindi resta provato dal codice (stesso namespace di chiave) e non dal campo che sia proprio il sender a prenderlo.
18. Scan mentre un ALTRO scan e' in corso → rifiutato `scan_gia_in_corso`, frase leggibile.
    - **FAIL → PASS (16/08, PR #86)** — trovato ROSSO, ed e' il difetto piu' grave del giro: rispondeva **500** ("Errore interno temporaneo del server") invece del 409. Causa: `chiudi_se_orfana` confrontava `started_at` (aware su PostgreSQL, colonna `timestamptz`) con `datetime.utcnow()` (naive) → `TypeError` risalito fino all'endpoint. La suite gira su SQLite, che restituisce naive: verde nei test, rotto in produzione. Riverificato: `409` con `"Una scansione su questo numero e' gia' in corso: aspetta che finisca."`.
19. Kill-switch alzato → rifiuto `canale_fermo` con la frase che rimanda alla striscia.
    - **PASS (16/08)** — `409` con `"Il canale WhatsApp e' fermo (kill-switch alzato): riprendilo dalla striscia in alto prima di scansionare."`, e la striscia rossa e' visibile in alto. Il kill-switch e' stato alzato e **rimesso a `false`**, con rilettura di conferma.
20. RAM sotto soglia → rifiuto `ram_insufficiente` con frase leggibile.
    - **PASS (16/08)** — backend riavviato con `WA_DISCOVER_RAM_MIN_MB=999999`: `409` con `"Memoria insufficiente per aprire un browser: chiudi qualche finestra e riprova."`. Backend poi rimesso senza override.
21. Redis del backend fermo → 409 leggibile, mai 500.
    - **SKIP (16/08)** — su questa macchina Memurai e' il Redis di **produzione** e la lista lo vieta esplicitamente. Il caso equivalente e' comunque gia' coperto per davvero dall'adversarial A.11, che punta `arq_redis_settings` su una porta mai in ascolto (connessione TCP reale rifiutata) invece di fermare un servizio vero.

## Auto-guarigione e caso limite (22-24)

22. Run lasciata `running` oltre la soglia orfana → il gate la chiude da solo, mai `scan_gia_in_corso` su quella run.
    - **PASS (16/08)** — run seminata con `started_at` di 9 ore fa (soglia: 420 min). Risposta: `409 ram_insufficiente` — cioe' un motivo **diverso**, che e' l'esito atteso — e a DB la run orfana risulta `stato='failed'`, `motivo='run_orfana'`. Zero run `running` residue: nessuna scansione vera e' partita. Nota: questa e' la stessa strada che prima della PR #86 rispondeva 500, quindi il caso 22 era bloccato tanto quanto il 18.
23. `GET /wa/numbers/{id}/discover` su un numero senza run → `{"ultima": null, "storico": [], "in_corso": false}`.
    - **PASS (16/08)** — verificato su due numeri, `200` con esattamente quel corpo. E' anche la prova che la migrazione `035` e' viva: prima, questa rotta rispondeva 500.
24. Scan lanciato con la lista chat gia' scorsa a meta' → deve PARTIRE, non rifiutare con `sidebar_coperta`.
    - **SKIP (16/08)** — richiede la sidebar VERA di WhatsApp Web, quindi il telefono. Il difetto e' pero' **corretto a monte e verificato nel codice mergiato**: `lista_utilizzabile` ora valuta tutti i candidati con `_almeno_una_cliccabile`, non esce piu' sulla PRIMA riga (che da meta' lista in giu' sta dietro l'intestazione). Lo script `backend/scripts/scan_da_dove_sei.py` non ha piu' bisogno di patcharla.

## Cosa resta, e cosa serve per chiuderlo

| Caso | Cosa manca |
|---|---|
| 4 (meta' toast), 9, 14, 24 | **il telefono collegato**: sono gli unici che richiedono una scansione vera |
| 8 | un numero in uno stato non-`active` che nessuno usa in produzione |
| 21 | un Redis di test separato da quello di produzione |
| 12, 13 | **decisione di Tommaso**, non lavoro tecnico |
