# WhatsApp M4 — Reply watcher + opt-out — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **REQUIRED SUB-SKILL (Tommaso's standard):** superpowers:sviluppo-modulo (`D:\workspace-clone-second-brain\.claude\skills\sviluppo-modulo\SKILL.md`). Worktree isolato gia' creato (`D:\BOT OUTBOUND\.worktrees\feat-whatsapp-m4-reply-optout`, branch `feat/whatsapp-m4-reply-optout`). Implementer + reviewer dedicato per task, MAI subagent in parallelo sull'implementazione. QA agent dopo ogni task. Chiusura modulo: 20+ test manuali, 30+ adversarial, fix loop al 100%, poi review finale — collaudo di Tommaso solo a MVP (M5), non qui.

**Goal:** Il canale WhatsApp rileva le risposte dei contatti dalla lista chat (senza mai aprirle) e applica l'opt-out end-to-end, chiudendo il ciclo di M0-M3: invio -> risposta rilevata -> `replied` terminale; STOP -> DNC permanente, nessun ricontatto da nessuna campagna.

**Architecture:** Un nuovo modulo `app/services/wa_reply_watcher.py` orchestrato da un nuovo cron `wa_reply_scan` dentro `cron_worker.py` esistente (stesso processo ARQ del health-check, non un processo a se'). Riusa il POM gia' pronto (`WhatsAppWebPage.scan_chat_list()`, patrimonio M1, non si tocca) e la logica di opt-out gia' scritta da M3 (`wa_optout.looks_like_stop` / `persist_wa_optout`, esplicitamente condivisa per contratto). L'unico pezzo di infrastruttura davvero nuovo e' un lucchetto Redis per-profilo (`wa_profile_lock.py`) che sostituisce il controllo "chiedo in giro" (`_wa_send_job_is_active`) gia' in uso da M3 per evitare due Chromium sullo stesso profilo: tre consumatori oggi (invio, health-check, reply-scan) prendono lo stesso lock invece di conoscersi a vicenda a coppie.

**Tech Stack:** FastAPI/ARQ/SQLAlchemy async (esistenti), Redis via `arq.create_pool` (pattern gia' in uso, `app/services/work_enqueue.py` e `wa_number_manager.py`), pytest + `fakeredis` per gli unit test del lock, un test di integrazione contro Redis reale (skip se non raggiungibile, stesso pattern di `test_wa_cron.py::_redis_o_skip`).

## Global Constraints

- **Nessuna migrazione**: `wa_inbound_events`, `wa_contacts.chat_title/last_replied_at`, `wa_campaign_contacts.replied_at_step`, l'enum `WaMatchedBy` sono gia' nello schema (migrazione 025, M1). Verificato leggendo `backend/app/models/wa.py` prima di scrivere questo piano.
- **File patrimonio M1, MAI toccati**: `app/browser/whatsapp_page.py`, `app/browser/whatsapp_selectors.py`, `app/services/wa_session.py`, `app/utils/phone_pseudonym.py`. `scan_chat_list()` esiste gia' e basta cosi' com'e'.
- **`wa_reply_watcher` non apre MAI le chat**: solo `pom.scan_chat_list()` (sidebar). Nessuna chiamata a metodi che aprono una riga di conversazione. Vincolo di coesistenza (SDD §9) — violarlo marca "letto" e brucia le notifiche del cliente sul telefono.
- **Contatori a DB in SQL, mai read-modify-write** (contratto §4.2): `UPDATE wa_campaigns SET col = col + 1 WHERE id = :id`, mai leggere-sommare-riscrivere in Python.
- **Mai indovinare il matching**: title ambiguo (>=2 contatti stesso `chat_title` nel tenant) -> matching per quel title disabilitato, evento non associato + skip. Preferire un miss a un falso match.
- **Env var nuove, nomi esatti**: `WA_PROFILE_LOCK_TTL_MIN` (default `45`), `WA_LOCK_BUSY_RETRY_S` (default `90`). Nessun'altra var nuova: si riusano `WA_STOP_WORDS`, `WA_ACTIVE_HOURS`, `WA_GLOBAL_DAILY_CAP` non serve qui.
- **`WA_SEND_ENABLED` non gate il reply-watcher**: legge soltanto, e l'opt-out deve restare accurato anche a invio spento. Il solo gate e' `bot_state_service.is_wa_halted()` (kill-switch di canale), come per l'invio.
- **Branch/worktree**: gia' creato da `main` post-merge-PR#28 (`feat/whatsapp-m4-reply-optout`). Mai commit nel worktree di M3 (`D:\BOT OUTBOUND\.worktrees\feat-whatsapp-m3-invio`).
- **Emendamento al contratto gia' scritto** (commit `2fcdf7e` su questo branch): `wa_campaign_contacts.status=replied` ha DUE scrittori legittimi (guardia pre-invio M3 a chat aperta, reply-watcher M4 a chat chiusa) — stessa ridondanza intenzionale gia' in uso per `opted_out`. Non e' un conflitto da risolvere nel codice, e' by design.
- **Nota per la review di PR #28** (non-bloccante per questo piano, da segnalare a Tommaso separatamente): `wa_sender._esito_guardia_negativa`, ramo `ha_risposto` (riga 483-490), marca `cc.status = replied` ma non chiama `_incrementa_contatore_campagna(db, campaign.id, "replied")` — a differenza del ramo `optout` che la chiama. Il Task 8 di questo piano incrementa il contatore dal lato watcher; se il gap lato guardia non viene fixato in #28, `wa_campaigns.replied` resta sotto-contato per le risposte trovate dalla guardia (non per quelle trovate dal watcher).

---

### Task 1: `wa_profile_lock` — lucchetto Redis per-profilo

**Files:**
- Create: `backend/app/services/wa_profile_lock.py`
- Test: `backend/tests/test_wa_profile_lock.py`

**Interfaces:**
- Produces: `class WaProfileBusy(Exception)`; `async def held(number_id: str, *, ttl_min: int | None = None)` — async context manager, prova ad acquisire UNA VOLTA (nessuna attesa/poll), solleva `WaProfileBusy` se occupato, rilascia sempre in `finally` (solo se il possessore e' ancora lui — confronto per token, non un `DELETE` incondizionato).

- [ ] **Step 1: Scrivi il test che fallisce — acquisizione e rilascio base**

```python
# backend/tests/test_wa_profile_lock.py
import pytest
import fakeredis.aioredis

from app.services import wa_profile_lock


@pytest.fixture
def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis()

    async def _fake_pool():
        return client

    monkeypatch.setattr(wa_profile_lock.arq, "create_pool", lambda *_a, **_k: _fake_pool())
    return client


@pytest.mark.asyncio
async def test_held_acquisisce_e_rilascia(fake_redis):
    async with wa_profile_lock.held("num-1"):
        assert await fake_redis.exists("wa:profile-lock:num-1")
    assert not await fake_redis.exists("wa:profile-lock:num-1")


@pytest.mark.asyncio
async def test_held_solleva_se_gia_occupato(fake_redis):
    async with wa_profile_lock.held("num-1"):
        with pytest.raises(wa_profile_lock.WaProfileBusy):
            async with wa_profile_lock.held("num-1"):
                pass


@pytest.mark.asyncio
async def test_held_non_rilascia_lock_altrui_scaduto(fake_redis):
    """Se il TTL e' scaduto e un altro possessore ha gia' preso il lock,
    l'uscita del primo `held` NON deve cancellare il lock del secondo --
    e' il motivo per cui si confronta un token, non un DELETE incondizionato."""
    await fake_redis.set("wa:profile-lock:num-1", "token-vecchio", ex=1)
    async with wa_profile_lock.held("num-1") as token_nuovo:
        assert token_nuovo != "token-vecchio"
        current = await fake_redis.get("wa:profile-lock:num-1")
        assert current.decode() == token_nuovo
    assert not await fake_redis.exists("wa:profile-lock:num-1")
```

- [ ] **Step 2: Esegui e verifica che fallisca**

Run: `cd backend && pytest tests/test_wa_profile_lock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.wa_profile_lock'` (o `ImportError`, il modulo non esiste ancora). Se `fakeredis` non e' installato: `pip install fakeredis` prima (verifica in `requirements.txt`/`pyproject.toml` se serve aggiungerlo).

- [ ] **Step 3: Implementazione minima**

```python
# backend/app/services/wa_profile_lock.py
"""Lucchetto Redis per profilo Chromium WA: tre consumatori (invio M3,
health-check, reply-scan M4) possono voler aprire lo STESSO profilo
Chromium nello stesso momento. Chromium impedirebbe da solo un secondo
avvio concorrente (SingletonLock), ma `_open_wa_browser` (M1, frozen)
cancella quel file ad ogni avvio come pulizia da crash precedenti --
quindi il guardiano OS sparisce e serve un lock applicativo esplicito.

Un TENTATIVO SOLO, mai un'attesa: nessuno dei tre consumatori deve
bloccarsi dentro un job ARQ (lezione "mai sleep lunghi in job",
browser_bio/wa_worker). Se occupato, il chiamante decide (skip per un
cron, Retry breve per un job).

Token, non DELETE incondizionato: se il TTL scade mentre il possessore
originale e' ancora vivo (sessione piu' lunga del previsto) e un secondo
processo acquisisce nel frattempo, il rilascio del primo NON deve
cancellare il lock del secondo. Rischio residuo accettato (nessun Lua
script in questo repo, nessun altro lock ce l'ha): la finestra fra
GET e DELETE e' minuscola e il caso -- TTL scaduto E secondo acquirente
nella stessa manciata di millisecondi -- non e' mai stato osservato per
i lock TTL gia' in uso (wa_number_manager.apply_wa_cooldown)."""
import uuid
from contextlib import asynccontextmanager

import arq
from loguru import logger

from app.config import settings
from app.services.work_enqueue import arq_redis_settings


class WaProfileBusy(Exception):
    """Il profilo e' gia' in uso da un altro consumatore (invio/health-check/scan)."""


def _lock_key(number_id: str) -> str:
    return f"wa:profile-lock:{number_id}"


@asynccontextmanager
async def held(number_id: str, *, ttl_min: int | None = None):
    """Prova UNA VOLTA ad acquisire il lock del profilo `number_id`.
    Solleva WaProfileBusy se occupato. Rilascia in `finally`, solo se il
    valore a Redis e' ancora il TOKEN di questa acquisizione."""
    ttl_s = (ttl_min if ttl_min is not None else settings.wa_profile_lock_ttl_min) * 60
    token = uuid.uuid4().hex
    key = _lock_key(number_id)

    redis = await arq.create_pool(arq_redis_settings())
    try:
        acquired = await redis.set(key, token, nx=True, ex=ttl_s)
        if not acquired:
            raise WaProfileBusy(f"profilo {number_id} gia' in uso")
        try:
            yield token
        finally:
            current = await redis.get(key)
            if current is not None and current.decode() == token:
                await redis.delete(key)
            elif current is not None:
                logger.warning(f"[WA] lock profilo {number_id}: token cambiato "
                               "durante l'uso (TTL scaduto + nuovo possessore) -- "
                               "non rilascio un lock che non e' piu' mio")
    finally:
        await redis.aclose()
```

- [ ] **Step 4: Esegui e verifica che passi**

Run: `cd backend && pytest tests/test_wa_profile_lock.py -v`
Expected: 3 passed

- [ ] **Step 5: Aggiungi `wa_profile_lock_ttl_min` a `app/config.py`**

Modifica `backend/app/config.py`, subito dopo la riga `wa_global_daily_cap: int = 200` (fine blocco "Canale WhatsApp: invio (M3)"):

```python
    wa_global_daily_cap: int = 200              # SDD Q70, safety valve macchina

    # --- Canale WhatsApp: reply-watcher + opt-out (M4) --------------------
    # Lucchetto profilo Chromium: TTL generoso per coprire una mini-sessione
    # di invio nel caso peggiore (wa_session_max_msg=15 * delay mediano
    # lognormale, coda destra inclusa) senza scadere mentre e' ancora in uso.
    wa_profile_lock_ttl_min: int = 45
    # Retry breve quando un job di invio trova il profilo occupato (health-
    # check o reply-scan in corso): non e' la fine-sessione (break_s, minuti-
    # decine), e' "riprova fra un attimo".
    wa_lock_busy_retry_s: int = 90
```

- [ ] **Step 6: Test di integrazione contro Redis reale (mutua esclusione vera)**

Aggiungi in coda a `backend/tests/test_wa_profile_lock.py`, stesso pattern di `test_wa_cron.py::_redis_o_skip`:

```python
@pytest.fixture
async def _redis_o_skip():
    import arq
    from app.services.work_enqueue import arq_redis_settings
    try:
        pool = await arq.create_pool(arq_redis_settings())
        await pool.ping()
        await pool.aclose()
    except Exception:
        pytest.skip("Redis non raggiungibile in questo ambiente")


@pytest.mark.asyncio
async def test_held_vero_contro_redis_reale(_redis_o_skip):
    """Senza monkeypatch: verifica la mutua esclusione vera, non solo la
    logica mockata sopra."""
    from app.services import wa_profile_lock
    number_id = f"lock-test-{uuid.uuid4().hex[:8]}"
    async with wa_profile_lock.held(number_id):
        with pytest.raises(wa_profile_lock.WaProfileBusy):
            async with wa_profile_lock.held(number_id):
                pass
    # rilasciato: una seconda acquisizione ora riesce
    async with wa_profile_lock.held(number_id):
        pass
```

Aggiungi `import uuid` in cima al file test se non gia' presente.

Run: `cd backend && pytest tests/test_wa_profile_lock.py -v`
Expected: 4 passed (o 4 con l'ultimo skipped se Redis non e' in esecuzione in locale — verifica con `redis-cli ping` prima)

- [ ] **Step 7: Commit**

```bash
cd "D:\BOT OUTBOUND\.worktrees\feat-whatsapp-m4-reply-optout"
git add backend/app/services/wa_profile_lock.py backend/app/config.py backend/tests/test_wa_profile_lock.py
git commit -m "feat(wa): lucchetto Redis per-profilo, sostituisce il polling _wa_send_job_is_active"
```

---

### Task 2: Wire del lock nell'invio (`wa_worker.py`)

**Files:**
- Modify: `backend/app/workers/wa_worker.py:186` (apertura browser dentro `esegui_mini_sessione`), `backend/app/workers/wa_worker.py:360-394` (`wa_send_task`, gestione esito)
- Test: `backend/tests/test_wa_worker.py` (nuovi casi)

**Interfaces:**
- Consumes: `wa_profile_lock.held(number_id) -> AsyncContextManager[str]`, `wa_profile_lock.WaProfileBusy`
- Produces: `esito["motivo"] == "profilo_occupato"` come nuovo esito possibile di `esegui_mini_sessione`

- [ ] **Step 1: Scrivi il test che fallisce**

Aggiungi a `backend/tests/test_wa_worker.py` (segui lo stile dei test esistenti in quel file: fixture `db_session`, `monkeypatch` su `_open_wa_browser` per non aprire un browser vero):

```python
@pytest.mark.asyncio
async def test_mini_sessione_salta_se_profilo_occupato(db_session, monkeypatch):
    from app.workers import wa_worker
    from app.services import wa_profile_lock

    monkeypatch.setattr(wa_worker.settings, "wa_send_enabled", True)

    async def _sempre_occupato(number_id, ttl_min=None):
        raise wa_profile_lock.WaProfileBusy(number_id)

    class _CtxOccupato:
        def __call__(self, number_id, ttl_min=None):
            return self

        async def __aenter__(self):
            raise wa_profile_lock.WaProfileBusy(number_id)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wa_worker.wa_profile_lock, "held", _CtxOccupato())

    esito = await wa_worker.esegui_mini_sessione("qualunque-numero")
    assert esito["motivo"] == "profilo_occupato"
    assert esito["inviati"] == 0
```

(Nota per l'implementer: se `db_session`/le fixture del modulo richiedono un `number_id` che esiste davvero a DB per superare il check "numero non attivo" prima del lock, crea il numero con `factories_wa.make_number` come fanno gli altri test del file e passa il suo `id` — il punto del test e' che il lock si controlla PRIMA di entrare nel loop di invio, quindi va acquisito subito dopo il check "numero attivo".)

- [ ] **Step 2: Esegui e verifica che fallisca**

Run: `cd backend && pytest tests/test_wa_worker.py::test_mini_sessione_salta_se_profilo_occupato -v`
Expected: FAIL — `AttributeError: module 'app.workers.wa_worker' has no attribute 'wa_profile_lock'`

- [ ] **Step 3: Implementazione**

In `backend/app/workers/wa_worker.py`, aggiungi l'import in cima (accanto agli altri `from app.services import ...`):

```python
from app.services import wa_profile_lock, wa_sender, wa_timing
```

(sostituisce la riga 19 esistente `from app.services import wa_sender, wa_timing`)

Modifica `esegui_mini_sessione` (righe 182-186), inserendo il lock fra il calcolo di `proxy_url` (gia' letto sopra, riga 168-180) e l'apertura del browser:

```python
    quanti = None          # calcolato dopo il primo claim, sulla campagna vera
    processati = 0
    guasti_consecutivi = 0

    try:
        async with wa_profile_lock.held(number_id):
            async with _open_wa_browser(number_id, headless=True, proxy_url=proxy_url) as context:
                page = await context.new_page()
                await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
                pom = WhatsAppWebPage(page)
                browser_t0 = time.perf_counter()

                while quanti is None or processati < quanti:
                    # ... CORPO DEL WHILE INVARIATO (righe 193-292 esistenti,
                    # solo ri-indentato di un livello per stare dentro il
                    # nuovo `async with wa_profile_lock.held(...)`) ...
                    pass
    except wa_profile_lock.WaProfileBusy:
        esito["motivo"] = "profilo_occupato"
        return esito

    logger.info(f"[WA] mini-sessione {number_id}: {esito}")
    return esito
```

(Nota per l'implementer: questo e' un ri-indent del blocco esistente righe 186-294, non una riscrittura — sposta `async with _open_wa_browser(...)` di un livello dentro `async with wa_profile_lock.held(number_id):`, avvolgi tutto in `try/except WaProfileBusy`. Il corpo del `while` (righe 193-292) resta identico, solo con un'indentazione in piu'. Il log finale (riga 294) e il `return esito` (riga 295) restano FUORI dal blocco lock/browser, cosi' girano anche nel path normale.)

Modifica `wa_send_task` (righe 360-394): aggiungi `"profilo_occupato"` al ramo di retry breve, distinto dal break di fine-sessione:

```python
async def wa_send_task(ctx: dict, number_id: str) -> None:
    from arq.jobs import Job          # noqa: F401  (documenta la dipendenza)
    from arq.worker import Retry
    from app.services import wa_timing

    esito = await esegui_mini_sessione(number_id)

    if esito["motivo"] in ("send_disabled", "wa_halted", "numero_non_attivo",
                           "guasti_consecutivi", "niente_da_fare"):
        logger.info(f"[WA] {number_id}: sessione chiusa ({esito['motivo']}), "
                    "nessuna rischedulazione automatica")
        return

    if esito["motivo"] == "profilo_occupato":
        logger.info(f"[WA] {number_id}: profilo occupato (health-check o "
                    "reply-scan in corso), riprovo fra "
                    f"{settings.wa_lock_busy_retry_s}s")
        raise Retry(defer=int(settings.wa_lock_busy_retry_s))

    # cap_esaurito / fuori_finestra / completata -> si riprende dopo il break.
    break_s = wa_timing.wa_session_break_seconds(
        await _campagna_attiva_del_numero(number_id))
    raise Retry(defer=int(break_s))
```

- [ ] **Step 4: Esegui e verifica che passi**

Run: `cd backend && pytest tests/test_wa_worker.py -v`
Expected: tutti i test del file passano, incluso il nuovo

- [ ] **Step 5: Suite WA completa (una sola alla volta, per il vincolo del DB sqlite condiviso)**

Run: `cd backend && pytest tests/test_wa_worker.py tests/test_wa_sender.py tests/test_wa_profile_lock.py -v`
Expected: tutti passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/workers/wa_worker.py
git commit -m "feat(wa): wa_send_task prende il lucchetto profilo, retry breve se occupato"
```

---

### Task 3: Wire del lock nel health-check, rimozione del polling vecchio

**Files:**
- Modify: `backend/app/workers/cron_worker.py:19-38` (rimuovi `_wa_send_job_is_active`), `:41-118` (`wa_session_healthcheck`, sostituisci il check)
- Test: `backend/tests/test_wa_cron.py` (aggiorna i test che mockano `_wa_send_job_is_active`)

**Interfaces:**
- Consumes: `wa_profile_lock.held(number_id)`, `wa_profile_lock.WaProfileBusy`

- [ ] **Step 1: Aggiorna i test esistenti che dipendono dalla funzione rimossa**

In `backend/tests/test_wa_cron.py`, i test `test_healthcheck_salta_numero_con_wa_send_task_attivo`, `test_healthcheck_controlla_numero_senza_job_attivo` e `test_fix_c_wa_send_job_is_active_vero_contro_redis_reale` mockano/testano `cron_worker._wa_send_job_is_active`, che questo task rimuove. Sostituiscili:

```python
@pytest.mark.asyncio
async def test_healthcheck_salta_numero_con_profilo_occupato(db_session, monkeypatch):
    from app.workers import cron_worker
    from app.services import wa_profile_lock

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, status=WaNumberStatus.active)
    await db_session.commit()

    class _CtxOccupato:
        def __call__(self, number_id, ttl_min=None):
            return self

        async def __aenter__(self):
            raise wa_profile_lock.WaProfileBusy(number_id)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cron_worker.wa_profile_lock, "held", _CtxOccupato())

    async def _fake_check(number_id):
        raise AssertionError("check_session non deve essere chiamato se il profilo e' occupato")
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    esito = await cron_worker.wa_session_healthcheck({})
    assert esito["saltati_invio_attivo"] == 1
    assert esito["controllati"] == 0


@pytest.mark.asyncio
async def test_healthcheck_controlla_numero_con_profilo_libero(db_session, monkeypatch):
    from app.workers import cron_worker
    from app.models.wa import WaNumberStatus

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, status=WaNumberStatus.active)
    await db_session.commit()

    async def _fake_check(number_id):
        return WaNumberStatus.active
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    esito = await cron_worker.wa_session_healthcheck({})
    assert esito["controllati"] == 1
    assert esito["saltati_invio_attivo"] == 0
```

Rimuovi i tre test vecchi che referenziano `_wa_send_job_is_active` (compreso il fixture `_redis_o_skip` locale a quel file se non usato altrove in esso — verifica con grep prima di cancellare).

- [ ] **Step 2: Esegui e verifica che i nuovi falliscano**

Run: `cd backend && pytest tests/test_wa_cron.py -v`
Expected: FAIL sui due nuovi test — `AttributeError: module 'app.workers.cron_worker' has no attribute 'wa_profile_lock'`

- [ ] **Step 3: Implementazione**

In `backend/app/workers/cron_worker.py`:

Rimuovi l'intera funzione `_wa_send_job_is_active` (righe 19-38) e il suo import locale `from arq.jobs import Job, JobStatus` (era usato solo li').

Aggiungi in cima al file:

```python
from app.services import wa_profile_lock
```

Modifica il loop di `wa_session_healthcheck` (righe 71-78 dell'originale):

```python
    async with AsyncSessionLocal() as db:
        numeri = (await db.execute(
            select(WaNumber).where(WaNumber.status.notin_([
                WaNumberStatus.retired, WaNumberStatus.suspended,
                WaNumberStatus.pending_qr]))
        )).scalars().all()
        ids = [n.id for n in numeri]

    for number_id in ids:
        try:
            async with wa_profile_lock.held(number_id):
                esito["controllati"] += 1
                try:
                    stato = await check_session(number_id)
                except Exception as exc:
                    logger.error(f"[WA] health-check {number_id} fallito: {type(exc).__name__}")
                    continue
                if stato == WaNumberStatus.active:
                    continue
                esito["caduti"] += 1
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(WaCampaign)
                        .where(WaCampaign.wa_number_id == number_id,
                               WaCampaign.status == WaCampaignStatus.running)
                        .values(status=WaCampaignStatus.paused)
                    )
                    await db.commit()
                await notifier.send_telegram(
                    f"WhatsApp: numero {number_id[:8]} -> {stato.value}. "
                    "Campagne in pausa. Serve un nuovo QR (lo scansiona il cliente).",
                    level="error")
        except wa_profile_lock.WaProfileBusy:
            esito["saltati_invio_attivo"] += 1
            logger.info(f"[WA] health-check {number_id[:8]} saltato: "
                       "profilo occupato (invio o reply-scan in corso)")
```

(Rimuovi il blocco `redis = await arq.create_pool(...) / try / finally: await redis.aclose()` che avvolgeva il loop originale — non serve piu', il lock apre/chiude il proprio pool internamente. Rimuovi anche l'import `import arq` in cima al file SOLO se non usato altrove in `cron_worker.py` — verifica con grep prima.)

- [ ] **Step 4: Esegui e verifica che passi**

Run: `cd backend && pytest tests/test_wa_cron.py -v`
Expected: tutti passed (nessun test rimanente referenzia `_wa_send_job_is_active`)

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/cron_worker.py backend/tests/test_wa_cron.py
git commit -m "refactor(wa): health-check usa il lucchetto profilo, rimosso il polling _wa_send_job_is_active"
```

---

### Task 4: Matching contatto — funzione pura

**Files:**
- Create: `backend/app/services/wa_reply_watcher.py` (solo la funzione di matching in questo task, il resto nei task successivi)
- Test: `backend/tests/test_wa_reply_watcher.py`

**Interfaces:**
- Consumes: `ChatRow` (da `app.browser.whatsapp_page`, gia' esistente: `title`, `title_is_number`, `unread_count`, `preview`, `last_is_outbound`, `outgoing_state`, `muted`)
- Produces: `async def match_contact(db, tenant_id: str, row: ChatRow) -> tuple[WaContact | None, WaMatchedBy]`

- [ ] **Step 1: Scrivi il test che fallisce**

```python
# backend/tests/test_wa_reply_watcher.py
import pytest

from app.browser.whatsapp_page import ChatRow
from app.models.wa import WaMatchedBy
from tests.factories_wa import make_contact, make_tenant


def _row(title, *, title_is_number=False, preview="ciao", unread=1):
    return ChatRow(position=0, title=title, title_is_number=title_is_number,
                   unread_count=unread, preview=preview, last_is_outbound=False,
                   outgoing_state=None, muted=False)


@pytest.mark.asyncio
async def test_match_per_chat_title(db_session):
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    contatto = await make_contact(db_session, tenant, display_name="Marco")
    contatto.chat_title = "Marco Rossi"
    await db_session.commit()

    trovato, via = await match_contact(db_session, tenant.id, _row("Marco Rossi"))
    assert trovato.id == contatto.id
    assert via == WaMatchedBy.chat_title


@pytest.mark.asyncio
async def test_match_per_numero(db_session):
    from app.utils.phone_pseudonym import hmac_phone
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    contatto = await make_contact(db_session, tenant, e164="+393331234567")
    await db_session.commit()

    row = _row("+39 333 1234567", title_is_number=True)
    trovato, via = await match_contact(db_session, tenant.id, row)
    assert trovato.id == contatto.id
    assert via == WaMatchedBy.phone


@pytest.mark.asyncio
async def test_nessun_match(db_session):
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    trovato, via = await match_contact(db_session, tenant.id, _row("Sconosciuto"))
    assert trovato is None
    assert via == WaMatchedBy.none


@pytest.mark.asyncio
async def test_title_ambiguo_mai_indovinare(db_session):
    """Due contatti con lo stesso chat_title nel tenant: il matching per
    title si disabilita per quel title, mai un match a caso (SDD 7.3)."""
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    c1 = await make_contact(db_session, tenant, e164="+393330000001")
    c1.chat_title = "Marco"
    c2 = await make_contact(db_session, tenant, e164="+393330000002")
    c2.chat_title = "Marco"
    await db_session.commit()

    trovato, via = await match_contact(db_session, tenant.id, _row("Marco"))
    assert trovato is None
    assert via == WaMatchedBy.none
```

- [ ] **Step 2: Esegui e verifica che fallisca**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py -v`
Expected: FAIL — modulo non esiste

- [ ] **Step 3: Implementazione**

```python
# backend/app/services/wa_reply_watcher.py
"""Reply-watcher del canale WhatsApp (SDD §7.3): legge SOLO la lista chat
(sidebar), mai apre una conversazione -- aprirla marcherebbe "letto" e
brucerebbe le notifiche del cliente sul telefono (vincolo di coesistenza,
SDD §9). Matching contatto, dedup eventi, dispatch opt-out/replied.
"""
from sqlalchemy import func, select

from app.browser.whatsapp_page import ChatRow
from app.config import settings
from app.models.wa import WaContact, WaMatchedBy
from app.utils.phone_pseudonym import PhoneNormalizationError, hmac_phone, normalize_e164


async def match_contact(db, tenant_id: str, row: ChatRow) -> tuple[WaContact | None, WaMatchedBy]:
    """Tre livelli, in ordine, mai indovinare (SDD §7.3):
    1) title == wa_contacts.chat_title, MA solo se il title non e' ambiguo
       (>=2 contatti del tenant con lo stesso chat_title -> disabilitato
       per quel title).
    2) title parsabile come numero -> hmac -> wa_contacts.phone_hmac.
    3) nessun match -> (None, WaMatchedBy.none), diagnostica.

    hmac_phone si aspetta SEMPRE il numero normalizzato CON il '+'
    ricomposto (contratto di wa_ingest.py, M2: normalize_e164 ritorna le
    cifre senza '+', il '+' si riaggiunge subito prima di hmac_phone/
    encrypt -- mai l'output nudo di normalize_e164). Un title che supera
    il check title_is_number del POM (solo cifre/spazi/+) ma fallisce
    comunque normalize_e164 (lunghezza fuori range E.164) e' trattato come
    nessun match, non un errore: e' un titolo che sembra un numero ma non
    lo e' davvero."""
    if row.title_is_number:
        try:
            cifre = normalize_e164(row.title, default_country=settings.wa_ingest_default_country)
        except PhoneNormalizationError:
            return None, WaMatchedBy.none
        contatto = await db.scalar(
            select(WaContact).where(
                WaContact.tenant_id == tenant_id,
                WaContact.phone_hmac == hmac_phone("+" + cifre),
            )
        )
        if contatto is not None:
            return contatto, WaMatchedBy.phone
        return None, WaMatchedBy.none

    conteggio = await db.scalar(
        select(func.count(WaContact.id)).where(
            WaContact.tenant_id == tenant_id,
            WaContact.chat_title == row.title,
        )
    )
    if conteggio == 1:
        contatto = await db.scalar(
            select(WaContact).where(
                WaContact.tenant_id == tenant_id,
                WaContact.chat_title == row.title,
            )
        )
        return contatto, WaMatchedBy.chat_title

    return None, WaMatchedBy.none
```

- [ ] **Step 4: Esegui e verifica che passi**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_reply_watcher.py backend/tests/test_wa_reply_watcher.py
git commit -m "feat(wa): matching contatto a 3 livelli per il reply-watcher"
```

---

### Task 5: Dedup + dispatch opt-out/replied per una riga chat

**Files:**
- Modify: `backend/app/services/wa_reply_watcher.py` (aggiungi in coda)
- Test: `backend/tests/test_wa_reply_watcher.py` (aggiungi in coda)

**Interfaces:**
- Consumes: `wa_optout.looks_like_stop(text) -> bool`, `wa_optout.persist_wa_optout(db, contact_id, *, prova, campaign_id=None) -> int` (gia' esistenti, M3)
- Produces: `async def process_chat_row(db, *, tenant_id: str, wa_number_id: str, row: ChatRow) -> dict` — ritorna `{"esito": "optout"|"replied"|"non_associato"|"duplicato"|"ignorato", "contact_id": str|None}`

- [ ] **Step 1: Scrivi il test che fallisce**

```python
@pytest.mark.asyncio
async def test_process_row_optout(db_session):
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaContact

    tenant = await make_tenant(db_session)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    esito = await process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id="numero-fake",
        row=_row("Marco", preview="basta scrivermi"))
    assert esito["esito"] == "optout"

    await db_session.refresh(contatto)
    assert contatto.opted_out is True
    assert contatto.do_not_contact is True


@pytest.mark.asyncio
async def test_process_row_replied(db_session):
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaCampaignContact, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, step = await make_campaign(db_session, tenant, numero)
    cc = await make_campaign_contact(db_session, campagna, contatto,
                                      status=WaContactStatus.in_sequence)
    await db_session.commit()

    esito = await process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="si mi interessa"))
    assert esito["esito"] == "replied"

    await db_session.refresh(cc)
    assert cc.status == WaContactStatus.replied
    assert cc.replied_at_step == 0


@pytest.mark.asyncio
async def test_process_row_dedup_su_ultimo_evento(db_session):
    """Stessa preview del contatto gia' vista -> nessun secondo evento,
    nessuna doppia scrittura (SDD 7.3, dedup)."""
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaInboundEvent

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    riga = _row("Marco", preview="si mi interessa")
    primo = await process_chat_row(db_session, tenant_id=tenant.id,
                                   wa_number_id=numero.id, row=riga)
    secondo = await process_chat_row(db_session, tenant_id=tenant.id,
                                     wa_number_id=numero.id, row=riga)
    assert primo["esito"] in ("replied", "non_associato")
    assert secondo["esito"] == "duplicato"

    eventi = (await db_session.execute(
        select(WaInboundEvent).where(WaInboundEvent.contact_id == contatto.id)
    )).scalars().all()
    assert len(eventi) == 1


@pytest.mark.asyncio
async def test_process_row_non_associato_sempre_inserito(db_session):
    """Righe senza match sono diagnostica: si inseriscono comunque
    (contact_id=NULL), senza dedup -- basso volume, SDD 7.3."""
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaInboundEvent

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)

    esito = await process_chat_row(db_session, tenant_id=tenant.id,
                                   wa_number_id=numero.id,
                                   row=_row("Sconosciuto", preview="ciao"))
    assert esito["esito"] == "non_associato"

    eventi = (await db_session.execute(
        select(WaInboundEvent).where(WaInboundEvent.wa_number_id == numero.id)
    )).scalars().all()
    assert len(eventi) == 1
    assert eventi[0].contact_id is None
```

- [ ] **Step 2: Esegui e verifica che fallisca**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py -v`
Expected: FAIL sui 4 nuovi test — `AttributeError`/`ImportError`, `process_chat_row` non esiste

- [ ] **Step 3: Implementazione**

Aggiungi in coda a `backend/app/services/wa_reply_watcher.py`:

```python
from datetime import datetime

from app.browser.whatsapp_page import ChatRow
from app.models.wa import (WaCampaignContact, WaContactStatus, WaInboundEvent,
                           WaMatchedBy)
from app.services import wa_optout
from app.utils import events


async def _ultima_preview_vista(db, contact_id: str) -> str | None:
    ultimo = await db.scalar(
        select(WaInboundEvent.preview_text)
        .where(WaInboundEvent.contact_id == contact_id)
        .order_by(WaInboundEvent.detected_at.desc())
        .limit(1)
    )
    return ultimo


async def _incrementa_contatore_campagna(db, campaign_id: str, campo: str) -> None:
    """UPDATE ... SET x = x + 1 in SQL (contratto §4.2), stesso pattern di
    wa_sender._incrementa_contatore_campagna -- non importato da li' per non
    accoppiare i due moduli a una funzione privata dell'altro."""
    from sqlalchemy import update
    from app.models.wa import WaCampaign
    colonna = getattr(WaCampaign, campo)
    await db.execute(update(WaCampaign).where(WaCampaign.id == campaign_id)
                     .values({campo: colonna + 1}))


async def _campagna_attiva_del_contatto(db, contact_id: str) -> WaCampaignContact | None:
    """La riga wa_campaign_contacts NON terminale del contatto, se c'e' --
    usata sia per l'evento opt-out (campaign_id per il log) sia per la
    transizione a replied."""
    return await db.scalar(
        select(WaCampaignContact).where(
            WaCampaignContact.contact_id == contact_id,
            WaCampaignContact.status == WaContactStatus.in_sequence,
        )
    )


async def process_chat_row(db, *, tenant_id: str, wa_number_id: str, row: ChatRow) -> dict:
    """Un giro completo per una riga della lista chat con unread>0:
    match -> dedup -> opt-out o replied. Mai apre la chat (il chiamante
    passa gia' righe raccolte da scan_chat_list, che non apre nulla)."""
    contatto, matched_by = await match_contact(db, tenant_id, row)

    if contatto is None:
        db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                              contact_id=None, preview_text=row.preview,
                              matched_by=WaMatchedBy.none, processed=True))
        await db.commit()
        return {"esito": "non_associato", "contact_id": None}

    if await _ultima_preview_vista(db, contatto.id) == row.preview:
        return {"esito": "duplicato", "contact_id": contatto.id}

    if wa_optout.looks_like_stop(row.preview):
        cc_attiva = await _campagna_attiva_del_contatto(db, contatto.id)
        await wa_optout.persist_wa_optout(
            db, contatto.id, prova=row.preview,
            campaign_id=cc_attiva.campaign_id if cc_attiva else None)
        db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                              contact_id=contatto.id, preview_text=row.preview,
                              matched_by=matched_by, processed=True))
        await db.commit()
        return {"esito": "optout", "contact_id": contatto.id}

    cc_attiva = await _campagna_attiva_del_contatto(db, contatto.id)
    if cc_attiva is not None:
        cc_attiva.status = WaContactStatus.replied
        cc_attiva.replied_at_step = cc_attiva.current_step
        cc_attiva.next_action_at = None
        contatto.last_replied_at = datetime.utcnow()
        await _incrementa_contatore_campagna(db, cc_attiva.campaign_id, "replied")
        db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                              contact_id=contatto.id, preview_text=row.preview,
                              matched_by=matched_by, processed=True))
        await db.commit()
        return {"esito": "replied", "contact_id": contatto.id}

    db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                          contact_id=contatto.id, preview_text=row.preview,
                          matched_by=matched_by, processed=True))
    await db.commit()
    return {"esito": "ignorato", "contact_id": contatto.id}
