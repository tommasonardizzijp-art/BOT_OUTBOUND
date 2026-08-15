# Adversarial test suite — Lancio dell'auto-discover WhatsApp (obiettivo: ROMPERE il sistema)

**Lista scritta, non eseguita**: stessa nota del file dei test manuali. Da eseguire quando backend/worker/frontend sono avviati e il profilo WhatsApp e' libero.

Criterio di PASS **INVERTITO**: passa se il sistema **si difende** (errore chiaro, nessuna scrittura sporca, invariante intatta). Un 500, un errore DB grezzo all'utente, una scrittura parziale o un'invariante violata = FAIL anche se "sembrava funzionare".

Livello misto: browser per cio' che la UI esprime, chiamata diretta all'API (script/httpx, `asyncio.gather`) per race, payload malformati, burst — un adversarial fatto solo dalla UI non e' adversarial. Dati con prefisso `ADVWAD-<random>`.

**Invarianti da riverificare dopo OGNI blocco** (query dirette a DB, non dedotte dalla UI): nessuna riga `wa_discover_runs` resta `stato='running'` oltre la soglia orfana senza che il gate l'abbia sanata; nessun `wa_discover_runs.errore` contiene un numero di telefono in chiaro (vincolo P12 — cercare sequenze di 6+ cifre non mascherate); nessuna riga `wa_messages`/invio scritta durante una finestra di scan, filtrando per lo stesso `number_id` (la Fase A e' sola lettura); `wa_discover_runs.copertura` sempre fra 0 e 100 inclusi, mai negativo ne' oltre 100.

## A. Concorrenza e race (1-12)

1. **Due `POST /wa/numbers/{id}/discover` veri e concorrenti** sullo stesso numero, `asyncio.gather` su due sessioni HTTP/DB indipendenti (non due chiamate sequenziali) → uno solo riceve `200`, l'altro `409 scan_gia_in_corso`; a DB una sola riga `wa_discover_runs` con `stato='running'` per quel numero (l'indice unico parziale e' li' per questo).
2. **Due `chiudi_run` concorrenti sulla stessa run**, `asyncio.gather` su due sessioni DB indipendenti (una con esito di successo, una con errore) → la riga finale e' internamente coerente: o `done` coi contatori dell'esito ed `errore` nullo, o `failed` con l'errore e i contatori a zero, MAI un ibrido (es. `stato='failed'` coi contatori di una raccolta riuscita).
3. **`chiudi_se_orfana` e il worker che chiude la run "vera" in corsa**: forzare `started_at` di una run oltre la soglia orfana MENTRE il job ARQ reale la sta ancora processando (o simulare con un secondo `chiudi_run` concorrente) → nessuna eccezione, la run finisce chiusa una volta sola, mai due chiusure che si sovrascrivono in un ordine imprevedibile.
4. **Orfana chiusa dal gate MA una guardia successiva rifiuta comunque** (es. RAM sotto soglia mockata) → verificare A DB (sessione fresca, non quella della richiesta) che la run orfana risulti comunque chiusa, non "running" per un altro giro (il difetto reale trovato in review sulla dipendenza dal commit del chiamante).
5. **Doppio click ravvicinato dal browser vero** (non solo dal codice) su "Scansiona contatti" → al massimo una riga `running` a DB, il secondo tentativo di rete (se parte) riceve `409`.
6. **`GET` e `POST` concorrenti** sullo stesso numero (uno legge lo stato mentre l'altro avvia) → il `GET` non deve mai vedere uno stato a meta' scrittura (nessuna riga con `stato` NULL o campi parzialmente popolati).
7. **`enqueue_wa_discover` che solleva un'eccezione** (Redis giu' proprio nell'istante dell'accodamento, non prima) → la run aperta viene chiusa subito con `accodamento_fallito`, mai lasciata `running`.
8. **Worker ARQ spento e `POST`** (job accodato ma nessun worker lo consuma, poi si forza `enqueue_wa_discover` a tornare `False` per simulare) → risposta `409 accodamento_fallito`, run chiusa, non "running" in attesa di un worker che non arrivera' mai.
9. **Job ARQ cancellato dal proprio timeout** (simulare sollevando `asyncio.CancelledError` dal motore, Task 11) → la run finisce `failed`/`cancellato`, e l'eccezione risale comunque fino al chiamante (non ingoiata).
10. **Run lasciata `running` a mano oltre il TTL** (UPDATE diretto a DB su `started_at`), poi un `POST` sullo stesso numero → il gate del Task 10 la chiude DA SOLO e sblocca il numero (non `409 scan_gia_in_corso` sulla run morta); verificare A DB con una sessione fresca, non quella della richiesta, che la vecchia riga sia davvero `failed`/`run_orfana` e non ancora `running`.
11. **Redis irraggiungibile PROPRIO durante il gate** (fermare Redis di test, mai quello di produzione, subito prima del controllo `browser_occupato`) → `409 browser_occupato` (fail-closed, trattato come occupato), MAI un `500`.
12. **Lock del profilo presente ma di un ALTRO numero** (prendere il lock Redis `wa:profile-lock:*` su un numero B, poi `POST` sul numero A) → rifiutato `browser_occupato` anche per A: il gate e' GLOBALE sulla macchina, non per-numero (guardia 4, Task 3) — verificare che non venga invece rifiutato con un codice diverso o, peggio, lasciato passare.

