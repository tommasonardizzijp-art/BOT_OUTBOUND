# WhatsApp M3 — Invio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Prima di scrivere una riga di codice, invoca la skill `sviluppo-modulo`** (obbligatoria per ogni modulo di codice di questo repo): implementazione subagent-driven, un implementer + un reviewer dedicato per ogni task, mai in parallelo sull'implementazione (M3 non ha frontend: niente agent-teams, quello è il caso di M2).

**Goal:** Costruire l'invio del canale WhatsApp — `wa_sender` (apertura chat, guardia opt-out/reply, TOCTOU, invio, verifica spunta), `wa_worker` (mini-sessioni per-numero, claim atomico, cap in AND, defer anti-ban), `wa_number_manager` (warmup/cap/cooldown), `wa_optout` (STOP → DNC permanente), kill-switch `wa_halted`, e l'API `app/api/wa_ops.py` — così che una campagna a un messaggio, seminata da `wa_seed_campaign.py` (M2, PR-0), possa girare su un numero di test con `WA_SEND_ENABLED` acceso a mano.

**Architecture:** Mini-sessioni per numero ARQ (`wa_send_task`), stesso scheletro di `services/browser_bio.py` (claim atomico via `UPDATE ... WHERE locked_by IS NULL`, `Retry(defer=...)` a fine sessione/pausa anti-ban, escalation su fallimenti consecutivi) applicato a `wa_campaign_contacts` invece che a `Follower`. Il trasporto è `WhatsAppWebPage` (M1, frozen): `wa_sender` lo usa senza modificarlo, decidendo lui la politica (guardia V2, mappa segnale→esito, quarantena risync). Un solo browser Chromium per numero alla volta, riusando il lock e l'apertura di `wa_session._open_wa_browser` (M1, frozen) — non un secondo meccanismo di apertura.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async, ARQ (Redis), Patchright (via `WhatsAppWebPage`), pytest + pytest-asyncio, SQLite di test (via `conftest.py`/PR-0).

## Global Constraints

- **Il contratto M2↔M3 (`docs/whatsapp/contratto-M2-M3.md`) vince su questo piano.** Ogni riferimento sotto a "§N" senza altra indicazione è quel documento.
- `app/models/wa.py`, `app/database.py`, `app/main.py`, `app/config.py`, `tests/conftest.py` sono **congelati dopo PR-0**: nessun task di questo piano li modifica. Le variabili d'ambiente usate sotto (`WA_SEND_ENABLED`, `WA_DAILY_CAP_DEFAULT`, `WA_WARMUP_STEPS`, `WA_SEND_DELAY_MEDIAN_S`, `WA_SEND_DELAY_SIGMA`, `WA_SESSION_MIN_MSG`, `WA_SESSION_MAX_MSG`, `WA_BREAK_MIN_MIN`, `WA_BREAK_MAX_MIN`, `WA_ACTIVE_HOURS`, `WA_RESYNC_QUARANTINE_MIN`, `WA_GUARD_TAIL_N`, `WA_GUARD_HISTORY_MIN`, `WA_LOCK_TIMEOUT_MIN`, `WA_MAX_FAILURES_PER_CONTACT`, `WA_STOP_WORDS`, `WA_GLOBAL_DAILY_CAP`) sono la lista **chiusa** di §5.2, mappate su `settings.<nome_snake_case>` per lo stesso pattern osservato in `app/config.py` (es. `bio_browser_session_cap_min`). **Task 0 verifica che esistano davvero con questi nomi** prima di scrivere codice che le legge — se PR-0 le ha chiamate diversamente, si corregge lì, non altrove.
- `backend/app/browser/whatsapp_page.py`, `whatsapp_selectors.py`, `services/wa_session.py`, `utils/phone_pseudonym.py` sono **patrimonio M1, non si modificano**. Si importano e si usano.
- `backend/app/services/wa_template.py`, `backend/scripts/wa_seed_campaign.py`, `backend/tests/factories_wa.py` sono di **M2 (PR-0)**: M3 li consuma, non li scrive né li modifica. Il Task 0 verifica le firme reali contro il contratto §2.4.
- **Ogni modifica a codice condiviso in produzione** (`app/workers/task_queue.py`, `app/workers/cron_worker.py`, `app/services/bot_state_service.py`, `app/models/bot_state.py`) porta il suo **test di non-regressione PRIMA della modifica** (il canale Instagram è in produzione).
- **Nessun `xfail`.** Mai un monkeypatch che si autoriferisce. Commenti/docstring in ASCII (`gia'`, `e'`); i markdown usano gli accenti. Non-ASCII nei sorgenti sempre come escape.
- **Ogni numero magico è una costante con la provenienza scritta accanto** (commento). Vedi in particolare Task 9 (misura di `READ_LAST_TICK_TIMEOUT_MS`, mai fatta finora).
- **`WA_SEND_ENABLED` parte a `false`.** Nessun task di questo piano lo accende: si accende a mano, dopo Fase 4 (Task 15), verificando prima che kill-switch, cap e numero di test siano a posto.
- **Contatori mai read-modify-write**: `wa_campaigns.sent/failed/opted_out` e `wa_numbers.sent_today` si incrementano in una singola `UPDATE ... SET x = x + 1` (§4.2), mai leggendo-e-riscrivendo in Python.
- **Una sola suite pytest alla volta in questo worktree**; **un solo comando pesante alla volta a livello di macchina** (7,4 GB totali) — controllare `D:\dev\tools\ram-guard\guard.ps1 stato` prima di un test che apre un browser.
- **Mai aprire `D:\dev\wa-poc\profile`** (PoC-1, in corsa fino al 10/08). Ogni test/prova con browser vero usa un profilo **nuovo** (convenzione `data/browser_profiles/wa_<uuid-di-test>`), mai quello di PoC-1. **Mai** impostare `PLAYWRIGHT_BROWSERS_PATH=D:` per queste prove: le build lì (149/151) sono diverse da `chromium-1208` (posizione di default su `C:`) su cui è nato il profilo di M0 — puntarci upgrada irreversibilmente qualunque profilo che lo attraversi.
- **Branch/worktree dedicato**: `feat/whatsapp-m3-invio`, da `main` aggiornato. Mai push diretto su `main`, mai commit nel worktree dell'altro cantiere (M2). **Ordine di merge (§6.3): PR-0 (M2) → PR M2 → PR M3.** Prima di aprire la PR per la review: rebase su `main` aggiornato + ripasso completo suite + ciclo migrazioni su-giù-su.
- **Migrazione 027**: `down_revision = "025"`. Se al rebase la 026 (M2) esiste già su `main`, si cambia in `"026"` e si rifà il ciclo su-giù-su. La 025 non ha mai visto Postgres: applicarla (+027) su un Postgres di test è un rischio dichiarato, con un task suo (Task 3).
- **Fuori scope di questo piano** (da non implementare, con motivo):
  - **C4 / FM9** ("non intromettersi se l'ultimo messaggio è dell'umano-business"): richiederebbe un metodo del POM che legga l'ultimo messaggio **indipendentemente dalla direzione** — `read_inbound_tail` per contratto **filtra via l'outbound** (è la sua garanzia contro i falsi "nessuno STOP"). `whatsapp_page.py` è frozen: aggiungere un metodo è un emendamento, non una scelta di questo piano. La lista "cosa copre M3" del lead non include FM9 fra le failure mode assegnate — coerente con questa lettura. **Segnalato al lead come backlog per M4/emendamento**, non implementato qui.
  - **`wa_reply_watcher`, branching multi-step, FM17**: M4.
  - **Frontend**: M3 è backend puro (decisione 29/07); stato e kill-switch via `app/api/wa_ops.py` + alert Telegram (solo outbound, via `notifier.send_telegram` esistente — **nessuna modifica a `telegram_commands.py`**, che è polling bidirezionale condiviso con IG in produzione e resta fuori da questo piano).

---

## File Structure

| File | Stato | Responsabilità |
|---|---|---|
| `backend/app/services/wa_timing.py` | **Create** | Delay lognormale invio, session-break lognormale, cap messaggi/sessione — parametrizzati su `wa_campaigns`/`settings.wa_*`, senza toccare `utils/timing.py` (riuso as-is, SDD 6.1) |
| `backend/app/services/wa_number_manager.py` | **Create** | Cap effettivo (warmup ∧ numero ∧ campagna), contatore `sent_today` date-aware, cooldown (via Redis TTL: `wa_numbers` non ha `cooldown_until`) |
| `backend/app/services/wa_optout.py` | **Create** | Rilevamento STOP (regex su parole intere), persistenza DNC permanente per-tenant, stop di tutte le sequenze attive del contatto |
| `backend/app/services/wa_sender.py` | **Create** | Guardia V2 (mappa segnale→esito §3.2), quarantena risync (§3.4), guardia pre-invio + TOCTOU (§3.5), render (via `wa_template`, M2), invio, `delivery_check`, scrittura `wa_messages` e contatori |
| `backend/app/workers/wa_worker.py` | **Create** | `claim_next_wa_contact` (§7.3), mini-sessione per-numero (cap, break, FM2, startup guard), `wa_send_task` ARQ, `enqueue_wa_workers` (fan-out) |
| `backend/app/api/wa_ops.py` | **Modify** (skeleton PR-0 → riempito) | `GET /wa/ops/status`, `POST /wa/ops/halt`, `POST /wa/ops/resume`, `POST /wa/ops/numbers/{id}/kick` |
| `backend/app/models/bot_state.py` | **Modify** | Colonna `wa_halted` |
| `backend/alembic/versions/027_wa_halted.py` | **Create** | Migrazione additiva, `down_revision="025"` (o `"026"`, vedi Global Constraints) |
| `backend/app/services/bot_state_service.py` | **Modify** | `is_wa_halted`, `halt_wa`, `resume_wa` (funzioni nuove, quelle IG esistenti invariate) |
| `backend/app/workers/task_queue.py` | **Modify** | Registra `wa_send_task` in `WorkerSettings.functions` |
| `backend/app/workers/cron_worker.py` | **Modify** | Cron `wa_session_healthcheck` (health-check + rilascio cooldown + stale-lock release) |
| `backend/tests/test_wa_timing.py` | **Create** | |
| `backend/tests/test_wa_number_manager.py` | **Create** | |
| `backend/tests/test_wa_optout.py` | **Create** | |
| `backend/tests/test_wa_sender.py` | **Create** | Guardia con `WhatsAppWebPage` fittizio (nessun browser) |
| `backend/tests/test_wa_worker.py` | **Create** | Claim/mini-sessione con browser fittizio |
| `backend/tests/test_bot_state_wa.py` | **Create** | Non-regressione IG + comportamento wa_halted |
| `backend/tests/test_task_queue_wa_registration.py` | **Create** | Non-regressione registrazioni ARQ |
| `backend/tests/test_wa_ops_api.py` | **Create** | |
| `.superpowers/sdd/qa-m3-tests.md`, `qa-m3-adversarial.md` | **Create** | Liste Fase 4 |

---

### Task 0: Verifica impalcatura PR-0 e apertura cantiere

**Files:**
- Nessuna modifica di codice — solo verifica + worktree/branch.

**Interfaces:**
- Consumes: `app/config.py` (variabili §5.2), `app/services/wa_template.py` (firme §2.4), `backend/tests/factories_wa.py`, `backend/scripts/wa_seed_campaign.py` — tutti scritti da M2 in PR-0.
- Produces: conferma scritta (commento nel primo commit, o nota nel PROGRESS) di quali nomi reali usare nei task successivi, se divergono da questo piano.

- [ ] **Step 1: Verificare che PR-0 sia mergiata su `main`**

```bash
git log main --oneline -5
```
Deve comparire il commit di PR-0 (router vuoti, `app/config.py` con le 17 variabili WA, `wa_template.py`, `wa_seed_campaign.py`, `factories_wa.py`, `conftest.py` con `WA_TEST_DB_SLOT`). Se manca, **fermarsi**: M3 non parte prima che PR-0 sia in `main` (§8.4).

- [ ] **Step 2: Creare worktree + branch dedicato**

Skill `superpowers:using-git-worktrees`. Branch `feat/whatsapp-m3-invio` da `main` aggiornato, worktree separato da quello di M2.

- [ ] **Step 3: Leggere `app/config.py` reale e confrontare i nomi delle 17 variabili**

```bash
grep -n "wa_send_enabled\|wa_daily_cap_default\|wa_warmup_steps\|wa_send_delay\|wa_session_min_msg\|wa_session_max_msg\|wa_break_min_min\|wa_break_max_min\|wa_active_hours\|wa_resync_quarantine_min\|wa_guard_tail_n\|wa_guard_history_min\|wa_lock_timeout_min\|wa_max_failures_per_contact\|wa_stop_words\|wa_global_daily_cap" backend/app/config.py
```
Se un nome differisce da quello usato in questo piano, annotarlo qui e usare il nome reale in TUTTI i task successivi (i task sotto assumono i nomi elencati nei Global Constraints).

- [ ] **Step 4: Leggere `wa_template.py` reale e confrontare con la firma congelata del contratto §2.4**

```bash
sed -n '1,80p' backend/app/services/wa_template.py
```
Confermare `pick_wa_template(step, rng=None) -> tuple[str, str]`, `render_wa_template(template, *, display_name, attributes, rng=None) -> str`, `validate_wa_template(template, *, known_attributes) -> list[str]`. Se M2 ha cambiato qualcosa rispetto al contratto senza un emendamento in §9, **fermarsi e segnalare al lead** — non adattarsi in silenzio a una firma diversa da quella vincolante.

- [ ] **Step 5: Leggere `backend/tests/factories_wa.py` reale**

```bash
sed -n '1,150p' backend/tests/factories_wa.py
```
Annotare le firme esatte delle factory (tenant/numero/contatto/campagna/step). I test nei Task 2-14 di questo piano usano helper **locali** definiti nei singoli file di test (non dipendono da `factories_wa.py` per restare eseguibili anche se questo piano è stato scritto prima che M2 finisse PR-0) — se `factories_wa.py` espone già l'equivalente, sostituire l'helper locale con la factory condivisa è un refactor di cortesia, non un requisito bloccante per questo piano.

- [ ] **Step 6: Confermare lo script di seed**

```bash
python -m scripts.wa_seed_campaign --help
```
Deve accettare almeno `--tenant-label --number-label --number-phone --browser-profile --contact --campaign-name --campaign-type --template --daily-cap --start --dry-run --force-number-active` (§7.4). Serve al Task 14 (prova end-to-end senza UI).

---

### Task 1: `wa_timing.py` — delay invio e cadenza sessione

**Files:**
- Create: `backend/app/services/wa_timing.py`
- Test: `backend/tests/test_wa_timing.py`

**Interfaces:**
- Consumes: `app.config.settings.wa_send_delay_median_s`, `settings.wa_send_delay_sigma`, `settings.wa_session_min_msg`, `settings.wa_session_max_msg`, `settings.wa_break_min_min`, `settings.wa_break_max_min` (§5.2); `app.models.wa.WaCampaign` (campi `session_min_messages`, `session_max_messages`, `break_min_minutes`, `break_max_minutes`, nullable — override per-campagna, SDD §5.2).
- Produces: `wa_send_delay_seconds() -> float`, `wa_session_message_count(campaign) -> int`, `wa_session_break_seconds(campaign) -> float`, `effective_wa_active_hours(campaign) -> tuple[int, int]` — usati da Task 8 (invio) e Task 11 (mini-sessione).

- [ ] **Step 1: Scrivere il test che fallisce**

```python
# backend/tests/test_wa_timing.py
import math
from types import SimpleNamespace

from app.services import wa_timing


def test_wa_send_delay_seconds_stays_in_reasonable_band(monkeypatch):
    """Lognormale centrata su WA_SEND_DELAY_MEDIAN_S: non deve mai andare
    sotto 1s (firma robotica) ne' esplodere oltre 20 minuti (bug di sigma)."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_send_delay_median_s", 90)
    monkeypatch.setattr(settings, "wa_send_delay_sigma", 0.7)
    samples = [wa_timing.wa_send_delay_seconds() for _ in range(200)]
    assert all(1.0 <= s <= 1200.0 for s in samples)
    # non deve essere una costante (firma robotica identica a ogni chiamata)
    assert len(set(round(s, 1) for s in samples)) > 50


def test_wa_session_message_count_uses_campaign_override_when_set():
    campaign = SimpleNamespace(session_min_messages=3, session_max_messages=3,
                                break_min_minutes=None, break_max_minutes=None)
    for _ in range(20):
        assert wa_timing.wa_session_message_count(campaign) == 3


def test_wa_session_message_count_falls_back_to_settings_when_campaign_null(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_session_min_msg", 8)
    monkeypatch.setattr(settings, "wa_session_max_msg", 8)
    campaign = SimpleNamespace(session_min_messages=None, session_max_messages=None,
                                break_min_minutes=None, break_max_minutes=None)
    assert wa_timing.wa_session_message_count(campaign) == 8


def test_wa_session_break_seconds_campaign_override(monkeypatch):
    campaign = SimpleNamespace(session_min_messages=None, session_max_messages=None,
                                break_min_minutes=1, break_max_minutes=1)
    samples = [wa_timing.wa_session_break_seconds(campaign) for _ in range(20)]
    assert all(abs(s - 60.0) < 5.0 for s in samples)


def test_effective_wa_active_hours_parses_HHMM_range(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_active_hours", "09:30-19:30")
    campaign = SimpleNamespace(active_hours_start=None, active_hours_end=None)
    assert wa_timing.effective_wa_active_hours(campaign) == (9, 19)


def test_effective_wa_active_hours_campaign_override():
    campaign = SimpleNamespace(active_hours_start="08:00", active_hours_end="12:00")
    assert wa_timing.effective_wa_active_hours(campaign) == (8, 12)
```

- [ ] **Step 2: Lanciare il test e verificare che fallisca**

Run: `pytest backend/tests/test_wa_timing.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.wa_timing'`.

- [ ] **Step 3: Implementare `wa_timing.py`**

