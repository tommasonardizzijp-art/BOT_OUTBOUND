# Design — fix G2/G3/G5/G4 del canale WhatsApp + campagna auto-discover

- **Data**: 2026-08-08
- **Repo**: `D:\BOT OUTBOUND`, branch `main`, HEAD di partenza `1b1c421`
- **Stato**: approvato da Tommaso in sessione di brainstorming
- **Fonti**: referto `docs/superpowers/qa/2026-08-08-review-indipendente-verifica.md`, SDD `docs/whatsapp/SDD-whatsapp-channel.md`, contratto `docs/whatsapp/contratto-M2-M3.md`, catalogo DOM `docs/whatsapp/wa-dom-catalog.md`

Questo documento fonde due richieste che sarebbero diventate due piani in conflitto: i
quattro difetti "gravi" autorizzati dopo la review indipendente, e una modalità di
campagna nuova che scopre i contatti dalla lista chat di WhatsApp invece che da un CSV.
Sono fusi perché toccano lo stesso frontend e la stessa macchina a stati.

---

## 1. Ordine delle fasi e dipendenze

```
FASE 1 ─ frontend, branch proprio     G2 + G3 + G7        prima del collaudo
FASE 2 ─ backend,  branch proprio     G5 + G4 + drift FM2 prima del collaudo
        ────────── COLLAUDO DAL VIVO (sessione dedicata, serve Tommaso col telefono) ──────────
FASE 3 ─ PoC di misura DOM            info-contatto, Liste/tag, gruppi, tempi
FASE 4 ─ auto-discover                Fase A scan + Fase B approva/invia
```

**Perché 1 e 2 vengono prima di tutto**: sono le uniche cose in lista che servono *prima*
di un collaudo dal vivo. Oggi, se durante il collaudo scatta un guasto, l'operatore resta
bloccato senza via d'uscita dallo schermo (G2) e senza sapere che il canale si è fermato
(G3).

**Parallelizzazione**: Fase 1 tocca solo `frontend/`, Fase 2 solo `backend/` e `docs/`.
Nessun file condiviso → due branch e due PR in parallelo, senza conflitti.

