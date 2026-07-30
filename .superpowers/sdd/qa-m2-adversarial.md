# Adversarial test suite — WhatsApp M2 (obiettivo: ROMPERE il sistema)

Criterio di PASS INVERTITO: passa se il sistema SI DIFENDE (errore chiaro, nessuna scrittura sporca, invariante intatta). Un 500, un errore DB grezzo, una scrittura parziale o un'invariante violata = FAIL anche se "sembrava funzionare".

Livello misto: browser per ciò che la UI esprime, chiamata diretta all'API (script/httpx) per race, payload malformati, burst. Dati con prefisso `ADVM2-<random>`.

**Invarianti da riverificare dopo ogni blocco** (contratto `docs/whatsapp/contratto-M2-M3.md`): I1 (nessuna riga scritta da M2 ha `locked_by`/`locked_at` valorizzato), I3 (`next_action_at` mai NULL su una riga non terminale prodotta da M2), `optout_enabled` a DB coerente col tipo campagna, nessun numero di telefono completo in nessun log/report/risposta API.

## A. CSV ostili (1-11)

1. File da 10 MB → rifiutato con 422 (limite dichiarato), non un crash/timeout.
2. File con 5.001 righe (oltre `WA_INGEST_MAX_ROWS`) → rifiutato con messaggio che nomina il limite.
3. Solo header, zero righe → errore chiaro ("nessuna riga di dati"), non 0 creati silenzioso.
4. Header con colonna `numero` duplicata due volte → rifiutato (intestazioni duplicate).
5. File con BOM UTF-8 → header riconosciuto correttamente, non un falso "colonna numero assente".
6. File UTF-16 (non UTF-8/latin-1) → gestito senza `UnicodeDecodeError` grezzo (fallback o errore chiaro).
7. Separatore misto (alcune righe `,`, altre `;`) → comportamento definito (dialetto rilevato dalla prima riga), non colonne disallineate scritte a DB.
8. Cella da 10.000 caratteri in una colonna attributo → troncata secondo `wa_ingest_max_attrs_bytes`, contatto comunque creato, nessun crash.
9. Null byte (`\x00`) dentro una cella → rifiutato o sanificato, nessun errore DB grezzo verso l'utente.
10. Formula CSV injection (`=cmd|' /C calc'!A0`) in una colonna attributo → salvata come testo letterale, il report/export scarti la neutralizza (apice iniziale) se il valore compare li.
11. Newline dentro una cella quotata (`"riga\ncontinua"`) → parsata correttamente o errore chiaro; verificare se la riga successiva sparisce silenziosamente (gap noto, non ancora testato: documentare l'esito reale, anche se FAIL).

## B. Numeri plausibilmente sbagliati (12-19)

12. `+39 342 146 0077 ext. 12` → scartato con motivo, valore mascherato nel report (mai il numero completo).
13. `0039 342 146 0078` → normalizzato correttamente (prefisso internazionale valido).
14. `342.146.0079` (senza prefisso) → normalizzato con `WA_INGEST_DEFAULT_COUNTRY`.
15. `+39-342-146-0080` → normalizzato o scartato in modo coerente con gli altri formati equivalenti.
16. Numero di 3 cifre → scartato.
17. Numero di 25 cifre → scartato (fuori range E.164).
18. Stesso numero in due formati diversi nello stesso file (es. `+393421460077` e `0039 342 146 0077`) → un solo contatto creato (HMAC coerente), non due.
19. File SENZA riga di intestazione (prima riga sono numeri veri) → l'errore "colonna numero assente" NON stampa i numeri in chiaro come "colonne trovate" (fix Task 5/2).

## C. Concorrenza vera (20-24)

20. Doppio upload dello STESSO file in `Promise.all`/`asyncio.gather` → dedup regge, nessun contatto duplicato.
21. Due `avvia()` concorrenti su DUE campagne diverse sullo stesso numero (asyncio.gather, non sequenziale) → una sola passa (fix Task 7, già testato in automatico: riverificare qui a livello di sistema integrato).
22. Due `start` HTTP concorrenti sulla STESSA campagna → una sola transizione applicata, non doppio `started_at`.
23. Rimozione di un contatto mentre un lock fresco è simulato attivo (finestra di 20 minuti) → 409, riga intatta, nessuna cancellazione.
24. Ingest concorrente su due campagne diverse dello stesso tenant con lo stesso CSV → due `WaContact` condivisi (stesso tenant, stesso hmac) ma due `WaCampaignContact` distinti, nessun incrocio.

## D. Scoping tenant (25-28)

25. Creazione campagna con `wa_number_id` di un ALTRO tenant → rifiutata.
26. Ingest con `campaign_id` di un altro tenant (via chiamata diretta all'API, bypassando la UI) → rifiutato o isolato correttamente.
27. Lista contatti di una campagna che appartiene a un altro tenant (via API diretta) → verificare se esiste un controllo esplicito o se lo scoping è solo implicito (nota architetturale già emersa in review: nessun controllo tenant-utente in tutta la codebase, documentare l'esito reale).
28. KPI di una campagna di un altro tenant (via API diretta) → stesso comportamento del punto 27, documentare.

## E. Macchina a stati (29-35)

29. Ingest su campagna `running` → rifiutato (409).
30. Doppio `start` sequenziale sulla stessa campagna → il secondo fallisce con messaggio chiaro.
31. `stop` di una campagna già `stopped` → rifiutato, non un no-op silenzioso.
32. Modifica del template a campagna già avviata (`running`) → rifiutata (PATCH/PUT solo in `draft`).
33. `start` con numero in stato `qr_required` (non `active`) → rifiutato con messaggio che nomina la causa.
34. Riattivazione di un numero `active` (non `retired`/`suspended`) → rifiutata, non un no-op.
35. Riattivazione di un numero già riattivato due volte di fila (secondo giro su un numero ora `pending_qr`) → rifiutata (transizione ammessa solo da retired/suspended).

## F. Contratto con M3 (36-40)

36. Ogni riga creata dall'ingest ha `next_action_at` non NULL (I3) — verifica SQL diretta dopo un ingest reale.
37. Nessuna riga creata da M2 (ingest o seed) ha `locked_by`/`locked_at` valorizzato (I1) — verifica SQL diretta.
38. Un contatto `opted_out` prima dell'ingest non entra MAI in una nuova campagna dallo stesso file, nemmeno ri-caricato più volte.
39. `optout_enabled` a DB corrisponde sempre al tipo campagna al momento della creazione, anche tentando di passare `optout_enabled` esplicito nel payload di creazione (deve essere ignorato, fix Task 6 bloccante).
40. Tentativo di avviare due campagne sullo stesso numero passando per il servizio direttamente (bypass HTTP) — stessa garanzia del punto 21, verificata al livello più basso.

## G. PII (41-44)

41. Grep sui log del backend dopo un ingest reale di almeno 20 numeri veri (formattati in vari modi, alcuni malformati): zero occorrenze di un numero completo.
42. Il report scarti (sia in risposta API sia nel CSV scaricato dalla UI) non contiene mai un numero completo.
43. La lista contatti API (`GET /wa/contacts`) non contiene mai un numero completo, in nessun campo della risposta.
44. Un `chat_title` numerico (se mai popolato, oggi non lo è in M2) non viene mai mostrato: verificare che il campo non sia esposto da nessun endpoint M2.

## H. Invarianti SQL a fine run (45-47)

45. Per ogni campagna toccata durante i test: `total_contacts` == conteggio reale delle righe `wa_campaign_contacts` (verifica dopo ingest E dopo rimozioni, fix Task 12).
46. Nessun `wa_contacts` orfano creato senza una campagna associata (minimizzazione, Q23).
47. Nessun duplicato `(tenant_id, phone_hmac)` in `wa_contacts` dopo tutti i test di questo blocco.

---

**Nota sui gap gia' noti e non bloccanti** (da questa sessione, non da nascondere): il parser CSV inghiotte silenziosamente le righe successive a una virgoletta non chiusa (punto 11); non esiste ancora un percorso per far ripartire una campagna in stato `error` (M3 non costruito); lo scoping tenant-utente e' solo implicito in tutta la codebase (punti 27-28). Vanno rieseguiti qui per QUANTIFICARE l'impatto reale sul sistema integrato, non per riscoprirli.
