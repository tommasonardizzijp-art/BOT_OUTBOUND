# Fase A auto-discover — test adversarial

**Criterio di PASS INVERTITO**: passa se il sistema **si difende** (errore chiaro, nessuna
scrittura sporca, invariante intatta). Un 500, un errore DB grezzo, una scrittura parziale o
un'invariante violata = **FAIL**, anche se "sembrava funzionare".

Livelli mescolati: dove la UI non esiste ancora (la Fase A non ne ha una), si attacca
**direttamente le funzioni e il DB** — che è comunque il livello dove i danni avvengono.

I valori ostili qui sotto non sono inventati: dove è indicato "(vero)" vengono dai titoli e
dai dati realmente letti da Primero e dal numero personale l'11/08.

---

## A. Titoli ostili (`classifica`, `pannello`, `salvataggio`)

| # | Attacco | Difesa attesa |
|---|---|---|
| A1 | Titolo con emoji: `PRIMERO 🤵👨‍🌾` (vero) | non è un numero; salvato integro |
| A2 | Titolo con marcatori bidi `‪…‬` attorno a un numero (WhatsApp lo fa) | riconosciuto come numero, cifrato, **mai** in `chat_title` |
| A3 | Titolo di 10.000 caratteri | nessuna eccezione; troncato al limite colonna (200) senza rompere |
| A4 | Titolo con solo spazi / stringa vuota / `None` | scartato, nessuna riga fantasma |
| A5 | `4 cessi 🚽`, `Cuba 2 e bachata 2`, `I Sopra Savo` (veri) | **non** numeri — nessuna cifratura di un nome |
| A6 | Due titoli che normalizzano uguale (`Fulvio CBD` / `  fulvio   cbd `) | trattati come la stessa chat: aggiorna, non duplica |
| A7 | Titolo con null byte `\x00` | nessun crash; scritto o rifiutato in modo pulito, mai file/riga mutilati |
| A8 | Titolo che è SQL: `'; DROP TABLE wa_discovered_chats; --` | salvato come testo, tabella intatta (verifica via SQL) |

## B. Numeri ostili (`classifica.numero_dal_titolo`, `pannello.numero_dal_pannello`)

| # | Attacco | Difesa attesa |
|---|---|---|
| B1 | `+39 342 146 0077 99 88 77` (vero, troppe cifre) | nessun E.164; **etichetta mascherata** presente, riga deduplicabile |
| B2 | `123456789012345678`, `00 12 34 56 78 90 12 34 56 78` (veri) | come B1 |
| B3 | `+1 (555) 978-5671` (vero, estero) | normalizzato correttamente |
| B4 | Pannello con `Ultimo accesso 11/08/2026 alle 14:35` | **nessun** numero estratto |
| B5 | Pannello con `12 partecipanti` e nessun `+` | nessun numero, tipo = gruppo |
| B6 | Numero senza `+` nel pannello (`342 146 0077`) | nessun numero letto — **documentato**: 14/14 dei reali hanno il `+`; se emergesse un caso vero, è qui che va cambiato |
| B7 | Due numeri diversi nello stesso pannello | ne vince uno in modo deterministico, mai un misto delle due cifre |

## C. Sincronizzazione (`sincronizzazione`)

| # | Attacco | Difesa attesa |
|---|---|---|
| C1 | Anteprima di chat `Fulvio: sconto 50%` senza pannello sync | `None`, **non** 50 — gate non ingannato dai contenuti del cliente |
| C2 | `IT01879020517A2026%` (nome file vero) | `None`, non 2026 |
| C3 | `150%`, `-5%`, `0%` | 150/-5 scartati; `0` accettato come valore vero e blocca lo scan |
| C4 | Pannello senza percentuale (sync finita) | `None` → si procede, e il motivo lo dichiara |
| C5 | Percentuale che **scende** fra due letture | nessun crash; il gate rilegge, non memorizza |

## D. Concorrenza (`salvataggio`) — `asyncio.gather` vero, non sequenziale

