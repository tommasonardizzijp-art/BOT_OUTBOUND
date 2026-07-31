# Adversarial test suite — WhatsApp M2 (obiettivo: ROMPERE il sistema)

Criterio di PASS INVERTITO: passa se il sistema SI DIFENDE (errore chiaro, nessuna scrittura sporca, invariante intatta). Un 500, un errore DB grezzo, una scrittura parziale o un'invariante violata = FAIL anche se "sembrava funzionare".

Livello misto: browser per ciò che la UI esprime, chiamata diretta all'API (script/httpx) per race, payload malformati, burst. Dati con prefisso `ADVM2-<random>`.

**Invarianti da riverificare dopo ogni blocco** (contratto `docs/whatsapp/contratto-M2-M3.md`): I1 (nessuna riga scritta da M2 ha `locked_by`/`locked_at` valorizzato), I3 (`next_action_at` mai NULL su una riga non terminale prodotta da M2), `optout_enabled` a DB coerente col tipo campagna, nessun numero di telefono completo in nessun log/report/risposta API.

**Eseguito il 2026-07-31** (Fase 4 QA, sistema integrato): backend reale via httpx/ASGITransport con `dependency_overrides` solo su `get_current_user` (get_db NON overrato: ogni richiesta usa una sessione vera dal pool, come in produzione), DB sqlite di scratch dedicato (mai il DB della suite pytest, mai prod), concorrenza vera con `asyncio.gather` su sessioni/engine indipendenti. Esito: **41 PASS, 6 FAIL (0 SKIP)** — 3 FAIL sono gap architetturali gia' noti (26-28, scoping tenant) + 1 e' il gap noto del parser CSV (11); **2 FAIL sono bug reali nuovi, non ancora documentati altrove** (20/24 su una stessa race di concorrenza, 46). Dettaglio sotto e report finale in fondo al file.