```python
# backend/app/services/wa_timing.py
"""Timing del canale WhatsApp (invio, sessioni, break). Stesso stile di
utils/timing.py (lognormale, mai delay uniformi) MA parametrizzato sui
campi per-campagna di wa_campaigns (fallback ai default globali WA_*) --
riuso as-is di timing.py (SDD 6.1), non lo si modifica: quel file resta
di IG, questo e' il suo equivalente WA, non un secondo branch dello stesso
modulo condiviso.
"""
import math
import random

from app.config import settings


def wa_send_delay_seconds() -> float:
    """Delay tra due invii consecutivi dello stesso numero. Lognormale
    centrata su WA_SEND_DELAY_MEDIAN_S (default 90s, SDD 10.3), sigma
    WA_SEND_DELAY_SIGMA (default 0.7, stesso principio anti-firma-piatta
    di utils.timing.random_delay_seconds)."""
    median = float(settings.wa_send_delay_median_s)
    sigma = float(settings.wa_send_delay_sigma)
    mu = math.log(max(1.0, median))
    delay = random.lognormvariate(mu, sigma)
    return max(1.0, min(median * 8.0, delay))


def _effective_int_pair(campaign_lo, campaign_hi, settings_lo: int, settings_hi: int) -> tuple[int, int]:
    """Campo per-campagna (nullable) vince se ENTRAMBI lo/hi sono valorizzati;
    altrimenti fallback ai default globali WA_*. Un solo campo valorizzato
    e l'altro nullo verrebbe letto come range invertito/degenere: si tratta
    come 'non configurato' e si cade sul default intero."""
    if campaign_lo is not None and campaign_hi is not None:
        return int(campaign_lo), int(campaign_hi)
    return int(settings_lo), int(settings_hi)


def wa_session_message_count(campaign) -> int:
    """Quanti messaggi in una mini-sessione prima del break anti-ban.
    Campo per-campagna (session_min/max_messages) se presente, altrimenti
    WA_SESSION_MIN_MSG/MAX_MSG (default 8/15, SDD 10.3)."""
    lo, hi = _effective_int_pair(
        getattr(campaign, "session_min_messages", None),
        getattr(campaign, "session_max_messages", None),
        settings.wa_session_min_msg, settings.wa_session_max_msg,
    )
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def wa_session_break_seconds(campaign) -> float:
    """Pausa lunga anti-ban tra una mini-sessione e la successiva sullo
    stesso numero. Lognormale (sigma 0.6, stesso valore di
    human_behavior.session_break_seconds: range pienamente coperto senza
    ammassarsi al centro)."""
    lo_min, hi_min = _effective_int_pair(
        getattr(campaign, "break_min_minutes", None),
        getattr(campaign, "break_max_minutes", None),
        settings.wa_break_min_min, settings.wa_break_max_min,
    )
    lo_s, hi_s = lo_min * 60, hi_min * 60
    if hi_s < lo_s:
        lo_s, hi_s = hi_s, lo_s
    mid = (lo_s + hi_s) / 2
    val = random.lognormvariate(math.log(max(1.0, mid)), 0.6)
    return max(float(lo_s), min(float(hi_s), val))


def effective_wa_active_hours(campaign) -> tuple[int, int]:
    """(ora_inizio, ora_fine) in ora locale del tenant. Campo per-campagna
    (stringhe 'HH:MM') se presente, altrimenti WA_ACTIVE_HOURS globale
    (default '09:30-19:30', Europe/Rome, SDD 10.3). Si tronca ai minuti:
    la granularita' oraria basta al gate finestra (SDD Q68 propone
    lognormale semplice dentro l'ora, non picchi orari)."""
    start_s = getattr(campaign, "active_hours_start", None)
    end_s = getattr(campaign, "active_hours_end", None)
    if start_s and end_s:
        return int(start_s.split(":")[0]), int(end_s.split(":")[0])
    lo_s, hi_s = settings.wa_active_hours.split("-")
    return int(lo_s.split(":")[0]), int(hi_s.split(":")[0])
```

- [ ] **Step 4: Rilanciare il test e verificare che passi**

