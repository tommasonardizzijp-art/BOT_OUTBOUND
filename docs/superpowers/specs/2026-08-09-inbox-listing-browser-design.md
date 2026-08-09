# Listing contatti dall'inbox DM via browser — design

Data: 2026-08-09
Stato: approvato da Tommaso, pronto per il piano di implementazione

## Problema

Le campagne `scrape_mode=dm_threads` raccolgono i contatti dai DM gia' avviati. Oggi esiste
un solo motore, via private API (`direct_v2/inbox/`, pagine da 20). Serve un secondo motore
che faccia lo stesso lavoro **via browser**, per gli account su cui non vogliamo (o non
possiamo) usare la private API.

Il tentativo precedente fu rimosso (migration 020) con questa motivazione, scritta in
`build_inbox_source`: la lista DM su instagram.com mostra solo il **nome visualizzato**, non
l'@username ne' il pk, quindi dal DOM non si ricava nessun identificatore usabile.

**La motivazione era incompleta.** Misurato il 2026-08-09 (`scripts/probe_inbox_web_username.py`,
profilo sacrificabile `claudio.abbigliamentovincente`): e' vero che la *lista* non espone lo
username, ma **aprendo il thread lo username c'e'**, sia come href sia come testo dell'header:

```
href nel DOM      : /lerocchettebyelena/
header del thread : 'lerocchettebyelena - Instagram'
```

## Fatti misurati (non assunti)

Tutto quanto segue e' stato verificato sul campo, non dedotto.

| Misura | Esito | Fonte |
|---|---|---|
| Username recuperabile dal thread aperto | Si', href + header | `probe_inbox_web_username.py` |
| Payload JSON intercettabile coi thread | **Zero response** — i DM viaggiano su canale realtime | idem |
| Tempo click → username leggibile | 299 ms min / 512 ms mediana / 1019 ms max, 0 perdite su 6 | `probe_inbox_web_tempi.py` |
| Nomi leggibili dalla lista senza aprire | 9 per schermata, testo intero anche se troncato a schermo | idem |
| pk (`ig_user_id`) presente nella pagina del thread | **NO** — l'unico numero lungo vicino e' `55555555555554`, un segnaposto | probe pk dedicato |
| Indicatore di caricamento (spinner / `aria-busy` / `progressbar`) | **NON ESISTE** — 0 su 10 giri di scroll | probe segnali scroll |
| Altezza del contenitore durante lo scroll | cresce a ogni caricamento: 1152 → 1872 → 2232 → ... → 5112 | idem |
| Ritardo di caricamento | subito dopo lo scroll l'altezza e' ancora la vecchia, ~1-2s dopo e' cresciuta | idem |
| Numero di righe nel DOM | **oscilla** (78, 96, 75, 78, 93, 72...): lista virtualizzata | idem |
| Chi ha scritto per ultimo | leggibile **dalla lista**, prefisso `Tu:` sull'anteprima | probe messaggi |
| Anteprima ultimo messaggio | nella lista, **troncata** | idem |
| Data nella lista | **relativa** (`22 sett`) | idem |
| Data nel thread aperto | **assoluta** (`9 feb 2026, 20:28`), formato localizzato | idem |
| Testo integrale dei messaggi | leggibile via `document.body.innerText` a thread aperto | idem |
| Delimitatore di fine conversazione | la riga `Scrivi un messaggio...` (localizzata) | idem |
| Segnale visivo di chat non letta (pallino/grassetto/aria-label) | **NON MISURATO in modo conclusivo** — su 10 righe di probe tutte identiche (`pallini=0`, `pesi=['400']`), ma l'account di prova non aveva nessuna chat realmente non letta al momento della misura (zero badge numerici, zero attributi `unread` nel DOM) | `probe_inbox_web_nonlette.py`, 2026-08-09 |
| Richieste fallite verso endpoint inbox durante scroll sano | **ZERO** su 12 giri di scroll (altezza 1152→3672), zero in assoluto, zero ristrette a `direct_v2`/`graphql` | `probe_inbox_web_requestfailed.py`, 2026-08-09 |

**Conseguenza sulla velocita'**: il dato e' leggibile in mezzo secondo, quindi *l'apertura* non
e' il collo di bottiglia. Il throughput reale lo decidono le **pause**, non la lettura: vedi
"Il ritmo dipende dalla zona". Attenzione al numero `~1200 chat/ora` stampato dal probe: assume
una pausa fissa di 1,5 s, cioe' nessuna sosta e nessuno stacco. Non e' il ritmo del motore.

**Conseguenza sui dati**: il canale browser non conosce il pk. Vedi "Targa provvisoria".

## Decisioni prese (16 domande a Tommaso)

1. **I due motori convivono**, scelti per campagna via `inbox_engine`. L'alternanza automatica
   e' esplicitamente fuori scope.
2. **Perimetro**: solo conversazioni 1-a-1. Niente scheda Generali, niente Richieste.
3. **Dati raccolti**: username, nome visualizzato, data ultimo messaggio, chi ha scritto per
   ultimo, **testo integrale** dell'ultimo messaggio, provenienza del dato.
4. **Ritmo**: differenziato per zona — pause piene dove si aprono chat nuove, ritmo rapido dove
   si attraversa la zona gia' nota (decisione rivista dopo il ricalcolo del throughput).
5. **Strategia di ripresa**: automatica, sempre dalla cima, due modalita' che si alternano da
   sole. Nessun segnaposto di posizione.
6. **Soglia per entrare in scorrimento veloce**: 10 nomi gia' noti consecutivi.
7. **Contatto gia' noto incontrato durante la raccolta**: si salta senza riaprire, ma si
   aggiornano data e ultimo messaggio.
8. **Criteri di stop**: fondo lista, tetto contatti (`list_target`), tempo massimo di sessione.
   Il quarto criterio scelto da Tommaso ("troppo gia'-visto") **non e' implementabile come
   stop** in questo motore — vedi "Perche' non esiste uno stop per troppo gia'-visto": diventa
   un avviso sull'esito.
9. **Diversivi**: in base al contenuto — ci si sofferma dove c'e' stato uno scambio vero, si
   tira dritto dove c'e' solo il nostro messaggio mai risposto.
10. **Pause**: tre livelli, con le soste di medio livello *usate* per rileggere la
    conversazione, non come attesa a vuoto.
11. **Sessione**: 30-55 minuti, poi stacco.
12. **Tetto giornaliero**: configurabile per campagna, nessun valore imposto.
13. **Blocco/challenge**: ferma tutto e avvisa subito.
14. **Avvio**: dentro la campagna, come oggi, con la scelta del motore.
15. **Arricchimento**: **indispensabile** (la campagna di Michele serve proprio a estrarre bio
    e contatti). Va fatto **via browser**; il pulsante API va reso grigio e non funzionante.
16. **Collaudo riuscito se**: nessun contatto perso, la ripresa cade nel punto giusto, i tempi
    non mostrano schemi ripetitivi.