```

- [ ] **Step 4: Esegui e verifica che passi**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py -v`
Expected: 8 passed (i 4 di Task 4 + i 4 nuovi)

- [ ] **Step 5: Emit eventi (`wa.reply.received`)**

Aggiungi l'emissione evento nel ramo `replied` (dopo `await db.commit()`, prima del `return`):

```python
        events.emit(cc_attiva.campaign_id, "wa.reply.received",
                    f"contatto {contatto.id[:8]}: risposta rilevata dalla lista chat",
                    level="info")
```

(`events` e' gia' importato in cima al modulo dallo Step 3 sopra — nessun import locale da spostare.)

Aggiungi test:

```python
@pytest.mark.asyncio
async def test_process_row_replied_emette_evento(db_session, monkeypatch):
    from app.services import wa_reply_watcher
    from app.models.wa import WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact

    emessi = []
    monkeypatch.setattr(wa_reply_watcher.events, "emit",
                        lambda *a, **k: emessi.append((a, k)))

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, step = await make_campaign(db_session, tenant, numero)
    await make_campaign_contact(db_session, campagna, contatto,
                                status=WaContactStatus.in_sequence)
    await db_session.commit()

    await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="ok grazie"))
    assert len(emessi) == 1
    assert emessi[0][0][1] == "wa.reply.received"
```