| # | Attacco | Difesa attesa |
|---|---|---|
| D1 | 20 salvataggi paralleli della **stessa** chat | 1 sola riga; nessuna eccezione risale |
| D2 | Salvataggio parallelo di stessa chat con numero e senza | 1 riga, **con** il numero (il dato migliore vince) |
| D3 | Due scansioni dello stesso numero in parallelo | nessun duplicato; conteggio finale = chat distinte |
| D4 | Salvataggio mentre la stessa riga viene promossa | lo stato avanzato non retrocede |

## E. Macchina a stati (`salvataggio`, Fase B)

| # | Attacco | Difesa attesa |
|---|---|---|
| E1 | Ri-scansione di una chat già `promosso` | resta `promosso`, non torna `nuovo` |
| E2 | Doppia promozione della stessa riga | la seconda è no-op, **non** un secondo `WaContact` |
| E3 | Riga `scartato` che ricompare nello scan | resta `scartato` (decisione dell'operatore rispettata) |

## F. Apertura chat (`pannello`) — la trappola centrale

| # | Attacco | Difesa attesa |
|---|---|---|
| F1 | La sidebar si riordina fra risoluzione e click | verifica post-click fallisce → **nessuna scrittura**, esito `non_verificata` |
| F2 | L'header resta quello della chat precedente per 1s | le attese a pazienza crescente lo assorbono, nessun falso mismatch |
| F3 | Il pannello info non si apre (il 5% misurato) | esito `verificata`, numero `None`, riga **salvabile** |
| F4 | `titolo_atteso` = `None` | nessun click, nessuna scrittura |
| F5 | Riga sparita dal DOM virtualizzato | nessun click su un elemento a caso |

## G. Invarianti finali — via SQL, a fine run

Queste non sono test di funzione: si eseguono **dopo** uno scan reale e valgono più di
qualunque asserzione unitaria.

| # | Query | Deve tornare |
|---|---|---|
| G1 | righe con un numero di telefono riconoscibile in `chat_title` o `display_name` | **0** (P12) |
| G2 | righe con `numero_leggibile = true` e `phone_hmac IS NULL` | **0** |
| G3 | righe con `chat_title IS NULL` **e** `phone_hmac IS NULL` | **0** (non deduplicabili né identificabili) |
| G4 | `(number_id, chat_title)` duplicati | **0** |
| G5 | `(number_id, phone_hmac)` duplicati con hmac non nullo | **0** |
| G6 | righe di un tenant dopo `wa_purge_tenant.py --yes` | **0** (GDPR) |
| G7 | conteggio raccolto vs `aria-rowcount` dichiarato | scarto spiegabile (gruppi/chat con sé stessi), **mai** silenzioso |

## H. Sola lettura — l'invariante che definisce la Fase A

| # | Attacco | Difesa attesa |
|---|---|---|
| H1 | Scan completo su un numero con campagne attive | `wa_messages` invariato: **zero** messaggi creati |
| H2 | Scan su un numero in `cooldown`/`suspended` | nessun invio, nessun cambio di stato del numero |
| H3 | Scan mentre il kill-switch WA è attivo | si ferma pulito, non "completa" a vuoto |
| H4 | Verifica che nessun `sent_today` sia cambiato dopo uno scan | invariato — la Fase A non tocca cap né warmup |

---

## Note di esecuzione

- **Una sola suite pytest alla volta**: `WA_TEST_DB_SLOT=<nome>` sempre.
- I test **non devono interrogare tabelle nude**: il `db_session` del conftest fa `rollback()`
  dopo il `commit()`, quindi non isola. Filtrare per `number_id` (vedi `_scoperte_di` in
  `tests/test_wa_discover_modello.py`).
- Ogni difesa aggiunta va verificata con la **prova del nove**: rimuovere la difesa e
  controllare che il test torni rosso. Un test che passa sia con che senza la protezione non
  sta proteggendo niente.
- G1-G7 e H1-H4 richiedono uno **scan reale** su Primero: vanno eseguiti nella sessione di
  collaudo con browser vivo, non qui.