## Architettura — dove si innesta

Un **secondo ramo accanto al primo**, non una modifica del primo.

```
                    list_followers()                 <- governo: stato campagna,
                          |                             kill-switch, resume da break.
                          v                             NON si tocca.
                 scrape_mode == 'dm_threads'?
                          | si
                          v
                  inbox_engine?
                    +-----+-----+
              'api' |           | 'browser'      <- il bivio nuovo, ~3 righe
                    v           v                   in scrape_list.py:81
          run_inbox_list   run_inbox_browser_list
          --------------   ----------------------
          INTOCCATO         File nuovo:
                            due modalita', pause a
                            3 livelli, click umano
                    +-----+-----+
                          v
              salvataggio contatti + stato campagna
```

**Punto di innesto esatto**: `backend/app/services/scrape_list.py:81-83`. Oggi:

```python
if getattr(campaign, "scrape_mode", "followers") == "dm_threads":
    from app.services.scrape_inbox import run_inbox_list
    return await run_inbox_list(campaign_id, db, campaign)
```

Diventa un bivio a due uscite su `campaign.inbox_engine`.

**File nuovo**: `backend/app/services/scrape_inbox_browser.py`.

**Zero righe modificate** in: `scrape_inbox.py`, `inbox_source.py`, `_sample_page_delay`,
`_inbox_page_delay`, e in ogni criterio del motore API.

**Riuso** (senza modificarli): `BrowserSession` (apertura profilo + lock cross-processo),
`human_input.human_click` (punto casuale nel box, mouse in 5-15 step, micro-pausa),
`is_challenge_exception` + `isolate_challenged_account`, `emit_event`, `is_halted`,
`inbox_collect` (dedup), il rilevatore di interstiziali di `browser_bio` (`__blocked`).

**Contratto di ritorno**: identico a `run_inbox_list` — secondi di defer al session-break
(il worker solleva `Retry(defer=...)`), `None` se completata o interrotta.

L'interruttore `inbox_engine` **esiste gia' e accetta gia' `'browser'`**: modello
(`campaign.py:174`), schema (`schemas/campaign.py:37,82` con `pattern='^(browser|api)$'`),
API (`api/campaigns.py:321-327`), piu' `engine_switch_resets_cursor` e i suoi test
adversarial. Era stato solo reso no-op. **Nessuna migrazione serve per questo.**

## Targa provvisoria

Il canale browser non conosce il pk (misurato). Ma `ig_user_id` non e' un campo qualunque:

- e' sotto `UniqueConstraint("campaign_id", "ig_user_id")` (`follower.py:24`);
- e' la **chiave di prenotazione cross-account** (`reservation.try_reserve`), usata dal
  `campaign_orchestrator` in ~20 punti per impedire che due account scrivano alla stessa
  persona.

Lasciarlo vuoto smonterebbe quella protezione. Quindi:

**Il motore browser assegna una targa provvisoria negativa**, derivata deterministicamente
dallo username (stesso username → sempre lo stesso numero). Instagram non assegna pk negativi,
quindi la collisione con una targa reale e' impossibile **per costruzione**, non per fortuna.

Verificato che regge a valle:
- `reservation.try_reserve` usa `ig_user_id` come chiave secca di upsert, non assume positivi;
- `send_dm(username=...)` invia **per username**: la targa non tocca l'invio.

### La targa e' un ponte, non una toppa

```
1. RACCOLTA (browser)      username + nome + ultimo messaggio
                           targa provvisoria negativa
                                   |
2. ARRICCHIMENTO (browser) instagram.com/<username>/  -> per NOME, non per targa
                           riporta bio, contatti, E LA TARGA VERA
                                   |
3. SOSTITUZIONE            la scheda diventa identica a una raccolta via API
```

Verificato che l'arricchimento via browser porta la targa vera su **entrambi** i percorsi:
`browser_bio.py:152` (`pk=user.get("id")`, percorso nativo) e `browser_bio.py:194`
(`shaped["id"] = g.get("pk") or g.get("id")`, fallback GraphQL).

**Dove vive la sostituzione**: dentro `fetch_and_store_bio_browser`, subito prima di
`browser_bio.py:570` (`upsert_lead(ig_user_id=follower.ig_user_id, ...)`). Questo file compare
fra i "riusi" ma **va modificato**: e' l'unico punto in cui la targa vera e il follower
coesistono. La modifica e' additiva e non tocca il percorso API.

**Precisazione su `browser_bio` e il pk** (correzione a una stesura precedente): la Fase Bio
browser naviga per username (`:278`), ma **usa il pk** per i contatti business —
`_fetch_public_contact_inpage(raw_page, shim.pk)` a `:524`, che esegue
`fetch('/api/v1/users/${pk}/info/')` a `:389`. Il pk usato e' quello **vero appena letto dalla
pagina**, non quello del DB, quindi la conclusione regge: la targa provvisoria non intralcia
l'interrogazione. Ma non e' vero che il canale browser ignori del tutto il pk.

### La funzione della targa: specificata, non lasciata all'implementazione

