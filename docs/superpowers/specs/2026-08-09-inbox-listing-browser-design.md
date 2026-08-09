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

**Conseguenza sulla velocita'**: ~1200 chat/ora con attesa prudente + pausa umana, quindi
~2.5h per 3000 contatti. Una stima iniziale di 8-25h era sbagliata perche' applicava il pacing
delle chiamate API (10-60s) all'apertura di una chat, gesto di natura diversa.

**Conseguenza sui dati**: il canale browser non conosce il pk. Vedi "Targa provvisoria".

## Decisioni prese (16 domande a Tommaso)

1. **I due motori convivono**, scelti per campagna via `inbox_engine`. L'alternanza automatica
   e' esplicitamente fuori scope.
2. **Perimetro**: solo conversazioni 1-a-1. Niente scheda Generali, niente Richieste.
3. **Dati raccolti**: username, nome visualizzato, data ultimo messaggio, chi ha scritto per
   ultimo, **testo integrale** dell'ultimo messaggio, provenienza del dato.
4. **Ritmo**: equilibrio, ~800-1200 chat/ora.
5. **Strategia di ripresa**: automatica, sempre dalla cima, due modalita' che si alternano da
   sole. Nessun segnaposto di posizione.
6. **Soglia per entrare in scorrimento veloce**: 10 nomi gia' noti consecutivi.
7. **Contatto gia' noto incontrato durante la raccolta**: si salta senza riaprire, ma si
   aggiornano data e ultimo messaggio.
8. **Criteri di stop**: tutti e quattro — fondo lista, tetto contatti (`list_target`),
   troppo gia'-visto, tempo massimo di sessione. Valori proposti nel dettaglio piu' sotto.
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

Casi da gestire nella sostituzione, tutti sotto test:
- la targa vera risulta **gia' presente** nella campagna → fondere, non duplicare
  (il `UniqueConstraint` altrimenti solleva);
- la sostituzione avviene mentre la scheda e' gia' bloccata dal processo di arricchimento
  (`locked_by_account_id`), quindi nessun altro puo' infilarsi;
- `upsert_lead` deve ricevere la targa **vera**, mai la provvisoria.

### Vincolo: arricchimento via browser obbligatorio

L'arricchimento via API interroga Instagram **con la targa** (`profile_lookup.py:49`,
`user_info_v1(pk)`): su un contatto appena raccolto cercherebbe una persona inesistente.

Quindi una campagna con `inbox_engine='browser'` deve avere `bio_engine='browser'`.

- **Backend**: l'API rifiuta `bio_engine='api'` su queste campagne, con messaggio esplicito.
- **Frontend**: il pulsante API va reso grigio e non cliccabile
  (`frontend/app/campaigns/[id]/page.tsx:1173-1186`), con spiegazione del perche'.

Il blocco serve su entrambi i lati: uno solo grafico si aggira.

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

```
   +------------------------ RACCOLTA -------------------------+
   |  apre la chat . legge username, nome, data,               |
   |  chi ha scritto per ultimo, testo                         |
   |  gia' in archivio? -> non duplica, aggiorna data+messaggio|
   |                                                           |
   |  contatore "gia' noti di fila"  --> 10 --------------+    |
   |  (si AZZERA appena ne trova uno nuovo)               |    |
   +------------------------------------------------------|---+
                            ^                             |
     3 sconosciuti          |                             v
     negli ultimi 10        |            +------- SCORRIMENTO --------+
                            +------------|  non apre niente           |
                                         |  legge solo i nomi         |
                                         +----------------------------+
```

**Perche' il contatore si azzera al primo nuovo**: con ~100 DM/giorno in uscita la lista si
rimescola di continuo e si formano zone a macchia di leopardo. Un contatore che non si azzera
farebbe passare il sistema in scorrimento veloce dentro una zona mista, **perdendo i nuovi in
mezzo**.

**Perche' 3-su-10 per tornare a raccogliere** (e non 1): un solo sconosciuto in una zona gia'
lavorata farebbe rimbalzare il sistema fra le due modalita', producendo un ritmo a scatti —
esattamente il tipo di firma che vogliamo evitare. E' un parametro, non una struttura.

**Ripresa fra sessioni**: sempre dalla cima. Nessun segnaposto di posizione salvato: la lista
si riordina a ogni DM in entrata, quindi "riparti dal contatto N" e' fragile per costruzione.
Il riorientamento costa poco perche' in scorrimento non si apre nulla.

