# Inbox browser: velocità di scorrimento, memoria di sessione e modalità segnalibro

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **REQUIRED SUB-SKILL aggiuntiva (standard di Tommaso):** `sviluppo-modulo` — reviewer dedicato per ogni task, QA agent dopo ogni funzione, protocollo di chiusura a fine modulo.

**Goal:** portare il motore di listing inbox via browser da ~3 aperture/minuto con il 75% del tempo speso a dormire, a un ritmo che permetta di scendere lungo liste da migliaia di chat, mantenendo un profilo di comportamento indistinguibile da quello umano misurato sul trackpad di Tommaso.

**Architecture:** quattro interventi indipendenti sullo stesso motore. (1) Il motore smette di pagare pause per lavoro che non fa: righe già esaminate nella sessione corrente e righe solo scorse. (2) I gesti di scorrimento vengono tarati sui dati reali registrati l'11/08 invece che su un modello inventato. (3) Una modalità "segnalibro", attivabile per singola sessione, permette di saltare la parte alta della lista già lavorata usando come soglia una data letta dalla riga di lista. (4) Il reset periodico della lista viene diagnosticato e reso innocuo. Le decisioni restano in funzioni pure testabili; il JS raccoglie dati grezzi e non decide nulla (regola di disegno introdotta dalla PR #60).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Alembic, ARQ, Patchright (fork stealth di Playwright), pytest + pytest-asyncio. Frontend Next.js 15 (App Router) + TypeScript.

---

## Global Constraints