`SHA-256` dello username **normalizzato** (minuscolo, senza `@` iniziale — vedi gli username
con la chiocciola gia' in DB), primi 63 bit, **negato**.

Le due scelte ovvie in Python sono entrambe sbagliate:
- `hash()` e' **randomizzato per processo** (PYTHONHASHSEED): deterministico dentro una
  sessione, **diverso alla successiva** → riga duplicata a ogni riavvio del worker. E un test
  "stesso username → stesso numero" che gira in un solo processo **passerebbe lo stesso**: e'
  esattamente il tipo di test che non vede il difetto. Il test di determinismo deve girare
  **su due processi separati**.
- `crc32` e' a 32 bit: su ~3000 contatti la collisione e' ~10⁻³, e lo spazio non e' confinato
  alla campagna — `GlobalContact.ig_user_id` e' globale a tutte le campagne
  (`global_contact_service.py:82-84`), quindi si riempie nel tempo. Due persone fuse in una
  riga, in silenzio.

### La fusione: lookup esplicita, non "provo e vediamo se solleva"

**La fusione non e' un caso limite: e' l'esito normale.** Ogni rename produce una targa diversa
per la stessa persona, e ogni contatto raccolto via API non ha `full_name`
(`scrape_inbox.py:179` lo scrive esplicitamente a `None`), quindi non e' riconoscibile.

Affidarla al vincolo UNIQUE fallisce, in due modi diversi a seconda del percorso — verificato:
- **percorso principale**: l'`IntegrityError` viene catturato a `browser_bio.py:1188-1190` →
  `outcome="error"` → la riga finisce `skipped` con `skip_reason="browser_error"`, la fusione
  **non avviene**, e tutto cio' che il browser aveva raccolto viene **buttato** col commit
  fallito. Nessun allarme.
- **percorso batch di pausa**: peggio. L'eccezione arriva a `browser_bio.py:1362-1364` che fa
  `break` **senza marcare il follower**; la selezione e' `limit(1)` **senza ORDER BY**
  (`:1351-1356`) → il giro dopo ripesca la stessa riga → stessa eccezione → batch a zero bio,
  per sempre. Il commento a `:1378-1379` documenta gia' questa forma di guasto, ma la
  protezione copre solo il ramo `not_found/private/error`, non l'eccezione inattesa.

Quindi: **lookup esplicita prima della scrittura**, in transazione propria.

**Regola di precedenza, dichiarata**: in una fusione **lo stato piu' avanzato vince sempre**.
Un contatto gia' `sent` non torna mai `pending` — altrimenti riceve un secondo messaggio.
Si preservano stato terminale, messaggi collegati e `locked_by_account_id`; si integrano solo i
campi vuoti.

### Atomicita': il requisito, non l'affermazione

Una stesura precedente affermava che il lock di arricchimento rendesse la sostituzione sicura.
**Falso**, verificato in `browser_bio.py:563-577`:
```python
follower.status = FollowerStatus.bio_scraped   # <- diventa mandabile
follower.locked_by_account_id = None           # <- lock RILASCIATO
await db.commit()                              # <- stesso commit
await upsert_lead(db, ig_user_id=follower.ig_user_id, ...)   # <- gira DOPO
```
Tre buchi: (1) il lock si rilascia nello stesso commit che rende la riga mandabile, quindi se la
sostituzione non e' **dentro quel commit** esiste una finestra in cui un worker DM claima una
riga con targa ancora provvisoria; (2) il lock protegge la riga in arricchimento, **non la riga
bersaglio della fusione**, che nessuno blocca; (3) `release_stale_locks` (cron ogni 15 min,
`LOCK_TIMEOUT_MINUTES=20`, `campaign_orchestrator.py:66,1251`) puo' togliere il lock sotto
l'arricchitore.

**Requisito**: sostituzione della targa e rilascio del lock nella **stessa transazione**, con la
riga bersaglio della fusione bloccata a sua volta.

Casi sotto test:
- targa vera **gia' presente** nella campagna → fusione per lookup, non `IntegrityError`
- fusione: lo stato piu' avanzato vince, un `sent` non torna `pending`
- `upsert_lead` riceve la targa **vera**, mai la provvisoria
- pk restituito **diverso** da una targa vera gia' registrata → username riassegnato, si ferma
- determinismo della targa **su due processi separati**

### Vincolo: l'arricchimento deve avvenire, e via browser

L'arricchimento via API interroga Instagram **con la targa** (`profile_lookup.py:49`,
`user_info_v1(pk)`): su un contatto appena raccolto cercherebbe una persona inesistente.

**Ma il vincolo su `bio_engine` da solo NON basta** — buco trovato in revisione adversarial,
verificato:

La Fase Bio non e' governata da `bio_engine` ma da `enrichment_level`, dichiarato
«ortogonale a `bio_engine`» (`campaign.py:46-48`). La guardia
`enrichment_blocca_la_fase_bio` sta a `scrape_bios.py:82` e fa `return None` **prima** del
dispatch su `bio_engine`, che e' a `scrape_bios.py:114`. E il default sulle campagne nuove e'
proprio `'none'` (`campaign.py:182-184`, `server_default=ENRICHMENT_NONE`).

Nella configurazione di **default**, quindi: `inbox_engine='browser'` +
`enrichment_level='none'` → la Fase Bio non parte affatto → i follower restano `pending` con
la targa provvisoria → e `follower_workability.py:33-38` li rende **inviabili cosi' come
sono**. Il ponte provvisoria→vera non viene mai attraversato.

Conseguenza grave: la targa negativa arriva fino a `GlobalContact`
(`browser_bio.py:570` → `global_contact_service.py:82-104`) e al dedup anti-doppio-DM
(`campaign_orchestrator.py:485-488`, `_mark_globally_contacted` a `:1503-1560`). La stessa
persona raccolta via API in un'altra campagna avrebbe una chiave diversa: **la protezione
contro il doppio DM cross-campagna non la riconoscerebbe**. E' esattamente la protezione che
la targa provvisoria doveva preservare.

### Tre presidi, non uno

Il primo presidio e' gia' saltato una volta in revisione: non ci si affida a un solo livello.

1. **Vincolo di configurazione**: `inbox_engine='browser'` richiede
   `enrichment_level != 'none'` **e** `bio_engine='browser'`. Rifiutato dall'API con
   messaggio esplicito; disabilitato nel frontend.
2. **Guardia difensiva sul confine dei dati**: `upsert_lead` / `GlobalContact` **rifiutano**
   una targa provvisoria (negativa). Se per qualunque motivo un contatto non arricchito ci
   arriva, si ferma li' e lo si registra. Regge anche se il vincolo (1) venisse aggirato da un
   percorso futuro.
3. **Export protetto**: le targhe provvisorie non compaiono nei CSV
   (`api/leads.py:363,374` esportano `ig_user_id` come **prima colonna**) — altrimenti numeri
   negativi finirebbero in un foglio Excel aperto da Tommaso.

**Limite dichiarato**: una campagna browser **senza** arricchimento non e' supportata. Se
servisse in futuro, la strada e' estendere il dedup globale a chiave username, non allentare
il vincolo.

### Frontend: l'interruttore inbox e' disabilitato via codice

Correzione a un'affermazione precedente di questo documento. Backend e schema accettano
`'browser'`, ma il frontend **no**: `frontend/app/campaigns/[id]/page.tsx:1084-1091` ha
`disabled` **cablato** (non condizionale), `cursor-not-allowed` e
`title="L'estrazione dell'inbox usa sempre l'API: il motore browser e' stato rimosso."`,
piu' il paragrafo esplicativo a `:1065-1067`. Va riabilitato e riscritto: **non e' a costo
zero**.

I pulsanti `bio_engine` da ingrigire stanno a `page.tsx:1167-1194` (`handleBioEngineSwitch('api')`
a `:1171`, `('browser')` a `:1184`). Il range 1173-1186 citato in una stesura precedente
tagliava a meta' entrambi i tag `<button>`.

**Bug collaterale da correggere**: `page.tsx:736` legge `campaign.inbox_engine ?? 'browser'`
mentre il backend ha default `'api'` (`campaign.py:174`). Su una campagna col campo nullo la UI
mostrerebbe uno stato **diverso da quello reale**.