- [ ] **Step 6: Esegui tutta la suite del modulo e verifica**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py -v`
Expected: 9 passed

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/wa_reply_watcher.py backend/tests/test_wa_reply_watcher.py
git commit -m "feat(wa): dedup + dispatch opt-out/replied per riga chat, riusa wa_optout di M3"
```

---

### Task 6: Selezione numeri da scansionare

**Files:**
- Modify: `backend/app/services/wa_reply_watcher.py`
- Test: `backend/tests/test_wa_reply_watcher.py`

**Interfaces:**
- Produces: `async def numeri_da_scansionare(db) -> list[str]` — id dei `WaNumber` attivi con almeno una campagna `running` che ha contatti `queued`/`in_sequence`.

- [ ] **Step 1: Scrivi il test che fallisce**

```python
@pytest.mark.asyncio
async def test_numeri_da_scansionare_solo_con_lavoro_vivo(db_session):
    from app.services.wa_reply_watcher import numeri_da_scansionare
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact

    tenant = await make_tenant(db_session)

    numero_vivo = await make_number(db_session, tenant, label="Vivo")
    contatto1 = await make_contact(db_session, tenant, e164="+393331111111")
    campagna1, _ = await make_campaign(db_session, tenant, numero_vivo,
                                       status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna1, contatto1,
                                status=WaContactStatus.in_sequence)

    numero_finito = await make_number(db_session, tenant, label="Finito")
    contatto2 = await make_contact(db_session, tenant, e164="+393332222222")
    campagna2, _ = await make_campaign(db_session, tenant, numero_finito,
                                       status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna2, contatto2,
                                status=WaContactStatus.completed)

    await db_session.commit()

    ids = await numeri_da_scansionare(db_session)
    assert numero_vivo.id in ids
    assert numero_finito.id not in ids
```