Run: `pytest backend/tests/test_wa_timing.py -v`
Expected: PASS (6 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_timing.py backend/tests/test_wa_timing.py
git commit -m "feat(wa): timing invio/sessione WA, riuso lognormale senza toccare utils/timing.py"
```

---

### Task 2: `wa_number_manager.py` — cap effettivo, contatore date-aware, cooldown

**Files:**
- Create: `backend/app/services/wa_number_manager.py`
- Test: `backend/tests/test_wa_number_manager.py`

**Interfaces:**
- Consumes: `app.models.wa.WaNumber` (`daily_cap`, `warmup_day`, `sent_today`, `sent_date`, `status`), `WaNumberStatus`, `app.models.wa.WaCampaign.daily_limit`, `settings.wa_daily_cap_default`, `settings.wa_warmup_steps`, `settings.wa_global_daily_cap`.
- Produces: `effective_wa_daily_cap(number, campaign) -> int`, `wa_sent_today(number) -> int`, `has_wa_send_budget(db, number, campaign) -> bool` (include il cap globale macchina), `record_wa_sent(db, number_id) -> None`, `apply_wa_cooldown(number_id, *, minutes) -> None`, `release_expired_wa_cooldowns() -> int` — usati da Task 11 (mini-sessione).

**Nota — `wa_numbers` non ha `cooldown_until`** (schema congelato, SDD 5.2/wa.py): a differenza di `InstagramAccount`, il cooldown WA non ha un timestamp di scadenza a DB. Si traccia con una chiave Redis a TTL (`wa:cooldown:{number_id}`), stesso stile del contatore soft-block di `browser_bio.py` (`_soft_block_incr`/`_soft_block_reset`). Se Redis viene azzerato, il numero resta in cooldown fino a resume manuale — accettabile: il resume manuale è comunque sempre disponibile via `app/api/wa_ops.py` (Task 14).

- [ ] **Step 1: Scrivere il test che fallisce**

```python
# backend/tests/test_wa_number_manager.py
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import wa_number_manager as wnm
from app.models.wa import WaNumberStatus


def _number(**over):
    base = dict(daily_cap=100, warmup_day=0, sent_today=0, sent_date=None,
                status=WaNumberStatus.active)
    base.update(over)
    return SimpleNamespace(**base)


def _campaign(daily_limit=None):
    return SimpleNamespace(daily_limit=daily_limit)


def test_effective_cap_uses_warmup_step_when_warming(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_daily_cap_default", 20)
    number = _number(daily_cap=200, warmup_day=3)  # 3o valore della lista = 30
    assert wnm.effective_wa_daily_cap(number, _campaign()) == 30


def test_effective_cap_past_warmup_uses_last_step(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    number = _number(daily_cap=200, warmup_day=99)
    assert wnm.effective_wa_daily_cap(number, _campaign()) == 100


def test_effective_cap_is_min_of_number_campaign_and_warmup(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    number = _number(daily_cap=200, warmup_day=7)   # step 7 = 100
    campaign = _campaign(daily_limit=15)
    assert wnm.effective_wa_daily_cap(number, campaign) == 15


def test_wa_sent_today_resets_on_new_day():
    number = _number(sent_today=45, sent_date="2000-01-01")
    assert wnm.wa_sent_today(number) == 0


def test_wa_sent_today_keeps_count_same_day():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    number = _number(sent_today=7, sent_date=today)
    assert wnm.wa_sent_today(number) == 7


@pytest.mark.asyncio
async def test_record_wa_sent_atomic_increment_with_rollover(db_session):
    from app.models.tenant import Tenant
    from app.models.wa import WaNumber
    import uuid

    tenant = Tenant(id=str(uuid.uuid4()), name="T", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(
        id=str(uuid.uuid4()), tenant_id=tenant.id, label="n", phone_hmac="h1",
        encrypted_phone="e1", daily_cap=100, warmup_day=0, sent_today=9,
        sent_date="2000-01-01",
    )
    db_session.add(number)
    await db_session.commit()

    await wnm.record_wa_sent(db_session, number.id)
    await db_session.refresh(number)
    assert number.sent_today == 1  # era di ieri: riparte da 1, non 10
    assert number.sent_date == datetime.utcnow().strftime("%Y-%m-%d")

    await wnm.record_wa_sent(db_session, number.id)
    await db_session.refresh(number)
    assert number.sent_today == 2  # stesso giorno: incrementa


@pytest.mark.asyncio
async def test_apply_and_release_wa_cooldown(monkeypatch):
    calls = {}

    class _FakeRedis:
        async def set(self, key, value, ex=None):
            calls["set"] = (key, value, ex)
        async def exists(self, key):
            return calls.get("exists_result", 0)
        async def aclose(self):
            pass

    async def _fake_pool(*a, **kw):
        return _FakeRedis()

    monkeypatch.setattr(wnm.arq, "create_pool", _fake_pool)
    await wnm.apply_wa_cooldown("num-1", minutes=30)
    assert calls["set"][0] == "wa:cooldown:num-1"
    assert calls["set"][2] == 30 * 60

    calls["exists_result"] = 0  # TTL scaduto in Redis
    released = await wnm.release_expired_wa_cooldowns()
    assert released == []  # nessun numero passato: serve la query DB (step successivo)
```

- [ ] **Step 2: Lanciare il test e verificare che fallisca**

Run: `pytest backend/tests/test_wa_number_manager.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.wa_number_manager'`.

- [ ] **Step 3: Implementare `wa_number_manager.py`**

```python
# backend/app/services/wa_number_manager.py
"""Cap/warmup/cooldown per i numeri WA. Pattern copiato da
account_manager.py (SDD 6.2: "il concetto si riusa, l'implementazione e'
cablata su InstagramAccount -> servizio wa_number_manager.py che replica
il pattern su wa_numbers, non si generalizza l'esistente in MVP" -- BT3).

wa_numbers NON ha cooldown_until (schema congelato): il timer di cooldown
vive in Redis (TTL), non a DB. Stesso stile del contatore soft-block di
browser_bio.py.
"""
from datetime import datetime

import arq
from loguru import logger

from app.config import settings
from app.services.work_enqueue import arq_redis_settings


def _utc_today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _parse_wa_warmup_steps(spec: str) -> list[int]:
    """"20,20,30,40,60,80,100" -> [20,20,30,40,60,80,100]. Lista ordinale
    (non range come account_manager.WARMUP_LIMITS): warmup_day 1-based
    indicizza direttamente, oltre la fine si resta sull'ultimo valore
    (regime raggiunto, SDD 10.3)."""
    return [int(x.strip()) for x in spec.split(",") if x.strip()]


def get_wa_warmup_cap(warmup_day: int) -> int:
    """Cap del giorno di warmup. warmup_day<=0 = fuori warmup: nessun tetto
    da qui (la composizione in effective_wa_daily_cap lo ignora)."""
    steps = _parse_wa_warmup_steps(settings.wa_warmup_steps)
    if not steps:
        return settings.wa_daily_cap_default
    idx = min(warmup_day, len(steps)) - 1
    return steps[max(0, idx)]


def effective_wa_daily_cap(number, campaign) -> int:
    """Minimo tra: daily_cap del numero (override admin), daily_limit della
    campagna (se impostato), e il gradino di warmup (se warmup_day>0).
    Nessuno di questi e' opzionale da solo -- e' la composizione ad
    AND che conta (contratto §... / SDD 10.3)."""
    candidates = [number.daily_cap]
    if getattr(campaign, "daily_limit", None) is not None:
        candidates.append(campaign.daily_limit)
    if (number.warmup_day or 0) > 0:
        candidates.append(get_wa_warmup_cap(number.warmup_day))
    return max(0, min(candidates))


def wa_sent_today(number) -> int:
    """Contatore di OGGI con reset lazy (stesso pattern di
    account_manager.effective_scrape_lookups / migrazione 018): se
    sent_date != oggi (UTC), il contatore e' di un giorno passato e vale 0
    senza dipendere da un cron di reset."""
    if getattr(number, "sent_date", None) != _utc_today_str():
        return 0
    return getattr(number, "sent_today", 0) or 0


async def has_wa_send_budget(db, number, campaign) -> bool:
    """Budget del NUMERO (cap effettivo) E del cap GLOBALE di macchina
    (WA_GLOBAL_DAILY_CAP, SDD Q70 -- safety valve su tutti i tenant)."""
    from sqlalchemy import select, func
    from app.models.wa import WaNumber

    if wa_sent_today(number) >= effective_wa_daily_cap(number, campaign):
        return False

    today = _utc_today_str()
    global_sent = await db.scalar(
        select(func.coalesce(func.sum(WaNumber.sent_today), 0)).where(
            WaNumber.sent_date == today,
        )
    ) or 0
    return int(global_sent) < settings.wa_global_daily_cap


async def record_wa_sent(db, number_id: str) -> None:
    """+1 atomico su sent_today, con rollover date-aware nella STESSA
    UPDATE (§4.2: mai read-modify-write, mai due statement separati per
    incremento e confronto data -- pattern scrape_lookups_date, mig. 018)."""
    from sqlalchemy import update, case
    from app.models.wa import WaNumber

    today = _utc_today_str()
    await db.execute(
        update(WaNumber).where(WaNumber.id == number_id).values(
            sent_today=case(
                (WaNumber.sent_date == today, WaNumber.sent_today + 1),
                else_=1,
            ),
            sent_date=today,
        )
    )
    await db.commit()


def _wa_cooldown_redis_key(number_id: str) -> str:
    return f"wa:cooldown:{number_id}"


async def apply_wa_cooldown(number_id: str, *, minutes: int) -> None:
    """Segnale di rischio (FM8-adiacente, SDD 8.3) -> status='cooldown' a
    DB (chiamante) + timer in Redis con TTL. Nessuna scrittura qui su
    WaNumber.status: e' compito del chiamante (wa_sender/wa_worker), che
    conosce il motivo da loggare nell'evento."""
    redis = await arq.create_pool(arq_redis_settings())
    try:
        await redis.set(_wa_cooldown_redis_key(number_id), "1", ex=minutes * 60)
    finally:
        await redis.aclose()


async def is_wa_cooldown_active(number_id: str) -> bool:
    redis = await arq.create_pool(arq_redis_settings())
    try:
        return bool(await redis.exists(_wa_cooldown_redis_key(number_id)))
    finally:
        await redis.aclose()


async def release_expired_wa_cooldowns() -> list[str]:
    """Per ogni WaNumber in status='cooldown', se la chiave Redis e'
    scaduta (TTL passato) lo riporta 'active'. Ritorna gli id rilasciati.
    Chiamato dal cron wa_session_healthcheck (Task 13), non da un timer
    a DB (non esiste una colonna cooldown_until)."""
    from sqlalchemy import select, update
    from app.database import AsyncSessionLocal
    from app.models.wa import WaNumber, WaNumberStatus

    released: list[str] = []
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WaNumber.id).where(WaNumber.status == WaNumberStatus.cooldown)
        )
        ids = [r[0] for r in result.all()]
        for number_id in ids:
            if not await is_wa_cooldown_active(number_id):
                await db.execute(
                    update(WaNumber).where(WaNumber.id == number_id)
                    .values(status=WaNumberStatus.active)
                )
                released.append(number_id)
        if released:
            await db.commit()
            logger.info(f"[WaNumberManager] cooldown rilasciato per {len(released)} numero/i")
    return released
```

- [ ] **Step 4: Rilanciare il test e verificare che passi**

Run: `pytest backend/tests/test_wa_number_manager.py -v`
Expected: PASS. (Il test `test_apply_and_release_wa_cooldown` esercita solo `apply_wa_cooldown` con un fake redis: `release_expired_wa_cooldowns` reale richiede DB + Redis, coperto in Task 13.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_number_manager.py backend/tests/test_wa_number_manager.py
git commit -m "feat(wa): cap effettivo, contatore date-aware e cooldown via Redis per wa_numbers"
```

---

### Task 3: kill-switch WA — `bot_state.wa_halted`, migrazione 027, e il primo Postgres vero

**Files:**
- Modify: `backend/app/models/bot_state.py`
- Create: `backend/alembic/versions/027_wa_halted.py`
- Modify: `backend/app/services/bot_state_service.py`
- Test: `backend/tests/test_bot_state_wa.py`

**Interfaces:**
- Consumes: `app.models.bot_state.BotState`, `app.services.bot_state_service` (`_ensure_row`, `is_halted`, `halt`, `resume` — esistenti, **invariate**).
- Produces: `is_wa_halted(db=None) -> bool`, `halt_wa(*, reason, by="user", db=None) -> bool`, `resume_wa(*, by="user", db=None) -> bool` — usati da Task 11 (mini-sessione) e Task 14 (API ops).

**Perché il kill-switch è per-canale (SDD §4.3):** `bot_state.halted` ferma i worker Instagram **in produzione**. Un incidente WhatsApp non deve fermare Instagram, e viceversa. Stessa tabella, campo nuovo, comandi separati.

- [ ] **Step 1: Scrivere PRIMA il test di non-regressione Instagram**

Il canale IG è in produzione: la modifica a `bot_state` porta il suo test di non-regressione **prima** della modifica (Global Constraints).

```python
# backend/tests/test_bot_state_wa.py
import pytest

from app.services import bot_state_service as bss


@pytest.mark.asyncio
async def test_halt_ig_non_ferma_wa(db_session):
    """Non-regressione IG + isolamento dei due canali: halt() e' il
    kill-switch Instagram e NON deve toccare wa_halted."""
    await bss.halt(reason="test IG", by="pytest", db=db_session)
    assert await bss.is_halted(db_session) is True
    assert await bss.is_wa_halted(db_session) is False
    await bss.resume(by="pytest", db=db_session)
    assert await bss.is_halted(db_session) is False


@pytest.mark.asyncio
async def test_halt_wa_non_ferma_ig(db_session):
    await bss.halt_wa(reason="test WA", by="pytest", db=db_session)
    assert await bss.is_wa_halted(db_session) is True
    assert await bss.is_halted(db_session) is False
    await bss.resume_wa(by="pytest", db=db_session)
    assert await bss.is_wa_halted(db_session) is False


@pytest.mark.asyncio
async def test_is_wa_halted_su_riga_assente_torna_false_e_non_solleva(db_session):
    """Fail-safe di lettura: se la riga singleton non esiste ancora,
    is_wa_halted deve rispondere False (nessun blocco fantasma), non
    esplodere dentro il check di un worker."""
    from sqlalchemy import delete
    from app.models.bot_state import BotState
    await db_session.execute(delete(BotState))
    await db_session.commit()
    assert await bss.is_wa_halted(db_session) is False
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_bot_state_wa.py -v`
Expected: FAIL con `AttributeError: module 'app.services.bot_state_service' has no attribute 'is_wa_halted'`.

- [ ] **Step 3: Aggiungere la colonna al modello**

```python
# backend/app/models/bot_state.py -- dentro class BotState, dopo last_resume_by
    # Kill-switch del canale WhatsApp, SEPARATO da `halted` (che resta di
    # Instagram, in produzione): un incidente su un canale non deve fermare
    # l'altro (SDD 4.3). Stessa tabella singleton, comandi dedicati.
    wa_halted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    wa_halted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    wa_halted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    wa_halted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

- [ ] **Step 4: Scrivere la migrazione 027**

```python
# backend/alembic/versions/027_wa_halted.py
"""kill-switch per-canale: wa_halted su bot_state

Additiva, nessun ALTER distruttivo. down_revision = "025": la 026 e' il
numero riservato a M2 (contratto §6.1) e potrebbe non esistere mai. Se al
rebase su main la 026 c'e', questo valore diventa "026" e si rifa' il ciclo
su-giu'-su (contratto §6.1).

Revision ID: 027
"""
from alembic import op
import sqlalchemy as sa

revision = "027"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default="0": la riga singleton esiste gia' in produzione e la
    # colonna e' NOT NULL -- senza default il backfill fallisce su Postgres.
    op.add_column("bot_state", sa.Column("wa_halted", sa.Boolean(), nullable=False,
                                          server_default=sa.text("0")))
    op.add_column("bot_state", sa.Column("wa_halted_reason", sa.Text(), nullable=True))
    op.add_column("bot_state", sa.Column("wa_halted_at", sa.DateTime(), nullable=True))
    op.add_column("bot_state", sa.Column("wa_halted_by", sa.String(255), nullable=True))


def downgrade() -> None:
    # batch_alter_table: SQLite non ha DROP COLUMN nativo prima della 3.35 e
    # alembic lo emula ricostruendo la tabella. Su Postgres e' un DROP COLUMN
    # normale.
    with op.batch_alter_table("bot_state") as batch:
        batch.drop_column("wa_halted_by")
        batch.drop_column("wa_halted_at")
        batch.drop_column("wa_halted_reason")
        batch.drop_column("wa_halted")
```

- [ ] **Step 5: Implementare le tre funzioni di servizio**

```python
# backend/app/services/bot_state_service.py -- in coda al file, senza toccare
# halt/resume/is_halted esistenti (sono di Instagram, in produzione)

async def is_wa_halted(db: AsyncSession | None = None) -> bool:
    """Kill-switch del canale WhatsApp. Fail-safe di lettura: se la riga
    singleton manca, torna False -- un errore qui bloccherebbe il canale
    per un motivo che non e' una decisione di nessuno."""
    if db is None:
        async with AsyncSessionLocal() as own_db:
            return await is_wa_halted(own_db)
    row = await db.scalar(select(BotState).limit(1))
    return bool(row.wa_halted) if row else False


async def halt_wa(*, reason: str, by: str = "user", db: AsyncSession | None = None) -> bool:
    if db is None:
        async with AsyncSessionLocal() as own_db:
            return await halt_wa(reason=reason, by=by, db=own_db)
    row = await _ensure_row(db)
    row.wa_halted = True
    row.wa_halted_reason = reason
    row.wa_halted_at = datetime.utcnow()
    row.wa_halted_by = by
    await db.commit()
    logger.warning(f"[WA KILL-SWITCH] canale WhatsApp fermato da {by}: {reason}")
    return True


async def resume_wa(*, by: str = "user", db: AsyncSession | None = None) -> bool:
    if db is None:
        async with AsyncSessionLocal() as own_db:
            return await resume_wa(by=by, db=own_db)
    row = await _ensure_row(db)
    row.wa_halted = False
    row.wa_halted_reason = None
    await db.commit()
    logger.info(f"[WA KILL-SWITCH] canale WhatsApp ripreso da {by}")
    return True
```

- [ ] **Step 6: Rilanciare i test**

Run: `pytest backend/tests/test_bot_state_wa.py -v`
Expected: PASS (3 test).

- [ ] **Step 7: Ciclo migrazione su-giù-su su SQLite**

```bash
cd backend
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
```
Expected: tre comandi a exit 0, nessun errore di colonna mancante. È lo stesso ciclo con cui è stata provata la 025.

- [ ] **Step 8: Applicare la catena a un Postgres VERO — è la prima volta**

⚠️ **La 025 non ha mai visto Postgres.** È stata provata solo su SQLite. Questo task è il primo che tocca Postgres davvero, e la differenza non è teorica: gli enum sono `native_enum=False` proprio per questo, e un `server_default` mancante su una colonna NOT NULL passa su SQLite vuoto e fallisce su una tabella popolata.

**Docker su questa macchina NON è installato** (verificato il 29/07). Il Postgres di prova è un **progetto Supabase nuovo e vuoto**: due minuti, gratis, stesso motore e stessa versione della produzione — quindi il test vale davvero — e si cancella dopo. Chiedere a Tommaso la connection string di quel progetto, e verificare **prima di ogni comando** che non sia quella di produzione.

```bash
# La stringa arriva da un progetto Supabase USA-E-GETTA, mai da .env.
export WA_PG_TEST="postgresql+asyncpg://postgres:<pwd>@<host-usa-e-getta>:5432/postgres"

# Controllo di sicurezza PRIMA di toccare qualsiasi cosa: se compare l'host di
# produzione, fermarsi.
echo "$WA_PG_TEST" | grep -q "$(grep -o 'db\.[a-z]*\.supabase\.co' ../.env | head -1)" \
  && echo "STOP: e' il DB di produzione" || echo "ok, non e' produzione"

DATABASE_URL="$WA_PG_TEST" alembic upgrade head
DATABASE_URL="$WA_PG_TEST" alembic downgrade -1
DATABASE_URL="$WA_PG_TEST" alembic upgrade head
```
Expected: la catena `024 → 025 → 027` sale, scende e risale pulita. Qualunque errore qui è **un difetto della 025 o della 027 da correggere adesso**, non al primo deploy.

⚠️ **Mai puntare `DATABASE_URL` al database di produzione per provare una migrazione**, e la ragione non è la prudenza generica (contratto §6.2): la **027 tocca `bot_state`**, la tabella del kill-switch di Instagram, viva e letta da ogni worker in produzione; e il valore del test è proprio il `downgrade`, che lì diventa un `DROP COLUMN` su una tabella in uso. Un `downgrade base` scritto al posto di `downgrade -1` cancella l'intero schema. L'8/07 sono nate 110 campagne fantasma in produzione partendo da un errore molto più piccolo.

Se il progetto usa-e-getta non è disponibile, **non ripiegare su prod**: si applica in avanti sulla produzione come **deploy** (una volta sola, con `pg_dump` fatto prima) e si scrive nella PR che **il percorso di rollback non è stato provato**. È una scelta legittima, ma va dichiarata, non sottintesa.

- [ ] **Step 9: Commit**

```bash
git add backend/app/models/bot_state.py backend/alembic/versions/027_wa_halted.py backend/app/services/bot_state_service.py backend/tests/test_bot_state_wa.py
git commit -m "feat(wa): kill-switch per-canale (wa_halted) + migrazione 027, IG non regredito"
```

---

### Task 4: `wa_optout.py` — STOP riconosciuto, DNC permanente per-tenant

**Files:**
- Create: `backend/app/services/wa_optout.py`
- Test: `backend/tests/test_wa_optout.py`

**Interfaces:**
- Consumes: `settings.wa_stop_words`, `app.models.wa` (`WaContact`, `WaCampaignContact`, `WaContactStatus`, `WaDncReason`, `WaCampaign`), `app.utils.events.emit`.
- Produces: `looks_like_stop(text) -> bool`, `persist_wa_optout(db, contact_id, *, prova, campaign_id=None) -> int` (ritorna quante `wa_campaign_contacts` ha fermato) — usati da Task 6 (guardia) e, in M4, dal watcher.

**Perché il tag è permanente e per-tenant (SDD §7.5, decisione 24/07):** lo STOP mette `opted_out` **e** `do_not_contact` sul contatto: non viene più ricontattato da **nessuna** campagna di quel tenant. La riattivazione esiste solo manuale, con motivazione (falso positivo). Un opt-out di troppo è meglio di uno mancato.

- [ ] **Step 1: Scrivere il test che fallisce**

```python
# backend/tests/test_wa_optout.py
import uuid

import pytest

from app.services import wa_optout


@pytest.mark.parametrize("testo", [
    "STOP",
    "stop",
    "Stop.",
    "  basta  ",
    "non scrivermi piu'",
    "CANCELLAMI da questa lista",
    "unsubscribe",
    "Va bene ma poi STOP grazie",
])
def test_looks_like_stop_riconosce_i_casi_plausibili(testo):
    assert wa_optout.looks_like_stop(testo) is True


@pytest.mark.parametrize("testo", [
    "",
    "ok grazie",
    "stopper",              # parola piu' lunga che CONTIENE stop
    "bastano due pezzi",    # 'bastano' non e' 'basta'
    "non scrivermi" ,       # NB: questo E' nella lista -> vedi test dedicato
])
def test_looks_like_stop_non_scatta_su_sottostringhe(testo):
    if testo == "non scrivermi":
        pytest.skip("frase presente nella lista: coperta dal test positivo")
    assert wa_optout.looks_like_stop(testo) is False


def test_looks_like_stop_su_none_non_solleva():
    """Finisce dentro una guardia: un'eccezione qui trasformerebbe un
    controllo di sicurezza in un crash che salta l'invio in modo casuale."""
    assert wa_optout.looks_like_stop(None) is False


@pytest.mark.asyncio
async def test_persist_wa_optout_ferma_tutte_le_campagne_del_tenant(db_session):
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus,
                               WaDncReason, WaNumber, WaSendCondition, WaSequenceStep)

    tenant = Tenant(id=str(uuid.uuid4()), name="T", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e")
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=f"c-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add_all([number, contact])
    await db_session.flush()
    # DUE campagne diverse dello stesso tenant: l'opt-out le ferma entrambe.
    righe = []
    for nome in ("camp-A", "camp-B"):
        camp = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id,
                          wa_number_id=number.id, name=nome,
                          campaign_type=WaCampaignType.marketing,
                          status=WaCampaignStatus.running)
        db_session.add(camp)
        await db_session.flush()
        cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=camp.id,
                               contact_id=contact.id, status=WaContactStatus.queued)
        db_session.add(cc)
        righe.append(cc)
    await db_session.commit()

    fermate = await wa_optout.persist_wa_optout(
        db_session, contact.id, prova="STOP")
    assert fermate == 2

    await db_session.refresh(contact)
    assert contact.opted_out is True
    assert contact.do_not_contact is True
    assert contact.dnc_reason == WaDncReason.optout
    assert contact.opted_out_at is not None
    for cc in righe:
        await db_session.refresh(cc)
        assert cc.status == WaContactStatus.opted_out


@pytest.mark.asyncio
async def test_persist_wa_optout_e_idempotente(db_session):
    """Un secondo STAOP sullo stesso contatto non deve rimettere in
    opted_out righe gia' terminali ne' contarle di nuovo."""
    from app.models.tenant import Tenant
    from app.models.wa import WaContact

    tenant = Tenant(id=str(uuid.uuid4()), name="T2", status="active")
    db_session.add(tenant)
    await db_session.flush()
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=f"c-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add(contact)
    await db_session.commit()

    primo = await wa_optout.persist_wa_optout(db_session, contact.id, prova="stop")
    secondo = await wa_optout.persist_wa_optout(db_session, contact.id, prova="stop")
    assert primo == 0 and secondo == 0
    await db_session.refresh(contact)
    assert contact.opted_out is True
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_optout.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.wa_optout'`.

- [ ] **Step 3: Implementare `wa_optout.py`**

```python
# backend/app/services/wa_optout.py
"""Opt-out del canale WhatsApp: riconoscimento dello STOP e persistenza del
tag DNC permanente per-tenant (SDD 7.5, decisione 24/07).

Due funzioni separate di proposito: `looks_like_stop` e' pura e testabile
senza DB (e' il giudizio), `persist_wa_optout` e' la scrittura. La guardia
pre-invio (wa_sender) usa entrambe; il watcher di M4 usera' le stesse.

"Visto una volta, vale per sempre": il DOM puo' smettere di mostrare lo
STOP (cronologia non sincronizzata, chat archiviata, messaggio cancellato),
la decisione no. Per questo la prova si scrive a DB e non si ricalcola.
"""
import re
from datetime import datetime

from loguru import logger
from sqlalchemy import select, update

from app.config import settings
from app.utils import events


def _stop_pattern() -> re.Pattern:
    """Parole/frasi intere, case-insensitive. \\b su entrambi i lati: senza,
    'stopper' verrebbe letto come uno STOP e un cliente perderebbe un
    contatto per una parola qualsiasi."""
    parole = [p.strip() for p in (settings.wa_stop_words or "").split(",") if p.strip()]
    if not parole:
        return re.compile(r"(?!x)x")  # non matcha mai: lista vuota = nessun STOP
    alternative = "|".join(re.escape(p) for p in parole)
    return re.compile(rf"\b({alternative})\b", re.IGNORECASE)


def looks_like_stop(text) -> bool:
    """True se il testo contiene una parola di opt-out. Non solleva MAI:
    finisce dentro una guardia di sicurezza, e un'eccezione qui
    trasformerebbe un controllo in un crash intermittente."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        return bool(_stop_pattern().search(text))
    except Exception as exc:  # pragma: no cover - difesa, non logica
        logger.error(f"looks_like_stop: pattern non valido ({exc}) -- "
                     "trattato come NESSUNO stop, il chiamante ha la sentinella")
        return False


async def persist_wa_optout(db, contact_id: str, *, prova: str,
                            campaign_id: str | None = None) -> int:
    """Marca il contatto opted_out + do_not_contact (permanente, per-tenant)
    e porta a `opted_out` tutte le sue righe campagna NON terminali, di
    QUALUNQUE campagna del tenant. Ritorna quante righe ha fermato.

    `prova` e' il testo dell'inbound che ha fatto scattare l'opt-out: si
    salva come prova dell'opposizione (SDD 7.5 punto 7). Il numero non
    compare: la riga e' agganciata a contact_id, che e' gia' pseudonimo.

    Idempotente: un secondo STOP non ricalcola nulla e non ri-conta righe
    gia' terminali.
    """
    from app.models.wa import (WaCampaignContact, WaContact, WaContactStatus,
                               WaDncReason)

    contact = await db.scalar(select(WaContact).where(WaContact.id == contact_id))
    if contact is None:
        logger.error(f"persist_wa_optout: contatto {contact_id} inesistente")
        return 0

    gia_optato = bool(contact.opted_out)
    if not gia_optato:
        contact.opted_out = True
        contact.opted_out_at = datetime.utcnow()
        contact.do_not_contact = True
        contact.dnc_reason = WaDncReason.optout

    terminali = (WaContactStatus.opted_out, WaContactStatus.completed,
                 WaContactStatus.skipped, WaContactStatus.replied)
    result = await db.execute(
        update(WaCampaignContact)
        .where(
            WaCampaignContact.contact_id == contact_id,
            WaCampaignContact.status.notin_(terminali),
        )
        .values(status=WaContactStatus.opted_out, next_action_at=None,
                locked_by=None, locked_at=None)
    )
    fermate = result.rowcount or 0
    await db.commit()

    logger.warning(
        f"[WA OPTOUT] contatto={contact_id} righe_fermate={fermate} "
        f"prova={prova[:60]!r}"
    )
    if campaign_id:
        events.emit(campaign_id, "wa.optout",
                    f"contatto {contact_id}: STOP rilevato, {fermate} sequenze fermate",
                    level="warning")
    return fermate
```

**Nota sul `next_action_at=None` qui dentro:** è l'unico punto in cui M3 lo azzera, e vale solo su righe che stanno **uscendo** verso uno stato terminale. Non contraddice l'invariante I3 del contratto (che riguarda le righe **non** terminali).

- [ ] **Step 4: Rilanciare i test**

Run: `pytest backend/tests/test_wa_optout.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_optout.py backend/tests/test_wa_optout.py
git commit -m "feat(wa): opt-out permanente per-tenant, STOP su parole intere"
```

---

### Task 5: `wa_sender` — apertura chat e mappa segnale → esito

**Files:**
- Create: `backend/app/services/wa_sender.py`
- Test: `backend/tests/test_wa_sender.py`

**Interfaces:**
- Consumes: `app.browser.whatsapp_page.WhatsAppWebPage` e `OpenResult` (M1, frozen), `app.models.wa` (`WaContactStatus`, `WaDncReason`), `settings.wa_max_failures_per_contact`.
- Produces: `EsitoApertura` (dataclass: `puo_inviare: bool`, `esito_contatto: str | None`, `motivo: str`, `colpa_nostra: bool`), `valuta_apertura(res: OpenResult) -> EsitoApertura` — usata da Task 6 e Task 8.

**La regola in una riga (contratto §3):** `OpenResult.ok = True` significa **solo** che il composer è comparso. La cronologia sta in `signal`, per intero. Se questa funzione è sbagliata, la guardia salta con `ok=True` e si scrive a chi aveva detto STOP.

- [ ] **Step 1: Scrivere il test che fallisce**

```python
# backend/tests/test_wa_sender.py
import pytest

from app.browser.whatsapp_page import OpenResult
from app.services import wa_sender


def _ok(signal: str) -> OpenResult:
    return OpenResult(True, 1234.0, signal)


def test_invia_solo_con_cronologia_agganciata():
    esito = wa_sender.valuta_apertura(_ok("cronologia:div[data-id]:37"))
    assert esito.puo_inviare is True
    assert esito.esito_contatto is None


def test_ok_true_ma_zero_messaggi_non_invia():
    """ok=True dice solo 'composer comparso'. Zero bolle agganciate = chat
    vuota o DOM che mente: in entrambi i casi non si scrive."""
    esito = wa_sender.valuta_apertura(_ok("cronologia:div[data-id]:0"))
    assert esito.puo_inviare is False


def test_conteggio_non_parsabile_non_invia():
    """Un segnale che non si sa leggere e' un segnale che dice no."""
    esito = wa_sender.valuta_apertura(_ok("cronologia:div[data-id]:molti"))
    assert esito.puo_inviare is False
    assert esito.colpa_nostra is True


@pytest.mark.parametrize("signal,atteso", [
    ("nessuna-cronologia:nessun-messaggio-nel-pannello", "skipped"),
    ("nessuna-cronologia:sezione-chat-vuota:nessuna-conversazione-esistente", "skipped"),
    ("nessuna-cronologia:nessuna-sezione-chat:solo-gruppi-o-contatti-senza-conversazione", "skipped"),
])
def test_chat_inesistente_e_colpa_del_contatto_non_nostra(signal, atteso):
    esito = wa_sender.valuta_apertura(OpenResult(False, 1.0, signal))
    assert esito.puo_inviare is False
    assert esito.esito_contatto == atteso
    assert esito.motivo == "no_existing_chat"
    assert esito.colpa_nostra is False


@pytest.mark.parametrize("signal", [
    "nessuna-cronologia:casella-ricerca-non-trovata",
    "nessuna-cronologia:ricerca-non-svuotata",
    "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio",
])
def test_guasti_nostri_non_bruciano_il_contatto(signal):
    """Un selettore rotto non deve bruciare una lista (SDD 11): il contatto
    resta queued, e' il NUMERO che si ferma."""
    esito = wa_sender.valuta_apertura(OpenResult(False, 1.0, signal))
    assert esito.puo_inviare is False
    assert esito.esito_contatto is None      # nessuna transizione di stato
    assert esito.colpa_nostra is True


def test_nessun_risultato_di_ricerca_e_ambiguo_e_non_decide_da_solo():
    """Puo' essere un numero non su WhatsApp o una ricerca rotta: chi
    chiama decide con il contesto della sessione (contratto §3.3)."""
    esito = wa_sender.valuta_apertura(
        OpenResult(False, 1.0, "nessuna-cronologia:nessun-risultato-di-ricerca"))
    assert esito.puo_inviare is False
    assert esito.esito_contatto is None
    assert esito.motivo == "ricerca_senza_risultati"
    assert esito.colpa_nostra is False


def test_segnale_sconosciuto_e_trattato_come_colpa_nostra():
    """Un segnale che il POM non produce oggi (versione futura, bug) non
    deve mai finire nel ramo 'skipped': si ferma il numero, non si brucia
    il contatto."""
    esito = wa_sender.valuta_apertura(OpenResult(False, 1.0, "boh:qualcosa-di-nuovo"))
    assert esito.puo_inviare is False
    assert esito.esito_contatto is None
    assert esito.colpa_nostra is True
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_sender.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.wa_sender'`.

- [ ] **Step 3: Implementare `valuta_apertura`**

```python
# backend/app/services/wa_sender.py
"""Invio di UN messaggio WhatsApp: apertura chat, guardie, invio.

Il POM (whatsapp_page.py) espone segnali e non decide; la politica sta
qui. Ogni funzione di questo modulo che decide "si invia / non si invia"
e' pura o quasi, perche' deve essere provabile senza browser: le tre volte
in cui M1 ha sbagliato una guardia, il difetto era nel giudizio, non nel
DOM.
"""
from dataclasses import dataclass

from loguru import logger

from app.browser.whatsapp_page import OpenResult

# Segnali del POM che significano "la chat 1:1 non esiste": colpa del dato,
# non nostra. Copiati alla lettera da whatsapp_page.open_chat /
# _apri_chat_da_risultati / _history_signal: se cambiano li', questo modulo
# smette di riconoscerli e cade nel ramo fail-closed (colpa nostra), che e'
# il fallimento giusto.
_SEGNALI_CHAT_INESISTENTE = (
    "nessuna-cronologia:nessun-messaggio-nel-pannello",
    "nessuna-cronologia:sezione-chat-vuota:nessuna-conversazione-esistente",
    "nessuna-cronologia:nessuna-sezione-chat:solo-gruppi-o-contatti-senza-conversazione",
)

# Segnali che dicono "la pagina non era nello stato che ci aspettavamo":
# infrastruttura nostra. Il contatto NON si tocca.
_SEGNALI_COLPA_NOSTRA = (
    "nessuna-cronologia:casella-ricerca-non-trovata",
    "nessuna-cronologia:ricerca-non-svuotata",
    "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio",
)

_SEGNALE_RICERCA_VUOTA = "nessuna-cronologia:nessun-risultato-di-ricerca"


@dataclass
class EsitoApertura:
    puo_inviare: bool
    esito_contatto: str | None   # 'skipped' | None (None = non si tocca lo stato)
    motivo: str
    colpa_nostra: bool           # True -> conta verso l'escalation FM2 del numero


def valuta_apertura(res: OpenResult) -> EsitoApertura:
    """Traduce (ok, signal) del POM nella decisione. Contratto §3.1-3.2.

    Fail-closed su tutto cio' che non e' riconosciuto: un segnale nuovo
    (POM aggiornato, WhatsApp cambiato) non deve mai finire nel ramo che
    marca il contatto, perche' quello e' irreversibile per il contatto e
    invisibile a chi guarda i log.
    """
    signal = res.signal or ""

    if res.ok and signal.startswith("cronologia:"):
        try:
            n = int(signal.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            logger.error(f"valuta_apertura: conteggio illeggibile in {signal!r} -- "
                         "non si invia")
            return EsitoApertura(False, None, "segnale_illeggibile", True)
        if n >= 1:
            return EsitoApertura(True, None, f"cronologia:{n}", False)
        return EsitoApertura(False, None, "cronologia_vuota", True)

    if signal in _SEGNALI_CHAT_INESISTENTE:
        return EsitoApertura(False, "skipped", "no_existing_chat", False)

    if signal in _SEGNALI_COLPA_NOSTRA:
        return EsitoApertura(False, None, signal.split(":", 1)[1], True)

    if signal == _SEGNALE_RICERCA_VUOTA:
        # Ambiguo: lo scioglie il chiamante col contesto di sessione (§3.3).
        return EsitoApertura(False, None, "ricerca_senza_risultati", False)

    logger.error(f"valuta_apertura: segnale non catalogato {signal!r} -- "
                 "trattato come guasto nostro, il contatto non si tocca")
    return EsitoApertura(False, None, "segnale_non_catalogato", True)
```

- [ ] **Step 4: Rilanciare i test**

Run: `pytest backend/tests/test_wa_sender.py -v`
Expected: PASS (11 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_sender.py backend/tests/test_wa_sender.py
git commit -m "feat(wa): mappa segnale apertura -> esito, fail-closed sui segnali non catalogati"
```

---

### Task 6: la guardia pre-invio — cecità, STOP, quarantena risync, incoerenza DB↔DOM

**Files:**
- Modify: `backend/app/services/wa_sender.py`
- Test: `backend/tests/test_wa_sender.py` (aggiunte)

**Interfaces:**
- Consumes: `WhatsAppWebPage.load_history`, `.read_inbound_tail`, `.sync_state` (M1, frozen); `wa_optout.looks_like_stop`; `settings.wa_guard_tail_n`, `settings.wa_guard_history_min`, `settings.wa_resync_quarantine_min`.
- Produces: `EsitoGuardia` (dataclass: `puo_inviare: bool`, `motivo: str`, `prova: str | None`), `async guardia_pre_invio(pom, *, gia_scritto_prima: bool, browser_avviato_da_s: float) -> EsitoGuardia` — usata da Task 8.

**Le tre cose che questa funzione non deve sbagliare:**

1. **Cecità ≠ silenzio.** `read_inbound_tail()` torna `None` per cecità e `[]` per silenzio. Trattarli uguale fa concludere "nessuno STOP" e inviare **sempre**, sembrando funzionare. In M1 esisteva un test che codificava proprio il comportamento sbagliato.
2. **La cronologia va caricata prima.** Senza `load_history`, nel DOM restano ~17 messaggi degli ultimi minuti e uno STOP di venti minuti prima **non esiste** (misurato in M0).
3. **`unknown` non vale `synced`.** Il selettore di sincronizzazione non è catalogato: al suo posto valgono la quarantena post-riconnessione e il controllo di incoerenza DB↔DOM (contratto §3.4).

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_sender.py -- in coda
class _PomFinto:
    """Doppio del POM: nessun browser. Ogni test costruisce lo scenario
    dichiarando cosa 'vede' il DOM."""
    def __init__(self, tail, *, history_ok=True, count=30, sync="unknown"):
        self._tail = tail
        self._history_ok = history_ok
        self._count = count
        self._sync = sync
        self.load_history_chiamata = False

    async def load_history(self, minimo: int = 80):
        from app.browser.whatsapp_page import HistoryInfo
        self.load_history_chiamata = True
        return HistoryInfo(ok=self._history_ok, before=0, after=self._count,
                           rounds=1, exhausted=True)

    async def read_inbound_tail(self, n: int = 40):
        return self._tail

    async def sync_state(self):
        return self._sync


@pytest.mark.asyncio
async def test_guardia_blocca_su_stop_in_coda():
    pom = _PomFinto(["ciao", "STOP", "ah no scusa"])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "optout"
    assert "STOP" in esito.prova


@pytest.mark.asyncio
async def test_guardia_blocca_su_stop_seguito_da_altri_messaggi():
    """Uno STOP seguito da altro NON diventa invisibile: la coda si legge
    tutta, non ci si ferma al primo messaggio."""
    pom = _PomFinto(["STOP", "cmq grazie", "buona giornata"])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False


@pytest.mark.asyncio
async def test_guardia_blocca_su_cecita_del_dom():
    """None = nessuna bolla agganciata. NON e' 'nessuno STOP'."""
    pom = _PomFinto(None)
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "coda_non_agganciata"


@pytest.mark.asyncio
async def test_guardia_passa_su_silenzio_vero():
    """[] = bolle presenti, nessun inbound: questo si', si invia."""
    pom = _PomFinto([])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is True


@pytest.mark.asyncio
async def test_guardia_carica_sempre_la_cronologia_prima_di_leggere():
    pom = _PomFinto([])
    await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert pom.load_history_chiamata is True


@pytest.mark.asyncio
async def test_quarantena_post_riconnessione_blocca(monkeypatch):
    """Nei primi minuti dopo l'avvio del browser la sincronizzazione e'
    ancora in corso e la guardia leggerebbe il vuoto (A9/FM16)."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 15)
    pom = _PomFinto([])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=60)
    assert esito.puo_inviare is False
    assert esito.motivo == "quarantena_risync"


@pytest.mark.asyncio
async def test_incoerenza_db_dom_blocca(monkeypatch):
    """Il DB dice che a questo contatto avevamo gia' scritto, il DOM mostra
    zero messaggi: il DOM sta mentendo (chat non sincronizzata)."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    pom = _PomFinto([], count=0)
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=True, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "incoerenza_db_dom"


@pytest.mark.asyncio
async def test_sync_state_synced_non_e_richiesto_ma_syncing_blocca(monkeypatch):
    """Oggi sync_state torna sempre 'unknown' (selettore non catalogato):
    'unknown' non blocca da solo. Ma se un giorno tornera' 'syncing', quello
    deve bloccare senza altre modifiche."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    pom = _PomFinto([], sync="syncing")
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "sincronizzazione_in_corso"
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_sender.py -v -k "guardia or quarantena or incoerenza or sync_state"`
Expected: FAIL con `AttributeError: module 'app.services.wa_sender' has no attribute 'guardia_pre_invio'`.

- [ ] **Step 3: Implementare la guardia**

```python
# backend/app/services/wa_sender.py -- in coda
@dataclass
class EsitoGuardia:
    puo_inviare: bool
    motivo: str
    prova: str | None = None      # il testo dell'inbound che ha bloccato


async def guardia_pre_invio(pom, *, gia_scritto_prima: bool,
                            browser_avviato_da_s: float) -> EsitoGuardia:
    """Guardia opt-out/reply a chat APERTA. E' la garanzia strutturale del
    canale (SDD 7.2): con questa, uno STOP non e' mai scavalcabile, anche
    tra campagne distanti mesi e anche se lo scan lista lo ha perso.

    Costo misurato in M0: mediana 5,7 s, p95 7,5 s, max 12,1 s -- di cui la
    quasi totalita' e' il caricamento della cronologia, che e' PARTE della
    guardia e non e' aggirabile (la conversazione e' virtualizzata).

    Ordine dei controlli scelto per costo crescente: prima quelli che non
    toccano il DOM.
    """
    from app.config import settings
    from app.services import wa_optout

    # 1. Quarantena post-riconnessione (contratto §3.4.2). Costo zero, e
    #    copre la finestra in cui QUALUNQUE lettura sarebbe inaffidabile.
    #    ATTENZIONE: 15 minuti e' un valore STIMATO, non misurato -- si
    #    rimisura quando il selettore SYNC_INDICATOR verra' catturato.
    quarantena_s = float(settings.wa_resync_quarantine_min) * 60
    if browser_avviato_da_s < quarantena_s:
        return EsitoGuardia(False, "quarantena_risync")

    # 2. Indicatore di sincronizzazione. Oggi torna sempre 'unknown' e
    #    'unknown' NON vale 'synced': semplicemente non e' un segnale.
    #    'syncing' invece blocca -- e blocchera' da solo il giorno in cui il
    #    selettore sara' catalogato, senza toccare questo codice.
    if await pom.sync_state() == "syncing":
        return EsitoGuardia(False, "sincronizzazione_in_corso")

    # 3. Caricare la cronologia FA PARTE della guardia: senza, nel DOM
    #    restano ~17 messaggi degli ultimi minuti e uno STOP di venti minuti
    #    prima non esiste (misurato in M0).
    info = await pom.load_history(minimo=int(settings.wa_guard_history_min))

    # 4. Incoerenza DB<->DOM: se avevamo gia' scritto a questo contatto e il
    #    DOM mostra zero messaggi, la chat non e' sincronizzata. Vale una
    #    query e chiude la falla piu' pericolosa che ci resta aperta.
    if gia_scritto_prima and info.after == 0:
        return EsitoGuardia(False, "incoerenza_db_dom")

    # 5. Coda inbound. None = CECITA' (nessuna bolla agganciata, o righe
    #    malformate): non e' silenzio, e non si invia. [] = silenzio vero.
    coda = await pom.read_inbound_tail(n=int(settings.wa_guard_tail_n))
    if coda is None:
        return EsitoGuardia(False, "coda_non_agganciata")

    for testo in coda:
        if wa_optout.looks_like_stop(testo):
            return EsitoGuardia(False, "optout", prova=testo[:300])

    # Una risposta qualsiasi ferma la sequenza (SDD 7.4, decisione 24/07),
    # ma NON e' questa funzione a marcarlo: qui si dice solo che c'e'.
    if coda:
        return EsitoGuardia(False, "ha_risposto", prova=coda[-1][:300])

    return EsitoGuardia(True, "silenzio")
```

**Nota su `ha_risposto`:** in MVP le campagne sono a un solo messaggio, quindi un contatto che ha risposto **prima** dell'unico invio è un contatto che non va disturbato. Il chiamante (Task 8) lo porta a `replied`. Quando M4 accenderà il multi-step, la stessa informazione servirà a valutare `if_no_reply` senza cambiare questa funzione.

- [ ] **Step 4: Rilanciare i test**

Run: `pytest backend/tests/test_wa_sender.py -v`
Expected: PASS (19 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_sender.py backend/tests/test_wa_sender.py
git commit -m "feat(wa): guardia pre-invio -- cecita' != silenzio, quarantena risync, incoerenza DB/DOM"
```

---

### Task 7: preparazione del testo — template WA, placeholder, CTA opt-out

**Files:**
- Modify: `backend/app/services/wa_sender.py`
- Test: `backend/tests/test_wa_sender.py` (aggiunte)

**Interfaces:**
- Consumes: `app.services.wa_template.pick_wa_template`, `.render_wa_template`, `TemplateRenderError` (M2/PR-0, firme congelate dal contratto §2.4); `app.models.wa.WaSequenceStep`, `WaCampaign`, `WaContact`.
- Produces: `prepara_testo(step, contact, campaign) -> tuple[str, str]` (testo finale, variante) — usata da Task 8.

**Due regole che vengono dal contratto, non da qui:**
- La CTA opt-out si appende **solo allo step 0** e **solo se `optout_enabled`** (§2.1, SDD §7.2). `render_wa_template` non la tocca: l'append è di M3, dopo il render.
- `optout_enabled` è di M2 in scrittura. M3 lo legge e obbedisce, non lo calcola.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_sender.py -- in coda
from types import SimpleNamespace


def _step(**over):
    base = dict(step_index=0, template_a="Ciao {nome}, promo attiva.",
                template_b=None, template_c=None, template_d=None)
    base.update(over)
    return SimpleNamespace(**base)


def _campaign(**over):
    base = dict(optout_enabled=True, optout_cta="Scrivi STOP per non ricevere piu' messaggi.")
    base.update(over)
    return SimpleNamespace(**base)


def _contact(**over):
    base = dict(display_name="Marco", attributes=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_cta_appesa_solo_allo_step_zero():
    testo, variante = wa_sender.prepara_testo(_step(step_index=0), _contact(), _campaign())
    assert testo.endswith("Scrivi STOP per non ricevere piu' messaggi.")
    assert variante == "a"

    testo2, _ = wa_sender.prepara_testo(_step(step_index=1), _contact(), _campaign())
    assert "STOP" not in testo2


def test_cta_non_appesa_se_optout_disabilitato():
    testo, _ = wa_sender.prepara_testo(
        _step(), _contact(), _campaign(optout_enabled=False))
    assert "STOP" not in testo


def test_cta_vuota_con_optout_attivo_solleva():
    """Una campagna marketing senza via d'uscita non deve partire. M2 lo
    blocca a 422 in creazione; qui e' la seconda rete, perche' i dati a DB
    possono essere stati scritti prima di quella validazione."""
    with pytest.raises(ValueError):
        wa_sender.prepara_testo(_step(), _contact(),
                                _campaign(optout_cta="   "))


def test_placeholder_mancante_solleva_e_non_manda_messaggio_monco():
    from app.services.wa_template import TemplateRenderError
    step = _step(template_a="Ciao {nome}, il tuo ultimo ordine e' del {ultimo_ordine}.")
    with pytest.raises(TemplateRenderError):
        wa_sender.prepara_testo(step, _contact(attributes={}), _campaign())


def test_placeholder_presente_viene_valorizzato():
    step = _step(template_a="Ciao {nome}, ultimo ordine {ultimo_ordine}.")
    testo, _ = wa_sender.prepara_testo(
        step, _contact(attributes={"ultimo_ordine": "10/01/2026"}), _campaign())
    assert "10/01/2026" in testo and "Marco" in testo
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_sender.py -v -k "prepara or cta or placeholder"`
Expected: FAIL con `AttributeError: module 'app.services.wa_sender' has no attribute 'prepara_testo'`.

- [ ] **Step 3: Implementare `prepara_testo`**

```python
# backend/app/services/wa_sender.py -- in coda
def prepara_testo(step, contact, campaign) -> tuple[str, str]:
    """(testo pronto da digitare, variante 'a'..'d').

    Il rendering vero sta in wa_template.py, che e' di M2 (contratto §2.4):
    qui si sceglie la variante, si rende, e si appende la CTA di opt-out --
    che il renderer NON deve conoscere, perche' e' una regola di campagna,
    non di template.
    """
    from app.services.wa_template import pick_wa_template, render_wa_template

    template, variante = pick_wa_template(step)
    testo = render_wa_template(
        template,
        display_name=getattr(contact, "display_name", None),
        attributes=getattr(contact, "attributes", None),
    )

    # CTA solo sul PRIMO messaggio della sequenza (SDD 7.2): ripeterla a
    # ogni step la trasforma in rumore, e l'obbligo ePrivacy riguarda il
    # primo contatto della campagna.
    if step.step_index == 0 and getattr(campaign, "optout_enabled", False):
        cta = (getattr(campaign, "optout_cta", None) or "").strip()
        if not cta:
            raise ValueError(
                "Campagna con optout_enabled=True e optout_cta vuota: non si "
                "manda marketing senza via d'uscita (contratto §2.1)."
            )
        testo = f"{testo}\n\n{cta}"

    return testo, variante
```

- [ ] **Step 4: Rilanciare i test**

Run: `pytest backend/tests/test_wa_sender.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_sender.py backend/tests/test_wa_sender.py
git commit -m "feat(wa): preparazione testo -- render via wa_template, CTA opt-out solo sullo step 0"
```

---

### Task 8: `invia_a_contatto` — l'invio, con la rilettura TOCTOU

**Files:**
- Modify: `backend/app/services/wa_sender.py`
- Test: `backend/tests/test_wa_sender.py` (aggiunte)

**Interfaces:**
- Consumes: tutto quanto sopra + `app.utils.crypto.decrypt`, `app.utils.phone_pseudonym.mask_phone`, `app.services.wa_number_manager.record_wa_sent`, `app.services.wa_optout.persist_wa_optout`, `app.utils.events.emit`.
- Produces: `EsitoInvio` (dataclass: `stato: str`, `motivo: str`) e `async invia_a_contatto(db, pom, *, campaign, step, cc, contact, number, browser_avviato_da_s) -> EsitoInvio` — usata da Task 11 (mini-sessione).

**La finestra TOCTOU (contratto §3.5):** fra la guardia e l'invio passano ~20 s misurati in M0 (`guardia_totale_ms` mediana 22,2 s). Uno STOP che arriva **dentro** quella finestra non è stato visto. La seconda lettura costa poco — la cronologia è già caricata — ed è il motivo per cui `load_history` e `read_inbound_tail` sono metodi **separati** nel POM.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_sender.py -- in coda
class _PomInvio(_PomFinto):
    """Estende il doppio con il composer: registra cosa e' stato digitato e
    permette di far comparire uno STOP TRA la guardia e l'invio."""
    def __init__(self, tail, *, tail_seconda_lettura=None, **kw):
        super().__init__(tail, **kw)
        self._tail_seconda = tail if tail_seconda_lettura is None else tail_seconda_lettura
        self._letture = 0
        self.inviato = None
        self.tick = "Consegnato"

    async def read_inbound_tail(self, n: int = 40):
        self._letture += 1
        return self._tail if self._letture == 1 else self._tail_seconda

    async def open_chat(self, e164: str):
        from app.browser.whatsapp_page import OpenResult
        return OpenResult(True, 100.0, "cronologia:div[data-id]:30")

    async def send_text(self, text: str):
        self.inviato = text

    async def read_last_tick(self):
        return self.tick


@pytest.mark.asyncio
async def test_stop_arrivato_nella_finestra_toctou_annulla_l_invio(db_session, monkeypatch):
    """La guardia non aveva visto nulla; nei 20 secondi successivi arriva
    STOP. La seconda lettura lo intercetta e il messaggio NON parte."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    pom = _PomInvio([], tail_seconda_lettura=["STOP"])

    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)

    assert pom.inviato is None
    assert esito.stato == "opted_out"


@pytest.mark.asyncio
async def test_cecita_nella_seconda_lettura_annulla_l_invio(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    pom = _PomInvio([], tail_seconda_lettura=None)
    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert pom.inviato is None
    assert esito.stato == "queued"      # colpa nostra: il contatto non si brucia


@pytest.mark.asyncio
async def test_invio_riuscito_scrive_messaggio_stato_e_contatori(db_session, monkeypatch):
    from app.config import settings
    from sqlalchemy import select
    from app.models.wa import WaMessage, WaMessageStatus, WaContactStatus
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    pom = _PomInvio([])

    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)

    assert esito.stato == "sent"
    assert pom.inviato is not None and "STOP" in pom.inviato   # CTA step 0
    msg = await db_session.scalar(select(WaMessage).where(
        WaMessage.contact_id == ctx["contact"].id))
    assert msg.status == WaMessageStatus.sent
    assert msg.delivery_check is not None
    assert msg.rendered_text == pom.inviato
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].status == WaContactStatus.completed   # MVP: 1 solo step
    assert ctx["cc"].current_step == 0
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].sent == 1
    await db_session.refresh(ctx["number"])
    assert ctx["number"].sent_today == 1


@pytest.mark.asyncio
async def test_il_numero_in_chiaro_non_finisce_mai_nei_log(db_session, monkeypatch, caplog):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session, e164="+393421460077")
    pom = _PomInvio([])
    with caplog.at_level("DEBUG"):
        await wa_sender.invia_a_contatto(
            db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
            contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert "+393421460077" not in caplog.text
    assert "3421460077" not in caplog.text
```

Helper dello scenario (stesso file, sopra i test):

```python
async def _scenario_invio(db_session, e164: str = "+393331112223"):
    """Tenant + numero + contatto + campagna running + step 0, tutto a DB.
    Deliberatamente locale a questo file: i test devono restare eseguibili
    anche se factories_wa.py (M2/PR-0) cambia forma."""
    import uuid
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus, WaNumber,
                               WaSendCondition, WaSequenceStep)
    from app.utils.crypto import encrypt
    from app.utils.phone_pseudonym import hmac_phone

    tenant = Tenant(id=str(uuid.uuid4()), name="T", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"n-{uuid.uuid4()}", encrypted_phone=encrypt("+390000000000"))
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=hmac_phone(e164), encrypted_phone=encrypt(e164),
                        display_name="Marco")
    db_session.add_all([number, contact])
    await db_session.flush()
    campaign = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id,
                          wa_number_id=number.id, name="c",
                          campaign_type=WaCampaignType.marketing,
                          status=WaCampaignStatus.running, optout_enabled=True,
                          optout_cta="Scrivi STOP per non ricevere piu' messaggi.")
    db_session.add(campaign)
    await db_session.flush()
    step = WaSequenceStep(id=str(uuid.uuid4()), campaign_id=campaign.id, step_index=0,
                          template_a="Ciao {nome}, promo attiva.",
                          send_condition=WaSendCondition.always, wait_days=0)
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id,
                           contact_id=contact.id, status=WaContactStatus.queued,
                           current_step=-1)
    db_session.add_all([step, cc])
    await db_session.commit()
    return {"tenant": tenant, "number": number, "contact": contact,
            "campaign": campaign, "step": step, "cc": cc}
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_sender.py -v -k "invia or toctou or cecita_nella_seconda"`
Expected: FAIL con `AttributeError: module 'app.services.wa_sender' has no attribute 'invia_a_contatto'`.

- [ ] **Step 3: Implementare `invia_a_contatto`**

```python
# backend/app/services/wa_sender.py -- in coda
@dataclass
class EsitoInvio:
    stato: str      # 'sent' | 'queued' | 'skipped' | 'failed' | 'opted_out' | 'replied'
    motivo: str


async def invia_a_contatto(db, pom, *, campaign, step, cc, contact, number,
                           browser_avviato_da_s: float) -> EsitoInvio:
    """Invia UN messaggio a UN contatto, con tutte le guardie. Non decide
    cap, finestra oraria o kill-switch: quelli li ha gia' verificati la
    mini-sessione (Task 11) prima di chiamare qui.

    Il numero in chiaro esiste solo dentro questa funzione, in memoria, il
    tempo di aprire la chat (P12): si decifra qui e non si logga mai --
    tutti i log usano mask_phone.
    """
    from sqlalchemy import func, select, update

    from app.models.wa import (WaCampaignContact, WaContactStatus, WaDeliveryCheck,
                               WaCampaign, WaMessage, WaMessageStatus, WaSequenceStep)
    from app.services import wa_number_manager, wa_optout
    from app.utils import events
    from app.utils.crypto import decrypt
    from app.utils.phone_pseudonym import mask_phone

    e164 = decrypt(contact.encrypted_phone)
    masked = mask_phone(e164)

    # --- apertura chat -----------------------------------------------------
    apertura = valuta_apertura(await pom.open_chat(e164))
    if not apertura.puo_inviare:
        logger.info(f"[WA] {masked}: apertura -> {apertura.motivo} "
                    f"(colpa_nostra={apertura.colpa_nostra})")
        if apertura.esito_contatto == "skipped":
            await _marca_contatto(db, cc, WaContactStatus.skipped,
                                  errore=apertura.motivo)
            return EsitoInvio("skipped", apertura.motivo)
        if apertura.colpa_nostra:
            return EsitoInvio("queued", apertura.motivo)
        # ambiguo (ricerca senza risultati): conta il fallimento, non brucia
        await _incrementa_fallimento(db, cc, apertura.motivo)
        return EsitoInvio("queued", apertura.motivo)

    # --- guardia pre-invio -------------------------------------------------
    gia_scritto = bool(await db.scalar(
        select(func.count(WaMessage.id)).where(
            WaMessage.contact_id == contact.id,
            WaMessage.status == WaMessageStatus.sent,
        )
    ))
    guardia = await guardia_pre_invio(pom, gia_scritto_prima=gia_scritto,
                                      browser_avviato_da_s=browser_avviato_da_s)
    if not guardia.puo_inviare:
        return await _esito_guardia_negativa(db, cc, contact, campaign, guardia, masked)

    # --- testo -------------------------------------------------------------
    try:
        testo, variante = prepara_testo(step, contact, campaign)
    except Exception as exc:
        logger.error(f"[WA] {masked}: render fallito ({type(exc).__name__}) -- "
                     "il contatto va in failed, il numero continua")
        await _incrementa_fallimento(db, cc, f"render:{type(exc).__name__}")
        return EsitoInvio("failed", "render")

    msg = WaMessage(campaign_id=campaign.id, contact_id=contact.id,
                    wa_number_id=number.id, step_index=step.step_index,
                    template_variant=variante, rendered_text=testo,
                    status=WaMessageStatus.sending)
    db.add(msg)
    await db.commit()

    # --- RILETTURA TOCTOU: fra guardia e invio passano ~20s misurati -------
    # Non si ricarica la cronologia (gia' fatta dalla guardia): costa poco ed
    # e' l'unica difesa contro uno STOP arrivato nel frattempo.
    coda2 = await pom.read_inbound_tail(n=int(__import__("app.config", fromlist=["settings"]).settings.wa_guard_tail_n))
    if coda2 is None:
        msg.status = WaMessageStatus.skipped
        msg.error = "coda_non_agganciata_seconda_lettura"
        await db.commit()
        return EsitoInvio("queued", "cecita_toctou")
    for testo_in in coda2:
        if wa_optout.looks_like_stop(testo_in):
            msg.status = WaMessageStatus.skipped
            msg.error = "stop_nella_finestra_toctou"
            await db.commit()
            await wa_optout.persist_wa_optout(db, contact.id, prova=testo_in,
                                              campaign_id=campaign.id)
            await _incrementa_contatore_campagna(db, campaign.id, "opted_out")
            logger.warning(f"[WA] {masked}: STOP arrivato nella finestra TOCTOU, "
                           "invio annullato")
            return EsitoInvio("opted_out", "stop_toctou")

    # --- invio -------------------------------------------------------------
    try:
        await pom.send_text(testo)
    except Exception as exc:
        msg.status = WaMessageStatus.failed
        msg.error = f"{type(exc).__name__}: {exc}"[:500]
        await db.commit()
        await _incrementa_fallimento(db, cc, "send_text")
        await _incrementa_contatore_campagna(db, campaign.id, "failed")
        logger.error(f"[WA] {masked}: invio fallito ({type(exc).__name__})")
        return EsitoInvio("failed", "send_text")

    tick = await pom.read_last_tick()
    msg.status = WaMessageStatus.sent
    msg.sent_at = datetime.utcnow()
    msg.delivery_check = _delivery_da_tick(tick)
    await db.commit()

    # chat_title si impara qui, ma SOLO se e' un nome: se e' un numero,
    # salvarlo metterebbe PII in chiaro a DB (P12, contratto §4.1).
    await _impara_chat_title(db, pom, contact)

    await wa_number_manager.record_wa_sent(db, number.id)
    await _incrementa_contatore_campagna(db, campaign.id, "sent")
    await _avanza_contatto(db, cc, campaign, step)
    contact.last_contacted_at = datetime.utcnow()
    await db.commit()

    events.emit(campaign.id, "wa.message.sent",
                f"inviato a {masked} (variante {variante}, spunta {tick})")
    logger.info(f"[WA] {masked}: inviato, spunta={tick}")
    return EsitoInvio("sent", "ok")
```

Funzioni di supporto (stesso file):

```python
def _delivery_da_tick(tick: str):
    """La spunta e' testo LOCALIZZATO IN ITALIANO (SDD A4, Q39): un cliente
    con interfaccia in altra lingua la rompe. Non e' un gate -- e'
    best-effort e finisce solo in delivery_check."""
    from app.models.wa import WaDeliveryCheck
    t = (tick or "").lower()
    if "letto" in t:
        return WaDeliveryCheck.double_tick
    if "consegnato" in t:
        return WaDeliveryCheck.single_tick
    if "orolog" in t or "attesa" in t:
        return WaDeliveryCheck.clock
    return WaDeliveryCheck.none


async def _impara_chat_title(db, pom, contact) -> None:
    """Il titolo serve al watcher di M4 per agganciare le risposte. Si
    salva SOLO se e' un nome: title_is_number distingue i contatti non in
    rubrica (8 su 68 misurati in M0), e per quelli il matching usa gia'
    phone_hmac."""
    if contact.chat_title:
        return
    try:
        righe = await pom.scan_chat_list()
    except Exception as exc:
        logger.debug(f"chat_title non appreso ({type(exc).__name__}): non e' un errore")
        return
    if righe and not righe[0].title_is_number and righe[0].title:
        contact.chat_title = righe[0].title[:200]
        await db.commit()


async def _incrementa_contatore_campagna(db, campaign_id: str, campo: str) -> None:
    """UPDATE ... SET x = x + 1 in SQL (contratto §4.2). Mai leggere,
    sommare e riscrivere: con due worker si perdono conteggi in silenzio."""
    from sqlalchemy import update
    from app.models.wa import WaCampaign
    colonna = getattr(WaCampaign, campo)
    await db.execute(update(WaCampaign).where(WaCampaign.id == campaign_id)
                     .values({campo: colonna + 1}))
    await db.commit()


async def _marca_contatto(db, cc, stato, *, errore: str | None = None) -> None:
    cc.status = stato
    cc.last_error = errore
    cc.next_action_at = None
    cc.locked_by = None
    cc.locked_at = None
    await db.commit()


async def _incrementa_fallimento(db, cc, motivo: str) -> None:
    """failure_count + rinvio a 6 ore (contratto §3.3). Oltre soglia il
    contatto diventa non-raggiungibile: e' l'unica via per cui M3 scrive un
    DNC 'unreachable'."""
    from datetime import timedelta
    from app.config import settings
    from app.models.wa import WaContactStatus, WaDncReason
    from sqlalchemy import select
    from app.models.wa import WaContact

    cc.failure_count = (cc.failure_count or 0) + 1
    cc.last_error = motivo[:500]
    cc.next_action_at = datetime.utcnow() + timedelta(hours=6)
    if cc.failure_count >= int(settings.wa_max_failures_per_contact):
        cc.status = WaContactStatus.skipped
        cc.next_action_at = None
        contact = await db.scalar(select(WaContact).where(WaContact.id == cc.contact_id))
        if contact is not None:
            contact.do_not_contact = True
            contact.dnc_reason = (WaDncReason.invalid_number
                                  if motivo == "ricerca_senza_risultati"
                                  else WaDncReason.unreachable)
    await db.commit()


async def _avanza_contatto(db, cc, campaign, step) -> None:
    """Dopo un invio riuscito: current_step avanza e il contatto si chiude
    se non ci sono altri step. In MVP c'e' solo lo step 0, quindi la strada
    normale e' 'completed' -- ma la query sullo step successivo e' gia'
    qui, cosi' M4 accende il multi-step senza toccare questa funzione."""
    from datetime import timedelta
    from sqlalchemy import select
    from app.models.wa import WaContactStatus, WaSequenceStep

    cc.current_step = step.step_index
    prossimo = await db.scalar(
        select(WaSequenceStep)
        .where(WaSequenceStep.campaign_id == campaign.id,
               WaSequenceStep.step_index == step.step_index + 1)
    )
    if prossimo is None:
        cc.status = WaContactStatus.completed
        cc.next_action_at = None
    else:
        cc.status = WaContactStatus.in_sequence
        cc.next_action_at = datetime.utcnow() + timedelta(days=int(prossimo.wait_days or 0))
    cc.locked_by = None
    cc.locked_at = None
    await db.commit()


async def _esito_guardia_negativa(db, cc, contact, campaign, guardia, masked: str) -> EsitoInvio:
    """Traduce l'esito della guardia in stato del contatto. Le tre uscite
    non sono equivalenti: 'optout' e 'ha_risposto' sono verita' sul
    contatto, tutto il resto e' un limite NOSTRO e lascia la riga queued."""
    from app.models.wa import WaContactStatus
    from app.services import wa_optout

    if guardia.motivo == "optout":
        await wa_optout.persist_wa_optout(db, contact.id, prova=guardia.prova or "",
                                          campaign_id=campaign.id)
        await _incrementa_contatore_campagna(db, campaign.id, "opted_out")
        logger.warning(f"[WA] {masked}: STOP in coda, invio annullato")
        return EsitoInvio("opted_out", "stop")

    if guardia.motivo == "ha_risposto":
        cc.status = WaContactStatus.replied
        cc.next_action_at = None
        cc.locked_by = None
        cc.locked_at = None
        await db.commit()
        logger.info(f"[WA] {masked}: ha gia' risposto, la sequenza si ferma qui")
        return EsitoInvio("replied", "ha_risposto")

    logger.warning(f"[WA] {masked}: guardia negativa ({guardia.motivo}) -- "
                   "il contatto resta queued, non e' colpa sua")
    return EsitoInvio("queued", guardia.motivo)
```

⚠️ **Nota per l'implementatore:** la riga con `__import__("app.config", ...)` nella rilettura TOCTOU è scritta così solo per tenere il blocco leggibile in questo piano. **Nel codice vero si importa `settings` in cima al modulo**, come negli altri file. Se resta un `__import__` inline nel sorgente, il reviewer lo rifiuta.

- [ ] **Step 4: Rilanciare i test**

Run: `pytest backend/tests/test_wa_sender.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_sender.py backend/tests/test_wa_sender.py
git commit -m "feat(wa): invio con rilettura TOCTOU, delivery_check, stati e contatori atomici"
```

---

### Task 9: misurare `READ_LAST_TICK_TIMEOUT_MS` invece di ereditarlo

**Files:**
- Create: `backend/scripts/wa_measure_tick.py` (script diagnostico usa-e-getta, non codice di produzione)
- Modify: `backend/app/browser/whatsapp_selectors.py` — **solo** il valore della costante e il commento di provenienza, previo emendamento al contratto §9 (è patrimonio M1)

**Perché è un task e non una nota:** `read_last_tick` usa un timeout **mai ri-misurato**. `delivery_check` è l'unica prova che il messaggio è partito, e su di esso si decide se fatturare su `sent` confermati (SDD Q8). Un timeout troppo corto produce `none` sistematici su invii perfettamente riusciti; troppo lungo, rallenta ogni invio di secondi che si moltiplicano per il volume.

I numeri magici in questo cantiere hanno già fatto danno due volte: un `8000` al posto del `90000` misurato ha riportato una regressione già pagata, e un `20000` scritto a mano ha prodotto un falso "SESSIONE PERSA" perché la lista chat aveva agganciato dopo **19.820 ms**.

- [ ] **Step 1: Verificare il valore attuale e la sua provenienza**

```bash
grep -n "READ_LAST_TICK_TIMEOUT_MS" -A 4 -B 4 backend/app/browser/whatsapp_selectors.py
```
Annotare il valore e il commento presenti oggi. Se il commento **non** dice da quale misura viene, questo task ha già la sua risposta: non viene da nessuna.

- [ ] **Step 2: Scrivere lo script di misura**

Lo script apre un profilo **di test** (mai quello di PoC-1), apre una chat controllata, e campiona quanto ci mette la spunta a comparire dopo un invio, per N invii. Va lanciato **solo** dentro Task 15 (prova dal vivo), quando `WA_SEND_ENABLED` viene acceso a mano: prima non ci sono invii veri da misurare.

```python
# backend/scripts/wa_measure_tick.py
"""Misura quanto ci mette la spunta a comparire dopo un invio reale.

Usa-e-getta come gli script di M0: serve a produrre UN numero con la sua
provenienza, non a restare in produzione.

NON usare il profilo di PoC-1 (D:\\dev\\wa-poc\\profile): un re-scan del QR
azzera 14 giorni di misura. NON impostare PLAYWRIGHT_BROWSERS_PATH.
"""
import asyncio
import statistics
import sys
import time

from app.browser.whatsapp_page import WhatsAppWebPage
from app.services.wa_session import _open_wa_browser, WHATSAPP_WEB_URL


async def main(number_id: str, e164: str, testo: str, ripetizioni: int = 5):
    misure = []
    async with _open_wa_browser(number_id, headless=False, proxy_url=None) as context:
        page = await context.new_page()
        await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
        pom = WhatsAppWebPage(page)
        for i in range(ripetizioni):
            res = await pom.open_chat(e164)
            if not res.ok:
                print(f"[{i}] apertura fallita: {res.signal}")
                continue
            await pom.send_text(f"{testo} ({i + 1})")
            t0 = time.perf_counter()
            spunta = "nessuna-spunta-letta"
            while (time.perf_counter() - t0) < 60:
                spunta = await pom.read_last_tick()
                if spunta != "nessuna-spunta-letta":
                    break
                await asyncio.sleep(0.5)
            ms = (time.perf_counter() - t0) * 1000
            misure.append(ms)
            print(f"[{i}] spunta={spunta!r} dopo {round(ms)} ms")
            await asyncio.sleep(30)
    if misure:
        print(f"n={len(misure)} mediana={round(statistics.median(misure))} ms "
              f"max={round(max(misure))} ms")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
```

- [ ] **Step 3: Fissare la costante con la provenienza scritta accanto**

Dopo la misura (Task 15), il nuovo valore va scritto **con la sua storia**, nella stessa forma degli altri selettori di M1:

```python
# Timeout di lettura della spunta. Misurato il <data> su <n> invii reali:
# mediana <x> ms, max <y> ms -> valore = max * 2 arrotondato.
# Prima di questa misura era <vecchio valore>, ereditato e mai verificato.
READ_LAST_TICK_TIMEOUT_MS = <nuovo valore>
```

⚠️ **`whatsapp_selectors.py` è patrimonio M1**: la modifica richiede un emendamento al contratto §9 (una riga: data, cosa cambia, perché) prima del commit.

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/wa_measure_tick.py backend/app/browser/whatsapp_selectors.py docs/whatsapp/contratto-M2-M3.md
git commit -m "chore(wa): READ_LAST_TICK_TIMEOUT_MS misurato, non piu' ereditato"
```

---

### Task 10: claim atomico di un contatto alla volta

**Files:**
- Create: `backend/app/workers/wa_worker.py`
- Test: `backend/tests/test_wa_worker.py`

**Interfaces:**
- Consumes: modelli `wa_*`, `settings.wa_lock_timeout_min`.
- Produces: `async claim_next_wa_contact(db, *, number_id, worker_id) -> tuple | None` (ritorna `(cc, contact, campaign, step)` o `None`) — usata da Task 11.

**La query è nel contratto §7.3, verbatim.** È l'interfaccia vera fra M2 e M3: M2 produce righe che la soddisfano, M3 le consuma. Chi la cambia cambia il contratto.

**Un contatto alla volta, non un batch** (contratto §7.3): il lock si tiene per la durata di **un** invio (mediana misurata 47 s, p95 60 s). Con `stale_cutoff` a 20 minuti e delay di ~90 s fra messaggi, un claim a batch parcheggerebbe righe sotto lock per l'intera mini-sessione, e una sessione morta a metà le renderebbe invisibili a chiunque altro per venti minuti.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_worker.py
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.workers import wa_worker


@pytest.mark.asyncio
async def test_claim_prende_la_riga_pronta(db_session):
    ctx = await _scenario_claim(db_session)
    preso = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1")
    assert preso is not None
    cc, contact, campaign, step = preso
    assert cc.id == ctx["cc"].id
    assert cc.locked_by == "w1"
    assert step.step_index == 0


@pytest.mark.asyncio
async def test_claim_ignora_next_action_at_null(db_session):
    """Invariante I3 del contratto: una riga senza appuntamento non e' una
    riga da inviare subito, e' una riga rotta. Fail-closed."""
    ctx = await _scenario_claim(db_session)
    ctx["cc"].next_action_at = None
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


@pytest.mark.asyncio
async def test_claim_ignora_contatto_optato_fuori(db_session):
    """M2 filtra all'ingest, ma fra ingest e invio passano settimane
    (invariante I4): si ricontrolla live."""
    ctx = await _scenario_claim(db_session)
    ctx["contact"].opted_out = True
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


@pytest.mark.asyncio
async def test_claim_ignora_campagna_non_running_e_numero_non_active(db_session):
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.paused
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None

    ctx["campaign"].status = WaCampaignStatus.running
    ctx["number"].status = WaNumberStatus.qr_required
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


@pytest.mark.asyncio
async def test_claim_rispetta_lock_fresco_e_recupera_lock_stale(db_session):
    ctx = await _scenario_claim(db_session)
    ctx["cc"].locked_by = "altro-worker"
    ctx["cc"].locked_at = datetime.utcnow()
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None

    # lock vecchio di 21 minuti: la sessione che lo teneva e' morta
    ctx["cc"].locked_at = datetime.utcnow() - timedelta(minutes=21)
    await db_session.commit()
    preso = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1")
    assert preso is not None and preso[0].locked_by == "w1"


@pytest.mark.asyncio
async def test_due_worker_concorrenti_non_prendono_la_stessa_riga(db_session):
    """Concorrenza VERA (gather), non due chiamate in fila: e' l'unico modo
    in cui il bug si manifesta."""
    from app.database import AsyncSessionLocal
    ctx = await _scenario_claim(db_session)

    async def _claim(worker_id):
        async with AsyncSessionLocal() as db:
            return await wa_worker.claim_next_wa_contact(
                db, number_id=ctx["number"].id, worker_id=worker_id)

    a, b = await asyncio.gather(_claim("w1"), _claim("w2"))
    assert (a is None) != (b is None), "esattamente uno dei due deve vincere"


@pytest.mark.asyncio
async def test_claim_ignora_contatto_oltre_soglia_fallimenti(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_max_failures_per_contact", 3)
    ctx = await _scenario_claim(db_session)
    ctx["cc"].failure_count = 3
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None
```

Lo helper `_scenario_claim` è identico a `_scenario_invio` del Task 8 più `next_action_at = datetime.utcnow() - timedelta(minutes=1)` sulla riga `wa_campaign_contacts`: copiarlo in questo file (il piano ripete apposta invece di rimandare — chi esegue può leggere i task fuori ordine).

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_worker.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.workers.wa_worker'`.

- [ ] **Step 3: Implementare il claim**

```python
# backend/app/workers/wa_worker.py
"""Worker di invio del canale WhatsApp: mini-sessioni per-numero.

Calco dichiarato: services/browser_bio.py (claim atomico, Retry(defer) a
fine sessione, escalation su fallimenti consecutivi), applicato a
wa_campaign_contacts invece che a Follower. Le differenze rispetto a quel
file sono tutte commentate: dove non c'e' commento, e' lo stesso pattern.
"""
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import and_, or_, select, update

from app.config import settings


async def claim_next_wa_contact(db, *, number_id: str, worker_id: str):
    """Prende UNA riga pronta per questo numero e la marca sotto lock.
    Ritorna (cc, contact, campaign, step) oppure None.

    La SELECT e' la query di eleggibilita' del contratto §7.3: se cambia
    qui, cambia il contratto -- non e' un dettaglio di implementazione, e'
    l'interfaccia su cui M2 costruisce le proprie righe.
    """
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaContact, WaContactStatus, WaNumber, WaNumberStatus,
                               WaSequenceStep)

    now = datetime.utcnow()
    stale_cutoff = now - timedelta(minutes=int(settings.wa_lock_timeout_min))

    riga = (
        select(WaCampaignContact, WaContact, WaCampaign)
        .join(WaCampaign, WaCampaign.id == WaCampaignContact.campaign_id)
        .join(WaContact, WaContact.id == WaCampaignContact.contact_id)
        .join(WaNumber, WaNumber.id == WaCampaign.wa_number_id)
        .where(
            WaCampaign.status == WaCampaignStatus.running,
            WaNumber.status == WaNumberStatus.active,
            WaNumber.id == number_id,
            WaCampaignContact.status.in_([WaContactStatus.queued,
                                          WaContactStatus.in_sequence]),
            WaCampaignContact.next_action_at.is_not(None),
            WaCampaignContact.next_action_at <= now,
            or_(WaCampaignContact.locked_by.is_(None),
                WaCampaignContact.locked_at < stale_cutoff),
            WaCampaignContact.failure_count < int(settings.wa_max_failures_per_contact),
            WaContact.opted_out.is_(False),
            WaContact.do_not_contact.is_(False),
        )
        .order_by(WaCampaignContact.next_action_at)
        .limit(1)
    )
    result = (await db.execute(riga)).first()
    if result is None:
        return None
    cc, contact, campaign = result

    # Claim atomico: la WHERE ripete la condizione di lock. Se un altro
    # worker ha vinto la corsa fra SELECT e UPDATE, rowcount e' 0 e qui si
    # esce senza errore -- stesso pattern di browser_bio.claim_next_pending.
    claim = await db.execute(
        update(WaCampaignContact)
        .where(
            WaCampaignContact.id == cc.id,
            or_(WaCampaignContact.locked_by.is_(None),
                WaCampaignContact.locked_at < stale_cutoff),
        )
        .values(locked_by=worker_id, locked_at=now)
    )
    await db.commit()
    if (claim.rowcount or 0) == 0:
        logger.debug(f"claim perso su {cc.id} (un altro worker e' arrivato prima)")
        return None

    step = await db.scalar(
        select(WaSequenceStep).where(
            WaSequenceStep.campaign_id == campaign.id,
            WaSequenceStep.step_index == (cc.current_step or -1) + 1,
        )
    )
    if step is None:
        # Contatto senza step successivo: non e' lavoro, e' una riga da
        # chiudere. Si rilascia il lock e si lascia al chiamante decidere.
        await db.execute(update(WaCampaignContact).where(WaCampaignContact.id == cc.id)
                         .values(locked_by=None, locked_at=None,
                                 status=WaContactStatus.completed, next_action_at=None))
        await db.commit()
        return None

    await db.refresh(cc)
    return cc, contact, campaign, step
```

- [ ] **Step 4: Rilanciare i test**

Run: `pytest backend/tests/test_wa_worker.py -v`
Expected: PASS (7 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/wa_worker.py backend/tests/test_wa_worker.py
git commit -m "feat(wa): claim atomico un contatto alla volta, query di eleggibilita' del contratto"
```

---

### Task 11: la mini-sessione per-numero — cap in AND, kill-switch, escalation, defer

**Files:**
- Modify: `backend/app/workers/wa_worker.py`
- Test: `backend/tests/test_wa_worker.py` (aggiunte)

**Interfaces:**
- Consumes: `claim_next_wa_contact` (Task 10), `wa_sender.invia_a_contatto` (Task 8), `wa_number_manager.has_wa_send_budget` / `.apply_wa_cooldown` (Task 2), `wa_timing.*` (Task 1), `bot_state_service.is_wa_halted` (Task 3), `wa_session._open_wa_browser` (M1), `settings.wa_send_enabled`.
- Produces: `async esegui_mini_sessione(number_id: str) -> dict` (contatori dell'esecuzione) — usata da Task 12 (`wa_send_task`).

**I quattro cancelli in AND, tutti con query live e mai con contatori stale** (SDD §7.2): kill-switch WA ∧ finestra oraria ∧ cap numero (warmup, date-aware) ∧ cap campagna. Più `WA_SEND_ENABLED`, che è il quinto e sta sopra tutti.

**Perché short-lived** (lezione `job_timeout` della Fase Bio): mai `sleep` lunghi dentro il job. Finita la sessione o esaurito il budget, si esce con `Retry(defer=...)` e il worker riprende dopo la pausa. Il browser si chiude a fine sessione: tenerlo aperto ore è lo scenario in cui il processo muore e nessuno se ne accorge (FM18).

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_worker.py -- in coda
@pytest.mark.asyncio
async def test_send_enabled_false_non_apre_nemmeno_il_browser(db_session, monkeypatch):
    """Il master switch sta SOPRA tutto: a false non si apre un browser,
    non si claima una riga, non si tocca niente."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_send_enabled", False)
    aperto = {"si": False}

    def _boom(*a, **kw):
        aperto["si"] = True
        raise AssertionError("browser aperto con WA_SEND_ENABLED=false")

    monkeypatch.setattr(wa_worker, "_open_wa_browser", _boom)
    esito = await wa_worker.esegui_mini_sessione("num-x")
    assert esito["motivo"] == "send_disabled"
    assert aperto["si"] is False


@pytest.mark.asyncio
async def test_kill_switch_wa_ferma_la_sessione(db_session, monkeypatch):
    from app.config import settings
    from app.services import bot_state_service as bss
    monkeypatch.setattr(settings, "wa_send_enabled", True)

    async def _halted(db=None):
        return True
    monkeypatch.setattr(bss, "is_wa_halted", _halted)
    esito = await wa_worker.esegui_mini_sessione("num-x")
    assert esito["motivo"] == "wa_halted"


@pytest.mark.asyncio
async def test_kill_switch_acceso_a_meta_sessione_interrompe_dopo_il_messaggio_corrente(
        db_session, monkeypatch):
    """FM15: tutto si ferma entro il job corrente. Non a meta' di un invio:
    dopo quello in corso."""
    stato = {"halted": False, "inviati": 0}

    async def _fake_invio(*a, **kw):
        stato["inviati"] += 1
        stato["halted"] = True      # qualcuno preme il kill-switch adesso
        from app.services.wa_sender import EsitoInvio
        return EsitoInvio("sent", "ok")

    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, fake_invio=_fake_invio,
        halted_getter=lambda: stato["halted"], contatti=5)
    assert stato["inviati"] == 1
    assert esito["motivo"] == "wa_halted"


@pytest.mark.asyncio
async def test_cap_raggiunto_esce_con_defer_e_non_marca_i_contatti(db_session, monkeypatch):
    """Cap non e' un fallimento dei contatti: le righe restano queued."""
    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, budget=False, contatti=3)
    assert esito["motivo"] == "cap_esaurito"
    assert esito["inviati"] == 0
    assert esito["falliti"] == 0


@pytest.mark.asyncio
async def test_fuori_finestra_oraria_non_invia(db_session, monkeypatch):
    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, ora_corrente=4, contatti=3)
    assert esito["motivo"] == "fuori_finestra"


@pytest.mark.asyncio
async def test_tre_guasti_nostri_consecutivi_fermano_il_numero(db_session, monkeypatch):
    """FM2: selettori rotti -> stop invii del numero e campagna in error.
    I contatti NON diventano failed: e' colpa nostra."""
    from app.services.wa_sender import EsitoInvio

    async def _sempre_guasto(*a, **kw):
        return EsitoInvio("queued", "casella-ricerca-non-trovata")

    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, fake_invio=_sempre_guasto, contatti=5)
    assert esito["motivo"] == "guasti_consecutivi"
    assert esito["inviati"] == 0
    assert esito["falliti"] == 0
```

Il doppio `_mini_sessione_con_doppi` (stesso file) sostituisce browser, POM e orologio, così la mini-sessione si prova **senza aprire nulla**:

```python
async def _mini_sessione_con_doppi(db_session, monkeypatch, *, contatti=1,
                                   budget=True, ora_corrente=12,
                                   fake_invio=None, halted_getter=None):
    """Esercita esegui_mini_sessione con browser/POM/orologio finti.
    Il browser vero e' esercitato SOLO nel Task 15 (prova dal vivo): qui si
    prova la LOGICA, che e' dove hanno abitato tutti i difetti di M1."""
    import contextlib
    from app.config import settings
    from app.services import bot_state_service as bss, wa_number_manager as wnm
    from app.services.wa_sender import EsitoInvio

    monkeypatch.setattr(settings, "wa_send_enabled", True)

    async def _halted(db=None):
        return halted_getter() if halted_getter else False
    monkeypatch.setattr(bss, "is_wa_halted", _halted)

    async def _budget(*a, **kw):
        return budget
    monkeypatch.setattr(wnm, "has_wa_send_budget", _budget)

    @contextlib.asynccontextmanager
    async def _ctx(*a, **kw):
        class _Ctx:
            async def new_page(self):
                class _P:
                    async def goto(self, *a, **kw): return None
                return _P()
        yield _Ctx()
    monkeypatch.setattr(wa_worker, "_open_wa_browser", _ctx)
    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: ora_corrente)
    monkeypatch.setattr(wa_worker, "WhatsAppWebPage", lambda page: object())

    async def _invio(*a, **kw):
        return EsitoInvio("sent", "ok")
    monkeypatch.setattr(wa_worker.wa_sender, "invia_a_contatto",
                        fake_invio or _invio)
    monkeypatch.setattr(wa_worker.wa_timing, "wa_send_delay_seconds", lambda: 0.0)
    monkeypatch.setattr(wa_worker.wa_timing, "wa_session_message_count", lambda c: contatti)

    ctx = await _scenario_claim(db_session, contatti=contatti)
    return await wa_worker.esegui_mini_sessione(ctx["number"].id)
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_worker.py -v -k "mini or cap or kill or finestra or guasti"`
Expected: FAIL con `AttributeError: module 'app.workers.wa_worker' has no attribute 'esegui_mini_sessione'`.

- [ ] **Step 3: Implementare la mini-sessione**

```python
# backend/app/workers/wa_worker.py -- in coda
import asyncio
import time
import uuid
from zoneinfo import ZoneInfo

from app.browser.whatsapp_page import WhatsAppWebPage
from app.services import wa_sender, wa_timing
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser

# Fuso del tenant. Italiano fisso in MVP (SDD Q6: solo italiano, finestra
# oraria Europe/Rome); quando arrivera' il multi-lingua, questo diventa un
# campo del tenant, non una costante.
_TZ_TENANT = ZoneInfo("Europe/Rome")

# Quanti guasti NOSTRI consecutivi (selettori, pagina in stato inatteso) su
# chat diverse fermano il numero. Tre: sotto si rischia di fermarsi per un
# blip di rete, sopra si insiste su un DOM rotto sprecando la lista.
# Contratto §3.2.
MAX_GUASTI_CONSECUTIVI = 3


def _ora_locale_corrente() -> int:
    return datetime.now(_TZ_TENANT).hour


async def esegui_mini_sessione(number_id: str) -> dict:
    """Una mini-sessione di invii per UN numero. Short-lived: apre il
    browser, manda al piu' N messaggi (wa_timing), chiude e lascia che sia
    il worker a rischedulare dopo il break. Mai sleep lunghi qui dentro.

    Ritorna un dizionario di contatori: e' quello che il task ARQ logga e
    che i test leggono.
    """
    from app.database import AsyncSessionLocal
    from app.models.wa import WaCampaignStatus, WaCampaign, WaNumber, WaNumberStatus
    from app.services import bot_state_service, wa_number_manager
    from app.utils import events

    esito = {"inviati": 0, "falliti": 0, "saltati": 0, "motivo": "completata"}
    worker_id = f"wa-{number_id[:8]}-{uuid.uuid4().hex[:6]}"

    # Cancello 0: il master switch. A false non si apre nemmeno il browser.
    if not settings.wa_send_enabled:
        esito["motivo"] = "send_disabled"
        return esito

    # Cancello 1: kill-switch di canale (query live, non cache).
    if await bot_state_service.is_wa_halted():
        esito["motivo"] = "wa_halted"
        return esito

    async with AsyncSessionLocal() as db:
        number = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
        if number is None or number.status != WaNumberStatus.active:
            esito["motivo"] = "numero_non_attivo"
            return esito
        proxy_url = number.proxy_url
        if not proxy_url:
            # T3 della SDD: numeri diversi che escono dallo stesso IP
            # risultano correlati. Non blocca (in test non c'e' proxy), ma
            # deve essere rumoroso: il warning non compra i proxy, pero'
            # rende impossibile dire "non lo sapevamo".
            logger.warning(f"[WA] numero {number_id} senza proxy: rischio T3 "
                           "(correlazione multi-numero sullo stesso IP)")

    quanti = wa_timing.wa_session_message_count(
        SimpleNamespaceCompat(session_min_messages=None, session_max_messages=None))
    guasti_consecutivi = 0

    async with _open_wa_browser(number_id, headless=True, proxy_url=proxy_url) as context:
        page = await context.new_page()
        await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
        pom = WhatsAppWebPage(page)
        browser_t0 = time.perf_counter()

        for _ in range(quanti):
            # I cancelli si ricontrollano a OGNI messaggio, non una volta a
            # inizio sessione: una sessione dura decine di minuti e nel
            # frattempo puo' cambiare tutto (kill-switch, cap, ora).
            if await bot_state_service.is_wa_halted():
                esito["motivo"] = "wa_halted"
                break

            async with AsyncSessionLocal() as db:
                number = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
                preso = await claim_next_wa_contact(db, number_id=number_id,
                                                    worker_id=worker_id)
                if preso is None:
                    esito["motivo"] = "niente_da_fare"
                    break
                cc, contact, campaign, step = preso

                ora = _ora_locale_corrente()
                inizio, fine = wa_timing.effective_wa_active_hours(campaign)
                if not (inizio <= ora < fine):
                    await _rilascia_lock(db, cc)
                    esito["motivo"] = "fuori_finestra"
                    break

                if not await wa_number_manager.has_wa_send_budget(db, number, campaign):
                    await _rilascia_lock(db, cc)
                    esito["motivo"] = "cap_esaurito"
                    break

                res = await wa_sender.invia_a_contatto(
                    db, pom, campaign=campaign, step=step, cc=cc, contact=contact,
                    number=number,
                    browser_avviato_da_s=time.perf_counter() - browser_t0)

                if res.stato == "sent":
                    esito["inviati"] += 1
                    guasti_consecutivi = 0
                elif res.stato in ("skipped", "opted_out", "replied"):
                    esito["saltati"] += 1
                    guasti_consecutivi = 0
                elif res.stato == "failed":
                    esito["falliti"] += 1
                    guasti_consecutivi = 0
                else:  # 'queued' = guasto nostro, il contatto non si tocca
                    await _rilascia_lock(db, cc)
                    guasti_consecutivi += 1

                if guasti_consecutivi >= MAX_GUASTI_CONSECUTIVI:
                    await _ferma_numero_per_guasto(db, number_id, campaign.id,
                                                   guasti_consecutivi)
                    esito["motivo"] = "guasti_consecutivi"
                    break

            # Delay lognormale FRA i messaggi, dentro la sessione. Non e' un
            # "sleep lungo": e' la mediana di 90s che rende il ritmo umano.
            await asyncio.sleep(wa_timing.wa_send_delay_seconds())

    logger.info(f"[WA] mini-sessione {number_id}: {esito}")
    return esito


async def _rilascia_lock(db, cc) -> None:
    from app.models.wa import WaCampaignContact
    await db.execute(update(WaCampaignContact).where(WaCampaignContact.id == cc.id)
                     .values(locked_by=None, locked_at=None))
    await db.commit()


async def _ferma_numero_per_guasto(db, number_id: str, campaign_id: str, n: int) -> None:
    """FM2: N fallimenti nostri consecutivi su chat diverse = DOM cambiato o
    pagina in stato inatteso. Si ferma il numero e si mette la campagna in
    error; i contatti restano queued perche' NON e' colpa loro. Un selettore
    rotto non deve bruciare una lista (SDD 11)."""
    from app.models.wa import WaCampaign, WaCampaignStatus, WaNumber, WaNumberStatus
    from app.services import notifier
    from app.utils import events

    await db.execute(update(WaNumber).where(WaNumber.id == number_id)
                     .values(status=WaNumberStatus.cooldown))
    await db.execute(update(WaCampaign).where(WaCampaign.id == campaign_id)
                     .values(status=WaCampaignStatus.error))
    await db.commit()
    events.emit(campaign_id, "wa.number.stopped",
                f"{n} guasti consecutivi: numero fermato, contatti intatti",
                level="error")
    await notifier.send_telegram(
        f"WhatsApp: numero fermato dopo {n} guasti consecutivi "
        f"(probabile DOM cambiato). Campagna in error, contatti NON bruciati.",
        level="error")
```

⚠️ **Due note per l'implementatore**, entrambe da risolvere nel codice vero e non da copiare così:
1. `SimpleNamespaceCompat` nel blocco sopra è un segnaposto illustrativo: il conteggio dei messaggi di sessione va calcolato **dopo** aver claimato la prima riga, usando la **campagna vera** (che ha gli override `session_min/max_messages`). Struttura corretta: claim della prima riga → `wa_session_message_count(campaign)` → ciclo.
2. `import` in cima al modulo, non sparsi: qui sono raggruppati per leggibilità del piano.

- [ ] **Step 4: Rilanciare i test**

Run: `pytest backend/tests/test_wa_worker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/wa_worker.py backend/tests/test_wa_worker.py
git commit -m "feat(wa): mini-sessione per-numero con cap in AND, kill-switch live, escalation FM2"
```

---

### Task 12: registrazione ARQ, fan-out per numero, startup guard

**Files:**
- Modify: `backend/app/workers/wa_worker.py` (task ARQ + enqueue)
- Modify: `backend/app/workers/task_queue.py` (registrazione in `WorkerSettings.functions`)
- Test: `backend/tests/test_task_queue_wa_registration.py`

**Interfaces:**
- Consumes: `app.services.work_enqueue.arq_redis_settings`, `esegui_mini_sessione` (Task 11).
- Produces: `wa_send_job_id(number_id) -> str`, `async wa_send_task(ctx, number_id) -> None`, `async enqueue_wa_workers(campaign_id) -> int`, `async recover_wa_sending_on_startup() -> int`.

**Perché il `_job_id` è per NUMERO e non per campagna** (SDD §7.2 + decisione 23/07 Q2): max **1 campagna `running` per numero** alla volta, quindi il numero identifica il lavoro. Con `_job_id = wa:send:{number_id}`, ARQ scarta da solo un secondo job sullo stesso numero (FM11) — che è la difesa vera contro due browser sullo stesso profilo, insieme al lock per-numero di `wa_session`.

- [ ] **Step 1: Scrivere il test di non-regressione + i nuovi**

```python
# backend/tests/test_task_queue_wa_registration.py
import pytest


def test_le_funzioni_instagram_restano_registrate():
    """Non-regressione: il canale IG e' in produzione. Si scrive PRIMA di
    toccare task_queue.py."""
    from app.workers.task_queue import WorkerSettings
    nomi = {getattr(f, "__name__", str(f)) for f in WorkerSettings.functions}
    for atteso in ("pre_generate_messages_task", "full_batch_generate_task",
                   "browser_bio_account_task", "browser_import_account_task"):
        assert atteso in nomi, f"{atteso} sparita dalla registrazione ARQ"


def test_wa_send_task_e_registrata():
    from app.workers.task_queue import WorkerSettings
    nomi = {getattr(f, "__name__", str(f)) for f in WorkerSettings.functions}
    assert "wa_send_task" in nomi


def test_job_id_e_per_numero_non_per_campagna():
    from app.workers.wa_worker import wa_send_job_id
    assert wa_send_job_id("num-1") == "wa:send:num-1"
    assert wa_send_job_id("num-1") == wa_send_job_id("num-1")   # deterministico


@pytest.mark.asyncio
async def test_recover_wa_sending_riporta_a_queued_i_messaggi_appesi(db_session):
    """FM14: il PC si riavvia a meta' invio. Un wa_messages 'sending' senza
    processo vivo e' lavoro appeso: si riapre, non si perde."""
    from sqlalchemy import select
    from app.models.wa import WaMessage, WaMessageStatus
    from app.workers.wa_worker import recover_wa_sending_on_startup

    ctx = await _scenario_messaggio_sending(db_session)
    n = await recover_wa_sending_on_startup()
    assert n == 1
    msg = await db_session.scalar(select(WaMessage).where(WaMessage.id == ctx["msg"].id))
    await db_session.refresh(msg)
    assert msg.status == WaMessageStatus.failed
    assert "recovery" in (msg.error or "")
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].locked_by is None
```

**Nota sulla scelta `sending → failed` e non `sending → queued`:** è la stessa lezione già pagata sul canale Instagram (memoria del repo: il recovery **non deve** fare chiamate per capire se il messaggio era partito). Un `sending` appeso può essere un messaggio **già consegnato**: rimetterlo in coda lo manderebbe due volte. `failed` + `locked_by=None` lo lascia visibile, non lo duplica, e il contatto resta gestibile a mano.

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_task_queue_wa_registration.py -v`
Expected: FAIL — `wa_send_task` non registrata, `wa_send_job_id` inesistente.

- [ ] **Step 3: Implementare task, enqueue e recovery**

```python
# backend/app/workers/wa_worker.py -- in coda
def wa_send_job_id(number_id: str) -> str:
    """Un solo job di invio per numero (SDD Q2: max 1 campagna running per
    numero). ARQ scarta il duplicato da solo -- FM11."""
    return f"wa:send:{number_id}"


async def wa_send_task(ctx: dict, number_id: str) -> None:
    """Task ARQ. Esce SEMPRE presto: la pausa fra mini-sessioni non si fa
    dormendo dentro il job (lezione job_timeout della Fase Bio), si fa
    rischedulando."""
    from arq.jobs import Job          # noqa: F401  (documenta la dipendenza)
    from app.services import wa_timing

    esito = await esegui_mini_sessione(number_id)

    if esito["motivo"] in ("send_disabled", "wa_halted", "numero_non_attivo",
                           "guasti_consecutivi", "niente_da_fare"):
        logger.info(f"[WA] {number_id}: sessione chiusa ({esito['motivo']}), "
                    "nessuna rischedulazione automatica")
        return

    # cap_esaurito / fuori_finestra / completata -> si riprende dopo il break.
    break_s = wa_timing.wa_session_break_seconds(
        await _campagna_attiva_del_numero(number_id))
    await _rischedula(number_id, defer_seconds=int(break_s))


async def enqueue_wa_workers(campaign_id: str) -> int:
    """Fan-out: un job per numero della campagna (in MVP il numero e' uno).
    Stessa forma di work_enqueue.enqueue_dm_workers_with_redis."""
    import arq
    from app.database import AsyncSessionLocal
    from app.models.wa import WaCampaign
    from app.services.work_enqueue import arq_redis_settings

    async with AsyncSessionLocal() as db:
        campaign = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
        if campaign is None:
            return 0
        number_ids = [campaign.wa_number_id]

    redis = await arq.create_pool(arq_redis_settings())
    try:
        n = 0
        for number_id in number_ids:
            job = await redis.enqueue_job("wa_send_task", number_id,
                                          _job_id=wa_send_job_id(number_id))
            if job is not None:
                n += 1
        return n
    finally:
        await redis.aclose()


async def recover_wa_sending_on_startup() -> int:
    """FM14: al riavvio, i wa_messages rimasti 'sending' sono lavoro appeso.
    NESSUNA chiamata al browser per capire se erano partiti: potevano
    esserlo, e rimetterli in coda li manderebbe due volte. Si marcano
    failed e si rilasciano i lock -- stessa scelta gia' fatta sul canale
    Instagram."""
    from app.database import AsyncSessionLocal
    from app.models.wa import (WaCampaignContact, WaMessage, WaMessageStatus)

    async with AsyncSessionLocal() as db:
        appesi = (await db.execute(
            select(WaMessage).where(WaMessage.status == WaMessageStatus.sending)
        )).scalars().all()
        for msg in appesi:
            msg.status = WaMessageStatus.failed
            msg.error = "recovery: processo interrotto durante l'invio (stato reale ignoto)"
        await db.execute(
            update(WaCampaignContact)
            .where(WaCampaignContact.locked_by.is_not(None))
            .values(locked_by=None, locked_at=None)
        )
        await db.commit()
        if appesi:
            logger.warning(f"[WA] recovery avvio: {len(appesi)} messaggi 'sending' "
                           "chiusi come failed (stato reale ignoto)")
        return len(appesi)
```

In `task_queue.py`, **due sole righe**: import e registrazione.

```python
# backend/app/workers/task_queue.py
from app.workers.wa_worker import wa_send_task    # in cima, con gli altri import

class WorkerSettings:
    functions = [
        # ... esistenti, INVARIATE ...
        wa_send_task,
    ]
```

- [ ] **Step 4: Rilanciare i test + la suite intera**

Run: `pytest backend/tests/test_task_queue_wa_registration.py -v && pytest backend/tests -q`
Expected: PASS. La suite intera perché si è toccato un file condiviso col canale in produzione.

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/wa_worker.py backend/app/workers/task_queue.py backend/tests/test_task_queue_wa_registration.py
git commit -m "feat(wa): wa_send_task su ARQ, fan-out per numero, recovery dei sending appesi"
```

---

### Task 13: cron — health-check sessione, rilascio cooldown, lock stale

**Files:**
- Modify: `backend/app/workers/cron_worker.py`
- Test: `backend/tests/test_wa_cron.py`

**Interfaces:**
- Consumes: `wa_session.check_session` (M1), `wa_number_manager.release_expired_wa_cooldowns` (Task 2).
- Produces: `async wa_session_healthcheck(ctx) -> dict` registrata in `CronWorkerSettings.cron_jobs`.

**FM1 + FM18.** L'health-check è quello che accorge il sistema che una sessione è caduta (numero → `qr_required`, campagne in pausa, alert). Il 27/07 il browser del daemon è morto e **nessuno se n'è accorto per 15 minuti**, fino allo scan successivo: il liveness deve essere più fitto dell'attività che protegge, non coincidere con essa.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_cron.py
import pytest


@pytest.mark.asyncio
async def test_healthcheck_mette_in_pausa_le_campagne_del_numero_caduto(db_session, monkeypatch):
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario_claim(db_session)

    async def _fake_check(number_id):
        return WaNumberStatus.qr_required
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    await cron_worker.wa_session_healthcheck({})
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.paused


@pytest.mark.asyncio
async def test_healthcheck_non_tocca_le_campagne_se_la_sessione_e_viva(db_session, monkeypatch):
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario_claim(db_session)

    async def _fake_check(number_id):
        return WaNumberStatus.active
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    await cron_worker.wa_session_healthcheck({})
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.running


@pytest.mark.asyncio
async def test_healthcheck_rilascia_i_lock_stale(db_session, monkeypatch):
    from datetime import datetime, timedelta
    from app.models.wa import WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario_claim(db_session)
    ctx["cc"].locked_by = "worker-morto"
    ctx["cc"].locked_at = datetime.utcnow() - timedelta(minutes=45)
    await db_session.commit()

    async def _fake_check(number_id):
        return WaNumberStatus.active
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    await cron_worker.wa_session_healthcheck({})
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].locked_by is None
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_cron.py -v`
Expected: FAIL — `wa_session_healthcheck` non esiste.

- [ ] **Step 3: Implementare il cron**

```python
# backend/app/workers/cron_worker.py -- in coda alle funzioni, poi registrare
from arq import cron

from app.services.wa_session import check_session


async def wa_session_healthcheck(ctx: dict) -> dict:
    """Ogni 30 minuti nelle ore attive (SDD Q56): per ogni numero non
    ritirato, guarda se la sessione e' viva; se e' caduta mette in pausa le
    sue campagne e avvisa. In piu' rilascia cooldown scaduti e lock stale.

    Il check apre il browser headless (check_session, M1): e' l'operazione
    piu' cara di questo cron, ed e' il motivo per cui gira su un cron
    dedicato e non dentro il worker di invio.
    """
    from app.database import AsyncSessionLocal
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaNumber, WaNumberStatus)
    from app.services import notifier, wa_number_manager
    from app.config import settings
    from datetime import datetime, timedelta
    from sqlalchemy import select, update

    esito = {"controllati": 0, "caduti": 0, "cooldown_rilasciati": 0, "lock_rilasciati": 0}

    async with AsyncSessionLocal() as db:
        numeri = (await db.execute(
            select(WaNumber).where(WaNumber.status.notin_([
                WaNumberStatus.retired, WaNumberStatus.suspended,
                WaNumberStatus.pending_qr]))
        )).scalars().all()
        ids = [n.id for n in numeri]

    for number_id in ids:
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

    esito["cooldown_rilasciati"] = len(await wa_number_manager.release_expired_wa_cooldowns())

    async with AsyncSessionLocal() as db:
        cutoff = datetime.utcnow() - timedelta(minutes=int(settings.wa_lock_timeout_min))
        res = await db.execute(
            update(WaCampaignContact)
            .where(WaCampaignContact.locked_by.is_not(None),
                   WaCampaignContact.locked_at < cutoff)
            .values(locked_by=None, locked_at=None)
        )
        await db.commit()
        esito["lock_rilasciati"] = res.rowcount or 0

    logger.info(f"[WA] health-check: {esito}")
    return esito
```

Registrazione (stesso file, in `CronWorkerSettings.cron_jobs`, **senza toccare i cron esistenti**):

```python
    cron_jobs = [
        # ... esistenti, INVARIATE ...
        cron(wa_session_healthcheck, minute={0, 30}, hour=set(range(9, 20))),
    ]
```

- [ ] **Step 4: Rilanciare i test + suite intera** (si è toccato un file condiviso)

Run: `pytest backend/tests/test_wa_cron.py -v && pytest backend/tests -q`

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/cron_worker.py backend/tests/test_wa_cron.py
git commit -m "feat(wa): cron health-check sessione, rilascio cooldown e lock stale"
```

---

### Task 14: `app/api/wa_ops.py` — kill-switch, stato, avvio manuale

**Files:**
- Modify: `backend/app/api/wa_ops.py` (scheletro creato in PR-0)
- Test: `backend/tests/test_wa_ops_api.py`

**Interfaces:**
- Consumes: `bot_state_service.is_wa_halted/halt_wa/resume_wa` (Task 3), `wa_worker.enqueue_wa_workers` (Task 12), `wa_number_manager.release_expired_wa_cooldowns`.
- Produces: `GET /api/wa/ops/status`, `POST /api/wa/ops/halt`, `POST /api/wa/ops/resume`, `POST /api/wa/ops/campaigns/{id}/kick`.

M3 è **backend puro** (decisione 29/07): questi endpoint sono l'unica interfaccia, e l'operatore li usa da `curl` o dalla pagina che M2 costruirà. Nessun file sotto `frontend/` viene toccato da questo piano.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_ops_api.py
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_status_riporta_kill_switch_e_conteggi(db_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/wa/ops/status")
    assert r.status_code in (200, 401)      # 401 se l'auth e' attiva: e' corretto
    if r.status_code == 200:
        body = r.json()
        assert "wa_halted" in body and "send_enabled" in body


@pytest.mark.asyncio
async def test_halt_e_resume_cambiano_solo_il_canale_wa(db_session):
    from app.services import bot_state_service as bss
    await bss.halt_wa(reason="via API", by="test", db=db_session)
    assert await bss.is_wa_halted(db_session) is True
    assert await bss.is_halted(db_session) is False
    await bss.resume_wa(by="test", db=db_session)
    assert await bss.is_wa_halted(db_session) is False


@pytest.mark.asyncio
async def test_kick_su_campagna_non_running_non_accoda_nulla(db_session, monkeypatch):
    """Idempotenza/macchina a stati: un kick su una campagna in draft non
    deve creare lavoro."""
    from app.api import wa_ops
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.draft
    await db_session.commit()

    accodati = {"n": 0}

    async def _fake_enqueue(campaign_id):
        accodati["n"] += 1
        return 1
    monkeypatch.setattr(wa_ops, "enqueue_wa_workers", _fake_enqueue)

    esito = await wa_ops.kick_campaign(ctx["campaign"].id, db=db_session)
    assert accodati["n"] == 0
    assert esito["accodati"] == 0
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_ops_api.py -v`

- [ ] **Step 3: Implementare gli endpoint**

```python
# backend/app/api/wa_ops.py
"""Operativita' del canale WhatsApp: kill-switch, stato, avvio manuale.

M3 e' backend puro (decisione 29/07): non c'e' UI in questo modulo. Le
pagine, quando arriveranno, le costruisce M2 contro questi endpoint.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from app.database import get_db
from app.models.wa import (WaCampaign, WaCampaignStatus, WaMessage,
                           WaMessageStatus, WaNumber, WaNumberStatus)
from app.services import bot_state_service
from app.workers.wa_worker import enqueue_wa_workers

router = APIRouter(prefix="/wa/ops", tags=["wa-ops"])


class HaltRequest(BaseModel):
    reason: str


@router.get("/status")
async def wa_ops_status(db=Depends(get_db)) -> dict:
    from app.config import settings
    oggi = func.date(WaMessage.sent_at) == func.date(func.now())
    return {
        "wa_halted": await bot_state_service.is_wa_halted(db),
        "send_enabled": bool(settings.wa_send_enabled),
        "numeri_attivi": await db.scalar(
            select(func.count(WaNumber.id)).where(WaNumber.status == WaNumberStatus.active)),
        "campagne_running": await db.scalar(
            select(func.count(WaCampaign.id)).where(WaCampaign.status == WaCampaignStatus.running)),
        "inviati_oggi": await db.scalar(
            select(func.count(WaMessage.id)).where(
                WaMessage.status == WaMessageStatus.sent, oggi)) or 0,
    }


@router.post("/halt")
async def wa_ops_halt(body: HaltRequest, db=Depends(get_db)) -> dict:
    await bot_state_service.halt_wa(reason=body.reason, by="api", db=db)
    return {"wa_halted": True, "reason": body.reason}


@router.post("/resume")
async def wa_ops_resume(db=Depends(get_db)) -> dict:
    await bot_state_service.resume_wa(by="api", db=db)
    return {"wa_halted": False}


@router.post("/campaigns/{campaign_id}/kick")
async def kick_campaign(campaign_id: str, db=Depends(get_db)) -> dict:
    """Riaccoda il worker di invio per la campagna. Serve dopo un resume o
    quando un job e' andato perso. Non forza nulla: se la campagna non e'
    running, non accoda -- lo start delle campagne e' di M2."""
    campaign = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campaign is None:
        raise HTTPException(status_code=404, detail="campagna inesistente")
    if campaign.status != WaCampaignStatus.running:
        return {"accodati": 0, "motivo": f"campagna in stato {campaign.status.value}"}
    return {"accodati": await enqueue_wa_workers(campaign_id)}
```

- [ ] **Step 4: Rilanciare i test**

Run: `pytest backend/tests/test_wa_ops_api.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/wa_ops.py backend/tests/test_wa_ops_api.py
git commit -m "feat(wa): API ops -- kill-switch di canale, stato, kick manuale"
```

---

### Task 15: Fase 4 — chiusura modulo (QA, prova dal vivo, review)

**Files:**
- Create: `.superpowers/sdd/qa-m3-tests.md` (≥20 test funzionali)
- Create: `.superpowers/sdd/qa-m3-adversarial.md` (≥30 test adversarial)

**Questo task non è un'appendice: vale come i precedenti messi insieme.** Ogni difetto serio di M1 è stato trovato **leggendo il codice o attaccandolo**, mai dalla suite verde. Il Critical più grave — la pulizia dei lock-file di Chromium fuori dal lock — è sfuggito a tutti e quattro i batch QA ed è emerso solo nella review finale, perché il launcher finto non scriveva lock-file veri.

Modelli da cui partire, non da zero: `d:\dev\thevista-app-magazzino\.superpowers\sdd\qa-50-tests.md` e `qa-adversarial-tests.md`.

- [ ] **Step 1: Scrivere la lista funzionale (≥20)**

Deve coprire almeno: seed di una campagna con lo script di M2 → mini-sessione a secco (`WA_SEND_ENABLED=false`) → accensione → invio singolo → stato del contatto → contatori campagna → `sent_today` del numero → cap raggiunto → break e ripresa → fuori finestra oraria → kill-switch da API → resume → kick → health-check che rileva la sessione caduta → recovery al riavvio → contatto già in DNC che non viene mai claimato → campagna in pausa che ferma tutto → `chat_title` appreso solo se nome → `delivery_check` popolato → evento `wa.message.sent` visibile.

- [ ] **Step 2: Scrivere la lista adversarial (≥30), a criterio di PASS INVERTITO**

**PASS = il sistema si difende.** Per il gruppo opt-out il criterio va scritto esplicitamente così: **PASS = il messaggio NON parte.**

Categorie obbligatorie:

| Gruppo | Casi minimi |
|---|---|
| **Cecità del DOM** | `read_inbound_tail` → `None`; righe malformate (chiave mancante, `text=None`, riga non-dict); `scan_chat_list` che solleva; composer che non compare pur con `ok=True` |
| **STOP ovunque** | ultimo messaggio · in mezzo alla coda · seguito da una nostra risposta · `Stop.` con punto · `  STOP  ` con spazi · dentro una frase (`va bene ma poi STOP`) · `stopper` che **non** deve scattare |
| **Chat non sincronizzata** | cronologia vuota ma `wa_messages` dice che avevamo scritto · browser avviato 30 s fa · `sync_state` che torna `syncing` |
| **TOCTOU** | STOP che compare solo nella seconda lettura · cecità solo nella seconda lettura · risposta che compare nella seconda lettura |
| **Concorrenza vera** | due worker `asyncio.gather` sullo stesso numero · due `enqueue` con lo stesso `_job_id` · claim su riga con lock a 19 min 59 s e a 20 min 01 s |
| **Cap al confine** | invio numero esattamente al cap · cap globale macchina raggiunto da un **altro** numero · `sent_date` di ieri con `sent_today` alto · mezzanotte che passa **durante** la sessione |
| **Macchina a stati** | campagna messa in pausa a metà sessione · numero portato a `qr_required` mentre gira · contatto già `opted_out` che viene claimato · doppio `persist_wa_optout` |
| **Guasti nostri** | 3 segnali `casella-ricerca-non-trovata` di fila (numero fermato, contatti **intatti**) · segnale mai visto prima · conteggio cronologia non numerico |
| **PII** | run E2E completo poi **grep sul log**: zero numeri interi, zero `rendered_text` con numeri, `chat_title` numerico mai salvato |
| **Invarianti SQL a fine run** | nessuna riga `queued` con `locked_by` valorizzato da più di 20 min · nessun `wa_messages` in `sending` · `sent` della campagna == conteggio dei `wa_messages` in stato `sent` · nessun contatto `opted_out` con righe non terminali |

Il livello va **mescolato**: browser dove la UI lo esprime, chiamata diretta ai servizi (script) per race, payload malformati e burst. Un adversarial fatto solo da un livello non è adversarial.

- [ ] **Step 3: Eseguire tutto, con un QA agent, e fare il fix loop fino al 100%**

"Passano quasi tutti" = modulo non chiuso.

- [ ] **Step 4: La prova dal vivo — accendere `WA_SEND_ENABLED`**

Prima di accendere, **verificare nell'ordine**:

1. `bot_state.wa_halted = false` e il kill-switch risponde da API.
2. Un numero di test **nuovo**, con profilo **nuovo** (`data/browser_profiles/wa_<uuid>`): **mai** `D:\dev\wa-poc\profile` (PoC-1 in corsa fino al 10/08), **mai** `PLAYWRIGHT_BROWSERS_PATH=D:`.
3. `daily_cap = 3` sul numero e `daily_limit = 3` sulla campagna: il primo invio vero si fa con un tetto che rende impossibile un danno grosso.
4. Destinatari: **solo numeri controllati** (Tommaso o conoscenti che hanno già una chat con quel numero). Mai contatti di un cliente.
5. `D:\dev\tools\ram-guard\guard.ps1 stato` — un browser costa 1,2 GB misurati.

Poi: seed con lo script di M2 → `POST /api/wa/ops/campaigns/{id}/kick` → osservare log ed eventi. **Un solo messaggio**, verificato sul telefono, prima di provarne tre.

Durante questa prova gira anche `wa_measure_tick.py` (Task 9): è l'unico momento in cui esistono invii veri da misurare.

- [ ] **Step 5: Whole-branch review**

**REQUIRED SUB-SKILL:** `superpowers:requesting-code-review` su tutto il branch. È la fase che in M1 ha trovato il difetto peggiore.

- [ ] **Step 6: Rebase, suite, migrazioni, PR**

```bash
git fetch origin && git rebase origin/main
cd backend && alembic upgrade head && alembic downgrade -1 && alembic upgrade head
pytest tests -q
```
Se la 026 di M2 è entrata su `main`, **cambiare `down_revision` della 027 da `"025"` a `"026"`** e rifare il ciclo. Solo dopo: apertura della PR.

- [ ] **Step 7: Commit finale delle liste QA**

```bash
git add .superpowers/sdd/qa-m3-tests.md .superpowers/sdd/qa-m3-adversarial.md
git commit -m "qa(wa-m3): liste funzionale e adversarial di fine modulo, fix loop chiuso al 100%"
```

---

## Stima

| Fase | Task | Sessioni di lavoro |
|---|---|---|
| Fondamenta (timing, cap, kill-switch, opt-out) | 0-4 | ~1,5 |
| Invio (apertura, guardia, testo, TOCTOU, tick) | 5-9 | ~2 |
| Worker (claim, mini-sessione, ARQ, cron, API) | 10-14 | ~2 |
| **Fase 4** (QA ≥20 + ≥30 adversarial, fix loop, prova dal vivo, review) | 15 | **~1,5** |
| | | **~7 sessioni** |

La Fase 4 è una riga della tabella come le altre di proposito: è il 20% del cantiere, non un dopo.

## Cosa questo piano lascia aperto, e a chi

- **C4 / FM9** ("non intromettersi se l'ultimo messaggio è dell'umano-business"): richiede un metodo del POM che legga l'ultimo messaggio **a prescindere dalla direzione**, mentre `read_inbound_tail` filtra via l'outbound per contratto — è la sua garanzia contro i falsi "nessuno STOP". `whatsapp_page.py` è patrimonio M1: aggiungere un metodo è un emendamento al contratto, non una scelta di questo piano. **Va deciso prima di M4.**
- **Selettore dell'indicatore di sincronizzazione**: si cattura al primo re-scan del QR che capiterà comunque (dopo il 10/08). Finché manca, valgono quarantena e incoerenza DB↔DOM.
- **Proxy mai validato** (Q98): il codice avvisa quando un numero parte senza proxy, ma il warning non compra i proxy. Blocca il go-live commerciale, non questo modulo su numero di test.
- **`WA_WARMUP_STEPS` e i parametri di §10.3 sono proposte, non misure**: A6 si verifica solo con la rampa di M5.