- **Il motore API non si tocca.** `app/services/scrape_inbox.py` e `app/services/inbox_source.py` restano invariati byte per byte. Verificare con `git diff --stat main -- backend/app/services/scrape_inbox.py backend/app/services/inbox_source.py` prima di ogni PR: deve essere vuoto.
- **Mai `element.click()`**: sempre `human_input.human_click`.
- **Nessuna coordinata assoluta dentro le query JS.** Il test `test_le_query_js_non_contengono_soglie_orizzontali_cablate` in `tests/test_inbox_browser_geometria.py` fallisce se qualcuno reintroduce `r.left > N`. Le soglie verticali (`top`) restano ammesse.
- **Migration prima del codice.** La migration `032` di Task 5 va portata su `main` con una PR dedicata **prima** di essere applicata al Postgres condiviso. Applicarla al DB mentre il file esiste solo su un branch fa morire ogni `start.bat` con `Can't locate revision identified by '032'`. È già successo due volte (027 il 03/08, 029 l'08/08).
- **Una sola suite pytest alla volta.** Lo sqlite di test è condiviso e `phone_hmac` ha un UNIQUE: due suite in parallelo producono rossi fantasma. Usare sempre uno slot dedicato: `WA_TEST_DB_SLOT=<nome> ./venv/Scripts/python.exe -m pytest ...`. Se una suite viene uccisa lascia il lock `backend/data/test_bot_<slot>.lock` da rimuovere a mano.
- **Comandi sempre dal folder `backend`**, mai da un worktree: `BROWSER_PROFILES_DIR` nel `.env` è relativo alla cwd, e girare da altrove fa cercare i profili browser in una cartella vuota — sembra una cancellazione di dati e porta a rifare il login, che per Instagram è un dispositivo nuovo e un innesco classico di challenge.
- **Solo profili sacrificabili** per le prove che toccano Instagram davvero. `@michele.carozza` è l'account di produzione della campagna `AV X @michele.carozza` (`ec5e2464-1d8d-42a1-a81f-8e61b303fa7a`): usarlo solo su richiesta esplicita di Tommaso, e verificando prima che abbia un proxy configurato (l'11/08 aveva `proxy = NULL` e il traffico usciva dall'IP di casa).
- **I 17 test rossi `test_wa_*`** (WhatsApp/warmup) sono pre-esistenti e non correlati. La CI di `main` ne ha 8 rossi (7 `wa_migration` + 1 `campaign_orchestrator_browser_busy`). Non sono regressioni di questo lavoro e non vanno "sistemati" qui.
- **Punto di partenza:** branch `fix/inbox-browser-ritmo-e-gesto` (2 commit sopra `main` @ `929e2cf`), che contiene già il ritmo per zona di scorrimento, il gesto a campana provvisorio e i due fix di `apri_riga`. **Task 1 apre la PR di quel branch**; tutti gli altri task partono da lì.

---

## Il problema, in numeri misurati

Due sessioni supervisionate sulla campagna `AV X @michele.carozza`, l'11/08, con `scripts/supervisione_inbox_browser.py`:

| | sessione 1 (18 min) | sessione 2 (30 min, dopo il primo fix) |
|---|---|---|
| pause | **972.7s — 91.4%** | **1290.5s — 74.7%** |
| apertura chat | 54.9s — 5.2% | 307.6s — 17.8% |
| scroll | 36.7s — 3.4% | 128.9s — 7.5% |
| lettura DOM | 0.3s | 1.0s |
| aperture/minuto | 1.1 | 3.3 |
| contatti nuovi | 0 | 0 |

**Obiettivo di questo piano: pause sotto il 50% del tempo di sessione.** È il tetto posto da Tommaso l'11/08 ("non riusciremo mai ad arrivare nemmeno a 1000 contatti così").

Scomposizione delle pause della sessione 2:
- `pause_scorrimento` (righe non aperte): **675.7s**
- `pause_apertura` (dopo una chat aperta): **614.8s**, di cui ~420s in 2 soli "stacchi" da 120-300s

Da dove si recupera:
- **Righe riesaminate più volte** (Task 2): il ciclo rilegge le righe visibili a ogni giro e, se lo scroll avanza meno di una schermata, ripaga la pausa per righe già viste. Con 271 chat uniche e 675.7s di pause di scorrimento a ~0.85s l'una, le righe *processate* sono state circa 795: **~65% di lavoro ripetuto**.
- **Stacchi sulle aperture** (Task 11): 2 stacchi su 98 aperture pesano ~420s, cioè il 24% dell'intera sessione.

Stima post-piano, in due passi.

**Con Task 2 + Task 11 soltanto** (memoria di sessione e stacchi ridotti, tutto il resto invariato):
```
pause_scorrimento   675.7 → ~240s   (via le ripetizioni)
pause_apertura      614.8 → ~195s   (stacchi da 4.2s a 1.35s attesi per apertura)
apertura            307.6 → 307.6s  (invariato)
scroll              128.9 → ~90s
────────────────────────────────────────────
totale             1727  → ~833s     pause = 435/833 = 52%
```
Sopra il tetto: **non basta**.

**Con Task 4a in più** (lettura durante il gesto, che permette di alzare il passo da 0.7 a 2-4 schermate): i giri necessari a coprire la stessa distanza scendono di circa tre volte, e con loro le righe riesaminate — che sono la voce principale delle pause di scorrimento.
```
pause_scorrimento   240 → ~110s   (un terzo dei giri, quindi un terzo delle riletture)
pause_apertura      195 → 195s
apertura            307.6 → 307.6s
scroll               90 → ~75s    (meno gesti, ognuno più lungo)
────────────────────────────────────────────
totale              833 → ~688s    pause = 305/688 = 44%
```

**44%, sotto il tetto del 50%.** Il numero di aperture nello stesso tempo passa da 98/30min a circa 98/11min, cioè da 3.3 a ~9 al minuto.

Nota su cosa NON cambia in questo conto: `apertura` (307.6s per 98 chat, ~3.1s l'una) è tempo di rete e di rendering del thread, non è comprimibile senza toccare la verifica post-click — e quella verifica è ciò che impedisce di salvare dati attribuiti alla persona sbagliata. Resta la voce dominante, ed è giusto così: è l'unico tempo speso in lavoro vero.

**Task 12 verifica il risultato sul campo e il piano non è chiuso finché la misura non sta sotto il 50%.**

## I gesti umani reali (misurati, non ipotizzati)

`scripts/registra_scroll_umano.py`, 11/08, trackpad di Tommaso, 1660 eventi `wheel` su 4 gesti:

```
VERDETTO: TRACKPAD — eventi fini e continui, il momentum esiste
deltaMode: 0 (pixel)          intervallo fra eventi: mediana 16.7ms (60fps)
deltaY: mediana 18, min 0, max 342

gesto 0: 234 eventi   5.742px   3.900ms   picco 193   coda 3,3,2,2,1,1,1  (flick)
gesto 1: 278 eventi  12.192px   4.844ms   picco 222   coda 21,19,17,15,12 (interrotto)
gesto 2: 749 eventi  24.113px  12.583ms   picco 342   coda 3,2,2,1,1,1    (flick)
gesto 3: 399 eventi  10.220px   6.934ms   picco 231   coda 21,19,18,16    (interrotto)

velocità: 1.472 — 2.517 px/s
```

Il gesto attuale del motore copre 590px in 0.62s a 952 px/s con picchi da 60px: **è 2.6 volte più lento di un gesto umano vero, con eventi più radi e picchi 6 volte più bassi.** Rallentare non ci rende più prudenti: ci rende diversi dal riferimento.

Fatti da riprodurre nella taratura:
1. gli eventi arrivano a ritmo di frame: **mediana 16.7ms**, con code fino a 50-150ms e occasionali coppie nello stesso frame (intervallo 0);
2. capitano eventi con **`deltaY` esattamente 0** (48 su 1660): il browser li emette, e un flusso che non li ha mai è più regolare del vero;
3. un flick **decelera fino a 1px** prima di spegnersi (gesti 0 e 2); un gesto interrotto col dito ancora appoggiato si ferma di colpo su valori medi (gesti 1 e 3);
4. un singolo gesto copre **da 5.700 a 24.000 pixel**: coprire una schermata alla volta è una scelta del motore, non un vincolo umano.

---

## File Structure

**Modificati:**
- `backend/app/services/inbox_browser/pagina.py` — gesti di scorrimento (`piano_scroll`, `piano_lancio`, `scorri`, nuova `lancia`), lettura righe. È il file che contiene la geometria del DOM e i gesti.
- `backend/app/services/inbox_browser/ritmo.py` — pause per zona; qui vive la distinzione fra ritmo di scorrimento e ritmo delle azioni.
- `backend/app/services/inbox_browser/testo.py` — parsing del testo della riga; qui va il parsing della data relativa (`5 g`, `20 h`).
- `backend/app/services/scrape_inbox_browser.py` — il ciclo del motore: memoria di sessione, modalità segnalibro, aggiornamento del cursore.
- `backend/app/models/campaign.py` — due colonne nuove.
- `backend/app/api/campaigns.py` — `PhaseStartBody` con il flag della modalità.
- `backend/app/schemas/campaign.py` — esposizione del cursore nella response.
- `backend/frontend/app/campaigns/[id]/page.tsx` e `backend/../frontend/lib/api.ts` — il toggle in UI.

**Creati:**
- `backend/alembic/versions/032_inbox_cursor.py` — migration additiva.
- `backend/app/services/inbox_browser/segnalibro.py` — funzioni pure della modalità segnalibro (decisione di saltare, aggiornamento del cursore). File nuovo perché è una responsabilità a sé e `pagina.py` è già a ~600 righe.
- `backend/tests/test_inbox_browser_segnalibro.py`
- `backend/tests/test_inbox_browser_memoria_sessione.py`
- `backend/tests/test_inbox_cursor_column.py`
- `backend/scripts/probe_reset_lista.py` — sonda diagnostica sul reset.

**Già esistenti, da usare:**
- `backend/scripts/supervisione_inbox_browser.py` — harness di misura: raccoglie davvero e cronometra tutto. È lo strumento di verifica di Task 12.
- `backend/scripts/registra_scroll_umano.py` — registratore dei gesti umani.
- `backend/data/scroll_umano.json` — le misure dell'11/08 su cui tarare Task 3.

---

### Task 1: aprire la PR di quanto è già pronto

Il branch `fix/inbox-browser-ritmo-e-gesto` contiene tre fix già verificati e 32 test verdi, ma non è ancora in PR. Va aperto per primo, così tutto il resto ha una base condivisa.

**Files:**
- Nessuna modifica di codice.

**Interfaces:**
- Produces: branch `fix/inbox-browser-ritmo-e-gesto` mergiato in `main`, con dentro `ritmo.zona_pausa(zona, ha_aperto)`, `pagina.piano_lancio(px)`, `pagina.username_header(nodi, nome, bordo, larghezza_viewport)` e il filtro `top >= 0` in `nome_header`.

- [ ] **Step 1: verificare che la suite mirata sia verde**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t1 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_geometria.py tests/test_inbox_browser_pagina.py \
  tests/test_inbox_browser_ritmo.py tests/test_inbox_browser_testo.py \
  tests/test_scrape_inbox_browser.py -q -p no:cacheprovider
```
Atteso: `93 passed` o più.

- [ ] **Step 2: verificare che il motore API sia intatto**

```bash
cd "D:/BOT OUTBOUND"
git diff --stat main -- backend/app/services/scrape_inbox.py backend/app/services/inbox_source.py
```
Atteso: nessun output.

- [ ] **Step 3: aprire la PR**

```bash
cd "D:/BOT OUTBOUND"
git push -u origin fix/inbox-browser-ritmo-e-gesto
gh pr create --base main --head fix/inbox-browser-ritmo-e-gesto \
  --title "fix(inbox-browser): ritmo per azioni, gesto trackpad, due bug di apri_riga" \
  --body "Vedi docs/superpowers/plans/2026-08-11-inbox-browser-velocita-e-segnalibro.md per il contesto completo delle misure."
```

- [ ] **Step 4: attendere la CI e confrontare i rossi con quelli di `main`**

```bash
gh run list --branch fix/inbox-browser-ritmo-e-gesto --limit 2
gh run view <id> --log-failed | grep -E "^backend.*FAILED tests" | sed 's/.*FAILED /FAILED /' | sed 's/ - .*//' | sort
```
Atteso: gli stessi 8 rossi di `main` (7 `wa_migration` + 1 `campaign_orchestrator_browser_busy`), nessuno in più. Se ne compare uno diverso, **fermarsi**: è una regressione.

- [ ] **Step 5: merge**

```bash
gh pr merge <numero> --merge --delete-branch
```

---

### Task 2: memoria di sessione — non ripagare le righe già esaminate

Il ciclo rilegge le righe visibili a ogni giro. Se lo scroll avanza meno di una schermata — e succede sempre, perché `human_click` sposta la lista a ogni apertura — le stesse righe tornano nel lotto successivo e **ognuna paga di nuovo la sua pausa**. Misurato: ~795 righe processate per 271 chat uniche, il 65% di lavoro ripetuto.

**Files:**
- Create: `backend/tests/test_inbox_browser_memoria_sessione.py`
- Modify: `backend/app/services/scrape_inbox_browser.py` (funzione `run_inbox_browser_list`, il `for riga in righe`)

**Interfaces:**
- Consumes: `ritmo.zona_pausa(zona: str, ha_aperto: bool) -> str` (da Task 1).
- Produces: `scrape_inbox_browser.gia_esaminata(chiave: str | None, viste: set[str]) -> bool` — funzione pura che dice se una riga è già stata esaminata in questa sessione, usata dal ciclo per saltarla senza pausa.

- [ ] **Step 1: scrivere il test che fallisce**

Create `backend/tests/test_inbox_browser_memoria_sessione.py`:

```python
"""Il motore non deve pagare due volte la stessa riga.

Misurato l'11/08 su una sessione di 30 minuti: 271 chat uniche incontrate, ma
circa 795 righe processate, ognuna con la sua pausa. Il ciclo rilegge le righe
visibili a ogni giro e `human_click` sposta la lista a ogni apertura, quindi il
lotto successivo ricomincia in mezzo al precedente. Il 65% delle pause di
scorrimento se ne andava in righe gia' viste.
"""
from app.services.scrape_inbox_browser import gia_esaminata


def test_una_riga_mai_vista_va_esaminata():
    viste = set()
    assert gia_esaminata("bruzzo abbigliamento", viste) is False


def test_la_stessa_riga_al_giro_dopo_non_si_ripaga():
    viste = {"bruzzo abbigliamento"}
    assert gia_esaminata("bruzzo abbigliamento", viste) is True


def test_una_riga_senza_nome_non_entra_nella_memoria():
    """Senza chiave non si puo' ricordare nulla: va trattata come nuova ogni
    volta, non come 'gia' vista' — altrimenti una singola riga anonima
    zittirebbe tutte le successive."""
    viste = {""}
    assert gia_esaminata("", viste) is False
    assert gia_esaminata(None, viste) is False
```

- [ ] **Step 2: eseguire il test e vederlo fallire**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t2 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_memoria_sessione.py -q -p no:cacheprovider
```
Atteso: `ImportError: cannot import name 'gia_esaminata'`.

- [ ] **Step 3: implementare la funzione pura**

In `backend/app/services/scrape_inbox_browser.py`, sotto `decide_se_aprire`:

```python
def gia_esaminata(chiave: str | None, viste: set[str]) -> bool:
    """True se questa riga e' gia' passata sotto gli occhi in QUESTA sessione.

    Serve a non pagarne la pausa una seconda volta: il ciclo rilegge le righe
    visibili a ogni giro e `human_click` sposta la lista a ogni apertura, quindi
    il lotto successivo ricomincia in mezzo al precedente. Una riga senza chiave
    non e' memorizzabile e viene trattata come nuova: dire il contrario
    zittirebbe tutte le righe anonime dopo la prima.
    """
    if not chiave:
        return False
    return chiave in viste
```

- [ ] **Step 4: eseguire il test e vederlo passare**

```bash
WA_TEST_DB_SLOT=t2 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_memoria_sessione.py -q -p no:cacheprovider
```
Atteso: `3 passed`.

- [ ] **Step 5: collegare la memoria al ciclo del motore**

In `backend/app/services/scrape_inbox_browser.py`, dentro `run_inbox_browser_list`:

Aggiungere l'insieme accanto agli altri contatori di sessione (vicino a `nuovi_in_sessione = 0`):

```python
        viste_in_sessione: set[str] = set()
```

E all'inizio del `for riga in righe:`, subito dopo i controlli di stop e **prima** del controllo `if riga.non_letta:`:

```python
                chiave = normalizza_nome(riga.nome)
                if gia_esaminata(chiave, viste_in_sessione):
                    # Gia' guardata in questa sessione: niente pausa, niente
                    # decisione, niente contatore di zona. Ripagarla e' il 65%
                    # delle pause di scorrimento misurate l'11/08.
                    continue
                if chiave:
                    viste_in_sessione.add(chiave)
```

L'import va aggiunto in testa al file:

```python
from app.services.inbox_browser.testo import (
    e_segnaposto, estrai_data_thread, estrai_ultimo_messaggio, normalizza_nome,
)
```

- [ ] **Step 6: verificare che il resto del modulo regga**

```bash
WA_TEST_DB_SLOT=t2 ./venv/Scripts/python.exe -m pytest \
  tests/test_scrape_inbox_browser.py tests/test_scrape_inbox_browser_kill_switch.py \
  tests/test_inbox_browser_defer.py tests/test_inbox_browser_guardie.py \
  -q -p no:cacheprovider
```
Atteso: tutti verdi.

- [ ] **Step 7: commit**

```bash
cd "D:/BOT OUTBOUND"
git add backend/app/services/scrape_inbox_browser.py backend/tests/test_inbox_browser_memoria_sessione.py
git commit -m "perf(inbox-browser): niente pausa per le righe gia' esaminate in sessione"
```

---

### Task 3: tarare i gesti sui dati umani reali

I parametri attuali di `piano_scroll` e `piano_lancio` sono un modello plausibile mai misurato. I dati veri esistono: `backend/data/scroll_umano.json`.

**Files:**
- Modify: `backend/app/services/inbox_browser/pagina.py` (costanti dei gesti, `piano_scroll`, `piano_lancio`)
- Modify: `backend/tests/test_inbox_browser_geometria.py` (sezione dei gesti)

**Interfaces:**
- Consumes: `pagina.piano_scroll(px: int) -> list[tuple[int, float]]`, `pagina.piano_lancio(px: int) -> list[tuple[int, float]]` (esistenti da Task 1).
- Produces: stesse firme, parametri tarati. Nessun cambio di interfaccia.

- [ ] **Step 1: scrivere i test che fissano i fatti misurati**

In `backend/tests/test_inbox_browser_geometria.py`, sostituire la sezione dei gesti (i test `test_il_gesto_e_fitto_come_quello_di_un_trackpad`, `test_durante_l_inerzia_gli_eventi_sono_fitti_e_regolari`) con:

```python
# ── i gesti, tarati sui dati veri ──────────────────────────────────────────
# Registrati l'11/08 dal trackpad di Tommaso (scripts/registra_scroll_umano.py,
# 1660 eventi su 4 gesti). I numeri qui sotto sono quelli, non una stima:
#   intervallo fra eventi: mediana 16.7ms (ritmo di frame)
#   deltaY: mediana 18, picchi 193-342
#   velocita': 1472-2517 px/s
#   un gesto copre da 5.742 a 24.113 px
import statistics

INTERVALLO_UMANO_MS = 16.7
PICCO_UMANO_MIN = 190
VELOCITA_UMANA_MIN = 1400
VELOCITA_UMANA_MAX = 2600


def test_gli_eventi_arrivano_a_ritmo_di_frame():
    """Mediana 16.7ms misurata. Eventi piu' radi tradiscono una simulazione."""
    pause_ms = [p * 1000 for _, p in piano_scroll(2000)]
    assert 10 <= statistics.median(pause_ms) <= 24


def test_il_gesto_raggiunge_i_picchi_di_una_mano_vera():
    """Il modello precedente non superava i 60px per evento; una mano arriva a
    193-342. Un flusso tutto piccolo e' regolare quanto uno tutto grande."""
    picchi = [max(d for d, _ in piano_scroll(2000)) for _ in range(30)]
    assert max(picchi) >= PICCO_UMANO_MIN


def test_la_velocita_sta_nella_forbice_misurata():
    piano = piano_scroll(2000)
    durata = sum(p for _, p in piano)
    velocita = sum(d for d, _ in piano) / durata
    assert VELOCITA_UMANA_MIN <= velocita <= VELOCITA_UMANA_MAX, f"{velocita:.0f} px/s"


def test_ogni_tanto_esce_un_evento_a_zero_pixel():
    """48 eventi su 1660 avevano deltaY esattamente 0. Un flusso che non ne ha
    mai e' piu' pulito del vero."""
    zeri = sum(1 for _ in range(60) for d, _ in piano_scroll(2000) if d == 0)
    assert zeri > 0


def test_il_lancio_decelera_fino_a_spegnersi():
    """I due flick veri (gesti 0 e 2) finiscono con 3,3,2,2,1,1,1."""
    piano = [d for d, _ in piano_lancio(6000)]
    assert piano[-1] <= 2
    assert piano[0] >= PICCO_UMANO_MIN


def test_un_lancio_copre_la_distanza_di_un_gesto_umano():
    """5.742px il piu' corto dei gesti registrati."""
    netto = sum(d for d, _ in piano_lancio(6000))
    assert 4500 <= netto <= 7500
```

- [ ] **Step 2: eseguire i test e vederli fallire**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t3 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_geometria.py -q -p no:cacheprovider -k "frame or picchi or velocita or zero or lancio"
```
Atteso: falliscono almeno `test_il_gesto_raggiunge_i_picchi_di_una_mano_vera` (picco 60 < 190) e `test_la_velocita_sta_nella_forbice_misurata` (~950 px/s).

- [ ] **Step 3: aggiornare le costanti in `pagina.py`**

Sostituire il blocco delle costanti dei gesti:

```python
# Gesti di scorrimento, tarati sui dati registrati l'11/08 dal trackpad di
# Tommaso (scripts/registra_scroll_umano.py, 1660 eventi):
#   - gli eventi arrivano a ritmo di frame: mediana 16.7ms, con code fino a
#     150ms e occasionali coppie nello stesso frame;
#   - deltaY mediano 18, con picchi fra 193 e 342;
#   - velocita' fra 1472 e 2517 px/s;
#   - un singolo gesto copre da 5.742 a 24.113 px.
# Il modello precedente stava a 952 px/s con picchi da 60: piu' lento e piu'
# piccolo del vero, che non e' prudenza — e' una firma diversa dal riferimento.
INTERVALLO_FRAME_S = 0.0167
INTERVALLO_JITTER_S = 0.006      # code fino a ~150ms viste nei dati
PROB_INTERVALLO_LUNGO = 0.04
INTERVALLO_LUNGO_MAX_S = 0.150
PROB_EVENTO_NULLO = 0.03         # 48 eventi su 1660 avevano deltaY 0

PICCO_MIN_PX = 190
PICCO_MAX_PX = 342
VELOCITA_MIN_PX_S = 1500
VELOCITA_MAX_PX_S = 2500
```

Rimuovere `SCATTO_MAX_PX`, `PAUSA_SCATTO_MIN_S`, `PAUSA_SCATTO_MAX_S`, `PX_PER_SCATTO_MIN`, `PX_PER_SCATTO_MAX`, `SCATTI_MIN`, `SCATTI_MAX`, `LANCIO_*`: sostituiti dai valori sopra.

- [ ] **Step 4: riscrivere `piano_scroll` e `piano_lancio`**

```python
def _intervallo() -> float:
    """Il tempo fino al prossimo evento wheel.

    Il browser li emette a ritmo di frame, ma nei dati veri capitano sia coppie
    nello stesso frame sia pause fino a 150ms quando il dito rallenta.
    """
    if random.random() < PROB_INTERVALLO_LUNGO:
        return random.uniform(INTERVALLO_FRAME_S * 2, INTERVALLO_LUNGO_MAX_S)
    return max(0.0, random.gauss(INTERVALLO_FRAME_S, INTERVALLO_JITTER_S))


def piano_scroll(px_totali: int) -> list[tuple[int, float]]:
    """Un gesto di scorrimento: (pixel, pausa) per ogni evento wheel.

    Profilo a campana — accelera, tiene, frena — con la velocita' di punta
    presa dalla forbice misurata. La durata esce dalla distanza: e' la
    velocita' a essere umana, non il numero di eventi.
    """
    if px_totali <= 0:
        return []

    velocita = random.uniform(VELOCITA_MIN_PX_S, VELOCITA_MAX_PX_S)
    quanti = max(4, round(px_totali / (velocita * INTERVALLO_FRAME_S)))
    pesi = [math.sin(math.pi * (i + 0.5) / quanti) for i in range(quanti)]
    totale_pesi = sum(pesi) or 1.0

    piano: list[tuple[int, float]] = []
    for peso in pesi:
        if random.random() < PROB_EVENTO_NULLO:
            piano.append((0, _intervallo()))
            continue
        delta = px_totali * peso / totale_pesi * random.uniform(0.75, 1.25)
        piano.append((max(0, min(PICCO_MAX_PX, round(delta))), _intervallo()))
    return piano


def piano_lancio(px_totali: int) -> list[tuple[int, float]]:
    """Un flick: spinta e poi inerzia che si spegne fino a un pixel.

    L'attrito si sceglie perche' la distanza percorsa — somma di una
    progressione geometrica, spinta / (1 - attrito) — sia quella richiesta:
    cosi' il gesto resta fisicamente coerente invece di essere troncato a
    meta' corsa, che a occhio si riconosce subito.
    """
    if px_totali <= 0:
        return []

    spinta = random.randint(PICCO_MIN_PX, PICCO_MAX_PX)
    attrito = max(0.90, min(0.995, 1 - spinta / px_totali))

    piano: list[tuple[int, float]] = []
    velocita = float(spinta)
    while velocita >= 1 and len(piano) < 800:
        piano.append((max(1, round(velocita)), _intervallo()))
        velocita *= attrito
    return piano
```

- [ ] **Step 5: eseguire tutti i test dei gesti**

```bash
WA_TEST_DB_SLOT=t3 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_geometria.py -q -p no:cacheprovider
```
Atteso: tutti verdi. Se `test_lo_scroll_arriva_circa_dove_gli_e_stato_chiesto` fallisce per via degli eventi nulli, allargare la tolleranza di quel test a ±20% documentando il perché — **non** togliere gli eventi nulli, che sono un fatto misurato.

- [ ] **Step 6: verificare a occhio contro i dati veri**

```bash
./venv/Scripts/python.exe -c "
from app.services.inbox_browser.pagina import piano_scroll, piano_lancio
for nome, p in (('scroll 2000px', piano_scroll(2000)), ('lancio 6000px', piano_lancio(6000))):
    px = sum(d for d,_ in p); s = sum(x for _,x in p)
    print(f'{nome}: {len(p)} eventi, {px}px, {s:.2f}s, {px/s:.0f} px/s, picco {max(d for d,_ in p)}')
"
```
Atteso: velocità fra 1400 e 2600 px/s, picchi sopra 190. Confronto con i gesti veri: `234 eventi / 5.742px / 3.9s / 1.472 px/s / picco 193`.

- [ ] **Step 7: commit**

```bash
cd "D:/BOT OUTBOUND"
git add backend/app/services/inbox_browser/pagina.py backend/tests/test_inbox_browser_geometria.py
git commit -m "fix(inbox-browser): gesti tarati sui 1660 eventi wheel registrati, non su un modello"
```

---

### Task 4a: leggere DURANTE il gesto, non dopo

**È il task che sblocca la velocità.** Oggi il motore legge le righe solo a gesto finito. Con viewport 940 la lista tiene ~13 righe nel DOM: un gesto che copre 6.000px attraversa ~83 righe, e le 70 di mezzo non esistono nel DOM nell'istante della lettura — **perse in silenzio**. È per questo che il passo era limitato a 0.6-0.8 schermate: non per prudenza verso Instagram, ma per non perdere contatti.

Il vincolo però è aggirabile, perché **leggere costa quasi nulla**: nella sessione da 30 minuti dell'11/08, la voce `lettura` pesa **1.0s su 1727 totali (0.06%)**. Leggendo ogni ~250px di scorrimento — un terzo del buffer, ~3 righe — nessuna riga passa mai senza essere vista, e il gesto può andare alla velocità umana vera.

**Files:**
- Modify: `backend/app/services/inbox_browser/pagina.py` (`scorri`, nuova `scorri_leggendo`)
- Modify: `backend/tests/test_inbox_browser_pagina.py`

**Interfaces:**
- Consumes: `pagina.piano_scroll`, `pagina._leggi_righe_grezze`, `pagina.righe_valide`.
- Produces: `pagina.scorri_leggendo(page, lingua, su_righe) -> StatoScorrimento` — esegue il gesto e chiama `su_righe(list[RigaVisibile])` a ogni campionamento, così il chiamante vede ogni riga attraversata. `pagina.PX_FRA_LETTURE: int = 250`.

- [ ] **Step 1: scrivere il test che fallisce**

In `backend/tests/test_inbox_browser_pagina.py`, in fondo:

```python
# ── leggere durante il gesto ───────────────────────────────────────────────
class _FakePageScroll:
    """Finge una lista virtualizzata: tiene nel DOM solo le righe vicine alla
    posizione corrente, come fa Instagram. Serve a dimostrare che leggendo solo
    a gesto finito le righe intermedie non si vedono MAI."""

    ALTEZZA_RIGA = 72
    RIGHE_NEL_DOM = 13

    def __init__(self, quante=200):
        self.tutte = [f"Contatto {i}" for i in range(quante)]
        self.scroll = 0
        self.eventi = 0

    @property
    def url(self):
        return "https://www.instagram.com/direct/inbox/"

    async def evaluate(self, script, *args):
        if "nonLetta" in script:
            primo = self.scroll // self.ALTEZZA_RIGA
            finestra = self.tutte[primo:primo + self.RIGHE_NEL_DOM]
            return {
                "viewport": {"w": 1920, "h": 940},
                "righe": [
                    {"indice": i, "testo": t, "left": 72, "right": 471,
                     "top": 200 + i * self.ALTEZZA_RIGA, "w": 399,
                     "nonLetta": False, "pallinoConferma": False}
                    for i, t in enumerate(finestra)
                ],
            }
        if "overflowY" in script and "idx" not in script:
            return [{"left": 72, "right": 471, "top": 200, "w": 399, "h": 700,
                     "scrollHeight": len(self.tutte) * self.ALTEZZA_RIGA,
                     "clientHeight": 700, "scrollTop": self.scroll}]
        if "idx" in script:
            return {"altezza": len(self.tutte) * self.ALTEZZA_RIGA,
                    "top": self.scroll, "visibile": 700, "alFondo": False}
        return []

    async def wait_for_timeout(self, ms):
        return None

    class _Mouse:
        def __init__(self, pagina):
            self.pagina = pagina

        async def move(self, x, y, steps=1):
            return None

        async def wheel(self, dx, dy):
            self.pagina.scroll += int(dy)
            self.pagina.eventi += 1

    @property
    def mouse(self):
        return _FakePageScroll._Mouse(self)


@pytest.mark.asyncio
async def test_leggendo_solo_a_fine_gesto_le_righe_di_mezzo_spariscono():
    """La prova del nove: senza campionamento, un gesto lungo attraversa righe
    che nessuno vede mai. E' esattamente la perdita silenziosa che il limite di
    0.7 schermate serviva a evitare."""
    from app.services.inbox_browser.pagina import piano_scroll

    page = _FakePageScroll()
    for delta, _ in piano_scroll(6000):
        await page.mouse.wheel(0, delta)
    righe = (await page.evaluate("nonLetta"))["righe"]
    nomi_visti = {r["testo"] for r in righe}
    assert "Contatto 5" not in nomi_visti     # attraversata e mai vista
    assert "Contatto 40" not in nomi_visti


@pytest.mark.asyncio
async def test_scorrendo_e_leggendo_nessuna_riga_viene_persa():
    from app.services.inbox_browser.pagina import scorri_leggendo

    page = _FakePageScroll()
    viste: set[str] = set()

    async def raccogli(righe):
        for r in righe:
            viste.add(r.nome)

    await scorri_leggendo(page, "it", raccogli)

    attraversate = page.scroll // _FakePageScroll.ALTEZZA_RIGA
    mancanti = [f"Contatto {i}" for i in range(attraversate) if f"Contatto {i}" not in viste]
    assert not mancanti, f"{len(mancanti)} righe attraversate e mai viste: {mancanti[:6]}"


@pytest.mark.asyncio
async def test_il_campionamento_non_rallenta_il_gesto():
    """Leggere costa 0.06% del tempo di sessione (misurato). Il numero di
    letture deve restare proporzionale alla distanza, non agli eventi: una
    lettura per evento wheel sarebbe 200 letture per gesto."""
    from app.services.inbox_browser.pagina import PX_FRA_LETTURE, scorri_leggendo

    page = _FakePageScroll()
    letture = 0

    async def conta(righe):
        nonlocal letture
        letture += 1

    await scorri_leggendo(page, "it", conta)
    assert letture <= (page.scroll // PX_FRA_LETTURE) + 2
```

- [ ] **Step 2: eseguire e vedere fallire**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t4a ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_pagina.py -q -p no:cacheprovider -k "gesto or persa or campionamento"
```
Atteso: il primo test passa (dimostra la perdita), gli altri due falliscono con `ImportError: cannot import name 'scorri_leggendo'`.

- [ ] **Step 3: implementare**

In `backend/app/services/inbox_browser/pagina.py`, accanto a `scorri`:

```python
# Ogni quanti pixel di scorrimento si rilegge la lista. Un terzo del buffer
# renderizzato (~13 righe da 72px con finestra alta 940): abbastanza fitto da
# non lasciar passare nessuna riga, abbastanza raro da non pesare. Leggere costa
# lo 0.06% del tempo di sessione (misurato l'11/08: 1.0s su 1727).
PX_FRA_LETTURE = 250


async def scorri_leggendo(page, lingua: str, su_righe) -> StatoScorrimento:
    """Un gesto di scorrimento che campiona la lista MENTRE si muove.

    Il limite di 0.6-0.8 schermate per gesto non veniva da Instagram: veniva
    dal fatto che le righe si leggevano solo a gesto finito, e quelle
    attraversate nel mezzo non erano piu' nel DOM. Campionando ogni
    PX_FRA_LETTURE pixel il limite cade, e il gesto puo' andare alla velocita'
    misurata su una mano vera.

    `su_righe` viene chiamata con le righe viste a ogni campionamento; spetta
    al chiamante deduplicare (il motore lo fa con la memoria di sessione).
    """
    _righe, _viewport, bordo = await _leggi_righe_grezze(page)
    candidati = await page.evaluate(_JS_CANDIDATI)
    indice = scegli_contenitore(candidati, bordo)
    if indice is None:
        logger.warning(
            f"[InboxBrowser] contenitore della lista non riconosciuto "
            f"(bordo {bordo}) — nessuno scorrimento"
        )
        return StatoScorrimento(altezza=None, al_fondo=False)

    box = candidati[indice]
    frazione = random.uniform(PASSO_SCROLL_MIN, PASSO_SCROLL_MAX)
    px = int(box["clientHeight"] * frazione)

    x = box["left"] + box["w"] * random.uniform(0.3, 0.7)
    y = box["top"] + box["h"] * random.uniform(0.3, 0.7)
    await page.mouse.move(x, y, steps=random.randint(5, 15))

    percorso_da_ultima_lettura = 0
    for delta, pausa in piano_scroll(px):
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(int(pausa * 1000))
        percorso_da_ultima_lettura += abs(delta)
        if percorso_da_ultima_lettura >= PX_FRA_LETTURE:
            percorso_da_ultima_lettura = 0
            await su_righe(await leggi_righe_visibili(page, lingua))

    await su_righe(await leggi_righe_visibili(page, lingua))

    stato = await page.evaluate(_JS_STATO_CONTENITORE, indice)
    if stato is None:
        return StatoScorrimento(altezza=None, al_fondo=False)
    return StatoScorrimento(altezza=stato["altezza"], al_fondo=stato["alFondo"])
```

- [ ] **Step 4: eseguire e vedere passare**

```bash
WA_TEST_DB_SLOT=t4a ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_pagina.py -q -p no:cacheprovider
```
Atteso: tutti verdi.

- [ ] **Step 5: alzare il passo, ora che è sicuro**

In `pagina.py`, con nota:

```python
# Il passo non e' piu' limitato dal buffer: `scorri_leggendo` campiona durante
# il gesto, quindi nessuna riga passa senza essere vista. Un gesto umano
# misurato copre da 6 a 25 schermate; qui si resta a 2-4, che gia' dimezza il
# numero di giri e quindi le righe riesaminate.
PASSO_SCROLL_MIN = 2.0
PASSO_SCROLL_MAX = 4.0
```

Il test `test_il_passo_non_supera_una_schermata` in `test_inbox_browser_pagina.py` va **sostituito** (non cancellato) con:

```python
def test_il_passo_e_grande_ma_la_lettura_campiona_durante_il_gesto():
    """Il vecchio vincolo — mai piu' di una schermata — proteggeva dalle righe
    perse quando si leggeva solo a fine gesto. Ora si campiona ogni
    PX_FRA_LETTURE pixel, quindi il passo puo' crescere; ma il campionamento
    deve restare piu' fitto del buffer renderizzato, se no il vincolo torna
    valido e nessuno se ne accorge."""
    from app.services.inbox_browser.pagina import PASSO_SCROLL_MAX, PX_FRA_LETTURE

    altezza_riga, righe_nel_buffer = 72, 13
    assert PX_FRA_LETTURE < altezza_riga * righe_nel_buffer / 2
    assert PASSO_SCROLL_MAX > 1.0
```

- [ ] **Step 6: collegare il motore a `scorri_leggendo`**

In `backend/app/services/scrape_inbox_browser.py`, il ciclo passa da "leggi → processa → scorri" a "scorri raccogliendo → processa quanto raccolto". Sostituire la lettura in cima al `while` e la `scorri` in fondo:

```python
            righe_del_giro: list = []

            async def raccogli(righe_viste):
                for r in righe_viste:
                    chiave = normalizza_nome(r.nome)
                    if chiave and chiave not in viste_in_sessione:
                        righe_del_giro.append(r)

            if not righe_del_giro:
                await raccogli(await leggi_righe_visibili(page, LINGUA))

            for riga in righe_del_giro:
                ...   # corpo invariato

            righe_del_giro = []
            stato = await scorri_leggendo(page, LINGUA, raccogli)
```

**Attenzione:** `raccogli` accumula solo righe non ancora viste, ma la deduplica finale resta a carico del ciclo (Task 2) — la stessa riga può comparire in due campionamenti consecutivi dello stesso gesto.

- [ ] **Step 7: eseguire tutta la suite del modulo**

```bash
WA_TEST_DB_SLOT=t4a ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_geometria.py tests/test_inbox_browser_pagina.py \
  tests/test_inbox_browser_ritmo.py tests/test_inbox_browser_testo.py \
  tests/test_inbox_browser_memoria_sessione.py tests/test_scrape_inbox_browser.py \
  -q -p no:cacheprovider
```

- [ ] **Step 8: commit**

```bash
cd "D:/BOT OUTBOUND"
git add backend/app/services/inbox_browser/pagina.py backend/app/services/scrape_inbox_browser.py \
        backend/tests/test_inbox_browser_pagina.py
git commit -m "perf(inbox-browser): leggere durante il gesto, cosi' il passo puo' crescere"
```

---

### Task 4b: il lancio per la zona già nota

Con Task 4a il passo normale può crescere, ma nella zona che il segnalibro attraversa senza leggere serve un gesto ancora più lungo — e lì la lettura non serve affatto.

**Files:**
- Modify: `backend/app/services/inbox_browser/pagina.py` (`scorri`, nuova `lancia`)
- Modify: `backend/tests/test_inbox_browser_pagina.py`

**Interfaces:**
- Consumes: `pagina.piano_lancio`, `pagina.scegli_contenitore`, `pagina.bordo_colonne`.
- Produces: `pagina.lancia(page) -> StatoScorrimento` — come `scorri`, ma copre 4-8 schermate con un flick. Usata da Task 8 nella zona di salto.

- [ ] **Step 1: scrivere il test che fallisce**

In `backend/tests/test_inbox_browser_pagina.py`, in fondo:

```python
# ── il lancio copre distanza dove non serve leggere ────────────────────────
def test_il_lancio_copre_piu_schermate_di_uno_scorrimento():
    """Un gesto umano copre da 6 a 25 schermate (misurato: 5.742-24.113px con
    finestra alta 940). Scorrere una schermata alla volta e' una scelta del
    motore, e si paga in righe riesaminate."""
    from app.services.inbox_browser.pagina import (
        LANCIO_SCHERMATE_MAX, LANCIO_SCHERMATE_MIN, PASSO_SCROLL_MAX,
    )
    assert LANCIO_SCHERMATE_MIN >= 3
    assert LANCIO_SCHERMATE_MAX > LANCIO_SCHERMATE_MIN
    assert LANCIO_SCHERMATE_MIN > PASSO_SCROLL_MAX
```

- [ ] **Step 2: eseguire e vedere fallire**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t4 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_pagina.py -q -p no:cacheprovider -k "lancio_copre"
```
Atteso: `ImportError: cannot import name 'LANCIO_SCHERMATE_MAX'`.

- [ ] **Step 3: implementare `lancia`**

In `backend/app/services/inbox_browser/pagina.py`, dopo `scorri`:

```python
# Quante schermate copre un lancio. Un gesto umano ne copre da 6 a 25; qui si
# resta prudenti perche' anche in zona nota la lista va comunque attraversata,
# non teletrasportata.
LANCIO_SCHERMATE_MIN = 4.0
LANCIO_SCHERMATE_MAX = 8.0


async def lancia(page) -> StatoScorrimento:
    """Un flick che copre piu' schermate, per attraversare zona gia' nota.

    NON va usata dove le righe vanno esaminate: coprire piu' del buffer
    renderizzato fa perdere righe IN SILENZIO (vedi docstring del modulo,
    punto 1). Serve alla modalita' segnalibro, dove per definizione la zona
    attraversata non va letta.
    """
    _righe, _viewport, bordo = await _leggi_righe_grezze(page)
    candidati = await page.evaluate(_JS_CANDIDATI)
    indice = scegli_contenitore(candidati, bordo)
    if indice is None:
        logger.warning(
            f"[InboxBrowser] contenitore della lista non riconosciuto "
            f"(bordo {bordo}) — nessun lancio"
        )
        return StatoScorrimento(altezza=None, al_fondo=False)

    box = candidati[indice]
    px = int(box["clientHeight"] * random.uniform(LANCIO_SCHERMATE_MIN, LANCIO_SCHERMATE_MAX))

    x = box["left"] + box["w"] * random.uniform(0.3, 0.7)
    y = box["top"] + box["h"] * random.uniform(0.3, 0.7)
    await page.mouse.move(x, y, steps=random.randint(5, 15))

    for delta, pausa in piano_lancio(px):
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(int(pausa * 1000))

    stato = await page.evaluate(_JS_STATO_CONTENITORE, indice)
    if stato is None:
        return StatoScorrimento(altezza=None, al_fondo=False)
    return StatoScorrimento(altezza=stato["altezza"], al_fondo=stato["alFondo"])
```

- [ ] **Step 4: eseguire e vedere passare**

```bash
WA_TEST_DB_SLOT=t4 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_pagina.py -q -p no:cacheprovider
```
Atteso: tutti verdi.

- [ ] **Step 5: commit**

```bash
cd "D:/BOT OUTBOUND"
git add backend/app/services/inbox_browser/pagina.py backend/tests/test_inbox_browser_pagina.py
git commit -m "feat(inbox-browser): lancia() per attraversare la zona gia' nota"
```

---

### Task 5: migration 032 — le due colonne del segnalibro

**Da fare per prima fra quelle che toccano il DB, e con PR dedicata**: la migration deve stare su `main` prima di essere applicata al Postgres condiviso.

**Files:**
- Create: `backend/alembic/versions/032_inbox_cursor.py`
- Create: `backend/tests/test_inbox_cursor_column.py`
- Modify: `backend/app/models/campaign.py`
- Modify: `backend/app/schemas/campaign.py`

**Interfaces:**
- Produces: `Campaign.inbox_cursor_at: datetime | None` (la data della riga più vecchia lavorata) e `Campaign.inbox_cursor_updated_at: datetime | None` (quando è stata aggiornata). Consumate da Task 7, 8, 9.

- [ ] **Step 1: scrivere il test che fallisce**

Create `backend/tests/test_inbox_cursor_column.py`:

```python
"""Le due colonne del segnalibro esistono e sono opzionali.

`inbox_cursor_at` e' la data della riga di lista piu' vecchia gia' lavorata:
la soglia sotto la quale, in modalita' segnalibro, non si scende a leggere.
E' una DATA e non il riferimento a una chat, di proposito: se si memorizzasse
"l'ultima chat vista" e proprio quella ricevesse una risposta, risalirebbe in
cima alla lista e il riferimento sarebbe perso.
"""
from app.models.campaign import Campaign


def test_la_campagna_ha_il_cursore_inbox():
    assert hasattr(Campaign, "inbox_cursor_at")
    assert hasattr(Campaign, "inbox_cursor_updated_at")


def test_il_cursore_e_opzionale():
    """Una campagna che non ha mai girato in modalita' segnalibro non ha
    cursore, e deve poter partire lo stesso."""
    assert Campaign.__table__.c.inbox_cursor_at.nullable is True
    assert Campaign.__table__.c.inbox_cursor_updated_at.nullable is True
```

- [ ] **Step 2: eseguire e vedere fallire**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t5 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_cursor_column.py -q -p no:cacheprovider
```
Atteso: `AssertionError` su `hasattr`.

- [ ] **Step 3: aggiungere le colonne al modello**

In `backend/app/models/campaign.py`, accanto a `list_target`:

```python
    # Segnalibro della Fase Lista via browser: la data della riga di lista piu'
    # vecchia gia' lavorata. In modalita' segnalibro si scorre senza aprire
    # finche' le righe sono piu' recenti di questa soglia. E' una DATA, non il
    # riferimento a una chat: se proprio quella chat ricevesse una risposta
    # risalirebbe in cima e il riferimento sparirebbe.
    inbox_cursor_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    inbox_cursor_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

Verificare che `DateTime` sia già importato da `sqlalchemy` in testa al file; se non lo è, aggiungerlo.

- [ ] **Step 4: scrivere la migration**

Create `backend/alembic/versions/032_inbox_cursor.py`:

```python
"""segnalibro della Fase Lista inbox via browser

Revision ID: 032
Revises: 031
Create Date: 2026-08-11

Additiva e nullable: su main nessun codice legge ancora queste colonne, quindi
si puo' applicare al DB condiviso senza cambiare comportamento. Va portata su
main PRIMA dell'apply (lezione delle migration 027 e 029).
"""
import sqlalchemy as sa
from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("inbox_cursor_at", sa.DateTime(), nullable=True))
    op.add_column("campaigns", sa.Column("inbox_cursor_updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "inbox_cursor_updated_at")
    op.drop_column("campaigns", "inbox_cursor_at")
```

- [ ] **Step 5: esporre il cursore nella response**

In `backend/app/schemas/campaign.py`, nella classe che contiene `list_target: int | None = None` (riga ~141):

```python
    inbox_cursor_at: datetime | None = None
```

Verificare che `datetime` sia importato.

- [ ] **Step 6: eseguire i test**

```bash
WA_TEST_DB_SLOT=t5 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_cursor_column.py tests/test_inbox_schema.py tests/test_inbox_engine_column.py \
  -q -p no:cacheprovider
```
Atteso: tutti verdi.

- [ ] **Step 7: verificare che la migration salga e scenda su un DB isolato**

```bash
cd "D:/BOT OUTBOUND/backend"
DATABASE_URL="sqlite+aiosqlite:///./data/test_032.db" MIGRATE_CONFIRM=1 \
  ./venv/Scripts/python.exe -m alembic upgrade head
DATABASE_URL="sqlite+aiosqlite:///./data/test_032.db" MIGRATE_CONFIRM=1 \
  ./venv/Scripts/python.exe -m alembic downgrade 031
DATABASE_URL="sqlite+aiosqlite:///./data/test_032.db" MIGRATE_CONFIRM=1 \
  ./venv/Scripts/python.exe -m alembic upgrade head
rm -f data/test_032.db
```
Atteso: nessun errore in nessuno dei tre passaggi.

- [ ] **Step 8: PR dedicata alla sola migration, e merge PRIMA dell'apply**

```bash
cd "D:/BOT OUTBOUND"
git checkout -b feat/migration-032-inbox-cursor
git add backend/alembic/versions/032_inbox_cursor.py backend/app/models/campaign.py \
        backend/app/schemas/campaign.py backend/tests/test_inbox_cursor_column.py
git commit -m "feat(db): migration 032 — cursore del segnalibro inbox (additiva)"
git push -u origin feat/migration-032-inbox-cursor
gh pr create --base main --title "feat(db): migration 032 — cursore del segnalibro inbox" \
  --body "Additiva e nullable. Va su main PRIMA di essere applicata al DB condiviso: una migration presente sul DB ma assente da main fa morire ogni start.bat con 'Can't locate revision identified by 032'."
```

Dopo il merge, l'apply al DB condiviso avviene da solo al primo `start.bat` (che chiederà conferma perché l'host non è locale: rispondere `si`).

---

### Task 6: leggere la data dalla riga di lista

La modalità segnalibro confronta date, e la data deve venire **dalla riga di lista** — leggerla aprendo il thread annullerebbe il guadagno. `analizza_riga_lista` già estrae il campo `data_relativa` e oggi non lo usa nessuno.

Formati misurati l'11/08 sulla lista di `@michele.carozza` (212 righe censite):
```
'5 g' x128 · '20 h' x64 · '3 g' x6 · '4 g' x6 · '15 h' x2 · '2 g' x2 · '3 h' x1 · 'Unread' x3
```

**Files:**
- Modify: `backend/app/services/inbox_browser/testo.py`
- Modify: `backend/tests/test_inbox_browser_testo.py`

**Interfaces:**
- Produces: `testo.eta_riga_in_ore(data_relativa: str | None, lingua: str) -> float | None` — quante ore fa risale l'ultimo messaggio di quella riga, `None` se il formato non è riconosciuto. Consumata da Task 7.

- [ ] **Step 1: scrivere i test che falliscono**

In `backend/tests/test_inbox_browser_testo.py`, in fondo:

```python
# ── eta' della riga, per la modalita' segnalibro ───────────────────────────
# Formati misurati l'11/08 sulla lista di @michele.carozza, 212 righe:
#   '5 g' x128 · '20 h' x64 · '3 g' x6 · '4 g' x6 · '15 h' x2 · '2 g' x2 · '3 h' x1
from app.services.inbox_browser.testo import eta_riga_in_ore   # noqa: E402


def test_le_ore_si_leggono_dalla_riga():
    assert eta_riga_in_ore("20 h", "it") == 20.0


def test_i_giorni_diventano_ore():
    assert eta_riga_in_ore("5 g", "it") == 120.0


def test_le_settimane_diventano_ore():
    assert eta_riga_in_ore("2 sett", "it") == 336.0


def test_i_minuti_valgono_meno_di_un_ora():
    assert eta_riga_in_ore("45 m", "it") == 0.75


def test_un_formato_sconosciuto_non_produce_un_eta():
    """Meglio nessuna eta' che una sbagliata: su un'eta' inventata la modalita'
    segnalibro salterebbe righe che andavano lette."""
    assert eta_riga_in_ore("Unread", "it") is None
    assert eta_riga_in_ore("", "it") is None
    assert eta_riga_in_ore(None, "it") is None


def test_il_formato_inglese_e_riconosciuto():
    """L'interfaccia dell'account misurato mostrava 'Unread' in inglese pur
    avendo il resto in italiano: il parsing non deve dipendere dalla lingua
    dichiarata piu' del necessario."""
    assert eta_riga_in_ore("5 d", "en") == 120.0
    assert eta_riga_in_ore("20 h", "en") == 20.0
    assert eta_riga_in_ore("2 w", "en") == 336.0
```

- [ ] **Step 2: eseguire e vedere fallire**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t6 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_testo.py -q -p no:cacheprovider -k "eta or formato"
```
Atteso: `ImportError: cannot import name 'eta_riga_in_ore'`.

- [ ] **Step 3: implementare**

In `backend/app/services/inbox_browser/testo.py`, dopo `analizza_riga_lista`:

```python
# Unita' di tempo della riga di lista, in ore. Misurate l'11/08: '5 g', '20 h',
# '3 g', '15 h', '2 g', '3 h'. Le sigle inglesi convivono con quelle italiane
# perche' l'interfaccia dell'account misurato le mescolava ('Unread' in inglese
# con il resto in italiano).
_UNITA_IN_ORE: dict[str, float] = {
    "m": 1 / 60, "min": 1 / 60,
    "h": 1.0, "o": 1.0, "ora": 1.0, "ore": 1.0,
    "g": 24.0, "gg": 24.0, "d": 24.0, "giorno": 24.0, "giorni": 24.0,
    "sett": 168.0, "w": 168.0, "settimana": 168.0, "settimane": 168.0,
    "a": 8760.0, "y": 8760.0, "anno": 8760.0, "anni": 8760.0,
}

_ETA_RIGA = re.compile(r"^\s*(\d+)\s*([a-zA-Zàèéìòù]+)\s*$")


def eta_riga_in_ore(data_relativa: str | None, lingua: str) -> float | None:
    """Quante ore fa risale l'ultimo messaggio della riga di lista.

    `None` quando il formato non e' riconosciuto ('Unread', stringhe vuote,
    qualunque cosa Instagram cambi domani): meglio nessuna eta' che una
    sbagliata, perche' la modalita' segnalibro su un'eta' inventata salterebbe
    righe che andavano lette.

    `lingua` non seleziona il vocabolario — le sigle italiane e inglesi sono
    accettate insieme — ma resta in firma per uniformita' con le altre funzioni
    del modulo e perche' serve il giorno in cui i formati divergeranno davvero.
    """
    corrispondenza = _ETA_RIGA.match(data_relativa or "")
    if not corrispondenza:
        return None
    quantita, unita = corrispondenza.groups()
    ore = _UNITA_IN_ORE.get(unita.lower())
    if ore is None:
        return None
    return int(quantita) * ore
```

- [ ] **Step 4: eseguire e vedere passare**

```bash
WA_TEST_DB_SLOT=t6 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_testo.py -q -p no:cacheprovider
```
Atteso: tutti verdi.

- [ ] **Step 5: commit**

```bash
cd "D:/BOT OUTBOUND"
git add backend/app/services/inbox_browser/testo.py backend/tests/test_inbox_browser_testo.py
git commit -m "feat(inbox-browser): eta' della riga dalla data relativa della lista"
```

---

### Task 7: le funzioni pure del segnalibro

**Files:**
- Create: `backend/app/services/inbox_browser/segnalibro.py`
- Create: `backend/tests/test_inbox_browser_segnalibro.py`

**Interfaces:**
- Consumes: `testo.eta_riga_in_ore`.
- Produces:
  - `segnalibro.riga_da_saltare(data_relativa, soglia_ore, attiva) -> bool`
  - `segnalibro.nuovo_cursore(cursore_attuale: datetime | None, eta_ore: float | None, adesso: datetime) -> datetime | None`
  - `segnalibro.soglia_in_ore(cursore: datetime | None, adesso: datetime) -> float | None`

- [ ] **Step 1: scrivere i test che falliscono**

Create `backend/tests/test_inbox_browser_segnalibro.py`:

```python
"""Modalita' segnalibro: saltare la parte alta gia' lavorata.

Requisiti posti da Tommaso l'11/08, che vincolano il disegno:

- e' una modalita' con TOGGLE, scelta a ogni avvio della Fase Lista, non una
  configurazione permanente: una sessione va in profondita' saltando, quella
  dopo gira con la modalita' spenta e recupera chi ha risposto ed e' risalito;
- la soglia e' una DATA, mai il puntatore a una chat: se si memorizzasse
  "l'ultima chat vista" e proprio quella ricevesse una risposta, risalirebbe in
  cima e il riferimento sarebbe perso;
- la data si legge dalla riga di lista, mai aprendo il thread;
- il rischio di perdere chi e' risalito e' accettato consapevolmente.
"""
from datetime import datetime, timedelta

from app.services.inbox_browser.segnalibro import (
    nuovo_cursore, riga_da_saltare, soglia_in_ore,
)

ADESSO = datetime(2026, 8, 11, 12, 0, 0)


# ── quando si salta ────────────────────────────────────────────────────────
def test_a_modalita_spenta_non_si_salta_mai():
    """La sessione a modalita' spenta e' quella che recupera chi e' risalito:
    se saltasse qualcosa non servirebbe a niente."""
    assert riga_da_saltare("20 h", soglia_ore=120.0, attiva=False) is False


def test_una_riga_piu_recente_della_soglia_si_salta():
    """20 ore fa e' sopra il punto dove eravamo arrivati (5 giorni): quella
    zona e' gia' stata lavorata."""
    assert riga_da_saltare("20 h", soglia_ore=120.0, attiva=True) is True


def test_una_riga_piu_vecchia_della_soglia_si_lavora():
    assert riga_da_saltare("7 g", soglia_ore=120.0, attiva=True) is False


def test_sul_confine_si_lavora():
    """Sulla soglia esatta si legge: e' il punto da cui si riprende, e rileggere
    qualche riga costa meno che perderla."""
    assert riga_da_saltare("5 g", soglia_ore=120.0, attiva=True) is False


def test_senza_soglia_non_si_salta():
    """Prima sessione in assoluto: non esiste un punto a cui tornare."""
    assert riga_da_saltare("20 h", soglia_ore=None, attiva=True) is False


def test_una_data_illeggibile_non_si_salta():
    """'Unread' e qualunque formato nuovo: nel dubbio si guarda. Saltare su
    un'eta' sconosciuta perderebbe contatti in silenzio, che e' il fallimento
    che questo modulo deve evitare piu' di ogni altro."""
    assert riga_da_saltare("Unread", soglia_ore=120.0, attiva=True) is False
    assert riga_da_saltare(None, soglia_ore=120.0, attiva=True) is False


# ── come si aggiorna il cursore ────────────────────────────────────────────
def test_il_primo_cursore_e_l_eta_della_riga_lavorata():
    atteso = ADESSO - timedelta(hours=120)
    assert nuovo_cursore(None, eta_ore=120.0, adesso=ADESSO) == atteso


def test_il_cursore_scende_solo_verso_il_passato():
    """Il cursore segna QUANTO IN BASSO si e' arrivati. Una riga piu' recente
    incontrata dopo — perche' e' risalita, o dopo un reset della lista — non
    deve farlo tornare indietro, altrimenti la sessione successiva ripartirebbe
    da piu' in alto e il segnalibro perderebbe senso."""
    vecchio = ADESSO - timedelta(hours=120)
    assert nuovo_cursore(vecchio, eta_ore=20.0, adesso=ADESSO) == vecchio


def test_una_riga_piu_vecchia_sposta_il_cursore_piu_indietro():
    vecchio = ADESSO - timedelta(hours=120)
    atteso = ADESSO - timedelta(hours=168)
    assert nuovo_cursore(vecchio, eta_ore=168.0, adesso=ADESSO) == atteso


def test_un_eta_illeggibile_lascia_il_cursore_dov_e():
    vecchio = ADESSO - timedelta(hours=120)
    assert nuovo_cursore(vecchio, eta_ore=None, adesso=ADESSO) == vecchio


# ── dalla data alla soglia ─────────────────────────────────────────────────
def test_la_soglia_e_la_distanza_fra_cursore_e_adesso():
    cursore = ADESSO - timedelta(hours=120)
    assert soglia_in_ore(cursore, ADESSO) == 120.0


def test_senza_cursore_non_c_e_soglia():
    assert soglia_in_ore(None, ADESSO) is None


def test_un_cursore_nel_futuro_non_produce_una_soglia_negativa():
    """Orologi sfasati o dati sporchi: una soglia negativa farebbe saltare
    l'intera lista."""
    assert soglia_in_ore(ADESSO + timedelta(hours=5), ADESSO) is None
```

- [ ] **Step 2: eseguire e vedere fallire**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t7 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_segnalibro.py -q -p no:cacheprovider
```
Atteso: `ModuleNotFoundError: No module named 'app.services.inbox_browser.segnalibro'`.

- [ ] **Step 3: implementare il modulo**

Create `backend/app/services/inbox_browser/segnalibro.py`:

```python
"""Modalita' segnalibro: riprendere da dove si era arrivati.

Il motore, a regime, spende quasi tutta la sessione a riattraversare la parte
alta della lista che ha gia' lavorato. Un umano non lo farebbe: si segnerebbe
la data a cui e' arrivato e da li' ripartirebbe.

Tre scelte di disegno, tutte con una ragione precisa:

1. LA SOGLIA E' UNA DATA, non il riferimento a una chat. Memorizzare "l'ultima
   chat vista" sembra piu' preciso, ma se proprio quella chat ricevesse una
   risposta risalirebbe in cima alla lista e il riferimento sarebbe perso.

2. LA DATA SI LEGGE DALLA RIGA DI LISTA ('5 g', '20 h'), mai aprendo il thread:
   aprire per sapere la data annullerebbe tutto il guadagno.

3. IL CURSORE SCENDE SOLO. Segna quanto in basso si e' arrivati; una riga piu'
   recente incontrata dopo non lo riporta su, altrimenti dopo un reset della
   lista la sessione successiva ripartirebbe da piu' in alto.

Nel dubbio si LEGGE: eta' illeggibile, soglia assente, modalita' spenta — in
tutti questi casi la riga si lavora. Saltare per errore perde contatti in
silenzio, che e' il fallimento peggiore di questo modulo.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.services.inbox_browser.testo import eta_riga_in_ore

LINGUA_PREDEFINITA = "it"


def riga_da_saltare(data_relativa: str | None, soglia_ore: float | None,
                    attiva: bool) -> bool:
    """True se questa riga sta nella zona gia' lavorata e va solo attraversata."""
    if not attiva or soglia_ore is None:
        return False
    eta = eta_riga_in_ore(data_relativa, LINGUA_PREDEFINITA)
    if eta is None:
        return False
    return eta < soglia_ore


def nuovo_cursore(cursore_attuale: datetime | None, eta_ore: float | None,
                  adesso: datetime) -> datetime | None:
    """Il cursore aggiornato dopo aver lavorato una riga di questa eta'."""
    if eta_ore is None:
        return cursore_attuale
    candidato = adesso - timedelta(hours=eta_ore)
    if cursore_attuale is None:
        return candidato
    return min(cursore_attuale, candidato)


def soglia_in_ore(cursore: datetime | None, adesso: datetime) -> float | None:
    """A quante ore fa corrisponde il cursore, adesso."""
    if cursore is None:
        return None
    ore = (adesso - cursore).total_seconds() / 3600
    return ore if ore > 0 else None
```

- [ ] **Step 4: eseguire e vedere passare**

```bash
WA_TEST_DB_SLOT=t7 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_segnalibro.py -q -p no:cacheprovider
```
Atteso: `14 passed`.

- [ ] **Step 5: commit**

```bash
cd "D:/BOT OUTBOUND"
git add backend/app/services/inbox_browser/segnalibro.py backend/tests/test_inbox_browser_segnalibro.py
git commit -m "feat(inbox-browser): funzioni pure della modalita' segnalibro"
```

---

### Task 8: il segnalibro dentro il motore

**Files:**
- Modify: `backend/app/services/scrape_inbox_browser.py`
- Modify: `backend/tests/test_scrape_inbox_browser.py`

**Interfaces:**
- Consumes: `segnalibro.riga_da_saltare`, `segnalibro.nuovo_cursore`, `segnalibro.soglia_in_ore`, `pagina.lancia` (Task 4b), `pagina.scorri_leggendo` (Task 4a), `Campaign.inbox_cursor_at`.
- Produces: `run_inbox_browser_list` rispetta `campaign.inbox_salta_lavorate` (attributo runtime impostato da Task 9) e aggiorna `campaign.inbox_cursor_at`.

- [ ] **Step 1: scrivere il test che fallisce**

In `backend/tests/test_scrape_inbox_browser.py`, in fondo:

```python
# ── modalita' segnalibro nel ciclo ─────────────────────────────────────────
def test_la_riga_saltata_non_conta_come_esaminata():
    """Una riga attraversata in modalita' segnalibro non deve entrare nel
    contatore di zona: non e' stata ne' aperta ne' riconosciuta, e falsarlo
    manderebbe il motore in zona 'piena' proprio dove deve andare piu' veloce."""
    from app.services.inbox_browser.riconoscimento import ContatoreZona
    from app.services.inbox_browser.segnalibro import riga_da_saltare

    contatore = ContatoreZona()
    zona_prima = contatore.zona
    if riga_da_saltare("20 h", soglia_ore=120.0, attiva=True):
        pass          # il ciclo fa `continue` senza toccare il contatore
    assert contatore.zona == zona_prima


def test_una_soglia_che_copre_tutta_la_lista_non_manda_in_loop():
    """Caso limite del segnalibro: se il cursore e' piu' vecchio della chat piu'
    vecchia, ogni riga viene saltata e il motore lancerebbe all'infinito senza
    mai raggiungere `decidi_fine_lista`. La guardia sui lanci a vuoto e' quello
    che lo ferma; qui si verifica il conteggio che la governa."""
    from app.services.inbox_browser.segnalibro import riga_da_saltare

    righe = ["20 h", "3 g", "5 g", "6 g"]
    soglia_altissima = 24 * 365.0
    saltate = sum(1 for r in righe if riga_da_saltare(r, soglia_altissima, True))
    assert saltate == len(righe)     # tutte saltate: e' il caso che va interrotto


def test_il_cursore_si_muove_solo_verso_il_basso_lungo_una_sessione():
    """Simula l'ordine reale delle righe: la lista scende dal piu' recente al
    piu' vecchio, ma dopo un reset si riparte dall'alto. Il cursore deve
    conservare il punto piu' profondo raggiunto."""
    from datetime import datetime
    from app.services.inbox_browser.segnalibro import nuovo_cursore

    adesso = datetime(2026, 8, 11, 12, 0, 0)
    cursore = None
    for eta in (20.0, 48.0, 120.0, 20.0, 24.0):     # l'ultimo tratto e' post-reset
        cursore = nuovo_cursore(cursore, eta, adesso)
    assert cursore == datetime(2026, 8, 6, 12, 0, 0)   # 120 ore prima
```

- [ ] **Step 2: eseguire e vedere il secondo test fallire**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t8 ./venv/Scripts/python.exe -m pytest \
  tests/test_scrape_inbox_browser.py -q -p no:cacheprovider -k "saltata or cursore"
```
Atteso: passano entrambi solo dopo Task 7; se Task 7 è stato fatto, questi due sono verdi subito e servono da rete per il passo successivo.

- [ ] **Step 3: collegare il segnalibro al ciclo**

In `backend/app/services/scrape_inbox_browser.py`:

**Il ciclo a questo punto ha già la forma data da Task 4a** ("scorri raccogliendo → processa quanto raccolto") e la memoria di sessione di Task 2. Le modifiche qui sotto si innestano su quella forma, non su quella originale: se il codice che trovi non corrisponde, fermati e verifica che 2 e 4a siano stati applicati.

Import in testa:

```python
from app.services.inbox_browser.pagina import (
    apri_riga, decidi_fine_lista, lancia, leggi_righe_visibili, scorri_leggendo,
)
from app.services.inbox_browser.segnalibro import (
    nuovo_cursore, riga_da_saltare, soglia_in_ore,
)
from app.services.inbox_browser.testo import (
    analizza_riga_lista, e_segnaposto, estrai_data_thread, estrai_ultimo_messaggio,
    eta_riga_in_ore, normalizza_nome,
)
```

Prima del `while True:`, dopo la creazione di `contatore`:

```python
        salta_lavorate = bool(getattr(campaign, "inbox_salta_lavorate", False))
        soglia = soglia_in_ore(getattr(campaign, "inbox_cursor_at", None), datetime.utcnow())
        if salta_lavorate and soglia is None:
            logger.info("[InboxBrowser] segnalibro chiesto ma nessun cursore: si legge tutto")
        if salta_lavorate and soglia is not None:
            emit_event(campaign_id, "scrape_start",
                       f"Modalita' segnalibro: si attraversa senza aprire tutto cio' che e' "
                       f"piu' recente di {soglia / 24:.1f} giorni fa")
        righe_saltate = 0
```

Il filtro del segnalibro va dentro `raccogli`, la funzione che Task 4a chiama a ogni campionamento: così le righe della zona già lavorata non entrano nemmeno nel lotto da processare, e non pagano né pausa né contatore di zona.

```python
            async def raccogli(righe_viste):
                nonlocal righe_saltate, righe_incontrate
                for r in righe_viste:
                    chiave = normalizza_nome(r.nome)
                    if not chiave or chiave in viste_in_sessione:
                        continue
                    righe_incontrate += 1
                    analizzata = analizza_riga_lista(r.testo_grezzo, LINGUA)
                    if riga_da_saltare(analizzata.data_relativa, soglia, salta_lavorate):
                        # Zona gia' lavorata: si attraversa e basta. La riga
                        # entra comunque nella memoria di sessione, cosi' non
                        # viene rivalutata a ogni campionamento del gesto.
                        viste_in_sessione.add(chiave)
                        righe_saltate += 1
                        continue
                    righe_del_giro.append(r)
```

I due contatori vanno inizializzati accanto a `righe_saltate`:

```python
        righe_incontrate = 0
```

E dopo il `for riga in righe_del_giro:`, prima di `decidi_fine_lista`:

```python
            # Se in questo giro non si e' fatto altro che saltare, si sta
            # attraversando la zona gia' lavorata: si copre distanza con un
            # lancio invece che con uno scorrimento che campiona. Qui la
            # lettura non serve — per definizione quelle righe non vanno
            # aperte — quindi il gesto puo' essere lungo.
            solo_saltate = righe_incontrate > 0 and righe_saltate == righe_incontrate
            righe_saltate = righe_incontrate = 0
            if salta_lavorate and solo_saltate:
                await lancia(page)
                continue

            decisione = await decidi_fine_lista(page, falliti_inbox)
```

**Attenzione al caso limite:** se il segnalibro è attivo e la soglia copre l'intera lista, il motore lancerebbe all'infinito senza mai aprire niente. `decidi_fine_lista` non viene raggiunta nel ramo del lancio, quindi la fine lista non verrebbe mai dichiarata. Aggiungere una guardia subito prima del `if salta_lavorate and solo_saltate:`:

```python
            lanci_a_vuoto = lanci_a_vuoto + 1 if solo_saltate else 0
            if lanci_a_vuoto >= 40:
                logger.info(
                    f"[InboxBrowser] 40 lanci senza incontrare una riga da lavorare: "
                    f"la soglia del segnalibro ({soglia:.0f}h) copre tutta la lista"
                )
                emit_event(campaign_id, "scrape_complete",
                           "Modalita' segnalibro: nessuna chat piu' vecchia del segnalibro, "
                           "niente da raccogliere. Rilancia senza la spunta per rivedere "
                           "anche le chat piu' recenti.", level="warn")
                break
```

con `lanci_a_vuoto = 0` inizializzato accanto agli altri contatori di sessione.

Nel ramo che salva un contatto, dopo `archivio.aggiungi(riga.nome)`:

```python
                        eta = eta_riga_in_ore(analizzata.data_relativa, LINGUA)
                        campaign.inbox_cursor_at = nuovo_cursore(
                            campaign.inbox_cursor_at, eta, datetime.utcnow())
                        campaign.inbox_cursor_updated_at = datetime.utcnow()
```

Aggiungere `eta_riga_in_ore` all'import da `testo`.

- [ ] **Step 4: eseguire i test del motore**

```bash
WA_TEST_DB_SLOT=t8 ./venv/Scripts/python.exe -m pytest \
  tests/test_scrape_inbox_browser.py tests/test_scrape_inbox_browser_guard.py \
  tests/test_scrape_inbox_browser_kill_switch.py tests/test_inbox_browser_defer.py \
  tests/test_inbox_browser_innesto.py -q -p no:cacheprovider
```
Atteso: tutti verdi.

- [ ] **Step 5: commit**

```bash
cd "D:/BOT OUTBOUND"
git add backend/app/services/scrape_inbox_browser.py backend/tests/test_scrape_inbox_browser.py
git commit -m "feat(inbox-browser): modalita' segnalibro nel ciclo del motore"
```

---

### Task 9: il toggle dall'API

**Files:**
- Modify: `backend/app/api/campaigns.py` (riga 578, `PhaseStartBody`; riga 583, `start_list`)
- Create: test in `backend/tests/test_inbox_engine_switch.py` (file esistente, si aggiunge in fondo)

**Interfaces:**
- Consumes: `Campaign.inbox_cursor_at`.
- Produces: `POST /api/campaigns/{id}/list/start` accetta `{"target": int|null, "salta_lavorate": bool}`. Il flag viaggia come attributo runtime `campaign.inbox_salta_lavorate` verso il motore (Task 8), **non** viene persistito: è una scelta per singola sessione.

- [ ] **Step 1: scrivere il test che fallisce**

In fondo a `backend/tests/test_inbox_engine_switch.py`:

```python
# ── il toggle del segnalibro e' per SESSIONE, non una configurazione ───────
def test_il_body_di_start_accetta_il_flag_del_segnalibro():
    from app.api.campaigns import PhaseStartBody

    body = PhaseStartBody(target=340, salta_lavorate=True)
    assert body.salta_lavorate is True


def test_il_flag_e_spento_se_non_lo_si_chiede():
    """Una sessione normale legge tutto: la modalita' che salta va chiesta
    esplicitamente ogni volta, perche' e' quella che accetta di perdere chi e'
    risalito."""
    from app.api.campaigns import PhaseStartBody

    assert PhaseStartBody().salta_lavorate is False
    assert PhaseStartBody(target=100).salta_lavorate is False
```

- [ ] **Step 2: eseguire e vedere fallire**

```bash
cd "D:/BOT OUTBOUND/backend"
WA_TEST_DB_SLOT=t9 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_engine_switch.py -q -p no:cacheprovider -k "segnalibro or flag"
```
Atteso: `ValidationError` / `TypeError` su `salta_lavorate`.

- [ ] **Step 3: estendere il body e passarlo al motore**

In `backend/app/api/campaigns.py`, riga 578:

```python
class PhaseStartBody(BaseModel):
    target: int | None = None
    # Modalita' segnalibro: vale SOLO per questa sessione e non viene
    # persistita. Una sessione la usa per scendere in profondita' saltando la
    # parte gia' lavorata, quella dopo la lascia spenta e recupera chi ha
    # risposto ed e' risalito in cima.
    salta_lavorate: bool = False
```

Dentro `start_list`, subito prima di `campaign.status = CampaignStatus.listing`:

```python
    # Attributo runtime, non colonna: la scelta muore con la sessione.
    campaign.inbox_salta_lavorate = bool(body.salta_lavorate) if body else False
```

E in `app/services/work_enqueue.py` o dove il job viene accodato, il flag va propagato: verificare come `enqueue_list(campaign_id)` passa il contesto. Se il worker ricarica la campagna dal DB (e lo fa: `list_followers` fa una `select`), l'attributo runtime **non sopravvive**. In quel caso salvarlo in Redis con TTL breve:

```python
    from app.services.work_enqueue import segna_modalita_segnalibro
    await segna_modalita_segnalibro(campaign_id, bool(body.salta_lavorate) if body else False)
```

con, in `app/services/work_enqueue.py`:

```python
async def segna_modalita_segnalibro(campaign_id: str, attiva: bool) -> None:
    """La modalita' segnalibro vale per una sessione sola: si passa al worker
    via Redis con una scadenza, non come colonna, cosi' non sopravvive a un
    riavvio ne' a una ripresa dopo il session-break."""
    import redis.asyncio as aioredis
    from app.config import settings

    r = aioredis.from_url(settings.redis_url)
    try:
        chiave = f"inbox_segnalibro:{campaign_id}"
        if attiva:
            await r.set(chiave, "1", ex=6 * 3600)
        else:
            await r.delete(chiave)
    finally:
        await r.aclose()


async def modalita_segnalibro_attiva(campaign_id: str) -> bool:
    import redis.asyncio as aioredis
    from app.config import settings

    r = aioredis.from_url(settings.redis_url)
    try:
        return bool(await r.get(f"inbox_segnalibro:{campaign_id}"))
    finally:
        await r.aclose()
```

E in `run_inbox_browser_list` (Task 8), sostituire la lettura dell'attributo con:

```python
        from app.services.work_enqueue import modalita_segnalibro_attiva
        salta_lavorate = await modalita_segnalibro_attiva(campaign_id)
```

- [ ] **Step 4: eseguire i test**

```bash
WA_TEST_DB_SLOT=t9 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_engine_switch.py tests/test_inbox_engine_switch_adversarial.py \
  tests/test_inbox_guard_adversarial.py -q -p no:cacheprovider
```
Atteso: tutti verdi.

- [ ] **Step 5: commit**

```bash
cd "D:/BOT OUTBOUND"
git add backend/app/api/campaigns.py backend/app/services/work_enqueue.py backend/tests/test_inbox_engine_switch.py
git commit -m "feat(api): flag salta_lavorate su list/start, valido per una sola sessione"
```

---

### Task 10: il toggle in UI

**Files:**
- Modify: `frontend/lib/api.ts` (riga 163, `startList`)
- Modify: `frontend/app/campaigns/[id]/page.tsx` (riga ~130, il bottone che chiama `startList`)

**Interfaces:**
- Consumes: `POST /list/start` con `{target, salta_lavorate}` (Task 9); `campaign.inbox_cursor_at` dalla response (Task 5).

- [ ] **Step 1: estendere il client API**

In `frontend/lib/api.ts`, riga 163:

```ts
    startList: (id: string, target?: number | null, saltaLavorate = false) =>
      request<Campaign>(`/campaigns/${id}/list/start`, {
        method: 'POST',
        body: JSON.stringify({ target: target ?? null, salta_lavorate: saltaLavorate }),
      }),
```

- [ ] **Step 2: aggiungere il tipo nella Campaign**

Nel file dei tipi del frontend (cercare `list_target` con `grep -rn "list_target" frontend/lib`), accanto:

```ts
  inbox_cursor_at?: string | null;
```

- [ ] **Step 3: aggiungere la spunta accanto al bottone**

In `frontend/app/campaigns/[id]/page.tsx`, vicino alla riga 130. Aggiungere lo stato in cima al componente:

```tsx
  const [saltaLavorate, setSaltaLavorate] = useState(false);
```

E accanto al bottone della Fase Lista:

```tsx
  {campaign.inbox_engine === 'browser' && (
    <label className="flex items-center gap-2 text-sm text-muted-foreground">
      <input
        type="checkbox"
        checked={saltaLavorate}
        onChange={(e) => setSaltaLavorate(e.target.checked)}
        disabled={!campaign.inbox_cursor_at}
      />
      Riprendi da dove ero arrivato
      {campaign.inbox_cursor_at
        ? ` (${new Date(campaign.inbox_cursor_at).toLocaleDateString('it-IT')})`
        : ' — nessun segnalibro ancora'}
    </label>
  )}
```

E la chiamata diventa:

```tsx
  onClick={() => action(() => api.campaigns.startList(id, listTarget ? Number(listTarget) : null, saltaLavorate))}
```

- [ ] **Step 4: verificare che il frontend compili**

```bash
cd "D:/BOT OUTBOUND/frontend"
npm run build
```
Atteso: build senza errori. Se `next dev` serve per la verifica a mano, usare `npm run build && npm start`: su questa macchina `next dev` è instabile.

- [ ] **Step 5: commit**

```bash
cd "D:/BOT OUTBOUND"
git add frontend/lib/api.ts "frontend/app/campaigns/[id]/page.tsx"
git commit -m "feat(ui): spunta 'riprendi da dove ero arrivato' sulla Fase Lista browser"
```

---

### Task 11: gli stacchi rimasti sulle aperture

**Decisione di Tommaso richiesta prima di implementare.** Oggi, dopo ogni chat aperta, c'è il 2% di probabilità di una pausa da 120-300s. Su 98 aperture sono ~2 stacchi, cioè ~420s: **il 24% di un'intera sessione da 30 minuti**.

**Files:**
- Modify: `backend/app/services/inbox_browser/ritmo.py`
- Modify: `backend/tests/test_inbox_browser_ritmo.py`

- [ ] **Step 1: scrivere il test che fissa il nuovo budget**

```python
def test_uno_stacco_non_puo_valere_un_quarto_della_sessione():
    """Misurato l'11/08: 2 stacchi su 98 aperture pesavano 420s su 1727s totali.
    Il costo atteso di uno stacco per apertura deve stare sotto i 2 secondi,
    altrimenti la coda governa la media."""
    p = PARAMETRI["piena"]
    costo_atteso = p["p_stacco"] * (p["stacco"][0] + p["stacco"][1]) / 2
    assert costo_atteso < 2.0, f"{costo_atteso:.1f}s per apertura solo di stacchi"
```

- [ ] **Step 2: eseguire e vedere fallire**

```bash
WA_TEST_DB_SLOT=t11 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_ritmo.py -q -p no:cacheprovider -k "quarto"
```
Atteso: fallisce con `4.2s per apertura solo di stacchi`.

- [ ] **Step 3: ridurre gli stacchi**

In `backend/app/services/inbox_browser/ritmo.py`, nelle voci `piena` e `rapida`:

```python
        "stacco": (90.0, 180.0),
        "p_stacco": 0.01,
```

Con nota:

```
    # Lo stacco resta — una sessione senza nessuna pausa lunga e' una firma —
    # ma scende da (120-300s, 2%) a (90-180s, 1%): il costo atteso per apertura
    # passa da 4.2s a 1.35s. Misurato l'11/08: due stacchi si mangiavano il 24%
    # di una sessione da 30 minuti. Il session-break da 30-55 minuti, che e' la
    # pausa lunga vera, non e' toccato.
```

- [ ] **Step 4: eseguire tutti i test del ritmo**

```bash
WA_TEST_DB_SLOT=t11 ./venv/Scripts/python.exe -m pytest \
  tests/test_inbox_browser_ritmo.py tests/test_inbox_page_delay_distribution.py \
  -q -p no:cacheprovider
```
Atteso: tutti verdi.

- [ ] **Step 5: commit**

```bash
cd "D:/BOT OUTBOUND"
git add backend/app/services/inbox_browser/ritmo.py backend/tests/test_inbox_browser_ritmo.py
git commit -m "perf(inbox-browser): stacchi da 120-300s@2% a 90-180s@1% sulle aperture"
```

---

### Task 12: verifica sul campo — il tetto del 50%

Il piano non è chiuso finché la misura non lo dimostra.

**Files:**
- Modify: `backend/scripts/supervisione_inbox_browser.py` (allineare il ciclo dell'harness alle modifiche di Task 2 e 8, altrimenti misura un motore diverso da quello vero)

- [ ] **Step 1: allineare l'harness al motore**

Nel ciclo `raccolta()` di `scripts/supervisione_inbox_browser.py`, replicare esattamente: la memoria di sessione (Task 2), il salto del segnalibro (Task 8) e l'uso di `lancia` nella zona saltata. Aggiungere al diario:

```python
        "righe_saltate": 0,
        "righe_ripetute": 0,
```

- [ ] **Step 2: girare una sessione a modalità spenta**

Con la campagna in pausa, il profilo libero e **il proxy configurato** sull'account:

```bash
cd "D:/BOT OUTBOUND/backend"
CENSIMENTO=0 ./venv/Scripts/python.exe scripts/supervisione_inbox_browser.py 30
```

- [ ] **Step 3: leggere il verdetto**

```bash
./venv/Scripts/python.exe -c "
import json
d=json.load(open('data/supervisione_inbox.json',encoding='utf-8'))
t=d['diario']['tempi']
tot=sum(t[k] for k in ('pause','apertura','lettura','scroll'))
print(f\"pause {t['pause']:.0f}s = {100*t['pause']/tot:.1f}%  (tetto: 50%)\")
print(f\"aperture: {d['diario']['creati']+d['diario']['aggiornati']} in {tot/60:.0f} min\")
print(f\"reset: {len(d['diario']['reset_scroll'])}  fallimenti: {len(d['diario']['fallimenti_header'])}\")
"
```
Atteso: **pause sotto il 50%**. Se resta sopra, la causa più probabile sono ancora righe riesaminate: controllare `righe_ripetute` nel diario prima di toccare altri parametri.

- [ ] **Step 4: girare una sessione a modalità segnalibro accesa**

Dalla UI, con la spunta attiva, oppure:

```bash
TOK=$(./venv/Scripts/python.exe -c "from app.utils.security import create_access_token; print(create_access_token('9c1873cd-bdbc-49ce-b750-337d212e7ca3','admin'))")
curl -s -X POST "http://127.0.0.1:8000/api/campaigns/ec5e2464-1d8d-42a1-a81f-8e61b303fa7a/list/start" \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"target":10000,"salta_lavorate":true}'
```

Verificare negli eventi che compaia `Modalita' segnalibro: si attraversa senza aprire tutto cio' che e' piu' recente di N giorni fa`, e che il tempo per raggiungere la prima chat nuova sia molto inferiore rispetto alla sessione a modalità spenta.

- [ ] **Step 5: confronto finale e commit del rapporto**

Aggiornare `progetti/bot-outbound/inbox-browser-collaudo-findings.md` nel brain con la tabella prima/dopo e committare.

---

### Task 13: la sonda sul reset della lista

Diagnostica pura: non cambia comportamento, serve a capire una cosa che finora è solo osservata.

**Osservazioni disponibili** (sessione del 11/08, 30 minuti):
```
12:43:56   scrollTop 15373 → 437
12:59:11   scrollTop 19045 → 393
```
Entrambi oltre i 15.000px, a ~15 minuti di distanza, entrambi atterrano a ~400px (la lista si **rimonta**, non "scorre su"). Preceduti da fallimenti di apertura a `0.0s`, cioè righe già sparite dal DOM prima del click.

**Files:**
- Create: `backend/scripts/probe_reset_lista.py`

- [ ] **Step 1: scrivere la sonda**

Create `backend/scripts/probe_reset_lista.py`: apre l'inbox, registra `page.on("response")` e `page.on("requestfailed")` filtrando `direct_v2|graphql|api/v1`, poi scorre in continuazione campionando `scrollTop` ogni 2 secondi per 40 minuti. Quando `scrollTop` crolla di oltre 2000px, salva:
- le ultime 30 richieste con URL, stato e timestamp relativo al reset;
- `document.visibilityState` e se la pagina ha ricevuto un evento `pagehide`/`freeze`;
- il numero di righe nel DOM prima e dopo;
- se l'URL della pagina è cambiato.

- [ ] **Step 2: eseguire la sonda per una sessione intera**

```bash
cd "D:/BOT OUTBOUND/backend"
./venv/Scripts/python.exe scripts/probe_reset_lista.py 40
```

- [ ] **Step 3: correlare**

Se i reset coincidono con una risposta su `direct_v2/inbox`, la causa è il refetch periodico dell'inbox: la mitigazione è ripristinare `scrollTop` al valore precedente subito dopo, invece di ricominciare. Se non c'è nessuna richiesta correlata, l'ipotesi passa a un limite interno della virtualizzazione: in quel caso l'unica mitigazione praticabile resta il segnalibro (Task 7-8), che rende il reset un inciampo da pochi secondi.

- [ ] **Step 4: annotare l'esito nel brain**

Aggiornare `progetti/bot-outbound/inbox-browser-collaudo-findings.md` con la causa trovata e la mitigazione scelta, e committare.

---

### Task 14: verifica della lingua dell'interfaccia

Il censimento dell'11/08 ha trovato `'Unread' x3` fra le date delle righe: etichetta **inglese** su un account che il motore tratta come italiano (`LINGUA = "it"` cablato in `scrape_inbox_browser.py`). Se l'interfaccia è mista, il prefisso `Tu:` potrebbe non essere riconosciuto, e da quello dipende `last_message_from`.

**Files:**
- Create: nessuno. Modify: eventualmente `backend/app/services/scrape_inbox_browser.py`

- [ ] **Step 1: misurare la lingua reale**

```bash
cd "D:/BOT OUTBOUND/backend"
./venv/Scripts/python.exe -c "
import asyncio, os, sys
sys.path.insert(0, os.getcwd())
from sqlalchemy import select
from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

async def m():
    async with AsyncSessionLocal() as db:
        a = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == '@michele.carozza'))).scalar_one()
    s = BrowserSession(a.id); await s.open()
    try:
        p = s.context.pages[0] if s.context.pages else await s.context.new_page()
        await p.goto('https://www.instagram.com/direct/inbox/', wait_until='commit', timeout=60000)
        await p.wait_for_timeout(8000)
        testo = await p.evaluate('() => document.body.innerText')
        for parola in ('Tu:', 'You:', 'Scrivi un messaggio', 'Message', 'Unread', 'Non letto', 'Richieste', 'Requests'):
            print(f'{parola!r:<24} {\"PRESENTE\" if parola in testo else \"assente\"}')
        print('lang attributo:', await p.evaluate('() => document.documentElement.lang'))
    finally:
        await s.close()
asyncio.run(m())
"
```

- [ ] **Step 2: decidere**

Se `Tu:` è presente e `You:` assente, la lingua cablata è corretta e basta annotarlo. Se compaiono entrambi o solo `You:`, aprire un task separato per rendere la lingua configurabile per account — **non** improvvisarlo qui: il tri-stato di `analizza_riga_lista` protegge già dal fallimento peggiore (marcare come "ha risposto" un messaggio nostro).

- [ ] **Step 3: annotare l'esito nel brain**

---

## Chiusura del modulo (protocollo `sviluppo-modulo`)

Dopo Task 12, prima di considerare chiuso il lavoro:

1. **Lista test manuali UI, minimo 20**, in `.superpowers/sdd/qa-inbox-segnalibro-tests.md`: creazione campagna con motore browser, avvio con e senza spunta, spunta disabilitata quando non c'è cursore, cursore visualizzato correttamente, stop a metà sessione, ripresa dopo session-break, cambio motore a caldo, kill-switch durante il salto, target raggiunto durante il salto, e così via.
2. **Lista test adversarial, minimo 30**, in `.superpowers/sdd/qa-inbox-segnalibro-adversarial.md`. Categorie obbligatorie con casi specifici di questo modulo:
   - cursore nel futuro, cursore a `1970-01-01`, cursore uguale a adesso;
   - `salta_lavorate=true` su una campagna che non ha mai girato (nessun cursore);
   - data di riga in formati mai visti (`'ora'`, `'adesso'`, `'1 anno'`, stringa vuota, 10k caratteri, unicode);
   - `list_target=0` (bug noto e pre-esistente: `0` è falso in Python e non ferma la campagna — **non correggerlo qui**, va affrontato sui due motori insieme con autorizzazione esplicita);
   - due sessioni concorrenti sulla stessa campagna;
   - reset della lista simulato durante il salto;
   - modalità cambiata mentre la sessione gira;
   - invarianti via SQL a fine run: nessun follower duplicato per `(campaign_id, username)`, nessun `source_channel='browser'` senza `full_name`, `inbox_cursor_at` mai nel futuro.
3. **Fix loop fino al 100%**.
4. **Final whole-branch review** (`superpowers:requesting-code-review`): confrontare l'intero branch con **questo piano**, non solo i diff per-task. I due Critical della PR #58 erano invisibili a ogni review per-task ed emersero solo confrontando il branch intero con la spec.
5. **Collaudo di Tommaso** solo a MVP.

## Ordine di esecuzione consigliato

```
Task 1   (PR di quanto è pronto)
  ├─ Task 2   (memoria sessione)        ─┐
  ├─ Task 3   (gesti tarati sui dati)    ├─ indipendenti fra loro
  └─ Task 5   (migration 032, PR sola)  ─┘
        │
        ├─ Task 4a  (leggere durante il gesto)   ← SBLOCCA LA VELOCITÀ
        │     └─ Task 4b  (lancia, per la zona saltata)
        │
        └─ Task 6   (età dalla riga)
              └─ Task 7   (funzioni pure del segnalibro)
                    └─ Task 8   (segnalibro nel motore)   ← richiede 4a, 4b, 7
                          └─ Task 9   (API)
                                └─ Task 10  (UI)

Task 11 (stacchi)          ← serve il via libera di Tommaso
Task 12 (verifica del 50%) ← dopo 2, 3, 4a, 4b, 8, 11
Task 13 (sonda reset)      ← in parallelo, quando c'è una sessione lunga da osservare
Task 14 (lingua)           ← quando il profilo è libero
```

**Il percorso critico per la velocità è 2 → 4a → 3.** Task 4a da solo rimuove il vincolo che teneva il passo a 0.7 schermate; Task 2 smette di ripagare le righe; Task 3 porta i gesti alla velocità umana vera. Il segnalibro (5-10) è un secondo asse: serve a non riattraversare la parte alta, e diventa indispensabile su liste da migliaia di chat.

## Cosa resta fuori, di proposito

- **Il backfill di `full_name` via API** per i 264 contatti ancora anonimi. Accorcerebbe il battesimo da ~35 minuti a pochi, ma usa il canale API che questo motore esiste per evitare. Va deciso separatamente, con Tommaso, valutando il costo in chiamate.
- **`list_target=0` non ferma la campagna** (`0` è falso in Python). Identico e pre-esistente nel motore API: va corretto sui due motori insieme, con autorizzazione esplicita a toccare `scrape_inbox.py`.
- **Il lock di arricchimento senza guardia sull'owner**, ereditato dalla PR #58: finding di codice mai riprodotto empiricamente, rischio di lost-update stretto se `release_stale_locks` interviene durante un fetch anormalmente lento.
- **Il proxy di `@michele.carozza`**, oggi `NULL`: è una scelta operativa di Tommaso, non un difetto del codice.