- [ ] **Step 2: Esegui e verifica che fallisca**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py::test_numeri_da_scansionare_solo_con_lavoro_vivo -v`
Expected: FAIL — funzione non esiste

- [ ] **Step 3: Implementazione**

Aggiungi in coda a `backend/app/services/wa_reply_watcher.py`:

```python
from app.models.wa import WaCampaign, WaCampaignStatus, WaNumber, WaNumberStatus


async def numeri_da_scansionare(db) -> list[str]:
    """Solo numeri attivi con almeno una campagna running che ha ancora
    contatti queued/in_sequence -- non serve scansionare un numero senza
    lavoro vivo (SDD §7.3: "solo numeri con campagne attive")."""
    righe = await db.execute(
        select(WaNumber.id)
        .join(WaCampaign, WaCampaign.wa_number_id == WaNumber.id)
        .join(WaCampaignContact, WaCampaignContact.campaign_id == WaCampaign.id)
        .where(
            WaNumber.status == WaNumberStatus.active,
            WaCampaign.status == WaCampaignStatus.running,
            WaCampaignContact.status.in_([WaContactStatus.queued,
                                          WaContactStatus.in_sequence]),
        )
        .distinct()
    )
    return [r[0] for r in righe.all()]
```

- [ ] **Step 4: Esegui e verifica che passi**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_reply_watcher.py backend/tests/test_wa_reply_watcher.py
git commit -m "feat(wa): selezione numeri con lavoro vivo per il reply-scan"
```

