# AUDIT REPORT — BOT OUTBOUND

Report completo: sicurezza, privacy, policy Instagram, rischi, bug, funzionalità, e precauzioni operative.

Data audit: 2026-04-14

---

## Indice

1. [Sicurezza e Privacy](#1-sicurezza-e-privacy)
2. [Funzionamento Offline](#2-funzionamento-offline)
3. [Bug e Problemi Funzionali](#3-bug-e-problemi-funzionali)
4. [Policy Instagram e Rischi Legali](#4-policy-instagram-e-rischi-legali)
5. [Matrice Rischi](#5-matrice-rischi)
6. [Vettori di Rilevamento Instagram](#6-vettori-di-rilevamento-instagram)
7. [Efficacia Anti-Detection](#7-efficacia-anti-detection)
8. [IP, Proxy e Precauzioni di Rete](#8-ip-proxy-e-precauzioni-di-rete)
9. [Linee Guida per l'Uso Sicuro](#9-linee-guida-per-luso-sicuro)
10. [Tutela del PC e degli Account](#10-tutela-del-pc-e-degli-account)
11. [Riepilogo e Priorita](#11-riepilogo-e-priorita)

---

## 1. Sicurezza e Privacy

### CRITICO — Nessuna autenticazione sulle API

Tutte le API del backend (porta 8000) sono completamente aperte. Chiunque sulla rete puo:
- Leggere tutti gli account Instagram e i relativi metadati
- Leggere tutti i follower scrapati (nomi, bio, username) — dati personali
- Leggere tutti i messaggi generati/inviati
- Avviare/fermare campagne
- Eliminare account e campagne

**Rischio**: Se il PC e connesso a una rete pubblica/condivisa, chiunque puo accedere a `http://tuo-ip:8000/api/accounts` e vedere tutto.

**Mitigazione attuale**: Il backend bind su `127.0.0.1` (localhost) di default con uvicorn, quindi e accessibile solo dal tuo PC. Questo e sufficiente per un uso locale, ma va verificato che `start.bat` non usi `--host 0.0.0.0`.

**Stato**: Sicuro per uso locale. NON esporre mai la porta 8000 su internet.

---

### ALTO — Password Instagram in chiaro durante il transito HTTP

Le password vengono inviate dal browser al backend via HTTP (non HTTPS). Su localhost questo non e un problema, ma attenzione a non accedere al frontend da un altro dispositivo sulla rete.

---

### ALTO — Session data non crittografata nel DB

Le password Instagram sono crittografate con Fernet (bene), ma il `session_data` (token, cookie di sessione Instagram) e salvato come JSON in chiaro nel database SQLite. Chi ha accesso al file `bot.db` puo impersonare qualsiasi account Instagram loggato.

**File**: `backend/data/bot.db`

**Mitigazione**: Non condividere mai il file `bot.db`. Proteggi la cartella `data/` con permessi di accesso.

---

### MEDIO — Profili browser senza protezione filesystem

I profili Chromium in `data/browser_profiles/` contengono cookie, localStorage e token di sessione Instagram. Sono creati con i permessi di default di Windows.

**Mitigazione**: Non condividere la cartella `data/browser_profiles/`.

---

### MEDIO — SECRET_KEY senza validazione all'avvio

Se la variabile `SECRET_KEY` nel `.env` e vuota o invalida, il backend parte comunque e crasha solo quando tenta di crittografare una password (al primo account aggiunto). Non c'e un check all'avvio.

**Stato attuale**: La tua `.env` ha una chiave Fernet valida generata automaticamente. Non e un problema immediato, ma e fragile.

---

### BASSO — Swagger/OpenAPI docs esposti

`http://localhost:8000/docs` mostra l'intera API con tutti gli endpoint. Non e un problema su localhost, ma non va esposto in rete.

---

## 2. Funzionamento Offline

### Il sistema funziona al 100% in locale?

**SI**, con queste dipendenze:

| Componente | Dove gira | Connessione internet necessaria? |
|---|---|---|
| Backend FastAPI | localhost:8000 | NO |
| Frontend Next.js | localhost:3000 | NO (dopo `npm install`) |
| Redis/Memurai | localhost:6379 | NO |
| Ollama + LLM | localhost:11434 | NO (dopo `ollama pull`) |
| SQLite database | File locale | NO |

**Connessione internet necessaria SOLO per**:
- Login su Instagram (instagrapi -> API Instagram)
- Scraping follower (API Instagram)
- Invio DM (browser Patchright -> instagram.com)

**Conclusione**: Tutta l'infrastruttura gira in locale. Internet serve solo per comunicare con Instagram. Se Instagram e down o bloccato, tutto il resto continua a funzionare.

---

## 3. Bug e Problemi Funzionali

### FIX APPLICATO — Errore hydration `<button>` dentro `<button>`

**File**: `frontend/app/accounts/page.tsx`
**Problema**: `DialogTrigger` (che renderizza un `<button>`) conteneva un componente `Button` (altro `<button>`). HTML invalido -> errore hydration React.
**Fix**: Uso del `render` prop di Base UI per evitare il nesting.

---

### PROBLEMI MINORI VERIFICATI

| # | File | Problema | Gravita |
|---|---|---|---|
| 1 | `scraper.py:186` | `total % 200 == 0` raramente trigga se i batch non sono esattamente 200 | Bassa |
| 2 | `campaign_orchestrator.py:192` | `_get_next_follower` non include follower con status `failed` per il retry — la feature retry dal frontend non ha effetto | Media |
| 3 | `human_behavior.py:34` | Active hours usa `datetime.utcnow()` ma i parametri sono pensati per orario locale — in Italia (UTC+2) c'e uno shift di 2 ore | Media |
| 4 | `campaign_orchestrator.py:48` | Una singola sessione DB per tutta la durata della campagna (potenzialmente ore) — rischio stale reads | Media |
| 5 | `ai_personalizer.py:106` | Check `{` e `}` nel messaggio troppo aggressivo — potrebbe rigettare messaggi validi con parentesi graffe | Bassa |
| 6 | `context_manager.py:76` | Canvas noise solo sul canale rosso e valore costante — fingerprint difesa debole | Media |
| 7 | `fingerprint.py` | `hardware_concurrency` e `device_memory` generati ma mai iniettati nel browser | Media |

### FUNZIONALITA VERIFICATE COME CORRETTE

- Login instagrapi con session restore (username + password passati correttamente)
- `daily_reset` cron registrato e funzionante
- `warmup_day` incrementato solo nel cron giornaliero, non per ogni messaggio
- Bio fetching implementato con `user_info()` per ogni follower
- Deduplicazione globale cross-campagna
- Import corretti, nessun `__import__` hack

---

## 4. Policy Instagram e Rischi Legali

### Violazioni specifiche dei ToS di Instagram

1. **Raccolta dati automatizzata** (Meta Platform Terms, Sezione 3.2) — Lo scraping dei follower tramite instagrapi viola direttamente i termini.

2. **Creazione account automatizzata** (Instagram ToS) — La rotazione di piu account per automazione e vietata.

3. **Spam** (Community Guidelines) — L'invio massivo di DM non richiesti a persone che non hanno dato consenso e classificato come spam.

4. **Automazione non autorizzata** — Sia instagrapi (reverse-engineering API privata) che Patchright (browser automation) sono metodi non autorizzati.

### Implicazioni legali

| Area | Rischio | Probabilita |
|---|---|---|
| **GDPR (UE)** | Raccolta e trattamento di dati personali (bio, nome, username) senza consenso e senza base giuridica. Sanzioni fino al 4% del fatturato o 20M EUR. | Bassa per operatori piccoli, ma un reclamo e sufficiente |
| **CFAA (USA) / Leggi anti-intrusione** | L'uso di API private reverse-engineered puo configurare accesso non autorizzato | Molto bassa per operatori non-US |
| **Azione legale Meta** | Meta ha perseguito legalmente servizi di scraping (es. Octopus Data, Massroot8) | Bassa per piccoli operatori, catastrofica se accade |

### Differenza API vs Browser

- **instagrapi** (API privata): piu rischioso legalmente perche reverse-engineera l'app mobile Instagram. Piu efficiente per lo scraping.
- **Patchright** (browser): meno rischioso perche interagisce con l'interfaccia web pubblica. Piu sicuro per l'invio DM.

---

## 5. Matrice Rischi

| # | Rischio | Probabilita (1-5) | Impatto (1-5) | Score | Note |
|---|---|---|---|---|---|
| 1 | **Ban account permanente** | 5 | 3 | **15** | Quasi certo nel tempo. Account sono sostituibili (a un costo) |
| 2 | **Ban IP (rete bloccata)** | 3 | 5 | **15** | Senza proxy, Instagram banna l'IP dopo aver rilevato cluster di bot |
| 3 | **Action block (temporaneo)** | 5 | 2 | **10** | Blocchi 24-48h molto comuni. Il cooldown li gestisce |
| 4 | **Phone verification loop** | 4 | 3 | **12** | Instagram forza verifica SMS quando sospetta automazione |
| 5 | **Shadow ban** | 4 | 2 | **8** | DM filtrati in "Richieste messaggi" o nascosti. Impossibile da rilevare |
| 6 | **Azione legale Meta** | 2 | 5 | **10** | Bassa probabilita per piccoli operatori, impatto catastrofico |
| 7 | **GDPR / Privacy** | 2 | 5 | **10** | Un reclamo a un DPA e a basso sforzo per chi riceve il DM |
| 8 | **Blocco da parte del destinatario** | 4 | 1 | **4** | Previsto e a basso impatto |
| 9 | **Rilevamento pattern automazione** | 4 | 4 | **16** | Piu alto rischio — una detection puo cascadare su tutti gli account |
| 10 | **Segnalazioni spam massive** | 3 | 4 | **12** | Se 5-10% dei destinatari segnala, gli account vengono azionati rapidamente |

**Top 3 rischi**: Rilevamento automazione (16), Ban account (15), Ban IP (15)

---

## 6. Vettori di Rilevamento Instagram

### Come Instagram puo rilevare il bot

**1. Pattern chiamate API (instagrapi)**
- Endpoint chiamati in rapida successione (solo follower list, nessuna attivita organica)
- Device fingerprint statico del client instagrapi
- Mancanza di traffico organico (nessun feed, storie, explore)

**2. Fingerprinting browser (Patchright)**
- Canvas noise implementato ma debole (solo canale rosso, valore costante)
- `hardware_concurrency` e `device_memory` mai iniettati nel browser
- Solo 5 viewport e 5 user-agent disponibili -> collisioni con >5 account
- User-agent con versioni Chrome 121-124 (obsolete nel 2026)
- Mismatch locale/timezone vs IP (es. `en-US` + `America/New_York` da IP italiano)

**3. Pattern invio messaggi**
- Sequenza ripetitiva: visita profilo -> scroll 5-30s -> click Messaggio -> digita -> invia
- **ZERO attivita organica** — il bot fa SOLO DM, mai like, storie, explore
- Struttura messaggi simile nonostante personalizzazione AI
- Invio al 100% dei follower sequenzialmente (un umano non lo fa mai)

**4. Anomalie comportamentali account**
- Warm-up lineare e prevedibile (5 -> 12 -> 20 DM/giorno = staircase rilevabile)
- Nessun messaggio in entrata (solo invio, mai lettura risposte)
- Account senza post, follower, storie = profilo bot

**5. Pattern di rete**
- **IP singolo per tutti gli account** — segnale fortissimo
- **Nessun proxy implementato** nel browser layer
- Connessioni multiple simultanee dalla stessa IP (`MAX_CONCURRENT_BROWSERS=3`)

---

## 7. Efficacia Anti-Detection

| Misura | Efficacia | Note |
|---|---|---|
| Timing log-normale | 6/10 | Meglio di delay fissi, ma sigma=0.4 e troppo stretto. Mancano le pause lunghe tipiche degli umani |
| Limiti sessione (10-20 msg) | 5/10 | Range ragionevole ma troppo stretto. Un umano varia molto di piu |
| Finestra oraria (8-23) | 4/10 | Troppo ampia (15h) e identica per tutti gli account |
| Profili browser persistenti | **8/10** | Punto di forza. Cookie e sessione persistenti = utente che ritorna |
| Fingerprint per account | 3/10 | Implementazione debole. Pool troppo piccoli, hardware props non iniettati |
| Typing umano | 7/10 | Buona implementazione con varianza. Mancano typo e correzioni |
| Warm-up graduale | 6/10 | Concetto giusto, esecuzione troppo prevedibile |
| Rotazione account | 5/10 | Distribuisce il carico ma crea distribuzione troppo uniforme |
| Deduplicazione globale | **8/10** | Importante e ben implementata |

**Score complessivo: ~5.8/10** — Fondamenta solide ma lacune significative in fingerprinting, attivita organica e protezione di rete.

---

## 8. IP, Proxy e Precauzioni di Rete

### Situazione attuale: NESSUN PROXY

Il sistema attualmente usa il tuo IP reale per tutte le connessioni. Questo e il **singolo rischio piu grande**.

### Perche il proxy e essenziale

| Scenario | Senza proxy | Con proxy |
|---|---|---|
| 3 account dallo stesso IP | Instagram li collega e banna tutti insieme | Ogni account appare da un luogo diverso |
| Ban di un account | L'IP viene flaggato, gli altri account sono a rischio | Solo quell'account viene colpito |
| Scraping intensivo | IP limitato dopo poche centinaia di richieste | Distribuzione del carico su piu IP |

### Tipi di proxy

| Tipo | Costo | Efficacia | Note |
|---|---|---|---|
| **Residenziali** | 5-15 USD/GB | Alta | IP reali di utenti ISP. Difficili da rilevare |
| **ISP/Static** | 2-5 USD/IP/mese | Media-Alta | IP di datacenter assegnati a ISP. Buon compromesso |
| **Datacenter** | 1 USD/IP/mese | Bassa | IP chiaramente di server. Instagram li rileva subito |
| **Mobile** | 15-30 USD/GB | Molto Alta | IP di rete mobile. Quasi impossibili da distinguere |

### Raccomandazioni proxy

1. **Minimo**: 1 proxy residenziale dedicato per ogni account Instagram
2. **Ideale**: Proxy residenziale con geo-matching (se l'account finge di essere a Roma, l'IP deve essere italiano)
3. **Mai**: Usare proxy datacenter (es. DigitalOcean, AWS) — Instagram li blocca immediatamente
4. **Mai**: Condividere lo stesso proxy tra piu account

### Come implementare (futuro)

Il campo `proxy` esiste gia nel modello Account (`account.proxy`). Il scraper lo usa:
```python
if account.proxy:
    client.set_proxy(account.proxy)
```

Ma il browser Patchright **non implementa il proxy**. Per una protezione completa serve aggiungere il proxy anche al browser context.

### Precauzioni senza proxy (uso attuale)

Se usi il bot SENZA proxy (come adesso):

1. **Massimo 1 account** — non usare mai piu account dallo stesso IP senza proxy
2. **Volume bassissimo** — max 10-15 DM/giorno
3. **Mai dallo stesso IP che usi per il tuo account personale** — se l'IP viene bannato, perdi anche l'accesso personale
4. **VPN NON e un proxy** — Le VPN consumer hanno IP condivisi e gia flaggati da Instagram
5. **Non fare scraping e invio dallo stesso IP** — Se possibile, fai scraping da un IP e invio da un altro

---

## 9. Linee Guida per l'Uso Sicuro

### Configurazione ottimale (.env)

```env
# TIMING — piu lento = piu sicuro
MIN_DELAY_SECONDS=180
MAX_DELAY_SECONDS=900
SESSION_MIN_MESSAGES=5
SESSION_MAX_MESSAGES=12
SESSION_BREAK_MIN_MINUTES=45
SESSION_BREAK_MAX_MINUTES=120

# ORARI — finestra piu stretta
ACTIVE_HOURS_START=9
ACTIVE_HOURS_END=21

# ACCOUNT — limiti conservativi
DEFAULT_DAILY_LIMIT=15
WARMUP_ENABLED=true
MAX_CONCURRENT_BROWSERS=1

# BROWSER
HEADLESS=false
```

### Regole d'oro

1. **Inizia LENTO** — I primi 7 giorni, non superare mai 5 DM/giorno per account
2. **Usa solo account secondari** — Mai il tuo account principale
3. **Un account = un proxy** — Se possibile, ogni account deve avere il suo IP
4. **Monitora i cooldown** — Se un account va in cooldown spesso, riduci `DEFAULT_DAILY_LIMIT`
5. **Non inviare DM commerciali diretti** — Punta a iniziare una conversazione, non a vendere
6. **Varia i template** — Usa template diversi per target diversi. Lo stesso template su migliaia di utenti e un pattern rilevabile
7. **HEADLESS=false per debug** — Osserva sempre le prime sessioni per verificare che il browser si comporti correttamente
8. **Controlla i log** — Visita `/messages` regolarmente per monitorare errori
9. **Non superare mai 30 DM/giorno** per account — anche se Instagram teoricamente permette 50-100, il rischio aumenta esponenzialmente
10. **Giorni di riposo** — Non inviare DM tutti i giorni. 1-2 giorni a settimana di pausa per account

### Warm-up ottimale consigliato

| Periodo | DM/giorno | Note |
|---|---|---|
| Giorni 1-5 | 2-3 | Solo testing |
| Giorni 6-14 | 5-8 | Inizio graduale |
| Giorni 15-30 | 10-12 | Volume moderato |
| Giorno 30+ | 15 max | Volume standard |

### Contenuto messaggi — Best practices

- **Brevi**: 2-3 frasi max. Messaggi lunghi = spam
- **Conversazionali**: Fai domande, non vendere
- **Variati**: Cambia template almeno ogni 500 destinatari
- **Pertinenti**: Usa il contesto AI per adattare i messaggi al target
- **No link**: Instagram penalizza i DM con link, specialmente da account nuovi
- **No emoji eccessive**: Max 1-2 emoji, come un umano reale

---

## 10. Tutela del PC e degli Account

### Rischi per il tuo PC

| Rischio | Probabilita | Mitigazione |
|---|---|---|
| **Malware via browser Patchright** | Molto bassa | Patchright scarica Chromium ufficiale. `--no-sandbox` e usato ma il rischio e minimo su pagine note (instagram.com) |
| **Consumo risorse eccessivo** | Media | Ogni browser consuma ~200-500MB RAM. Con `MAX_CONCURRENT_BROWSERS=3` = ~1.5GB extra. Riduci a 1 se hai poca RAM |
| **File temporanei che crescono** | Bassa | I profili browser in `data/browser_profiles/` crescono nel tempo. Puoi pulire quelli di account eliminati |
| **Processo bloccato** | Media | Se il worker ARQ crasha, i task restano in coda Redis. Riavvia con `start.bat` |
| **SQLite lock** | Bassa | WAL mode riduce il rischio. Se il DB si blocca, ferma tutti i processi e riavvia |

### Rischi per i tuoi account Instagram

| Rischio | Probabilita | Cosa fare |
|---|---|---|
| **Ban permanente dell'account usato per DM** | Alta (5/5 nel tempo) | Usa SOLO account dedicati/sacrificabili |
| **Ban dell'account usato per scraping** | Media (3/5) | Lo scraping e meno rischioso dei DM, ma un volume alto lo espone |
| **Ban del tuo account personale** | Bassa SE usi un IP diverso | MAI usare lo stesso IP del bot per il tuo account personale |
| **Ban IP che impatta altri servizi** | Media senza proxy | Un IP bannato da Instagram potrebbe influenzare tutti i dispositivi sulla stessa rete |

### Precauzioni minime

1. **Backup del database** — Copia `backend/data/bot.db` periodicamente. Contiene tutti i dati della campagna
2. **Non condividere la cartella `data/`** — Contiene credenziali Instagram (crittografate ma con chiave nel .env)
3. **Non pushare `.env` su git** — Il `.gitignore` lo esclude, ma verifica sempre
4. **Monitora il consumo RAM/CPU** — Task Manager -> cerca processi `chromium` e `python`
5. **Firewall** — Assicurati che le porte 8000, 3000, 6379, 11434 NON siano esposte su internet

---

## 11. Riepilogo e Priorita

### Da fare PRIMA di ogni campagna reale

| # | Azione | Priorita | Difficolta |
|---|---|---|---|
| 1 | Usa `HEADLESS=false` e osserva la prima sessione | **Critica** | Facile |
| 2 | Imposta `DEFAULT_DAILY_LIMIT=10-15` | **Critica** | Facile |
| 3 | Usa `MAX_CONCURRENT_BROWSERS=1` senza proxy | **Critica** | Facile |
| 4 | Verifica che le porte non siano esposte in rete | **Alta** | Facile |
| 5 | Non usare il tuo account Instagram personale | **Critica** | - |
| 6 | Valuta l'acquisto di proxy residenziali | **Alta** | Costo |

### Da implementare in futuro (Fase 6+)

| # | Feature | Impatto sulla sicurezza |
|---|---|---|
| 1 | Proxy nel browser Patchright | Riduce rischio ban IP da 15 a ~5 |
| 2 | Attivita organica (like, storie, explore) tra i DM | Riduce rilevamento automazione da 16 a ~8 |
| 3 | Autenticazione API (anche solo API key) | Protegge i dati se il PC e in rete |
| 4 | Crittografia `session_data` nel DB | Protegge i token Instagram |
| 5 | Pool fingerprint piu ampio (20+ viewport, UA aggiornati) | Migliora anti-fingerprinting |
| 6 | Active hours per-account con timezone locale | Rende il comportamento piu realistico |
| 7 | Giorni di riposo randomizzati per account | Riduce pattern detection |

---

*Report generato il 2026-04-14. Aggiornare dopo ogni modifica significativa al sistema.*