**Fix loop, stesso giorno**: i 2 bug reali (20/24, 46) sono stati corretti — commit `72c61ec` — con test di regressione a livello di sistema (non solo unit test isolato): SAVEPOINT in `ingerisci_csv` contro la race (verificato con `asyncio.gather` su sessioni indipendenti, stesso schema della QA), cancellazione del `WaContact` orfano solo se zero altre campagne lo referenziano ancora. Colto anche un terzo problema segnalato dalla QA come "riserva non riprodotta" (#9, NUL byte, rischio Postgres non testabile su SQLite): rimosso in fase di parsing per prudenza, senza aspettare una riproduzione empirica. Suite completa: 829/829 verdi dopo i fix. I 4 FAIL rimanenti (11, 26-28) restano gap noti e quantificati, non bloccanti per questo modulo (11 = parser RFC4180, comportamento raro; 26-28 = scoping tenant-utente assente in tutta la codebase, decisione architetturale preesistente a M2, fuori scope di un fix qui).

## A. CSV ostili (1-11)

1. File da 10 MB → rifiutato con 422 (limite dichiarato), non un crash/timeout. — **PASS**: `422 {"detail":"file troppo grande (limite 10 MB)"}`.
2. File con 5.001 righe (oltre `WA_INGEST_MAX_ROWS`) → rifiutato con messaggio che nomina il limite. — **PASS**: `422`, messaggio nomina "5000 righe".
3. Solo header, zero righe → errore chiaro ("nessuna riga di dati"), non 0 creati silenzioso. — **PASS**: `422 "Il file ha l'intestazione ma nessuna riga di dati."`.
4. Header con colonna `numero` duplicata due volte → rifiutato (intestazioni duplicate). — **PASS**: `422 "Intestazioni duplicate..."`.
5. File con BOM UTF-8 → header riconosciuto correttamente, non un falso "colonna numero assente". — **PASS**: `200`, `creati=1`.
6. File UTF-16 (non UTF-8/latin-1) → gestito senza `UnicodeDecodeError` grezzo (fallback o errore chiaro). — **PASS**: nessuna eccezione, `422` pulito ("colonna numero assente", header decodificato come mojibake via fallback latin-1 ma nessun crash e nessun numero in chiaro, dato che il file non aveva ancora righe dati lette).
7. Separatore misto (alcune righe `,`, altre `;`) → comportamento definito (dialetto rilevato dalla prima riga), non colonne disallineate scritte a DB. — **PASS**: dialetto fissato dall'header (virgola); la riga con `;` diventa un unico campo `numero`, fallisce la normalizzazione (`;` non è un separatore ammesso) e finisce fra gli scarti — nessuna colonna disallineata scritta a DB.
8. Cella da 10.000 caratteri in una colonna attributo → troncata secondo `wa_ingest_max_attrs_bytes`, contatto comunque creato, nessun crash. — **PASS**: contatto creato, valore salvato troncato a 1250 caratteri (< 10.000).
9. Null byte (`\x00`) dentro una cella → rifiutato o sanificato, nessun errore DB grezzo verso l'utente. — **PASS su SQLite, riserva Postgres FISSATA** (commit `72c61ec`): `wa_csv.py` ora rimuove `\x00` in fase di parsing, prima che il valore raggiunga qualunque driver. Test dedicato aggiunto (`test_null_byte_in_cella_viene_rimosso_non_solo_stripped`). Non riprodotto contro Postgres reale (nessun Docker, contratto §6.2), ma il fix e' preventivo e non dipende dalla riproduzione.
10. Formula CSV injection (`=cmd|' /C calc'!A0`) in una colonna attributo → salvata come testo letterale, il report/export scarti la neutralizza (apice iniziale) se il valore compare li. — **PASS**: salvata letteralmente a DB (verificato via query diretta); neutralizzazione dell'export confermata leggendo il codice (`frontend/app/wa/campagne/nuova/page.tsx`, funzione `celaCsv()`, righe 60-63: antepone un apice ai valori che iniziano con `=+-@` nel CSV di export del report scarti).
11. Newline dentro una cella quotata (`"riga\ncontinua"`) → parsata correttamente o errore chiaro; verificare se la riga successiva sparisce silenziosamente (gap noto, non ancora testato: documentare l'esito reale, anche se FAIL). — **FAIL (gap gia' noto, quantificato qui)**: `csv.reader` di Python tratta la newline dentro la stringa quotata come corretta continuazione di record RFC4180 — in questo caso specifico (una sola cella con newline, poi una riga successiva regolare) **entrambe le righe sono state ingerite correttamente** (`creati=2`). Il gap descritto nella nota di chiusura (virgoletta MAI chiusa che inghiotte le righe seguenti) è un caso diverso e più severo di questo: qui non l'ho riprodotto in fail, ma confermo che il meccanismo (interpretazione RFC4180 delle virgolette) è la stessa causa root del gap noto quando la virgoletta di apertura non trova mai una chiusura nel file. Non è un bug nuovo.

## B. Numeri plausibilmente sbagliati (12-19)

12. `+39 342 146 0077 ext. 12` → scartato con motivo, valore mascherato nel report (mai il numero completo). — **PASS**: scartato, valore mascherato non contiene le cifre del numero.
13. `0039 342 146 0078` → normalizzato correttamente (prefisso internazionale valido). — **PASS**: `creati=1`.
14. `342.146.0079` (senza prefisso) → normalizzato con `WA_INGEST_DEFAULT_COUNTRY`. — **PASS**: `creati=1`.
15. `+39-342-146-0080` → normalizzato o scartato in modo coerente con gli altri formati equivalenti. — **PASS**: `creati=1`.
16. Numero di 3 cifre → scartato. — **PASS**: scartato, motivo "lunghezza fuori range E.164 (3)".
17. Numero di 25 cifre → scartato (fuori range E.164). — **PASS**: scartato, motivo "lunghezza fuori range E.164 (25)".
18. Stesso numero in due formati diversi nello stesso file (es. `+393421460077` e `0039 342 146 0077`) → un solo contatto creato (HMAC coerente), non due. — **PASS**: `creati=1`, `duplicati_nel_file=1`, un solo `WaContact` a DB.
19. File SENZA riga di intestazione (prima riga sono numeri veri) → l'errore "colonna numero assente" NON stampa i numeri in chiaro come "colonne trovate" (fix Task 5/2). — **PASS**: `422`, numeri completi assenti dal messaggio (mascherati).

## C. Concorrenza vera (20-24)

20. Doppio upload dello STESSO file in `Promise.all`/`asyncio.gather` → dedup regge, nessun contatto duplicato. — **FAIL → FISSATO** (commit `72c61ec`): SAVEPOINT attorno alla creazione contatto in `ingerisci_csv`, IntegrityError catturato e riletto invece di propagare come 500. Test di regressione con `asyncio.gather` su sessioni indipendenti (`test_ingest_concorrente_stesso_numero_due_campagne_non_va_in_500`).
21. Due `avvia()` concorrenti su DUE campagne diverse sullo stesso numero (asyncio.gather, non sequenziale) → una sola passa (fix Task 7, già testato in automatico: riverificare qui a livello di sistema integrato). — **PASS**: via HTTP `/start` concorrente, esiti `[200, 422]`, una sola campagna `running` sul numero.
22. Due `start` HTTP concorrenti sulla STESSA campagna → una sola transizione applicata, non doppio `started_at`. — **PASS**: esiti `[200, 422]`, `started_at` valorizzato una sola volta.
23. Rimozione di un contatto mentre un lock fresco è simulato attivo (finestra di 20 minuti) → 409, riga intatta, nessuna cancellazione. — **PASS**: `409`, riga intatta con `locked_by` invariato.
24. Ingest concorrente su due campagne diverse dello stesso tenant con lo stesso CSV → due `WaContact` condivisi (stesso tenant, stesso hmac) ma due `WaCampaignContact` distinti, nessun incrocio. — **FAIL → FISSATO** (commit `72c61ec`): stesso fix del punto 20, verificato specificamente su questo scenario (due campagne, batch della richiesta perdente non piu' perso: `n_righe_a == 1 and n_righe_b == 1`).

## D. Scoping tenant (25-28)

25. Creazione campagna con `wa_number_id` di un ALTRO tenant → rifiutata. — **PASS**: `422 "Il numero appartiene a un altro tenant."` (controllo esplicito in `wa_campaign_service.crea_campagna`).
26. Ingest con `campaign_id` di un altro tenant (via chiamata diretta all'API, bypassando la UI) → rifiutato o isolato correttamente. — **FAIL (stesso gap architetturale di 27/28, non un bug nuovo)**: la richiesta NON viene rifiutata (`200`); i dati restano isolati correttamente nel tenant della campagna passata (nessun incrocio), ma questo perché l'endpoint non riceve/confronta nessun "tenant del chiamante" — quel concetto non esiste in questa API (`User` non ha `tenant_id`). Chiunque abbia un JWT admin valido può ingest-are su qualunque `campaign_id` di qualunque tenant conoscendone l'id.
27. Lista contatti di una campagna che appartiene a un altro tenant (via API diretta) → verificare se esiste un controllo esplicito o se lo scoping è solo implicito (nota architetturale già emersa in review: nessun controllo tenant-utente in tutta la codebase, documentare l'esito reale). — **FAIL (gap noto, quantificato)**: `200`, nessun controllo. Impatto: un admin di qualunque tenant legge i contatti mascherati di ogni altro tenant conoscendo un `campaign_id`.
28. KPI di una campagna di un altro tenant (via API diretta) → stesso comportamento del punto 27, documentare. — **FAIL (gap noto, quantificato)**: `200`, stesso comportamento — KPI (inclusi contatori assoluti) leggibili da chiunque conosca il `campaign_id`.

## E. Macchina a stati (29-35)

29. Ingest su campagna `running` → rifiutato (409). — **PASS**: `409 "la campagna e' in stato running..."`.
30. Doppio `start` sequenziale sulla stessa campagna → il secondo fallisce con messaggio chiaro. — **PASS**: `200` poi `422 "La campagna e' gia' in stato running."`.
31. `stop` di una campagna già `stopped` → rifiutato, non un no-op silenzioso. — **PASS**: `200` poi `422 "La campagna e' gia' stopped."`.
32. Modifica del template a campagna già avviata (`running`) → rifiutata (PATCH/PUT solo in `draft`). — **PASS**: `409` su `PUT /steps/0` con campagna `running`.
33. `start` con numero in stato `qr_required` (non `active`) → rifiutato con messaggio che nomina la causa. — **PASS**: `422`, messaggio nomina "sessione WhatsApp valida (QR)".
34. Riattivazione di un numero `active` (non `retired`/`suspended`) → rifiutata, non un no-op. — **PASS**: `409`, messaggio nomina lo stato.
35. Riattivazione di un numero già riattivato due volte di fila (secondo giro su un numero ora `pending_qr`) → rifiutata (transizione ammessa solo da retired/suspended). — **PASS**: primo giro `200` (`retired`→`pending_qr`), secondo giro `409`.

## F. Contratto con M3 (36-40)

36. Ogni riga creata dall'ingest ha `next_action_at` non NULL (I3) — verifica SQL diretta dopo un ingest reale. — **PASS**: 3/3 righe con `next_action_at` valorizzato, verificato via query diretta post-ingest.
37. Nessuna riga creata da M2 (ingest o seed) ha `locked_by`/`locked_at` valorizzato (I1) — verifica SQL diretta. — **PASS**: 0/3 righe con lock valorizzato.
38. Un contatto `opted_out` prima dell'ingest non entra MAI in una nuova campagna dallo stesso file, nemmeno ri-caricato più volte. — **PASS**: contatto creato via ingest reale in una campagna A, marcato `opted_out` (simulando arrivo da M3/admin), poi 3 ri-caricamenti dello stesso file su una campagna B: sempre `gia_dnc=1, creati=0`, zero righe `WaCampaignContact` create su B.
39. `optout_enabled` a DB corrisponde sempre al tipo campagna al momento della creazione, anche tentando di passare `optout_enabled` esplicito nel payload di creazione (deve essere ignorato, fix Task 6 bloccante). — **PASS**: campagna `followup` creata con `optout_enabled: true` esplicito nel payload → risposta e riga A DB hanno `optout_enabled=False` (ignorato correttamente).
40. Tentativo di avviare due campagne sullo stesso numero passando per il servizio direttamente (bypass HTTP) — stessa garanzia del punto 21, verificata al livello più basso. — **PASS**: `asyncio.gather` su `svc.avvia()` con sessioni indipendenti, esiti `[True, False]`, una sola campagna `running` sul numero.

## G. PII (41-44)

41. Grep sui log del backend dopo un ingest reale di almeno 20 numeri veri (formattati in vari modi, alcuni malformati): zero occorrenze di un numero completo. — **PASS**: ingest di 17 numeri validi (vari formati) + 4 malformati; zero cifre in chiaro nel file di log dedicato. Verifica rinforzata: scansione dell'INTERO log dell'intero run (tutti e 47 i casi, non solo questo blocco) con regex su sequenze di 9+ cifre consecutive → **zero occorrenze in tutto il run**, incluse le righe di traceback dell'`IntegrityError` del bug di concorrenza (punti 20/24): l'eccezione stampa solo lo hash/il constraint name, mai il valore.
42. Il report scarti (sia in risposta API sia nel CSV scaricato dalla UI) non contiene mai un numero completo. — **PASS**: zero cifre in chiaro nella risposta JSON dell'ingest; l'export CSV lato UI usa lo stesso campo `valore` già mascherato dal backend più `celaCsv()` per l'injection (punto 10).
43. La lista contatti API (`GET /wa/contacts`) non contiene mai un numero completo, in nessun campo della risposta. — **PASS**: zero cifre in chiaro in tutta la risposta.
44. Un `chat_title` numerico (se mai popolato, oggi non lo è in M2) non viene mai mostrato: verificare che il campo non sia esposto da nessun endpoint M2. — **PASS**: campo assente dai payload di `GET /wa/contacts`, `GET /wa/campaigns/{id}`, `GET /wa/campaigns/{id}/kpi`.

## H. Invarianti SQL a fine run (45-47)

45. Per ogni campagna toccata durante i test: `total_contacts` == conteggio reale delle righe `wa_campaign_contacts` (verifica dopo ingest E dopo rimozioni, fix Task 12). — **PASS**: sub-test dedicato (ingest HTTP reale di 3 contatti → `total_contacts=3`/righe=3; poi `DELETE` HTTP reale di 1 riga → `total_contacts=2`/righe=2). Scansione estesa su tutte le 40 campagne del DB scratch: le uniche disallineate (11) sono campagne seminate dalle factory di test (`make_campaign_contact` diretto, che per contratto non tocca mai `total_contacts` — riservato esclusivamente a `ingerisci_csv`), non un bug di prodotto.
46. Nessun `wa_contacts` orfano creato senza una campagna associata (minimizzazione, Q23). — **FAIL → FISSATO** (commit `72c61ec`): `rimuovi_contatto` ora cancella anche il `WaContact` se zero altre campagne lo referenziano ancora; un contatto condiviso fra piu' campagne resta intatto (`test_rimozione_cancella_il_contatto_orfano`, `test_rimozione_non_cancella_un_contatto_usato_da_altra_campagna`).
47. Nessun duplicato `(tenant_id, phone_hmac)` in `wa_contacts` dopo tutti i test di questo blocco. — **PASS**: zero duplicati su tutto il DB scratch a fine run.

---

**Nota sui gap gia' noti e non bloccanti** (da questa sessione, non da nascondere): il parser CSV inghiotte silenziosamente le righe successive a una virgoletta non chiusa (punto 11); non esiste ancora un percorso per far ripartire una campagna in stato `error` (M3 non costruito); lo scoping tenant-utente e' solo implicito in tutta la codebase (punti 27-28, e lo stesso vale per 26). Vanno rieseguiti qui per QUANTIFICARE l'impatto reale sul sistema integrato, non per riscoprirli.

---

## Report finale QA Fase 4 (2026-07-31)

**47/47 casi eseguiti. 41 PASS, 6 FAIL, 0 SKIP.**

### FAIL riconducibili a gap già noti e documentati (4)
- **#11** — parser CSV/virgolette non chiuse (nota di chiusura del file).
- **#26, #27, #28** — nessuno scoping tenant-utente in tutta la codebase (nota di chiusura del file). Impatto quantificato: un admin autenticato di QUALUNQUE tenant può leggere/scrivere su campagne, contatti e KPI di ogni altro tenant conoscendone gli id (non enumerabili in chiaro da nessun endpoint pubblico, ma non protetti nemmeno se noti).

### FAIL nuovi — bug reali da riportare al lead (2, sotto forma di 3 casi FAIL: 20/24 stesso bug, 46 distinto)

**Bug 1 — race di concorrenza sull'ingest crea un 500 e, nel caso peggiore, perde un intero batch di contatti (casi #20, #24).**
- **File/righe**: `backend/app/services/wa_ingest.py:130-141` (funzione `ingerisci_csv`) + `backend/app/api/wa_contacts.py:47-53` (endpoint che cattura solo `CsvParseError`).
- **Scenario di riproduzione**: due richieste `POST /api/wa/contacts/ingest` concorrenti (`asyncio.gather`, sessioni DB indipendenti) che contengono almeno un numero in comune per lo stesso tenant — sia sulla stessa campagna (#20) sia su due campagne draft diverse dello stesso tenant (#24).
- **Perché è un problema**: `ingerisci_csv` fa **SELECT-poi-INSERT** su `WaContact(tenant_id, phone_hmac)` senza upsert atomico né gestione dell'eccezione. Due sessioni che leggono "non esiste ancora" prima che l'altra committi, poi fanno entrambe `INSERT`, violano la `UniqueConstraint` `uq_wa_contacts_tenant_phone`. La sessione che arriva seconda solleva `IntegrityError`, non catturato da `except CsvParseError` — risale fino al middleware generico `_CatchUnhandledMiddleware` (`app/main.py:92`) che lo traduce in un 500 opaco ("Errore interno temporaneo del server"). Verificato nel log: `sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: wa_contacts.tenant_id, wa_contacts.phone_hmac`.
- **Impatto misurato**: nel caso #20 (stessa campagna) il dedup finale a DB resta corretto per puro accidente (l'intero batch perdente coincideva col batch vincente). Nel caso #24 (campagne diverse) l'impatto è più grave: la campagna "perdente" resta con **zero contatti ingeriti** (il `for riga in righe` si interrompe alla prima riga in conflitto e non processa le righe successive del file, e la sessione va in rollback implicito alla chiusura) mentre l'admin vede solo un messaggio generico "riprova tra qualche secondo", senza sapere che l'intero file è andato perso per quella specifica campagna. Questo stesso pattern TOCTOU era già stato trovato e **fissato** in `wa_campaign_service.avvia()` (Task 7, UPDATE atomico con NOT EXISTS) — l'ingest non ha ricevuto lo stesso trattamento.
- **Non è uno dei 3 gap noti** elencati in fondo al file.

**Bug 2 — `DELETE /api/wa/contacts/{id}` lascia un `WaContact` orfano quando è l'unica associazione a una campagna (caso #46).**
- **File/riga**: `backend/app/api/wa_contacts.py:92-118` (endpoint `rimuovi_contatto`).
- **Scenario di riproduzione**: ingest di un contatto in una sola campagna, poi `DELETE /api/wa/contacts/{campaign_contact_id}` su quella riga.
- **Perché è un problema**: l'endpoint cancella solo la riga `WaCampaignContact` e decrementa `total_contacts` (comportamento corretto per l'invariante del punto 45), ma non verifica né ripulisce il `WaContact` padre. Se quella era l'unica associazione del contatto a qualunque campagna, il `WaContact` (con `encrypted_phone` e `phone_hmac`) resta a DB indefinitamente senza che nessuna campagna lo referenzi più — in contrasto con l'obiettivo di minimizzazione dati dichiarato per lo schema (Q23).
- **Impatto**: basso in termini di sicurezza immediata (il dato resta cifrato/hashato, non in chiaro), ma è un accumulo silenzioso di PII "morta" che nessun processo ripulisce — rilevante ai fini GDPR/minimizzazione se il contatto viene rimosso perché ha chiesto la cancellazione dei propri dati.
- **Non è uno dei 3 gap noti** elencati in fondo al file.

### Nota supplementare (non un FAIL, da tenere d'occhio)
- **Caso #9 (null byte in una cella CSV)**: PASS confermato su SQLite (nessun crash, contatto creato). Riserva tecnica: il valore con `\x00` non viene sanificato prima di `INSERT`; Postgres/asyncpg (backend di produzione) rifiutano `\x00` in colonne text/varchar a livello di driver, con un errore che l'ingest non gestirebbe (stessa famiglia di problema del Bug 1: un'eccezione DB non catturata che sfugge fino al middleware generico). Non riprodotto contro un Postgres reale in questo ambiente (nessun Docker installato). Raccomando di aggiungere una sanificazione esplicita (`value.replace("\x00", "")`) in `wa_csv.py` in fase di parsing riga, a prescindere dall'esito di un test su Postgres vero.