Evoluzione possibile ma **fuori scope**: far usare all'arricchimento API il percorso per
username (`user_info_by_username_v1`, gia' usato da `import_resolver.py:94`), che
restituisce anche il pk. E' codice condiviso col motore veloce e non lo tocchiamo ora.

## Modello dati — migration 031

Aggiunte alla tabella `followers`, tutte **nullable**: le schede esistenti restano valide.

| Colonna | Tipo | Perche' |
|---|---|---|
| `last_message_at` | DateTime, null | data ultimo messaggio del thread |
| `last_message_from` | String(10), null | `'us'` / `'them'` — chi ha scritto per ultimo |
| `last_message_text` | Text, null | testo integrale (scelta esplicita di Tommaso) |
| `source_channel` | String(10), null | `'api'` / `'browser'` — provenienza del dato |

Il **nome visualizzato** riusa `full_name`, che esiste gia' ed e' nullable.

Nessuna modifica a colonne esistenti. Nessun vincolo nuovo.

## Le due modalita'

### REGOLA FONDANTE: si apre sempre cio' che non si riconosce

**Una riga il cui nome non e' riconosciuto viene SEMPRE aperta**, in qualunque modalita' ci si
trovi. Le modalita' non decidono *se* aprire: decidono solo **quanto in fretta si scorre e
quanto ci si ferma**.

```
   +--------------------- ZONA NUOVA (ritmo pieno) ------------------+
   |  apre ogni riga non riconosciuta                                |
   |  pause a tre livelli, soste di rilettura, diversivi             |
   |                                                                 |
   |  10 riconosciuti di fila  ----------------------------------+   |
   |  (contatore azzerato al primo non riconosciuto)             |   |
   +-------------------------------------------------------------|---+
                            ^                                    |
     3 non riconosciuti     |                                    v
     negli ultimi 10        |      +---- ZONA NOTA (ritmo rapido) -----+
                            +------|  scorre veloce, pause brevi       |
                                   |  MA apre comunque ogni riga non   |
                                   |  riconosciuta che incontra        |
                                   +-----------------------------------+
```

**Perche' questa regola e' fondante e non un dettaglio.** Nella stesura precedente la modalita'
scorrimento *non apriva niente*, e il rientro in raccolta chiedeva 3 non riconosciuti su 10.
Due revisori indipendenti hanno dimostrato che quel disegno **non perdeva qualche contatto: ne
raccoglieva zero a regime**.

Sequenza che lo prova, verificata sul design:
```
la lista e' ordinata per messaggio piu' recente
-> in cima ci sono i ~100 DM appena inviati dal bot = tutti gia' noti
-> righe 1-10 tutte note -> contatore a 10 -> SCORRIMENTO, zero chat aperte
-> da li' in poi 1-2 sconosciuti ogni 10 non superano mai la soglia 3-su-10
-> nessuna chat viene mai aperta -> sessione chiusa con 0 contatti
-> ripresa sempre dalla cima -> STESSA distribuzione -> zero anche domani
```
E l'avviso "nulla di nuovo, non serve rilanciare" sarebbe scattato **proprio quando i contatti
nuovi c'erano e sono stati saltati**: peggio del silenzio, perche' avrebbe scoraggiato il
rilancio che aggirava il difetto.

Con la regola fondante il buco non esiste **per costruzione**, non per taratura di una soglia.

**Perche' il contatore si azzera al primo non riconosciuto**: con ~100 DM/giorno in uscita la
lista si rimescola e si formano zone a macchia di leopardo. Il contatore governa solo il ritmo,
quindi l'errore peggiore che puo' fare ora e' andare piu' piano del necessario.

### Il riconoscimento NON autorizza a scrivere

Una riga riconosciuta viene saltata **e basta**: nessun dato viene aggiornato senza aprire.

Modifica la decisione 7 ("si salta senza riaprire, ma si aggiornano data e ultimo messaggio"),
per un motivo trovato in revisione:

```
archivio: "Marco Rossi" -> @marco.rossi88   (nome unico in archivio)
lista:    "Marco Rossi" -> in realta' @mrossi_design, mai visto
  -> il nome combacia ed e' unico -> classificato NOTO -> saltato
  -> con la vecchia regola: data e testo di @mrossi_design scritti
     sulla scheda di @marco.rossi88
```
I nomi visualizzati non sono univoci per costruzione. Saltare un contatto omonimo costa **un
contatto perso** (accettabile: Tommaso ha detto che perderne 2-3 ogni tanto va bene); scriverci
sopra costa **dati corrotti su una scheda sbagliata**, e quei campi guidano poi i diversivi —
il comportamento anti-ban verrebbe pilotato da dati falsi.

Quindi: i campi messaggio si scrivono **solo** quando la chat e' stata aperta e lo username
confermato. Il che e' anche l'unico modo di avere il testo integrale, visto che dalla lista
l'anteprima e' troncata.

**Ripresa fra sessioni**: sempre dalla cima. Nessun segnaposto di posizione salvato: la lista
si riordina a ogni DM in entrata, quindi "riparti dal contatto N" e' fragile per costruzione.
Il riorientamento costa poco perche' in scorrimento non si apre nulla.

**Contatti storici senza nome visualizzato**: le campagne esistenti hanno schede salvate senza
`full_name`. Decisione di Tommaso: **nessun codice dedicato**, in quei casi si rileggono le
chat. Il riconoscimento si popola da solo con l'uso.

## Dove si legge ogni dato (misurato)

```
DALLA LISTA — gratis, senza aprire nulla
  nome visualizzato       "KIDS Mstore Civitanova Marche (Uscita A14)"
  chi ha scritto x ultimo  prefisso "Tu:" sull'anteprima -> nostro; assente -> loro
  anteprima messaggio      TRONCATA
  data                     RELATIVA ("22 sett")

DAL THREAD APERTO — costa un'apertura
  username                 href "/modando__palermo/" + header "modando__palermo - Instagram"
  data                     ASSOLUTA ("9 feb 2026, 20:28")
  testo integrale          document.body.innerText, fino alla riga "Scrivi un messaggio..."
```

**Conseguenza sui diversivi**: sapere *prima di aprire* se c'e' stato uno scambio (assenza del
prefisso `Tu:`) rende gratuita la decisione se soffermarsi a rileggere la conversazione o
sbrigare la chat. Non serve aprire per decidere.

**Conseguenza sulla lettura dei messaggi**: i selettori "furbi" per il pannello conversazione
non funzionano (due tentativi falliti in fase di misura, 0 righe lette). Quello che funziona e'
`document.body.innerText` con delimitazione sulla riga del campo di scrittura.

### Stringhe localizzate: elenco unico

Tutte queste stringhe dipendono dalla **lingua dell'interfaccia dell'account**, non da una
impostazione nostra. Vanno in un unico elenco multilingua, non sparse nel codice:

| Cosa | Italiano | Inglese | Se sbagliata |
|---|---|---|---|
| autore = noi | `Tu:` | `You:` | **ogni** chat classificata come "ha risposto": classificazione tutta falsa, in silenzio |
| profilo cancellato | `Utente Instagram` | `Instagram User` | chat inutili aperte, tempo sprecato |
| fine conversazione | `Scrivi un messaggio...` | `Message...` | il testo dell'ultimo messaggio viene letto male |
| mesi nelle date | `feb`, `sett`, ... | `Feb`, `Sep`, ... | data non interpretabile |

Il primo caso e' il piu' pericoloso: non produce nessun errore, produce **dati sbagliati**.
Serve un test che giri su entrambe le lingue.

### Normalizzazione dei nomi

Il confronto fra nome letto e nome in archivio passa da una normalizzazione: minuscole, spazi
multipli compattati, spazi invisibili ed emoji rimossi.

**Segnaposto da ignorare senza aprire**: profili cancellati o disattivati (`Utente Instagram`).
Sono profili chiusi, aprirli e' tempo perso. La lista dei segnaposto e' **multilingua**:
l'etichetta dipende dalla lingua dell'interfaccia dell'account (`Utente Instagram` in italiano,
`Instagram User` in inglese, ecc.). Una lista monolingua funzionerebbe su un account e non
sull'altro.