## B. Numeri ostili (13-18)

13. `wa_discover_run_orfana_min` e `wa_discover_job_timeout_s` impostati a valori incoerenti (`orfana_min` sotto `job_timeout_s/60`) → l'avvio del backend deve fallire con un `ValueError` esplicito (il `model_validator`, Task 10), non un comportamento silenziosamente sbagliato in produzione.
14. `copertura` calcolata con `salvate+aggiornate+saltate_gia_note` superiore al `dichiarato` (es. dichiarato sceso fra un giro e l'altro) → clampata a 100, mai un valore tipo 137 mostrato in UI.
15. `dichiarato = 0` o `dichiarato = null` → `copertura` resta `None`/non calcolata, mai una divisione per zero che fa esplodere l'endpoint.
16. Contatori (`salvate`, `aggiornate`, `saltate_gia_note`, `non_verificate`) a valori enormi (simulare scrivendo direttamente a DB un intero grande) → la UI li mostra senza overflow di formattazione ne' crash del rendering.
17. `wa_discover_ram_min_mb` mockato a un valore negativo o a zero → il gate non deve rifiutare tutto ne' lasciare passare sempre per errore di confronto: verificare il comportamento reale e documentarlo.
18. `soglia_sync` passata a `esegui_discover_run` fuori range (negativa, o 1000) → nessun crash del gate di sincronizzazione, comportamento definito (o rifiuta sempre, o non rifiuta mai, ma senza eccezione non gestita).

## C. Stringhe e id ostili (19-25)

19. `number_id` di un ALTRO tenant → `404` (mai un dato di un tenant che trapela in un altro, IDOR).
20. `number_id` malformato (non un UUID, es. `"abc"`) → `404` pulito, non un 500 da errore di parsing DB.
21. `number_id` vuoto (`POST /wa/numbers//discover` o path-param assente) → errore di routing gestito (404/422), non un 500.
22. `number_id` da 10.000 caratteri → rifiutato/gestito senza crash del router ne' query DB con un parametro assurdo che va in timeout.
23. `number_id` con un null byte (`\x00`) incorporato → rifiutato o sanificato, nessun errore DB grezzo esposto all'utente.
24. Un motivo del motore NON presente in `MOTIVO_LABEL`/`MOTIVO_LABEL_SCAN` (scrivere a DB un valore inventato tipo `"motivo_mai_visto"` su una run gia' chiusa) → la UI mostra il codice grezzo, non una cella vuota, "undefined" o un crash di rendering.
25. `errore` di una run chiusa contenente un numero di telefono in chiaro nel testo dell'eccezione originale (es. un motore che un giorno solleva con un numero nel messaggio) → verificato che la sanificazione lo maschera prima di finire a DB (test gia' coperto a livello unit, qui riverificare end-to-end passando dal worker vero).

## D. Il `detail` oggetto e il client (26-28)

26. **`detail` oggetto `{codice, messaggio}` nel 409**: forzare OGNI codice di rifiuto (`numero_non_attivo`, `canale_fermo`, `browser_occupato`, `scan_gia_in_corso`, `ram_insufficiente`, `accodamento_fallito`) e verificare in UI che il toast mostri SEMPRE la frase leggibile, MAI la stringa letterale `[object Object]` ne' `Errore 409` generico.
27. Risposta col `Content-Type` sbagliato o corpo non-JSON dal backend (simulabile con un proxy/mock) → il client (`req<T>` in `waApi.ts`) non deve sollevare un'eccezione non gestita che rompe la pagina — verificare il fallback `Errore {status}`.
28. Rete che cade a meta' del `POST` (disconnessione simulata) → la UI non deve mostrare "in corso" per sempre senza via d'uscita: verificare che un refresh manuale/il polling successivo recuperi lo stato vero.

## E. Permessi e tampering (29-32)

29. Chiamata diretta all'endpoint SENZA autenticazione (`get_current_user` non superato) → `401`/`403`, mai l'esecuzione dello scan.
30. Chiamata con un JWT/sessione di un utente valido ma senza i permessi sul tenant del numero (se il modello di permessi lo prevede) → rifiutata, non IDOR.
31. Tampering sul body del `POST` (aggiungere campi extra tipo `{"tenant_id": "..."}` per provare a forzare un tenant diverso da quello del numero) → ignorato, il `tenant_id` della run resta SEMPRE quello risolto dal numero lato server, mai dal client (il modulo lo dichiara esplicitamente in `wa_numbers.py`).
32. Chiamare `wa_discover_runs.chiudi_run`/`apri_run` direttamente (bypassando l'endpoint, come farebbe uno script interno malevolo o buggato) con un `number_id` inesistente → nessuna eccezione non gestita, comportamento definito.

## F. Macchina a stati (33-37)

33. Doppio `approve` concettuale: due `POST` sequenziali (non concorrenti) sullo stesso numero, il secondo DOPO che il primo ha gia' aperto la run → il secondo riceve `409 scan_gia_in_corso` in modo deterministico.
34. `POST` su un numero `pending_qr`/`qr_required`/`disconnected`/`retired`/`suspended` → sempre `409 numero_non_attivo`, mai uno scan che parte su un numero non pronto.
35. Run chiusa `done` poi un secondo `chiudi_run` sulla STESSA run con un esito diverso (idempotenza) → la seconda chiamata NON deve sovrascrivere `finished_at` ne' i contatori della prima (gia' coperto a livello unit — riverificare che regga anche end-to-end via due chiamate del worker).
36. Kill-switch alzato A META' di uno scan gia' in corso (non solo prima di partire) → lo scan in corso si ferma, la run si chiude con un motivo che lo dice (`wa_halted`), non resta "running" per sempre.
37. Riattivazione di un numero (`retired`→`pending_qr`) MENTRE una run e' (impossibilmente, ma verificarlo) ancora aperta su quel numero → nessuna corruzione, comportamento definito e documentato.

## G. Rate-limit e idempotenza (38-40)

38. `_job_id` di due scansioni sullo stesso numero in rapida successione → NON collidono (sono legati al `run_id`, non al `number_id`): verificare che ARQ accodi entrambi i job distintamente anche se il secondo viene rifiutato a monte dal gate prima di arrivare all'accodamento.
39. Ripetere lo stesso `POST` con lo stesso `Idempotency-Key` (se il layer HTTP ne avesse uno — verificare se esiste, altrimenti documentare che non c'e' e che la difesa reale e' l'indice unico parziale).
40. 20 `POST` in rapida sequenza su 20 numeri DIVERSI, tutti `active` → ognuno apre la propria run indipendentemente (il gate `browser_occupato` e' globale sul BROWSER, non sul conteggio delle run: verificare che dal secondo in poi vengano rifiutati con `browser_occupato`, visto che un solo browser puo' girare alla volta su questa macchina — non un bug, e' il comportamento voluto).

## H. Volumi e rendering (41-44)

41. Storico con 50+ run sullo stesso numero → `GET /discover` rispetta `wa_discover_storico_limit` (default 10), non restituisce tutto lo storico intero.
42. `/wa/numeri` con 50+ numeri, tutti con una run recente → la colonna "Ultimo scan" non genera 50 richieste di rete separate senza controllo (verificare che SWR deduplichi/non saturi il backend — le due `useSWR` con la stessa chiave condividono la cache per riga, ma 50 righe sono comunque 50 chiavi diverse: documentare il volume reale di richieste).
43. Pagina `/wa/scoperti` aperta durante uno scan che aggiunge centinaia di chat nuove → il refresh automatico a fine scan non deve bloccare l'interfaccia ne' far esplodere il rendering della tabella.
44. Motivo/errore lunghissimo (oltre 2000 caratteri, il troncamento di `chiudi_run`) → la UI mostra il testo troncato senza rompere il layout della cella/toast.

## I. Verifica finale invarianti (45-48)

45. Query diretta: `SELECT COUNT(*) FROM wa_discover_runs WHERE stato='running' AND started_at < now() - INTERVAL wa_discover_run_orfana_min` → deve tornare **zero** dopo che il gate e' stato chiamato almeno una volta su ogni numero toccato dal giro di test (l'auto-guarigione e' nel gate, non in un cron: una riga trovata qui senza che nessuno abbia mai richiamato il gate su quel numero non e' un fallimento del Task 10, ma va comunque verificato che il PRIMO tentativo successivo la sani).
46. Query diretta: nessuna riga `wa_discover_runs.errore` contiene una sequenza di 6+ cifre non intervallata da `<num>` (la sanificazione P12).
47. Query diretta: nessuna riga in una tabella di invio (`wa_messages` o equivalente) scritta con `created_at`/`sent_at` durante la finestra `[started_at, finished_at]` di una run di discover sullo STESSO `number_id` (la Fase A e' sola lettura, non deve mai toccare l'invio).
48. Query diretta: `SELECT number_id, COUNT(*) FROM wa_discover_runs WHERE stato='running' GROUP BY number_id HAVING COUNT(*) > 1` → zero righe, sempre (l'indice unico parziale del Task 1 e' la garanzia strutturale, ma va riverificato che regga anche dopo tutto il giro adversarial, non solo in isolamento).

## J. Regressione Task 12 — `lista_utilizzabile` (49)

49. **Scan che riprende da meta' lista** (sidebar gia' scorsa dal giro precedente o da scroll manuale, non ripartita da zero): lanciarlo con la sidebar in QUELLA posizione → deve raccogliere normalmente, MAI fermarsi al primo giro con `motivo="sidebar_coperta"` solo perche' la prima riga renderizzata sta dietro l'intestazione. Caso opposto da riverificare nello stesso giro: un pannello VERO aperto sopra la lista (es. Impostazioni rimasta aperta) deve ancora far scattare `sidebar_coperta` — la protezione non va indebolita, solo la falsa positiva su lista scorsa.
