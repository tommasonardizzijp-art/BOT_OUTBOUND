# Ordine di lavoro — avviare lo sviluppo del listing inbox via browser

**Incolla questo prompt in una sessione nuova aperta su `D:\BOT OUTBOUND`.**

---

Esegui il piano `docs/superpowers/plans/2026-08-09-inbox-listing-browser.md`, task per task,
con subagenti **Sonnet**, partendo dal **Task 0**.

Prima di toccare qualunque cosa, leggi in quest'ordine:
1. `docs/superpowers/specs/2026-08-09-inbox-listing-browser-design.md` — il perché di ogni scelta
2. `docs/superpowers/plans/2026-08-09-inbox-listing-browser.md` — il cosa fare, passo per passo
3. `CLAUDE.md` della repo

Il branch `docs/spec-inbox-listing-browser` contiene specifica e piano, **6 commit, nessuna riga
di codice di produzione**. Mergialo su `main` prima di iniziare (è sola documentazione), oppure
lavora a partire da lì.

## Modalità di esecuzione

**REQUIRED SUB-SKILL:** `sviluppo-modulo` (standard di Tommaso) + `superpowers:subagent-driven-development`.

- Worktree isolato, branch dedicato, PR. **Mai push diretto su `main`.**
- Un subagente **implementer** per task, mai due in parallelo sull'implementazione.
- Un subagente **reviewer dedicato** per ogni task: approva il codice.
- Un subagente **QA** dopo ogni funzione: prova che *funzioni davvero*, non che sia scritta bene.
  Sono due ruoli distinti, entrambi obbligatori.
- TDD su ogni task. Typecheck e test verdi prima di ogni commit.

## I subagenti sono Sonnet: cosa cambia

Il piano è scritto per non richiedere inferenze, ma **tre punti hanno bisogno di istruzioni
extra nel prompt del subagente**. Non darli per scontati.

**1. Le fixture dei test.** Diversi task dicono «riusare le fixture esistenti». Un subagente
Sonnet le inventerà se non le trova. Nel prompt del subagente incolla il risultato di:

```bash
cd backend && grep -n "^def \|^async def \|@pytest.fixture" tests/conftest.py
```

e digli esplicitamente: *usa queste fixture, non crearne di nuove, adatta i nomi dei parametri
dei test a queste*.

**2. Il ciclo principale del Task 9.** È l'unico punto del piano descritto come elenco numerato
invece che come codice, perché dipende dall'esito del Task 0. Prima di lanciare quel task,
**scrivi tu il codice del ciclo** basandoti sull'elenco degli undici punti e passalo al
subagente già fatto. Non chiedergli di dedurlo.

**3. La prova del nove.** Ogni task la richiede: rimettere il difetto e verificare che il test
torni rosso. Un subagente Sonnet tende a dichiararla fatta senza eseguirla. Nel prompt chiedi
**l'output testuale del test fallito**, non un riassunto. Se non te lo mostra, non è stata fatta.

## Vincoli non negoziabili

- **Il motore API non si tocca.** A fine lavoro questo comando deve dare **output vuoto**:
  ```bash
  git diff --stat main -- backend/app/services/scrape_inbox.py backend/app/services/inbox_source.py
  ```
  Se un task sembra richiedere di modificarli, **fermati e chiedi**: significa che il disegno
  ha un problema, non che il vincolo va allentato.
- **Mai `element.click()`**: sempre `human_input.human_click`.
- **Solo profili sacrificabili** per qualunque prova che tocchi Instagram. Usa
  `claudio.abbigliamentovincente`. **MAI `@michele.carozza`**: è il profilo importante del
  cliente, ordine esplicito di Tommaso.
- **Una suite pytest alla volta**: sqlite condiviso. Usa `WA_TEST_DB_SLOT=<nome>` per uno slot tuo.
- **Migrazioni prima del codice**: `python -m scripts.migrate`, dopo aver fermato bot e backend
  zombie (un `idle in transaction` blocca gli `ALTER TABLE`).
- **`PLAYWRIGHT_BROWSERS_PATH` non va toccata**: il profilo usa `chromium-1208` su `C:`,
  puntarla su `D:` distrugge il profilo.

## Il Task 0 può fermare tutto, ed è previsto

Il primo task non scrive codice: misura due segnali su cui la specifica ha ancora un'ipotesi.

- **Se le chat non lette non sono distinguibili dalla lista**: fermati e chiedi a Tommaso. Non
  indovinare. Lui ha deciso di aprire solo le chat già lette per non bruciare il badge dei non
  letti, e sbagliare qui significa aprire proprio quelle che voleva preservare.
- **Se le richieste fallite risultano rumore anche ristrette agli endpoint inbox**: adotta la
  regola conservativa già scritta nella spec (altezza ferma + in fondo → fine lista) e
  aggiornala di conseguenza.

In entrambi i casi, **scrivi l'esito nella spec** prima di procedere ai task che lo usano.

## I tre test che valgono più degli altri

Se uno di questi non discrimina davvero, il modulo ha un buco invisibile.

**Task 2 — la targa su due processi.** L'implementazione ovvia userebbe `hash()`, che è
randomizzato per processo: darebbe una scheda duplicata a ogni riavvio del worker. Un test
scritto in modo naturale (due chiamate nello stesso processo) **passerebbe lo stesso**. Il test
deve girare su processi separati.

**Task 3 — il tri-stato sull'autore.** Se l'interfaccia è in inglese e cerchiamo `Tu:`, ogni
chat risulta «ha risposto»: nessun errore, solo dati falsi che poi guidano anche il
comportamento anti-ban. Con la lingua sbagliata la funzione deve rispondere `None` («non lo
so»), **mai** `False`.

**Task 9 — la regola fondante.** Rimettendo «in zona rapida non aprire», il test deve segnalare
5 contatti persi su 5. È il difetto che due revisori indipendenti hanno trovato nel disegno
originale: il motore avrebbe raccolto **zero** contatti a regime, annunciando «nessuna novità».

## Chiusura del modulo

Non è chiuso quando la suite è verde. Serve il protocollo di `sviluppo-modulo` fase 4:
minimo **20 test manuali UI** e minimo **30 adversarial** con criterio di PASS invertito (passa
se il sistema *si difende*), fix loop fino al 100%, review finale del branch.

Il collaudo manuale di Tommaso è **solo a MVP**, non alle milestone intermedie.

## Due cose che restano a Tommaso, non farle tu

- gli username con la chiocciola in DB (`@michele.carozza`, `@5columnbusiness`): se ne occupa lui
- il file `backend/data/test_bot_default.lock`, lasciato da una suite interrotta: finché resta,
  le suite sullo slot predefinito falliscono in collection