**Nomi non univoci**: un nome che compare piu' di una volta in archivio non vale come
riconoscimento. In quel caso la chat si apre comunque.

**Troncamento**: i nomi lunghi appaiono troncati a schermo ma il testo nel DOM e' intero
(verificato: letto `Abbigliamento Vincente | Supporto Social settore ABBIGLIAMENTO` per
intero). Leggiamo il testo, non i pixel. Va comunque sotto test: se cambiasse, saremmo ciechi
senza accorgercene.

## Lista virtualizzata: lo scorrimento non puo' saltare

Instagram tiene nel DOM solo le righe vicine al viewport e **rimuove quelle che escono**
(misurato: il conteggio righe oscilla fra 72 e 96 mentre l'altezza cresce in modo monotono).

**Conseguenza vincolante**: se lo scorrimento avanza a salti piu' grandi del buffer renderizzato,
le righe in mezzo non entrano **mai** nel DOM e si perdono **in silenzio** — nessun errore,
nessun segnale, semplicemente contatti mancanti.

Quindi anche la modalita' scorrimento veloce avanza a **passi inferiori a una schermata**
(proposta: 0.6-0.8 dell'altezza visibile, randomizzato) e **legge a ogni passo**. Resta veloce
perche' non apre nulla, ma non salta. Vale sia per lo scorrimento veloce sia per l'avanzamento
durante la raccolta.

Corollario per i test: un test deve dimostrare che **nessun nome viene saltato** fra due passi
consecutivi, non solo che i nomi letti sono corretti.

## Fondo, lento o piantato

Tre situazioni che si assomigliano e vanno distinte, perche' portano a esiti opposti:

| | cosa succede | esito |
|---|---|---|
| **fondo** | non c'e' davvero altro | lista completata, esito normale |
| **lento** | IG sta caricando, arrivera' | aspettare: non e' un problema |
| **piantato** | connessione persa / IG bloccato | chiudere **senza** dichiarare completato + avvisare |

Sbagliare la distinzione e' costoso: dichiarare "esaurita" una lista solo lenta fa perdere
**tutti i contatti che stavano sotto**, in silenzio.

**Non esiste un indicatore di caricamento** da osservare (misurato: 0 spinner su 10 giri).
I segnali disponibili sono altri:

1. **Altezza del contenitore** — cresce a ogni caricamento riuscito. E' il segnale primario.
   Il numero di righe **non** e' utilizzabile: oscilla per via della virtualizzazione.
2. **Posizione di scroll** — siamo effettivamente in fondo, o stiamo ancora scendendo?
3. **Richieste di rete fallite verso gli endpoint dell'inbox**, dentro la finestra di attesa.

**Correzione importante sul punto 3.** Una stesura precedente lo definiva "il discrimine vero,
un segnale di fatto" — ed era l'unico segnale della sezione **senza una misura alle spalle**.
`page.on("requestfailed")` scatta su **qualunque** richiesta abortita: anteprime cancellate
durante lo scroll, prefetch annullati, tracker bloccati, `net::ERR_ABORTED` di navigazione. Su
una SPA come Instagram, in 30-55 minuti di scorrimento, "zero richieste fallite" non si verifica
**mai**.

Conseguenza se lasciato cosi': la condizione "fine lista" non sarebbe **mai** vera, ogni fine
legittima verrebbe classificata "piantato", la campagna non arriverebbe mai a `ready` e Tommaso
riceverebbe un allarme a ogni giro. Il ramo "fine lista" sarebbe **codice morto**.

Va quindi ristretto agli endpoint dell'inbox (`/api/v1/direct_v2/...` o la query GraphQL della
lista) e **misurato con un probe dedicato prima** di costruirci sopra una macchina a stati.

**Probe eseguito il 2026-08-09** (`probe_inbox_web_requestfailed.py`, account
`claudio.abbigliamentovincente`): **zero richieste fallite** in assoluto su 12 giri di scroll
sano (altezza cresciuta monotonicamente 1152 → 3672), quindi zero anche ristrette agli
endpoint inbox. Il segnale ristretto e' **pulito**: si adotta come discriminante per
"piantato", nei limiti di questa misura (una sessione di ~30s, un solo account, connessione
via proxy funzionante — non e' escluso che condizioni di rete peggiori producano rumore anche
ristretto; se in produzione si osservano falsi "piantato", il fallback e' la regola
conservativa sotto).

**Procedura**: mai decidere al primo dubbio. Attese a pazienza crescente (1, 2, 4, 8, 16 s;
tetto complessivo ~60 s), rileggendo i tre segnali a ogni giro.

- altezza cresciuta → si riprende normalmente, non era ne' fondo ne' guasto
- attese esaurite + altezza ferma + in fondo allo scorrimento + **zero richieste fallite verso
  gli endpoint inbox** (`direct_v2`/`graphql`) → **fine lista**, campagna completata
- attese esaurite + **almeno una richiesta fallita verso gli endpoint inbox** nel frattempo →
  **piantato**: chiusura pulita, campagna NON completata, avviso a Tommaso
- attese esaurite + altezza ferma ma **non** in fondo → anomalia: chiusura pulita e avviso
  (non dovrebbe accadere; se accade e' un cambio di struttura della pagina)

## Dedup in scrittura: sullo USERNAME (decisione di Tommaso)

La chiave di deduplicazione in scrittura e' lo **username**, non la targa.

Motivo: i contatti raccolti via API hanno `full_name = None` (`scrape_inbox.py:179`), quindi non
sono riconoscibili dal nome e le loro chat verrebbero riaperte. Arrivando con una targa
provvisoria diversa dalla targa vera che hanno gia' in archivio, un dedup basato sulla targa
non scatterebbe e produrrebbe **una riga duplicata per ogni contatto gia' presente** — e su una
campagna con arricchimento attivo quella riga duplicata puo' portare a un **secondo DM**.

Regola: prima di inserire, si cerca per `(campaign_id, username)`. Se esiste, si **aggiorna**
quella riga (rispettando la precedenza di stato: il piu' avanzato vince). Si inserisce solo se
non esiste.

Conseguenza sul conteggio: `list_target` va valutato sui **contatti distinti**, non sul numero
di righe (`run_inbox_list` conta `COUNT(Follower)` a `scrape_inbox.py:151-153`): righe duplicate
gonfierebbero il conteggio e chiuderebbero la campagna in anticipo, con meno contatti reali di
quelli richiesti.

Nota: questo **non** sostituisce il `UniqueConstraint("campaign_id","ig_user_id")`, che resta a
proteggere il percorso API. Sono due reti a maglie diverse.

## Conferme di lettura: si aprono solo le chat gia' lette (decisione di Tommaso)

Aprire una chat la **marca come letta** e manda la conferma di lettura al destinatario. Il
motore API non lo fa (`visual_message_return_type=unseen`, `inbox_source.py:58-64`): e' una
differenza di comportamento fra i due motori che chi sceglie un "engine" non si aspetta.

Su 6 chat di probe e' irrilevante. Su centinaia al giorno significa che Tommaso perde il badge
dei non letti — l'unico modo umano di accorgersi di una risposta vera — e che il destinatario
vede comparire "Visto" senza ricevere risposta.

**Decisione**: si aprono **solo le conversazioni gia' lette**. Quelle con messaggi non letti
vengono saltate e lasciate intatte, cosi' il segnale delle risposte vere resta leggibile.

**Probe eseguito il 2026-08-09** (`probe_inbox_web_nonlette.py`, account
`claudio.abbigliamentovincente`): risultato **inconcludente**, non "segnale assente". Le 10
righe lette dalla lista risultano tutte identiche su `pallini` (0) e `pesi` del font
(sempre `400`, mai un valore diverso), nessuna `aria-label`. Ma questo account **non aveva
nessuna chat realmente non letta** al momento della misura — verificato con tre controlli
indipendenti, tutti a sola lettura: nessun badge numerico in tutta la pagina, nessun
attributo DOM contenente `unread`/`non letto`, l'icona "Messaggi" in sidebar senza alcun
conteggio annesso (a differenza dell'icona Notifiche, che nello stesso screenshot mostra un
pallino rosso — il meccanismo di badge esiste sull'interfaccia, solo non per i DM in questo
momento). Il probe non ha quindi potuto testare l'ipotesi per mancanza di un caso di
controllo, non ha dimostrato che il segnale non esista.

**Decisione riportata esplicitamente a Tommaso, come prescritto**: non si è indovinata
un'euristica al suo posto. Il probe va rilanciato quando `claudio.abbigliamentovincente` (o
un altro account sacrificabile) ha almeno una risposta in arrivo non ancora letta
manualmente. Finché questo esito manca, la macchina a stati che decide se aprire una chat in
base al suo stato di lettura **non può essere implementata con certezza** — vedi Task 8/9.

Conseguenza sul perimetro: i contatti che hanno risposto e non sono ancora stati letti **non
vengono raccolti** in quel passaggio. Verranno raccolti dopo che Tommaso li ha letti. E' il
prezzo di preservare il segnale.

## Perche' non esiste uno stop per "troppo gia'-visto"

Il motore API ha `inbox_empty_page_stop = 8`: dopo 8 pagine consecutive senza contatti nuovi
si ferma e avvisa. Serve perche' li' **la lista non ha un fondo osservabile** — IG puo' tenere
`has_older=True` all'infinito, e senza quel contatore la raccolta girerebbe a vuoto per sempre
in silenzio (bug realmente accaduto).

Trasporre quel criterio qui sarebbe **codice morto**: dopo 10 gia'-noti consecutivi il motore
passa in scorrimento veloce e **smette di aprire chat**, quindi un contatore di "chat aperte a
vuoto" non raggiungerebbe mai una soglia piu' alta di 10. La soglia scatterebbe sempre prima.

E non serve: col browser il fondo e' un fatto osservabile (l'altezza smette di crescere), quindi
il rischio del giro infinito e' gia' coperto da un segnale reale invece che da un contatore.

**Al suo posto, un avviso sull'esito**: se una sessione si chiude — per fondo o per tempo —
avendo raccolto **zero contatti nuovi**, l'evento lo dichiara esplicitamente, cosi' Tommaso sa
che rilanciarla non serve finche' non arrivano nuovi DM in entrata. E' lo stesso messaggio che
il motore API emette come `drained`, ma calcolato su un segnale affidabile.

## Comportamento

### Il ritmo dipende dalla zona (decisione di Tommaso)

Il throughput annunciato in una stesura precedente (~1200 chat/ora) era **smentito dalla
tabella delle pause della spec stessa**. Il probe misurava 1200/ora assumendo una pausa fissa
di 1,5 s: nessuna sosta, nessuno stacco. Con le pause a tre livelli:

```
apertura + lettura                    1,0 s
pausa normale   1-4 s     (88%)       2,2 s
sosta          10-30 s    (10%)       2,0 s
stacco          2-5 min    (2%)       4,2 s
                                   ────────
                                      9,4 s per chat  ->  382 chat/ora
```
Cioe' **7,8 ore per 3000 contatti** e **287 chat per una sessione da 45 minuti**, non 900.
La stima "8-25h" che il documento dichiarava sbagliata era corretta nell'estremo basso.

**Decisione**: pause piene solo dove si aprono chat nuove, ritmo rapido dove si attraversa la
zona gia' nota. Il tempo si spende dove serve.

| | Zona nuova (si apre) | Zona nota (si attraversa) |
|---|---|---|
| pausa normale | 1-4 s, quasi sempre | 0,4-1,2 s |
| sosta | 10-30 s, ~1 su 10 | ~1 su 40 |
| stacco | 2-5 min, ~1 su 50 | invariato |
| diversivi | si', in base al contenuto | no: non c'e' niente da rileggere |

Il ritmo rapido e' credibile proprio perche' *e'* quello che fa una persona: si scorre in fretta
le conversazioni gia' viste e ci si ferma su quelle che interessano. La variabilita' resta
(stessa distribuzione, parametri diversi), quindi non nasce nessun ritmo fisso.

**Nota**: qualunque riga non riconosciuta viene aperta **anche in zona nota**, con le pause
piene. La zona non decide se aprire, decide solo quanto si corre fra un'apertura e l'altra.

La distribuzione e' **lognormale troncata per riestrazione**, mai clampata: il clamp accumula
la coda sui bound (misurato sul motore API: 45% dei delay su due valori fissi) ed e' una firma
peggiore di un delay costante. Si riusa la *formula*, non il codice: i motori restano separati.

**Le soste non sono attese a vuoto**: la sosta E' il momento in cui si rilegge la
conversazione, e ci si sofferma **dove c'e' stato uno scambio vero**, non su un monologo mai
risposto. E' credibile e costa zero, perche' quel dato va comunque letto.

**Click**: sempre `human_click`, mai `element.click()` diretto (i probe usano il click diretto
perche' sono misure, non comportamento).

**Scorrimento**: non solo verso il basso — risalite occasionali, come il dito che scappa.

**Sessione**: 30-55 minuti, poi stacco lungo gestito dal governo esistente.

**Tetto giornaliero**: configurabile per campagna. Default proposto **1500 chat/giorno**.

Il valore va scelto insieme alla durata della sessione, altrimenti i due parametri si
contraddicono — errore gia' commesso una volta in questo documento (tetto 800 contro una
sessione che ne apriva 900).

Col ritmo differenziato per zona, una sessione da 45 minuti apre fra ~290 (tutta zona nuova,
pause piene) e ~900 chat (in gran parte attraversamento rapido). 1500 copre due sessioni piene
anche nel caso veloce, quindi il tetto non scatta mai a sorpresa a meta' lavoro.

### Parametri e valori proposti

Tutti configurabili, nessuno cablato nel codice. I valori sono un punto di partenza motivato,
non una misura: vanno tarati sull'uso.

| Parametro | Default | Motivo |
|---|---|---|
| noti consecutivi → scorrimento | 10 | scelto da Tommaso |
| sconosciuti su finestra → raccolta | 3 su 10 | evita il rimbalzo fra modalita' in zone miste |
| pausa normale | 1-4 s | ritmo di chi passa in rassegna |
| pausa sosta / probabilita' | 10-30 s / 0.10 | coincide con la rilettura della conversazione |
| pausa stacco / probabilita' | 2-5 min / 0.02 | la distrazione vera, rara |
| durata sessione | 30-55 min | scelto da Tommaso |
| tetto giornaliero | 1500 chat | una sessione piena (~900) + una parziale; 800 verrebbe sfondato dalla prima |
| avviso "nulla di nuovo" | sessione chiusa con 0 contatti nuovi | non e' uno stop: e' l'esito da comunicare (vedi sotto) |
| passo di scorrimento | 0.6-0.8 dell'altezza visibile, randomizzato | sopra il buffer renderizzato si perdono righe **in silenzio** |
| attese prima di dichiarare la fine | 1, 2, 4, 8, 16 s (tetto ~60 s) | la lentezza normale non deve mai essere scambiata per fine lista |

## Due trappole silenziose, e le loro verifiche di identita'

Entrambe producono **dati sbagliati senza sollevare nessun errore**. Sono la classe di guasto
peggiore: il sistema riferisce successo mentre fa danno.

### Il click atterra sulla riga sbagliata

`human_input.human_click` clicca su **coordinate**, non su un elemento
(`human_input.py:99-107`): calcola il riquadro, muove il mouse in 5-15 passi, attende 50-150 ms,
poi `page.mouse.click(x, y)`. Fra il calcolo e il click passa una finestra reale.

```
t0  bounding_box() della riga "Elena" a y=430
t1  arriva un DM da una chat piu' in basso -> quella salta in cima
    -> tutto cio' che sta fra cima e cursore scende di una posizione
t2  mouse.click(x, 430) -> a y=430 ora c'e' la riga PRECEDENTE
    -> si apre la chat sbagliata. mouse.click riesce sempre: nessun errore.
```
Leggeremmo dati corretti **della persona sbagliata**, e la riga che volevamo aprire verrebbe
considerata fatta. `human_click` e' nato per le pagine profilo, che stanno ferme; l'inbox si
riordina da sola. La spec lo elencava fra i riusi "senza modificarli" senza notare che il
contesto d'uso ne cambia i presupposti.

**Verifica obbligatoria**: dopo il click si confronta il nome nell'header del thread aperto con
il nome della riga che si intendeva aprire. Se non combaciano: non si salva niente, non si
avanza oltre quella riga, si riprova.

**Vincolo correlato**: mai riusare un riferimento a una riga attraverso una pausa. Fra due
aperture puo' passare una sosta di 10-30 s o uno stacco di 2-5 minuti, e la lista e'
virtualizzata: la riga puo' essere uscita dal DOM, o peggio le coordinate restano valide ma
puntano ad altro. La riga va **ri-risolta per contenuto immediatamente prima del click**.

### Lo username cambia proprietario

Il riconoscimento usa il **nome**, la targa e l'arricchimento usano lo **username**. I due
cambiano in momenti diversi, e da qui nasce il caso peggiore dell'intero progetto:

```
t0  raccolto:  nome "Elena Rocchetti", username @lerocchette, targa -H("lerocchette")
t1  la persona rinomina: @lerocchette -> @elenarocchette (il NOME resta lo stesso)
t2  sessione dopo: il nome combacia -> riga SALTATA -> il rename non viene mai rilevato
t3  Instagram LIBERA @lerocchette; un terzo lo prende
t4  arricchimento: browser_bio.py:489 usa follower.username -> visita /lerocchette/
    -> trova un profilo VALIDO, di un'ALTRA persona
    -> ne salva bio, contatti e pk sulla scheda di Elena
t5  invio: campaign_orchestrator.py:1462 -> send_dm(username=follower.username)
    -> il DM va alla persona sbagliata
```
Nessun passaggio solleva un errore: a t4 la guardia esistente confronta lo username restituito
con quello richiesto (`browser_bio.py:261-262`) e **combacia**, perche' il profilo esiste
davvero. Il motore API non ha questo problema: interroga per pk, che i rename non toccano.

**Verifica obbligatoria**: se un contatto ha gia' una targa **vera** e l'arricchimento
restituisce un pk **diverso**, lo username ha cambiato proprietario. Non si scrive niente, si
marca il contatto come da rivedere e si segnala. E' un controllo di due righe che chiude un
caso da danno reputazionale.

Per i contatti con targa ancora provvisoria non esiste riferimento: il primo arricchimento
fissa la targa vera, e da li' in poi il controllo protegge.

## Presupposti e interazioni emerse in revisione

**Un solo account inbox.** `api/campaigns.py:73-77` (`inbox_account_count_ok`) impone
`active_count == 1` per `dm_threads`. Vale anche col motore browser: e' un presupposto, non
qualcosa da aggiungere. Il test adversarial "due sessioni sullo stesso account" ci si appoggia.

**Cambio motore a caldo.** `scrape_bios.py:188-199` ha un auto-defer (`ENGINE_SWITCH_DEFER`)
per quando `bio_engine` cambia mentre il job sta girando: senza, il lock arq in-progress resta
appeso e blocca anche un nuovo avvio. Il motore inbox browser ha lo stesso problema in forma
**peggiore** — sessioni da 30-55 minuti contro una pagina API. Serve il gemello.

Nota: la guardia di `campaigns.py:322-326` protegge solo il cambio a campagna ferma
(`draft/ready/paused/error`), ma una campagna in listing passa per `listing_break`, che **non**
e' fra gli stati bloccati. Da coprire.

**Listing e arricchimento non devono sovrapporsi.** `browser_bio.py:918-935` conta i `pending`
residui per decidere se il pool e' smaltito e la Fase Bio puo' chiudersi. Se il listing browser
continua a inserire follower mentre la Fase Bio browser gira, quel conteggio **non torna mai a
zero** e la Fase Bio non si chiude. Vanno serializzate: prima il listing, poi l'arricchimento.
(Il lock di profilo di `BrowserSession` gia' impedisce due browser sullo stesso account, il che
copre il caso in pratica — ma la dipendenza va dichiarata, non lasciata implicita.)

**Nessun messaggio AI senza arricchimento.** `ai_personalizer.py:415,478` selezionano solo
follower in `bio_scraped`. Con `enrichment_level='none'` restano `pending` e nessun messaggio
AI viene mai generato: e' un'altra ragione per cui il vincolo sull'arricchimento e' corretto,
non una limitazione arbitraria.

## Errori

| Situazione | Comportamento |
|---|---|
| Challenge / blocco IG | ferma tutto, isola l'account, avvisa su Telegram |
| Interstiziale IG | riusa il rilevatore di `browser_bio` (`__blocked`), ferma il batch |
| Chat non apribile / username non trovato | salta registrando il motivo, **mai** salvare a meta' |
| Lista che smette di caricare | vedi "Fondo, lento o piantato" — mai una decisione al primo dubbio |
| Sessione web non loggata | errore esplicito, **nessun** tentativo di login automatico |
| Kill-switch globale | stop immediato, campagna in `paused` |
| Interruzione improvvisa | stato campagna coerente; la sessione dopo riparte dalla cima |

## Test

### Funzionali
- riconoscimento: zona a macchia di leopardo — **il nuovo in mezzo non deve sparire**
- passaggio a ritmo rapido dopo 10 riconosciuti consecutivi, e ritorno al ritmo pieno a 3-su-10
- azzeramento del contatore al primo non riconosciuto
- **in ritmo rapido una riga non riconosciuta viene comunque aperta** (regola fondante)
- contatto riconosciuto: saltato **senza scrivere nulla** (nessun aggiornamento al buio)
- dedup in scrittura per `(campaign_id, username)`: un contatto gia' presente viene aggiornato,
  mai duplicato, qualunque sia la sua targa
- perimetro: i gruppi vengono scartati
- normalizzazione: maiuscole, spazi doppi, emoji, spazi invisibili
- segnaposto ignorati in italiano **e** in inglese
- nome troncato
- **stringhe localizzate**: la stessa lista, letta con interfaccia in italiano e in inglese,
  deve produrre la **stessa** classificazione di "chi ha scritto per ultimo". E' il test che
  intercetta il fallimento piu' insidioso: nessun errore, solo dati sbagliati
- interfaccia in una lingua non prevista → si rifiuta di classificare invece di indovinare
- archivio vuoto alla prima esecuzione
- i tre criteri di stop, uno per uno (fondo, tetto contatti, tempo sessione)
- sessione chiusa con zero contatti nuovi → l'avviso "nulla di nuovo" viene emesso
- sessione con almeno un contatto nuovo → l'avviso **non** viene emesso

### Sulla targa
- determinismo: stesso username → sempre lo stesso numero
- **impossibilita' di collisione** con pk reali: va dimostrata, non sperata
- targa vera che arriva ed e' gia' presente nella campagna → fusione, non `IntegrityError`
- sostituzione mentre la scheda e' bloccata da un altro account
- `upsert_lead` riceve la targa vera, mai la provvisoria
- **`GlobalContact` rifiuta una targa negativa**: il presidio difensivo scatta anche se il
  vincolo di configurazione viene aggirato
- **campagna browser con `enrichment_level='none'` viene RIFIUTATA** dall'API (il buco trovato
  in revisione: senza questo test rientra in silenzio)