---

### Task 7: Orchestrazione — `scan_number` (apre il browser sotto lock, chiama il POM)

**Files:**
- Modify: `backend/app/services/wa_reply_watcher.py`
- Test: `backend/tests/test_wa_reply_watcher.py`

**Interfaces:**
- Consumes: `wa_profile_lock.held`, `_open_wa_browser` (da `app.services.wa_session`, M1 frozen), `WhatsAppWebPage.scan_chat_list()`, `bot_state_service.is_wa_halted()`
- Produces: `async def scan_number(number_id: str) -> dict` — `{"scansionate": int, "optout": int, "replied": int, "non_associati": int, "motivo": str|None}`

- [ ] **Step 1: Scrivi il test che fallisce**

```python
@pytest.mark.asyncio
async def test_scan_number_processa_le_righe_non_lette(db_session, monkeypatch):
    from app.services import wa_reply_watcher
    from app.database import AsyncSessionLocal

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    righe_finte = [
        _row("Marco", preview="ciao", unread=1),
        _row("Altro", preview="test", unread=0),  # unread=0, va ignorata
    ]

    class _PomFinto:
        def __init__(self, page):
            pass

        async def scan_chat_list(self):
            return righe_finte

    monkeypatch.setattr(wa_reply_watcher, "WhatsAppWebPage", _PomFinto)

    class _ContextFinto:
        async def new_page(self):
            class _PageFinta:
                async def goto(self, *a, **k):
                    pass
            return _PageFinta()

    class _BrowserCtx:
        def __call__(self, number_id, headless=True, proxy_url=None):
            return self

        async def __aenter__(self):
            return _ContextFinto()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wa_reply_watcher, "_open_wa_browser", _BrowserCtx())

    async def _mai_halted():
        return False
    monkeypatch.setattr(wa_reply_watcher.bot_state_service, "is_wa_halted", _mai_halted)

    esito = await wa_reply_watcher.scan_number(numero.id)
    assert esito["scansionate"] == 1  # solo la riga con unread>0
```