**Migrazioni Alembic**: head attuale `029`. Fasi 1 e 2 **non hanno migrazioni**. La `030`
è libera e va alla Fase 4. Nessuna contesa (il problema di PR #47 non si ripresenta).

---

## 2. FASE 1 — G2, G3, G7 (frontend + una piccola aggiunta backend)

### 2.1 G2 — bottone "Recupera" per le campagne in `error`

**Problema.** FM2 mette la campagna in `error`. In quello stato la pagina di dettaglio
espone solo "Stop" → `stopped`, irreversibile. Un guasto nostro (selettore rotto, mezz'ora
di lavoro) costringe a chiudere per sempre una campagna o a fare una `UPDATE` a mano.

**L'endpoint esiste già e non è mai stato collegato**: `POST /wa/campaigns/{id}/recover`
(`backend/app/api/wa_campaigns.py:261-315`), body `RecoverRequest.motivo` (`max_length=500`,
validator che rifiuta il vuoto con 422, righe 245-258), transizione `error → paused`
(righe 282-288). `grep -ri recover frontend/` → zero risultati.

**Soluzione.**
- `frontend/lib/waApi.ts`: aggiungere `campagne.recover(id, motivo)` accanto a
  `start/pause/resume/stop` (righe 213-217).
- `frontend/app/wa/campagne/[id]/page.tsx`: nuovo predicato `puoRecuperare = status === 'error'`
  accanto a `puoAvviare` (220), `puoRiprendere` (222), `puoFermare` (223).
- Modale del motivo: **tendina di motivi frequenti + casella libera**. Tendina:
  "selettore rotto", "sessione WhatsApp caduta", "altro". La casella libera è sempre
  disponibile e obbligatoria quando si sceglie "altro".
- **Il motivo finisce solo nel log e nell'evento**: la tabella campagne non ha una colonna
  note. La UI non deve promettere di rimostrarlo.

**Decisione presa: due click, non uno.** Dopo il recupero la campagna resta in `paused` e
va ripresa a mano. L'endpoint di proposito non porta mai a `running`, perché le condizioni
di avvio (numero attivo, nessun'altra campagna sullo stesso numero, riaccodamento del
worker) le verifica solo `avvia`. Un "recupera e riparti" creerebbe due verità diverse su
quando si può inviare.

### 2.2 G3 — striscia di stato del canale

**Problema.** Il canale può fermarsi da solo (circuit breaker opt-out > 25% con almeno 10
invii) e la UI non lo dice: la pagina campagna continua a mostrare "In corso" con i numeri
fermi. Stessa cecità sul master switch `WA_SEND_ENABLED`, che sta nel `.env`. È
esattamente il modo in cui il collaudo M5 è finito 90/90 verde con zero messaggi spediti.

**Soluzione.** Componente condiviso montato nel layout di **tutte** le pagine `/wa/*`
(numeri, lista campagne, dettaglio campagna). Il valore vero della striscia è vederla
*mentre* si guarda una campagna che sembra in corso e non lo è: sulla sola home non
intercetta quel momento.

Contenuto della striscia:
- canale fermo sì/no **e il motivo**;
- master switch acceso/spento (**sola lettura**);
- numeri attivi, campagne in corso, inviati oggi;
- **cap effettivo del numero** (aggancio della decisione G4, vedi §3.2).

Polling proprio ogni 10s (stesso ritmo del polling KPI già presente nel dettaglio).

Bottoni:
- **"Ferma il canale"** → `POST /wa/ops/halt` con `{"reason": "<testo>"}`, stesso modale
  tendina+libero di G2.
- **"Riprendi"** → modale che **mostra prima il motivo per cui il canale si era fermato**
  (se è stato il breaker: quale campagna e a che percentuale di opt-out), poi chiede
  conferma. Il caso brutto è riprendere senza aver capito che il numero sta bruciando.
  Poi `POST /wa/ops/resume` (nessun body).

**Aggiunta backend necessaria.** `GET /wa/ops/status` (`backend/app/api/wa_ops.py:38-52`)
restituisce già `wa_halted`, `send_enabled`, `numeri_attivi`, `campagne_running`,
`inviati_oggi`, ma **non il motivo dello stop**. Il motivo è salvato a DB (fix B1, PR #52)
ma non esposto. Va aggiunto un campo `motivo_stop` (nullable) alla risposta. È l'unica
modifica backend della Fase 1. Nessuna migrazione: il dato esiste già.

**Trappola da non ripetere.** In `frontend/lib/api.ts:404-405` esiste già
`halt(reason, kind?)` → `POST /admin/halt`: è **l'interruttore del canale Instagram**
(`bot_state_service.halt`, colonna diversa da `halt_wa`) e non ferma WhatsApp. Non
riusarlo, non confonderli.

**Master switch: sola lettura, decisione presa.** Renderlo comandabile da UI vorrebbe dire
spostarlo dal file di configurazione al database: crea una seconda verità da tenere
allineata, richiede una migrazione e una regola su chi vince in caso di disaccordo. È una
decisione di architettura, non un bottone. Rimandata senza costi.

### 2.3 G7 — bottone QR per i numeri `disconnected`

Incluso perché tocca gli stessi file e costa quasi nulla. Se durante il collaudo la
sessione WhatsApp cade — e cade — senza questo bottone si resta fuori e si deve rimettere
mano da riga di comando.

**Da verificare in fase di piano**: quale endpoint espone già il QR / la ri-associazione,
e in quali stati va mostrato il bottone.

---

## 3. FASE 2 — G5, G4, drift SDD (backend + documenti)

### 3.1 G5 — la recovery all'avvio non deve declassare chi è già stato servito

**Problema.** All'avvio, `recover_wa_sending_on_startup()`
(`backend/app/services/wa_worker.py:765-811`) chiude i messaggi rimasti in `sending` come
`failed` e mette il contatto in `skipped`. L'`UPDATE` sul contatto (righe 794-797) **non
filtra per lo stato attuale del contatto**: un contatto che ha lasciato un messaggio
orfano ma è poi stato servito bene torna da `completed` (o `replied`) a `skipped`. Non
causa invii sbagliati, ma corrompe i conteggi della campagna e la sua chiusura automatica.

**Interazione con la guardia di PR #52** (`backend/app/services/wa_sender.py:317-352`):
quella guardia, trovando un `WaMessage` in `sending`/`sent` per la tripla
(campaign_id, contact_id, step_index), non rimanda e mette il contatto in `skipped`,
lasciando però il messaggio in `sending`. Al riavvio successivo la recovery lo ritrova e
declassa di nuovo. Oggi lo stesso orfano è gestito da due meccanismi in due momenti, e il
secondo disfa il lavoro del primo.

**Soluzione.** Aggiungere `WaCampaignContact.status.in_(('queued', 'in_sequence'))` alla
`WHERE` dell'update sul contatto. Le due cose smettono di pestarsi: la recovery tocca solo
chi è ancora in lavorazione, il secondo passaggio diventa un no-op.

**TDD**: test scritto prima e **visto fallire per la ragione giusta**. Un rosso che arriva
da un helper incompleto non conta, il test va rifatto (lezione della sessione precedente:
`template_variant NOT NULL`).

### 3.2 G4 — la rampa di warmup viene disattivata, non riparata

**Problema.** `advance_wa_warmup_if_needed()`
(`backend/app/services/wa_number_manager.py:62-126`) avanza `warmup_day` guardando solo
`warmup_advanced_date`, mai gli invii reali. Avanza nei giorni in cui il processo si
accende: backend spento tre giorni → non avanza; riavviato cinque volte in un giorno →
avanza di uno. Un numero può arrivare in cima alla rampa senza aver mai mandato niente, ed
è la situazione attuale del numero del progetto (master switch spento da giorni).

Non fa danno **oggi** perché il cap effettivo è `min(daily_cap, limite_campagna, gradino)`
(`effective_wa_daily_cap()`, righe 129-139) e `daily_cap` vale 20 di default. Il giorno in
cui si alza `daily_cap` — la leva documentata per crescere — la rampa diventa l'unico
freno rimasto, e si è già sciolta da sola.

**Decisione di Tommaso: la rampa si spegne, in questa fase non serve.** Il ragionamento è
che seguirà gli invii personalmente; la rampa diventerà utile quando il processo sarà
automatizzato.

**Come si spegne, concretamente.** Il gradino entra nel `min()` **solo se `warmup_day > 0`**.
Quindi disattivare significa `warmup_day = 0` sui numeri: operazione sui dati, non sul
codice. Conseguenze, tutte volute:
- il freno resta **solo `daily_cap`** → per questo la striscia G3 deve mostrare il cap
  effettivo, altrimenti l'unica protezione rimasta è invisibile;
- i gradini maturati a vuoto **si azzerano da soli**: riaccendendo la rampa in futuro si
  riparte da 1. Nessuna mina lasciata indietro.

**Da verificare nel piano**: dove viene invocata `advance_wa_warmup_if_needed()`. Il giro
di verifica non l'ha trovata né nel worker né in `main.py`. Se non la chiama nessuno, la
rampa è già inerte e l'unica azione che resta è `warmup_day = 0` sui numeri.

**Decisione futura, già presa, da non ri-discutere.** Quando la rampa verrà riaccesa,
la politica è: **avanza solo nei giorni con almeno un invio reale, e decade di un gradino
dopo X giorni di inattività**. È la politica più vicina a come ragiona WhatsApp: un numero
fermo un mese che riparte a 100/giorno è sospetto.

### 3.3 Drift SDD/contratto vs codice — vince il documento, con una rete

**Il drift, verificato.** `SDD-whatsapp-channel.md:524` e `contratto-M2-M3.md:143` dicono
entrambi che il segnale `nessuna-cronologia:nessun-messaggio-nel-pannello` porta il
contatto in `skipped` con motivo `no_existing_chat` (guardia V2, non è un guasto). Il
codice fa l'opposto: quel segnale sta in `_SEGNALI_COLPA_NOSTRA`
(`backend/app/services/wa_sender.py:38-43`), `valuta_apertura` (righe 56-81) lo classifica
`colpa_nostra=True` e **arma FM2** (righe 306-307), che dopo 3 volte ferma il numero 4 ore.

**Decisione: hanno ragione i documenti, si cambia il codice.** Il segnale si sposta in
`_SEGNALI_CHAT_INESISTENTE` e torna a produrre `skipped`/`no_existing_chat`.

**Rete di sicurezza, richiesta esplicitamente.** Da sola, quella riga toglie il campanello
d'allarme: se il DOM si rompe *proprio in quel punto*, invece di fermarsi il sistema brucia
l'intera campagna marcando tutti come freddi, in silenzio. Quindi:

> **5 `no_existing_chat` consecutivi armano comunque FM2.**

Distingue "un pannello lento su una cronologia vecchia" (uno ogni tanto, si salta e si va
avanti) da "il DOM è cambiato" (tutti di fila, ci si ferma). Il contatore è **consecutivo**:
un invio riuscito lo azzera.

**Aggiornamento documenti.** SDD § guardia V2 e contratto M2-M3 vanno aggiornati per
descrivere il contatore, che oggi non è scritto da nessuna parte. Il rischio di non farlo è
concreto: su questo canale la documentazione è stata usata come fonte più di una volta, e
chi progetterà il prossimo pezzo leggendola concluderebbe l'opposto di quello che il
sistema fa.

---

## 4. FASE 3 — PoC di misura DOM (dopo il collaudo dal vivo)

**Quando**: dopo il collaudo, per decisione di Tommaso. Nessuna interferenza sulla stessa
sessione WhatsApp usata per i test.

**Perché serve prima della Fase 4**: il pannello info-contatto **non è mai stato mappato**
(zero menzioni nel catalogo DOM), e senza sapere se il numero è leggibile metà del disegno
della Fase A è aria.

**Cosa deve misurare, in ordine di importanza:**

1. **Pannello info-contatto**: selettori per aprirlo e per leggere il numero, e soprattutto
   **su che percentuale di chat il numero risulta leggibile**. Se è bassa, la Fase A cambia
   forma.
2. **Liste / etichette di WhatsApp**: esistono e sono pilotabili **su WhatsApp Web** (non
   solo su iPhone)? Selettori della barra dei filtri in alto. Da questo dipende l'intero
   disegno "una lista per volta" della Fase A.
3. **Riconoscere un gruppo**: oggi nel catalogo non esiste nessun selettore né attributo,
   solo una nota testuale. Serve un modo programmatico.
4. **Secondi per chat**: il numero che decide se il disegno regge. 500 chat sono venti
   minuti o quattro ore?

**Forma**: script isolato in `backend/scripts/poc_wa/`, come M0. Solo misura, nessun invio.

---

## 5. FASE 4 — campagna auto-discover, a due fasi

### 5.1 Il problema di partenza

Il numero WhatsApp di Primero ha anni di chat reali coi clienti ma **zero contatti nel DB
del bot**: nessun CSV da caricare, nessuna fonte esterna. L'unica fonte di verità è la
lista chat di WhatsApp stessa.

### 5.2 Perché due fasi e non un colpo solo

L'idea originale era: scorri dal fondo, apri, manda, leggi il numero, salva. È stata
scartata per un rischio concreto: **il fondo della lista chat è per definizione la
popolazione più fredda e più vecchia** — quella con la più alta probabilità di rispondere
STOP o di segnalare. Il circuit breaker si arma al 25% di opt-out. La modalità sarebbe
progettata per pescare esattamente la popolazione che può spegnere il canale, con un motore
nuovo mai collaudato, sul numero aziendale del negozio. In più, lì sotto non ci sono solo
clienti: fornitori, corrieri, numeri sbagliati, ex dipendenti, parenti.

### 5.3 Il disegno approvato

```
FASE A (nuova, SOLA LETTURA — non manda niente, non tocca cap/breaker/warmup)
  selezioni una Lista/etichetta di WhatsApp  →  scan completo di quella lista
     per ogni chat 1:1:
       ├─ nome + numero leggibili       → salvo entrambi
       ├─ solo nome (numero non letto)  → salvo lo stesso, marcato "numero non letto"
       ├─ gruppo                         → escluso
       └─ chat con te stesso             → escluso
     chat silenziate: INCLUSE (scelta esplicita: possono essere clienti veri)

FASE B (riusa il motore già collaudato)
  la UI mostra i contatti scoperti  →  spunti chi contattare  →  campagna normale
  ── selezionare È l'approvazione: nessuno stato nuovo da inventare ──
```

**Perché la Lista/etichetta risolve il problema più grosso.** La sidebar di WhatsApp è
virtualizzata (`data-virtualized`, il DOM ricicla i nodi fuori viewport) ed è il punto
esatto dove uno scroll "fino in fondo" su anni di chat si rompe. Lavorando **dentro una
lista filtrata da WhatsApp stesso** il perimetro è definito dall'operatore, non
dall'ordinamento della sidebar: si spezzano i clienti in liste da ~500 e il motore ne fa
una per volta, tutta in un passaggio. Sparisce anche il problema della ripresa dopo
un'interruzione: si rilancia la stessa lista e i già visti li scarta il DB.

**Scelta sui contatti senza numero**: si salvano comunque, marcati. Non sono contattabili
dal bot (senza numero non c'è `phone_hmac`), ma restano visibili e recuperabili a mano
dalla rubrica del telefono. Nessuna informazione buttata via.

### 5.4 Modello dati — serve una tabella di staging

`WaContact` (`backend/app/models/wa.py:157-196`) ha `phone_hmac` e `encrypted_phone`
entrambi **`NOT NULL`**, e la unique è composita `(tenant_id, phone_hmac)` (riga 164) — non
su `phone_hmac` da solo. Quindi **un contatto senza numero non può stare in `WaContact`**.

**Tabella nuova `wa_discovered_chats`** (migrazione **`030`**), con:
- `tenant_id`, `chat_title`, `display_name`;
- `encrypted_phone` e `phone_hmac` **nullable**;
- `source_list_label` (quale Lista/etichetta è stata scansionata);
- `discovered_at`, `numero_leggibile` (bool), `status` (`nuovo` / `promosso` / `scartato`).

La Fase B **promuove** in `WaContact` solo i contatti approvati e con numero, creando poi le
righe `WaCampaignContact`. Il dedup verso i contatti veri resta quello esistente
(`tenant_id, phone_hmac`).

Vantaggio del separare: la roba grezza scoperta dal browser non sporca i contatti veri del
bot, e una ri-scansione della stessa lista è un'operazione innocua.

### 5.5 Cosa NON cambia

- La Fase A **non invia**: non tocca warmup, cap giornaliero, circuit breaker, dead-man
  switch. Tutte le guardie esistenti restano valide perché operano sulla Fase B, che è il
  flusso di campagna normale già collaudato.
- La **regola V2** ("si scrive solo a chat già esistenti") è rispettata per costruzione: la
  Fase A raccoglie *solo* chat esistenti.

---

## 6. Fuori perimetro, con motivo

| Cosa | Perché fuori |
|---|---|
| `UniqueConstraint` di B2 | Serve prima guardare cosa contiene già `wa_messages` in produzione, non ispezionabile da qui. **Da chiedere a Tommaso** quando ci si arriva. |
| Master switch comandabile da UI | Sposterebbe il flag dal `.env` al DB: decisione di architettura, rimandabile senza costi. Per ora sola lettura. |
| Riparare la rampa di warmup | Disattivata per scelta (§3.2). La politica per quando verrà riaccesa è già decisa e scritta. |
| Collaudo dal vivo | Sessione dedicata, serve Tommaso col telefono. |

---

## 7. Vincoli di esecuzione

- **TDD** su ogni fix: test scritto prima e **visto fallire per la ragione giusta**.
- **Codice → branch + PR**, mai commit diretti su `main`.
- `pytest`: DB sqlite locale, **mai** Supabase di produzione, **una sola suite alla volta**
  (il DB di test è condiviso, due run in parallelo danno rossi fantasma).
- Non avviare backend, worker o cron per fare analisi: il canale è fermo.
- Attenzione ai `git add` di cartella: nel worktree ci sono file non tracciati di altre
  sessioni (`backend/scratch_graphql.json`, `backend/scratch_profile.txt`,
  `backend/scripts/set_proxy.py`). Non inglobarli.
- Prima di dire "verde": pensare a cosa il runner CI **non** ha. Una dipendenza presente
  solo nel venv locale non fa fallire un test, fa fallire la **collection** e ferma tutta
  la suite.