- **campagna browser con `bio_engine='api'` viene RIFIUTATA**
- export CSV: nessuna targa provvisoria nella prima colonna
- lo username **cambia** fra due sessioni (rename del profilo): la targa derivata cambia →
  verificare che non nasca un doppione e che il dedup per username lo intercetti

### Sul comportamento
- distribuzione delle pause: **nessuna pila sui bordi** (stesso test scritto per il motore API,
  che ha smascherato il difetto reale)
- coefficiente di variazione sopra soglia
- si usa `human_click`, mai il click diretto

### Su virtualizzazione e fine lista
- **nessun nome saltato fra due passi di scorrimento consecutivi** (il fallimento qui e' silenzioso:
  e' il test piu' importante del gruppo)
- un passo piu' grande del buffer **deve** far fallire il test: e' la prova che il test discrimina
- altezza ferma + in fondo + zero richieste fallite verso gli endpoint inbox → dichiarato
  **fine lista**
- altezza ferma + richieste fallite verso gli endpoint inbox → dichiarato **piantato**,
  campagna NON completata
- altezza che cresce dopo 8 s di attesa → si riprende, non si dichiara nulla (il caso "lento")
- altezza ferma ma non in fondo → anomalia segnalata, non completamento silenzioso

### Sulle due trappole silenziose
- **click che atterra sulla riga sbagliata**: si simula un riordino fra il calcolo del riquadro
  e il click → la verifica post-apertura deve accorgersene e **non salvare niente**