- [ ] **Step 2: Esegui e verifica che fallisca**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py::test_scan_number_processa_le_righe_non_lette -v`
Expected: FAIL — `scan_number` non esiste

- [ ] **Step 3: Implementazione**

Aggiungi in cima a `backend/app/services/wa_reply_watcher.py` (import) e in coda (funzione):

```python
from app.browser.whatsapp_page import WhatsAppWebPage
from app.services import bot_state_service, wa_profile_lock
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser
```

```python
async def scan_number(number_id: str) -> dict:
    """Una scansione della lista chat per UN numero: apre il browser sotto
    lucchetto profilo, legge SOLO la sidebar (mai una chat), processa ogni
    riga con unread>0. Short-lived, nessun sleep lungo."""
    from app.database import AsyncSessionLocal
    from app.models.wa import WaNumber, WaNumberStatus

    esito = {"scansionate": 0, "optout": 0, "replied": 0, "non_associati": 0, "motivo": None}

    if await bot_state_service.is_wa_halted():
        esito["motivo"] = "wa_halted"
        return esito

    async with AsyncSessionLocal() as db:
        numero = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
        if numero is None or numero.status != WaNumberStatus.active:
            esito["motivo"] = "numero_non_attivo"
            return esito
        tenant_id, proxy_url = numero.tenant_id, numero.proxy_url

    try:
        async with wa_profile_lock.held(number_id):
            async with _open_wa_browser(number_id, headless=True, proxy_url=proxy_url) as context:
                page = await context.new_page()
                await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
                pom = WhatsAppWebPage(page)
                righe = await pom.scan_chat_list()
    except wa_profile_lock.WaProfileBusy:
        esito["motivo"] = "profilo_occupato"
        return esito

    async with AsyncSessionLocal() as db:
        for row in righe:
            if row.unread_count <= 0:
                continue
            esito["scansionate"] += 1
            risultato = await process_chat_row(db, tenant_id=tenant_id,
                                               wa_number_id=number_id, row=row)
            if risultato["esito"] == "optout":
                esito["optout"] += 1
            elif risultato["esito"] == "replied":
                esito["replied"] += 1
            elif risultato["esito"] == "non_associato":
                esito["non_associati"] += 1

    logger.info(f"[WA] reply-scan {number_id}: {esito}")
    return esito
