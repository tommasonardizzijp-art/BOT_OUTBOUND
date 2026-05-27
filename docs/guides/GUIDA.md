# GUIDA — BOT OUTBOUND

Guida completa all'utilizzo del bot per l'outreach su Instagram.

---

## Indice

1. [Primo avvio](#1-primo-avvio)
2. [Gestione Account Instagram](#2-gestione-account-instagram)
3. [Creare una Campagna](#3-creare-una-campagna)
4. [Template Messaggio — Variabili disponibili](#4-template-messaggio--variabili-disponibili)
5. [Contesto AI — Come usarlo bene](#5-contesto-ai--come-usarlo-bene)
6. [Esempi pratici di template](#6-esempi-pratici-di-template)
7. [Ciclo di vita di una campagna](#7-ciclo-di-vita-di-una-campagna)
8. [Parametri di configurazione (.env)](#8-parametri-di-configurazione-env)
9. [Warm-up account](#9-warm-up-account)
10. [Regole anti-ban](#10-regole-anti-ban)
11. [Troubleshooting](#11-troubleshooting)
12. [Proxy e diversificazione IP](#12-proxy-e-diversificazione-ip)
13. [Usare i telefoni Android come proxy](#13-usare-i-telefoni-android-come-proxy)
14. [Scala — raggiungere 100-150 DM al giorno](#14-scala--raggiungere-100-150-dm-al-giorno)

---

## 1. Primo avvio

### Prerequisiti
- **Memurai** in esecuzione (servizio Windows, parte automaticamente)
- **Ollama** in esecuzione con modello scaricato (`ollama pull llama3.2`)
- **Patchright** installato (`pip install patchright && patchright install chromium`)

### Avvio
```
Doppio click su start.bat
```

Si aprono 3 finestre:
- **Backend** — API FastAPI su `http://localhost:8000`
- **Worker** — ARQ che gestisce i task in background
- **Frontend** — Dashboard su `http://localhost:3000`

### Verifica sistema
Apri `http://localhost:8000/api/health` — deve rispondere:
```json
{"status":"ok","ollama":"ok","redis":"ok","database":"ok"}
```

---

## 2. Gestione Account Instagram

### Aggiungere un account
1. Vai su **Account** nel menu laterale
2. Clicca il bottone viola **Aggiungi account** (in alto a destra)
3. Inserisci:
   - **Username**: il nome utente Instagram (senza @)
   - **Password**: la password dell'account
   - **Proxy** (opzionale): indirizzo del proxy (es. `http://user:pass@host:port`)
   - **Limite DM/giorno**: quanti messaggi al giorno (default 20, consigliato non superare 30)
4. Clicca **Aggiungi account**

> L'account viene salvato ma **NON è ancora loggato**. Devi fare il login prima di poter usarlo.

---

### Login — Come funziona (IMPORTANTE)

Dopo aver aggiunto un account, vedrai due bottoni accanto:

| Bottone | Colore | Cosa fa | Rischio |
|---|---|---|---|
| **Login Browser** | 🟢 Verde | Apre un browser sul tuo PC, fai login tu | **Nessuno** |
| **Login API** | 🟡 Giallo | Login automatico via API instagrapi | **Alto** (possibile ban IP) |

#### Metodo consigliato: Login Browser (passo per passo)

Questo è il metodo **sicuro**. Il bot apre un browser Chromium reale sul tuo computer e tu fai il login manualmente, come faresti normalmente su Instagram.

**Ecco cosa succede esattamente quando clicchi "Login Browser":**

1. **Clicca il bottone verde "Login Browser"** accanto all'account
   - Il bottone diventa "In attesa..." con uno spinner

2. **Si apre un browser Chromium** direttamente sul tuo schermo
   - Non è il tuo Chrome/Firefox — è un browser separato gestito dal bot (Patchright)
   - Si apre la pagina di login di Instagram: `instagram.com/accounts/login/`

3. **Fai il login normalmente nel browser che si è aperto**
   - Inserisci username e password
   - Se Instagram ti chiede la verifica a due fattori (2FA), inserisci il codice
   - Se ti chiede "era proprio tu?", conferma
   - Se ti mostra un captcha, risolvilo
   - Insomma: gestisci tutto tu, come se stessi usando Instagram normalmente

4. **Appena il login è completato** (vedi la home di Instagram):
   - Il bot rileva automaticamente che sei loggato (controlla i cookie)
   - Il browser si chiude da solo dopo ~3 secondi
   - I cookie della sessione vengono salvati

5. **Il bot verifica la sessione**
   - Fa una singola chiamata API per confermare che i cookie funzionano
   - Se tutto ok: appare il toast verde "Sessione salvata!"
   - Se qualcosa non va: appare un messaggio di errore con istruzioni

6. **L'account è ora "active"** — pronto per le campagne

> **Tempo massimo**: Hai 5 minuti per completare il login. Se non fai in tempo, clicca di nuovo il bottone.

> **Il browser che si apre NON è il tuo browser personale.** È un browser isolato con un profilo dedicato a quell'account. I tuoi dati personali (password salvate, cronologia, ecc.) non vengono toccati.

#### Perché NON usare "Login API"

Il bottone giallo "Login API" usa le API private di Instagram (via instagrapi) per fare il login in modo automatico. Questo è rischioso perché:
- Instagram rileva facilmente i login automatici
- Può bloccare il tuo IP (anche per 24-48 ore)
- Può bloccare l'account stesso
- Se il tuo IP viene messo in blacklist, neanche il login manuale funzionerà finché non cambi rete

Usalo **solo** se il Login Browser non funziona per qualche motivo tecnico, e fai una rete diversa (es. hotspot mobile).

---

### Sessione scaduta — Cosa fare

Le sessioni Instagram durano tipicamente settimane/mesi, ma possono scadere se:
- Cambi password dall'app Instagram
- Instagram ti disconnette per sicurezza
- Passi troppo tempo senza usare l'account

Se la sessione scade, il bot te lo dice quando provi ad avviare una campagna:
```
La sessione di @username è scaduta. Vai su Account → 'Login Browser' per rifare il login.
```

**Soluzione**: clicca di nuovo "Login Browser" e rifai il login. I cookie verranno aggiornati.

---

### Stati account

| Stato | Significato |
|---|---|
| 🟢 `active` | Pronto per inviare DM |
| 🔵 `warming_up` | Account nuovo in fase di warm-up (limiti ridotti) |
| 🟡 `cooldown` | Temporaneamente fermo dopo rate limit di Instagram |
| 🔴 `challenge_required` | Instagram ha chiesto verifica email/telefono |
| ⛔ `banned` | Account bannato da Instagram |
| ⚫ `disabled` | Disabilitato manualmente |

### Gestire un challenge
Se un account va in stato `challenge_required`:
1. Apri l'email/telefono associato all'account Instagram
2. Prendi il codice di verifica
3. Nella sezione Account clicca **Inserisci codice challenge**
4. L'account torna in stato `active`

### Consigli account
- Usa sempre account **secondari/dedicati**, mai il tuo account principale
- Un account nuovo parte automaticamente in **warm-up** (vedi sezione 9)
- Puoi aggiungere più account — il bot li ruota automaticamente
- Limite consigliato: **20-30 DM/giorno** per account (Instagram blocca oltre 50-100)
- **Fai sempre il Login Browser** prima di avviare qualsiasi campagna

---

## 3. Creare una Campagna

1. Vai su **Campagne** → **Nuova campagna**
2. Compila i campi:

| Campo | Descrizione | Esempio |
|---|---|---|
| **Nome campagna** | Nome interno per identificarla | `Campagna Aprile — @fitness_roma` |
| **Pagina target** | Username Instagram da cui scrapare i follower | `fitness_roma` (senza @) |
| **Template messaggio** | Il testo base del DM (vedi sezione 4) | `Ciao {nome}, ho visto che segui...` |
| **Contesto AI** | Istruzioni aggiuntive per l'AI (vedi sezione 5) | `Tono informale, max 3 righe` |

3. Clicca **Crea campagna** → si apre la pagina dettaglio
4. Clicca **Avvia scraping** per raccogliere i follower
5. Quando lo scraping è completo → clicca **Avvia**

---

## 4. Template Messaggio — Variabili disponibili

Il template è il **punto di partenza** che l'AI usa per generare il messaggio personalizzato.
Non è un template rigido: l'AI lo rielabora tenendo conto di nome e bio del destinatario.

### Variabili supportate

| Variabile | Cosa inserisce | Note |
|---|---|---|
| `{nome}` | Nome reale del profilo (es. "Marco") | Se non disponibile, usa `@username` |
| `{name}` | Identico a `{nome}` (alias inglese) | Stessa cosa, entrambi funzionano |

> **Nota**: le variabili vengono usate come fallback se l'AI non genera un messaggio valido.
> Normalmente l'AI incorpora nome e bio in modo naturale **senza bisogno di segnaposto espliciti**.

### Come funziona l'AI
1. Riceve il tuo template come "linea guida"
2. Legge username, nome completo e bio Instagram del destinatario
3. Genera un messaggio unico che segue il tuo intento ma suona naturale
4. Se la generazione fallisce → usa il template con `{nome}` sostituito

---

## 5. Contesto AI — Come usarlo bene

Il campo **Contesto AI** è opzionale ma potente. Serve a dare istruzioni aggiuntive al modello su come scrivere.

### Cosa puoi specificare

**Tono:**
```
Tono informale e diretto, come se fosse un amico che scrive.
```

**Lunghezza:**
```
Massimo 2 frasi, breve e conciso.
```

**Cosa evitare:**
```
Non menzionare prezzi o offerte. Non sembrare commerciale.
```

**Settore/contesto:**
```
Siamo un'agenzia di marketing per ristoranti. 
L'obiettivo è offrire una consulenza gratuita.
```

**Lingua:**
```
Scrivi sempre in italiano, anche se la bio è in inglese.
```

**Combinazione consigliata:**
```
Tono informale e curioso. Max 3 righe. 
Fai riferimento alla bio se contiene qualcosa di interessante.
Non iniziare mai con "Ciao!".
Scrivi in italiano.
```

---

## 6. Esempi pratici di template

### Agenzia marketing → ristoranti
**Template:**
```
Ciao {nome}, ho visto che sei nel settore food — 
sto aiutando alcuni ristoranti a crescere su Instagram 
senza spendere in ads. Ti farebbe piacere saperne di più?
```
**Contesto AI:**
```
Tono professionale ma amichevole. Max 3 righe.
Se la bio menziona un tipo di cucina o un locale specifico, citalo.
Non iniziare con "Ciao!". Scrivi in italiano.
```

---

### Personal trainer → potenziali clienti
**Template:**
```
{nome} ho visto il tuo profilo — 
segui già un programma di allenamento 
o stai cercando qualcosa di nuovo?
```
**Contesto AI:**
```
Tono curioso e diretto, come un trainer che fa una domanda genuina.
Max 2 frasi. Non menzionare prezzi. Scrivi in italiano.
```

---

### E-commerce → clienti competitor
**Template:**
```
Ciao {nome}! Ho visto che ti piace [settore] — 
ho qualcosa che potrebbe interessarti, 
posso mandarti i dettagli?
```
**Contesto AI:**
```
Molto breve, massimo 2 righe. Tono informale.
Crea curiosità senza svelare subito il prodotto.
Se la bio dice dove vive o lavora, menzionalo naturalmente.
```

---

### Networking B2B
**Template:**
```
{nome} lavori nel [settore]? 
Sto connettendo professionisti del settore 
per uno scambio di idee — ti va di fare una chiacchierata?
```
**Contesto AI:**
```
Tono professionale ma non formale. 
Se la bio indica il ruolo o l'azienda, usalo per personalizzare.
Max 2-3 frasi. Scrivi in italiano.
```

---

## 7. Ciclo di vita di una campagna

```
draft → scraping → ready → running → paused/completed/error
```

| Stato | Significato | Azioni disponibili |
|---|---|---|
| `draft` | Appena creata | Avvia scraping |
| `scraping` | Raccolta follower in corso | — (automatico) |
| `ready` | Follower raccolti, messaggi generati | Avvia |
| `running` | Invio DM in corso | Pausa, Stop |
| `paused` | Messa in pausa manualmente | Riprendi, Stop |
| `completed` | Tutti i DM inviati | — |
| `error` | Errore critico | Controlla i log |

### Stati dei follower (visibili nel dettaglio campagna)

| Stato | Significato |
|---|---|
| `pending` | Follower trovato, bio non ancora scaricata |
| `bio_scraped` | Bio scaricata, messaggio non ancora generato |
| `message_generated` | Messaggio AI pronto, non ancora inviato |
| `sent` | DM inviato con successo ✅ |
| `failed` | Invio fallito (ritentabile) |
| `skipped` | Saltato (già contattato in altra campagna) |
| `replied` | Ha già risposto (rilevato automaticamente) |

---

## 8. Parametri di configurazione (.env)

File `.env` nella root del progetto. Modificalo con un editor di testo.

### Timing invio DM

| Parametro | Default | Descrizione |
|---|---|---|
| `MIN_DELAY_SECONDS` | `120` | Pausa minima tra un DM e l'altro (2 min) |
| `MAX_DELAY_SECONDS` | `480` | Pausa massima tra un DM e l'altro (8 min) |
| `SESSION_MIN_MESSAGES` | `10` | Minimo DM per sessione prima della pausa |
| `SESSION_MAX_MESSAGES` | `20` | Massimo DM per sessione prima della pausa |
| `SESSION_BREAK_MIN_MINUTES` | `30` | Pausa minima tra sessioni |
| `SESSION_BREAK_MAX_MINUTES` | `60` | Pausa massima tra sessioni |

### Orario attivo

| Parametro | Default | Descrizione |
|---|---|---|
| `ACTIVE_HOURS_START` | `8` | Ora di inizio invio (8:00 UTC) |
| `ACTIVE_HOURS_END` | `23` | Ora di fine invio (23:00 UTC) |

> ⚠️ Gli orari sono in **UTC**. Se sei in Italia (UTC+2 in estate), `ACTIVE_HOURS_START=8` corrisponde alle 10:00 ora italiana.

### Account e browser

| Parametro | Default | Descrizione |
|---|---|---|
| `DEFAULT_DAILY_LIMIT` | `20` | Max DM per account per giorno |
| `WARMUP_ENABLED` | `true` | Abilita il protocollo warm-up per nuovi account |
| `MAX_CONCURRENT_BROWSERS` | `3` | Max browser Patchright aperti contemporaneamente |
| `HEADLESS` | `true` | `false` = vedi il browser durante l'invio (utile per debug) |

### AI (Ollama)

| Parametro | Default | Descrizione |
|---|---|---|
| `OLLAMA_MODEL` | `llama3.2` | Modello da usare. Alternative: `llama3.2:1b` (più veloce), `mistral` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | URL del server Ollama |

---

## 9. Warm-up account

Un account nuovo viene automaticamente messo in stato `warming_up`.
Il bot rispetta questi limiti progressivi:

| Giorni | DM/giorno massimi |
|---|---|
| Giorni 1-3 | 5 DM/giorno |
| Giorni 4-7 | 12 DM/giorno |
| Giorni 8-14 | 20 DM/giorno |
| Giorno 15+ | Limite normale (da `.env`) |

Il contatore avanza automaticamente ogni giorno grazie al cron ARQ.
Non c'è bisogno di fare nulla — il bot si autogestisce.

---

## 10. Regole anti-ban

Queste regole sono già implementate nel codice. **Non disabilitarle.**

1. **Mai delay uniformi** — il bot usa distribuzioni log-normali con alta varianza (sigma 0.7) per tempi imprevedibili
2. **Sessioni limitate** — 10-20 DM per sessione, poi pausa obbligatoria 30-60 min
3. **Finestra oraria** — nessun invio fuori dagli orari configurati
4. **Profili browser persistenti** — ogni account ha il suo profilo Chromium, mai modalità incognito
5. **Warm-up graduale** — account nuovi iniziano con 3-5 DM/giorno
6. **Deduplicazione** — il bot non contatta mai lo stesso utente due volte, neanche su campagne diverse
7. **Fingerprint stabile** — ogni account ha sempre lo stesso user-agent, viewport e timezone
8. **Browsing umano** — il bot scorre il profilo prima di cliccare "Messaggio", con scroll randomizzati (piccoli/grandi, pause lettura, hover mouse)
9. **Typing naturale** — ogni tasto viene digitato con un delay statisticamente variabile, con pause tra parole
10. **Ordine follower random** — i follower vengono contattati in ordine casuale, mai sequenziale

### Segnali di rischio da monitorare
- Account che va spesso in `cooldown` → riduci `DEFAULT_DAILY_LIMIT`
- Account in `challenge_required` ripetuto → è quasi certamente un problema di IP → aggiungi un proxy (vedi sezione 12-13)
- Account `banned` → smetti di usarlo, non c'è recupero automatico
- Più account bannati nello stesso giorno → il tuo IP è segnalato → **usa proxy immediatamente**

---

## 11. Troubleshooting

### Il backend non parte
```
Controlla che il venv sia attivato:
cd backend
venv\Scripts\activate
uvicorn app.main:app --reload --port 8000
```
Leggi l'errore nella finestra CMD del backend.

---

### Health check mostra `ollama: error`
- Verifica che Ollama sia in esecuzione: cerca l'icona nella tray bar
- Oppure esegui: `ollama serve`
- Verifica che il modello sia scaricato: `ollama list`

---

### Health check mostra `redis: error`
- Apri **Servizi Windows** e verifica che **Memurai** sia "In esecuzione"
- Tasto destro su Memurai → Avvia

---

### Lo scraping si blocca o restituisce 0 follower
- L'account usato per lo scraping potrebbe essere limitato
- Prova con un account diverso
- La pagina target potrebbe avere i follower privati

---

### I DM non vengono inviati
1. Verifica che ci sia almeno un account in stato `active`
2. Verifica che l'orario sia dentro `ACTIVE_HOURS_START` e `ACTIVE_HOURS_END`
3. Controlla i log nella sezione **Messaggi** per vedere gli errori
4. Prova con `HEADLESS=false` nel `.env` per vedere cosa fa il browser

---

### Errore challenge su account
1. Vai su **Account** nella dashboard
2. Trova l'account in stato `challenge_required`
3. Controlla email/telefono dell'account per il codice Instagram
4. Clicca **Inserisci codice challenge** e inseriscilo

---

### Comandi remoti Telegram
- `/status` mostra kill-switch, campagne attive/in pausa e account da controllare.
- `/pause` mostra bottoni Telegram con le campagne attive: scegli una sola campagna da mettere in pausa.
- `/resume` mostra bottoni Telegram con le campagne in pausa: scegli una sola campagna da far ripartire.
- `/halt motivo` attiva il kill-switch globale di emergenza e ferma tutto il bot.
- `/unhalt` disattiva il kill-switch globale e riaccoda i lavori che erano ancora attivi.

Da dashboard web, nella sidebar admin, il kill-switch globale ha due pulsanti espliciti: **Blocca tutto** quando il bot e' operativo e **Sblocca** quando il blocco e' attivo.

Un problema su un singolo account Instagram non ferma automaticamente tutto il bot: l'account viene isolato (`cooldown`, `challenge_required` o `banned`) e vengono messe in pausa solo le campagne che non hanno altri account DM utilizzabili. Il kill-switch globale resta riservato a problemi sistemici o al comando manuale `/halt`.

---

---

## 12. Proxy e diversificazione IP

### Perché serve un proxy

Ogni volta che il bot manda un messaggio, Instagram vede da dove arriva la richiesta — il tuo **indirizzo IP**, che è come l'indirizzo di casa tua su internet.

Il problema: se hai 5 account che inviano DM, Instagram vede 5 persone diverse che abitano tutte nello stesso posto. Questo è un segnale sospetto che può portare a ban di tutti gli account in blocco.

Con un proxy, ogni account esce da un IP diverso — Instagram vede 5 persone in 5 posti diversi.

---

### Tipi di proxy a confronto

| Tipo | Fiducia Instagram | Costo | Note |
|---|---|---|---|
| **IP di casa/ufficio** | Alta | €0 | Solo 1 IP → max 2-3 account senza rischi |
| **VPS/server datacenter** | Bassa ⚠️ | €5-15/mese | Instagram riconosce gli IP "da server", alto rischio ban |
| **ISP proxy residenziale statico** | Alta | €2-5/IP/mese | IP fisso intestato a un privato, buon compromesso |
| **Proxy residenziale rotante** | Alta | €3-10/GB | IP diverso per ogni connessione, costoso ma efficace |
| **Mobile proxy 4G/5G** | Altissima ✅ | €30-80/IP/mese | IP identico a un vero utente che usa il telefono |
| **Tuo telefono Android come proxy** | Altissima ✅ | ~€5-10/mese (solo SIM) | Vedi sezione 13 — la soluzione migliore se hai dispositivi disponibili |

> **Regola pratica**: mai usare IP da datacenter (VPS, AWS, DigitalOcean). Instagram li riconosce e li tratta con sospetto automaticamente. Usa sempre IP residenziali o mobili.

---

### Come configurare un proxy su un account

1. Vai su **Account** nella dashboard
2. Clicca **Modifica** (icona matita) sull'account
3. Nel campo **Proxy** inserisci l'indirizzo nel formato:
   ```
   http://utente:password@indirizzo:porta
   ```
   oppure senza autenticazione:
   ```
   http://indirizzo:porta
   ```
4. Salva — il proxy viene usato immediatamente per tutti i messaggi di quell'account

---

### Quanti proxy servono?

Regola conservativa: **1 IP ogni 1-2 account**.

| Account attivi | IP diversi consigliati |
|---|---|
| 1-2 | 1 (il tuo IP di casa va bene) |
| 3-4 | 2 |
| 5-6 | 3 |
| 10 | 5 |
| 20 | 8-10 |

---

## 13. Usare i telefoni Android come proxy

Questa è la **soluzione migliore** se hai dispositivi Android disponibili. È gratuita (o quasi), semplice da configurare, e offre la massima fiducia da parte di Instagram perché il traffico esce da un vero IP mobile 4G.

### Come funziona (spiegazione semplice)

Immagina che il bot sia un impiegato in ufficio che deve mandare lettere. Invece di mandarle dalla sua scrivania (il tuo PC, con il suo IP fisso), le dà a dei "corrieri" (i tuoi telefoni). I corrieri le mandano dai loro indirizzi (gli IP 4G). Instagram vede solo l'indirizzo del corriere, non quello dell'ufficio.

```
[PC con il bot]
      ↓
[Telefono 1 — SIM Vodafone] → Instagram vede un IP mobile Vodafone
[Telefono 2 — SIM TIM]      → Instagram vede un IP mobile TIM
[Telefono 3 — SIM Wind]     → Instagram vede un IP mobile Wind
```

Ogni telefono ha un IP diverso assegnato dalla sua compagnia telefonica. Per Instagram, quell'IP è identico a quello di un vero utente che usa l'app sul telefono — impossibile da distinguere.

---

### Cosa ti serve

- Uno o più telefoni Android (qualsiasi modello economico va bene, anche vecchi)
- Una SIM con piano dati per ogni telefono (bastano 1-2 GB/mese per il volume del bot)
- L'app **iProxy** installata su ogni telefono

---

### Configurazione passo per passo

#### Sul telefono (da fare una volta per dispositivo)

1. **Installa iProxy** dal Play Store  
   cerca: `iProxy Online — Mobile Proxy`

2. **Apri l'app e registrati** (piano gratuito disponibile per pochi dispositivi)

3. **Crea una nuova connessione** nell'app:
   - Tipo: `HTTP`
   - L'app ti mostrerà un **indirizzo e una porta**, tipo: `proxy.iproxy.online:12345`
   - Ti darà anche username e password se il piano lo prevede

4. **Assicurati che il telefono sia connesso**:
   - WiFi: connesso alla stessa rete del PC (per uso locale) oppure a qualsiasi rete
   - Dati mobili: **attivi** (deve essere acceso il 4G/5G — è da lì che uscirà il traffico)

> ⚠️ **Importante**: il WiFi serve per comunicare con il PC, i dati mobili (4G) servono per uscire su internet. Entrambi devono essere attivi contemporaneamente.

#### Sul PC (nel bot)

5. Vai su **Account** → **Modifica** sull'account da assegnare a quel telefono

6. Nel campo **Proxy** inserisci:
   ```
   http://username:password@proxy.iproxy.online:12345
   ```
   (usa i dati che ti ha mostrato l'app iProxy)

7. Salva. Da ora quel messaggio uscirà dall'IP del telefono.

---

### Schema consigliato con più telefoni

```
Account @nome1  →  Telefono 1 (SIM Vodafone)
Account @nome2  →  Telefono 1 (SIM Vodafone)   ← max 2 account per telefono

Account @nome3  →  Telefono 2 (SIM TIM)
Account @nome4  →  Telefono 2 (SIM TIM)

Account @nome5  →  Telefono 3 (SIM Wind)
```

---

### I telefoni devono restare accesi?

Sì. I telefoni devono:
- Essere **accesi** (non in modalità aereo, non spenti)
- Avere **WiFi e dati mobili entrambi attivi**
- Essere collegati alla **corrente** (altrimenti si scaricano)
- Avere l'app **iProxy aperta** (o in background attiva)

In pratica: li metti da una parte su un supporto, collegati alla corrente, li dimentichi lì. Non devi toccarli.

---

### Alternative a iProxy

| App | Piano gratuito | Note |
|---|---|---|
| **iProxy.online** | Sì (2 dispositivi) | La più semplice, consigliata |
| **Proxidize Mobile** | No (prova gratuita) | Più funzioni, pagamento mensile |
| **OpenVPN + tethering** | Sì (richiede tecnico) | Soluzione fai-da-te, gratis ma complessa |

---

## 14. Scala — raggiungere 100-150 DM al giorno

### Calcolo realistico

Con i parametri di produzione consigliati:
- Delay medio tra messaggi: ~5 minuti (distribuzione lognormale 2-8 min)
- Sessione: 10-20 messaggi poi 30-60 min di pausa
- DM per account al giorno: **20-25** (limite `DEFAULT_DAILY_LIMIT`)

| Account attivi | DM/giorno totali | IP necessari |
|---|---|---|
| 1 | 20-25 | 1 (IP di casa) |
| 3 | 60-75 | 2 |
| 5 | 100-125 | 3 |
| 6-7 | 120-175 | 3-4 |
| 10 | 200-250 | 5 |

**Per raggiungere 100-150 DM/giorno: ti servono 5-7 account con 3-4 IP diversi.**

---

### Configurazione consigliata per iniziare (budget minimo)

**Hardware**: 1 PC + 3 telefoni Android (anche economici/vecchi)  
**SIM**: 3 SIM con piano dati base (~€5-10/mese ciascuna)  
**App proxy**: iProxy (piano gratuito o base)  
**Costo totale**: ~€15-30/mese  
**DM/giorno**: 100-150

Questa configurazione con 5-6 account Instagram e 3 telefoni come proxy è la soluzione con il miglior rapporto costo/efficacia e il minor rischio ban.

---

### Configurazione avanzata (massima scala)

Se vuoi spingere oltre (300-500 DM/giorno):
- 10-15 account Instagram maturi (almeno 30 giorni di warm-up)
- 5-8 proxy mobili (telefoni o servizio a pagamento)
- Multi-account per campagna (funzione in sviluppo — vedi roadmap)
- Limiti: `DEFAULT_DAILY_LIMIT=30`, timing aggressivo

> ⚠️ Sopra i 30 DM/giorno per account il rischio ban aumenta significativamente, anche con proxy. Considera che Instagram può limitare gli account per pattern comportamentali indipendentemente dall'IP.

---

### Roadmap funzioni di scala (non ancora implementate)

Queste funzioni sono pianificate ma non ancora disponibili:

**Multi-account per campagna**: più account inviano DM dalla stessa campagna in parallelo, coordinati via Redis per non contattare lo stesso utente due volte. Raddoppia o triplica la velocità senza aumentare il rischio per singolo account.

**Multi-campagna parallela**: più campagne attive contemporaneamente su account diversi. Già parzialmente supportato dall'architettura — manca solo l'assegnazione esplicita account→campagna.

**Pagina leads**: visualizzazione del database contatti con storico campagne, account utilizzato, data, bio. Export CSV.

---

*Ultima modifica: 2026-05-11*