- riferimento a una riga riusato dopo una pausa lunga → deve essere ri-risolto, non riusato
- **username riassegnato**: pk restituito diverso da una targa vera gia' nota → si ferma, non
  scrive, segnala. E' il test che impedisce di mandare un DM a un estraneo
- profilo semplicemente sparito (404) → skip normale, **non** confuso col caso precedente

### Sul ritmo per zona
- in zona nota, una riga non riconosciuta viene **comunque aperta** (regola fondante)
- la sequenza "10 righe note in cima" (i DM appena inviati) **non** deve azzerare la raccolta:
  e' lo scenario che affossava il disegno precedente
- zona a tasso di nuovi del 10%: **tutti** vengono raccolti, nessuno saltato

### Sulle conferme di lettura
- una chat con messaggi non letti **non** viene aperta
- il riconoscimento "non letta" funziona; se il segnale manca, ci si ferma invece di indovinare

### Sulla tenuta / adversarial
- interruzione a meta' sessione e ripresa nel punto giusto
- kill-switch globale durante il lavoro
- due sessioni sullo stesso account (lock cross-processo)
- tetto giornaliero raggiunto a meta' sessione
- inbox vuota; inbox con una sola chat
- challenge a meta' listing → account isolato, campagna coerente

### Regressione sul motore API
- con `inbox_engine='api'` il percorso resta **identico**: stesse pause, stesso stop, stesso
  contratto di ritorno. I test esistenti di `scrape_inbox` devono restare verdi senza modifiche.

### Prova del nove
Per ogni test: reintrodurre il difetto e verificare che il test torni **rosso davvero**. Il
metodo ha gia' pagato due volte in questa sessione — ha confermato il difetto delle pause e ha
smascherato un test che non verificava quanto dichiarava.

## Fuori scope

- alternanza automatica fra i due motori (Tommaso: "ci penseremo dopo")
- scheda Generali e Richieste
- invio di messaggi di follow-up durante il listing (Tommaso: "al momento la eviterei")
- arricchimento via API sui contatti raccolti dal browser
- correzione degli username con la chiocciola in DB (`@michele.carozza`): Tommaso ci pensa lui