```

Aggiungi `from loguru import logger` in cima al modulo se non gia' presente.

- [ ] **Step 4: Esegui e verifica che passi**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_reply_watcher.py backend/tests/test_wa_reply_watcher.py
git commit -m "feat(wa): scan_number orchestra apertura-sotto-lock + scan_chat_list + dispatch"
```

---

### Task 8: Cron `wa_reply_scan` registrato in `cron_worker.py`

**Files:**
- Modify: `backend/app/workers/cron_worker.py`
- Test: `backend/tests/test_wa_cron.py`

**Interfaces:**
- Produces: `async def wa_reply_scan(ctx: dict) -> dict`, registrato in `CronWorkerSettings.cron_jobs`

- [ ] **Step 1: Scrivi il test che fallisce**

```python
@pytest.mark.asyncio
async def test_wa_reply_scan_gira_solo_sui_numeri_con_lavoro(db_session, monkeypatch):
    from app.workers import cron_worker
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna, contatto,
                                status=WaContactStatus.in_sequence)
    await db_session.commit()

    chiamate = []
    async def _fake_scan(number_id):
        chiamate.append(number_id)
        return {"scansionate": 0, "optout": 0, "replied": 0, "non_associati": 0, "motivo": None}
    monkeypatch.setattr(cron_worker.wa_reply_watcher, "scan_number", _fake_scan)

    esito = await cron_worker.wa_reply_scan({})
    assert chiamate == [numero.id]
    assert esito["numeri_scansionati"] == 1
```

- [ ] **Step 2: Esegui e verifica che fallisca**

Run: `cd backend && pytest tests/test_wa_cron.py::test_wa_reply_scan_gira_solo_sui_numeri_con_lavoro -v`
Expected: FAIL — `cron_worker.wa_reply_scan` non esiste

- [ ] **Step 3: Implementazione**

In `backend/app/workers/cron_worker.py`, aggiungi l'import:

```python
from app.services import wa_reply_watcher
```

Aggiungi la funzione (dopo `wa_session_healthcheck`):