**Contatti storici senza nome visualizzato**: le campagne esistenti hanno schede salvate senza
`full_name`. Decisione di Tommaso: **nessun codice dedicato**, in quei casi si rileggono le
chat. Il riconoscimento si popola da solo con l'uso.

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
3. **Richieste di rete fallite** — il browser le conosce (`page.on("requestfailed")`).
   E' il discrimine vero fra "fondo" e "piantato": un segnale di fatto, non una deduzione
   dai tempi.

**Procedura**: mai decidere al primo dubbio. Attese a pazienza crescente (1, 2, 4, 8, 16 s;
tetto complessivo ~60 s), rileggendo i tre segnali a ogni giro.

- altezza cresciuta → si riprende normalmente, non era ne' fondo ne' guasto
- attese esaurite + altezza ferma + in fondo allo scorrimento + **zero richieste fallite**
  → **fine lista**, campagna completata
- attese esaurite + **richieste fallite** nel frattempo → **piantato**: chiusura pulita,
  campagna NON completata, avviso a Tommaso
- attese esaurite + altezza ferma ma **non** in fondo → anomalia: chiusura pulita e avviso
  (non dovrebbe accadere; se accade e' un cambio di struttura della pagina)

## Comportamento

**Pause fra chat**, tre livelli a probabilita' calante:

| Livello | Durata | Frequenza |
|---|---|---|
| normale | 1-4 s | quasi sempre |
| sosta | 10-30 s | ~1 su 10 |
| stacco | 2-5 min | ~1 su 50 |

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

**Tetto giornaliero**: configurabile per campagna. Default proposto **800 chat/giorno** — sotto
la resa di una singola sessione piena (~1200/ora), cosi' il valore di partenza non e' mai
sorprendente. Da alzare consapevolmente, non un limite tecnico.

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
| tetto giornaliero | 800 chat | sotto una sessione piena |
| stop per gia'-visto | 40 chat aperte di fila senza un solo contatto nuovo | ~2 minuti di lavoro a vuoto: abbastanza per attraversare una zona mista, poco per sprecare una sessione |
| passo di scorrimento | 0.6-0.8 dell'altezza visibile, randomizzato | sopra il buffer renderizzato si perdono righe **in silenzio** |
| attese prima di dichiarare la fine | 1, 2, 4, 8, 16 s (tetto ~60 s) | la lentezza normale non deve mai essere scambiata per fine lista |

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
- transizione raccolta → scorrimento a 10 noti consecutivi, e ritorno a 3-su-10
- azzeramento del contatore al primo contatto nuovo
- contatto gia' noto: aggiorna data e messaggio senza duplicare
- perimetro: i gruppi vengono scartati
- normalizzazione: maiuscole, spazi doppi, emoji, spazi invisibili
- segnaposto ignorati in italiano **e** in inglese
- nome troncato
- archivio vuoto alla prima esecuzione
- i quattro criteri di stop, uno per uno

### Sulla targa
- determinismo: stesso username → sempre lo stesso numero
- **impossibilita' di collisione** con pk reali: va dimostrata, non sperata
- targa vera che arriva ed e' gia' presente nella campagna → fusione, non `IntegrityError`
- sostituzione mentre la scheda e' bloccata da un altro account
- `upsert_lead` riceve la targa vera, mai la provvisoria

### Sul comportamento
- distribuzione delle pause: **nessuna pila sui bordi** (stesso test scritto per il motore API,
  che ha smascherato il difetto reale)
- coefficiente di variazione sopra soglia
- si usa `human_click`, mai il click diretto

### Su virtualizzazione e fine lista
- **nessun nome saltato fra due passi di scorrimento consecutivi** (il fallimento qui e' silenzioso:
  e' il test piu' importante del gruppo)
- un passo piu' grande del buffer **deve** far fallire il test: e' la prova che il test discrimina
- altezza ferma + in fondo + zero richieste fallite → dichiarato **fine lista**
- altezza ferma + richieste fallite → dichiarato **piantato**, campagna NON completata
- altezza che cresce dopo 8 s di attesa → si riprende, non si dichiara nulla (il caso "lento")
- altezza ferma ma non in fondo → anomalia segnalata, non completamento silenzioso

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