```python
async def wa_reply_scan(ctx: dict) -> dict:
    """Ogni scan: solo numeri con lavoro vivo (campagna running + contatti
    queued/in_sequence), stessa finestra oraria degli invii (SDD §7.3).
    Non e' tempo-critico per l'MVP (campagne a 1 messaggio, Q29): serve per
    KPI e per la rete di opt-out, la garanzia vera resta la guardia
    pre-invio (§7.5 punto 7)."""
    from app.database import AsyncSessionLocal

    esito = {"numeri_scansionati": 0, "optout_totali": 0, "replied_totali": 0}
    async with AsyncSessionLocal() as db:
        ids = await wa_reply_watcher.numeri_da_scansionare(db)

    for number_id in ids:
        risultato = await wa_reply_watcher.scan_number(number_id)
        if risultato["motivo"] is None:
            esito["numeri_scansionati"] += 1
        esito["optout_totali"] += risultato["optout"]
        esito["replied_totali"] += risultato["replied"]

    logger.info(f"[WA] reply-scan: {esito}")
    return esito
```

Registra il cron in `CronWorkerSettings.cron_jobs`, sfalsato rispetto a `wa_session_healthcheck` (che gira a `{0, 30}`) per non farli scattare nello stesso minuto — riduce (non elimina, il lock resta la vera garanzia) la contesa sul lock:

```python
        cron(wa_session_healthcheck, minute={0, 30}, hour=set(range(9, 20))),
        cron(wa_reply_scan, minute={15, 45}, hour=set(range(9, 20))),
```

- [ ] **Step 4: Esegui e verifica che passi**

Run: `cd backend && pytest tests/test_wa_cron.py -v`
Expected: tutti passed

- [ ] **Step 5: Estendi il test di non-regressione dei cron esistente**

`test_wa_cron.py` ha gia' `test_i_cron_instagram_restano_registrati_e_healthcheck_wa_e_aggiunto`, che legge `{job.coroutine.__name__ for job in cron_worker.CronWorkerSettings.cron_jobs}` e verifica che i cron IG e `wa_session_healthcheck` siano tutti registrati. Aggiungi la riga finale:

```python
    assert "wa_session_healthcheck" in nomi
    assert "wa_reply_scan" in nomi
```

(non serve un test nuovo: questo e' gia' il test giusto, riusa il pattern `.coroutine.__name__` che il file stesso documenta come verificato per `cron_worker.py`.)

Run: `cd backend && pytest tests/test_wa_cron.py -v`
Expected: tutti passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/workers/cron_worker.py backend/tests/test_wa_cron.py
git commit -m "feat(wa): cron wa_reply_scan registrato, sfalsato dal health-check"
```

---

### Task 9: Integrazione end-to-end (senza browser vero) + suite WA completa

**Files:**
- Test: `backend/tests/test_wa_reply_watcher.py`

**Interfaces:**
- Nessuna nuova — verifica il flusso completo `numeri_da_scansionare -> scan_number -> process_chat_row` con dati realistici, senza mock parziali.

- [ ] **Step 1: Scrivi il test end-to-end**

```python
@pytest.mark.asyncio
async def test_e2e_optout_ferma_tutte_le_campagne_del_contatto(db_session, monkeypatch):
    """Scenario completo SDD §7.5: STOP su UNA campagna ferma TUTTE le righe
    non terminali del contatto, in QUALUNQUE campagna del tenant."""
    from app.services import wa_reply_watcher
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"

    camp_a, _ = await make_campaign(db_session, tenant, numero, name="A",
                                    status=WaCampaignStatus.running)
    camp_b, _ = await make_campaign(db_session, tenant, numero, name="B",
                                    status=WaCampaignStatus.running)
    cc_a = await make_campaign_contact(db_session, camp_a, contatto,
                                       status=WaContactStatus.in_sequence)
    cc_b = await make_campaign_contact(db_session, camp_b, contatto,
                                       status=WaContactStatus.queued)
    await db_session.commit()

    righe_finte = [_row("Marco", preview="stop non scrivermi piu'", unread=1)]

    class _PomFinto:
        def __init__(self, page):
            pass
        async def scan_chat_list(self):
            return righe_finte

    monkeypatch.setattr(wa_reply_watcher, "WhatsAppWebPage", _PomFinto)

    class _ContextFinto:
        async def new_page(self):
            class _PageFinta:
                async def goto(self, *a, **k):
                    pass
            return _PageFinta()

    class _BrowserCtx:
        def __call__(self, number_id, headless=True, proxy_url=None):
            return self
        async def __aenter__(self):
            return _ContextFinto()
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wa_reply_watcher, "_open_wa_browser", _BrowserCtx())

    async def _mai_halted():
        return False
    monkeypatch.setattr(wa_reply_watcher.bot_state_service, "is_wa_halted", _mai_halted)

    esito = await wa_reply_watcher.scan_number(numero.id)
    assert esito["optout"] == 1

    await db_session.refresh(contatto)
    await db_session.refresh(cc_a)
    await db_session.refresh(cc_b)
    assert contatto.opted_out is True
    assert contatto.do_not_contact is True
    assert cc_a.status == WaContactStatus.opted_out
    assert cc_b.status == WaContactStatus.opted_out
```

- [ ] **Step 2: Esegui e verifica**

Run: `cd backend && pytest tests/test_wa_reply_watcher.py -v`
Expected: tutti passed, incluso il nuovo end-to-end

- [ ] **Step 3: Suite WA completa (una alla volta, vincolo DB sqlite condiviso)**

Run: `cd backend && pytest tests/test_wa_profile_lock.py tests/test_wa_worker.py tests/test_wa_cron.py tests/test_wa_reply_watcher.py tests/test_wa_sender.py tests/test_wa_optout.py -v`
Expected: tutti passed. I 3 fail pre-esistenti in `test_wa_migration.py` (bug 013/014, non di questo modulo) possono restare rossi — non sono una regressione di M4, gia' segnalati nell'handoff.

- [ ] **Step 4: Typecheck/lint se il repo ne ha uno configurato**

Run: verifica in `backend/` se esiste un comando `ruff`/`mypy` in uso dagli altri moduli WA (controlla CI o script) ed eseguilo sui file nuovi/modificati.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_wa_reply_watcher.py
git commit -m "test(wa): end-to-end opt-out multi-campagna per il reply-watcher"
```

---

## Dopo l'ultimo task

Chiusura modulo secondo `sviluppo-modulo` Fase 4 (non nei task sopra: si scrive a runtime, non si pre-scrive nel piano):
1. Lista test manuali UI (minimo 20) — QA agent la esegue via browser reale (Playwright, `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers`) contro un numero WhatsApp di test, non contro il mock.
2. Lista adversarial (minimo 30): concorrenza vera (`asyncio.gather` fra `wa_send_task`/`wa_session_healthcheck`/`wa_reply_scan` sullo stesso numero — verifica che il lock tenga), title ambiguo, preview con SQL/XSS/unicode/10k char, doppio STOP idempotente, scan durante `WA_SEND_ENABLED=false`, numero `retired` durante uno scan in corso, TTL del lock scaduto con secondo acquirente attivo, invarianti a DB a fine run (nessuna riga `wa_campaign_contacts` con `status=replied` e `next_action_at` non NULL, nessun `wa_contacts.opted_out=True` con `do_not_contact=False`).
3. Fix loop al 100%, poi `superpowers:requesting-code-review` sull'intero branch.
4. PR verso `main`, review di Tommaso — collaudo manuale rimandato a M5 (MVP), non qui.
