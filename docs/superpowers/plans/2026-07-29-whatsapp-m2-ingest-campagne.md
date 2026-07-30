# WhatsApp M2 — Ingest + campagne Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Prima di scrivere una riga di codice, invoca la skill `sviluppo-modulo`** (obbligatoria per ogni modulo di codice di questo repo). ⚠️ **M2 costruisce backend E frontend insieme: l'implementazione va fatta con la skill `agent-teams`, un teammate per lato**, perché le due parti devono parlarsi mentre si scrivono (è il caso esplicitamente previsto dalla skill `sviluppo-modulo`, Fase 2). `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` è già attiva nei settings dal 29/07.

**Goal:** Rendere definibile end-to-end una campagna WhatsApp dalla UI admin: ingest di un CSV reale con report degli scarti riga per riga, anagrafica contatti dedupata e pseudonimizzata, CRUD di tenant/numeri/campagne/sequenze, start-pause-stop, e un mondo frontend WhatsApp separato da Instagram — **senza inviare niente a nessuno** (l'invio è M3).

**Architecture:** Backend FastAPI sopra le tabelle `wa_*` già congelate da M1 (nessuna colonna nuova). L'ingest è un parser difensivo riga-per-riga sul modello di `import_resolver.py`: normalizza in E.164, pseudonimizza con `hmac_phone`, cifra con Fernet, dedupa su `UNIQUE(tenant_id, phone_hmac)` e **non fallisce mai in blocco** — ogni riga scartata torna nel report col suo motivo. Il frontend è un mondo a sé sotto `frontend/app/wa/`, con picker di canale post-login e tema verde WhatsApp: nessuna pagina condivisa con Instagram, che resta intatto in produzione.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async, pytest + pytest-asyncio; Next.js 15 (App Router, `frontend/app/`), React 19, Tailwind + shadcn/ui (`frontend/components/ui`), client REST in `frontend/lib/api.ts`.

## Global Constraints

- **Il contratto M2↔M3 (`docs/whatsapp/contratto-M2-M3.md`) vince su questo piano.** Ogni "§N" senza altra indicazione è quel documento. Se il piano e il contratto divergono, il piano è sbagliato: si emenda il contratto (§9) e si cita l'emendamento.
- **`app/models/wa.py` è congelato**: nessuna colonna nuova. Se una funzionalità sembra richiederne una, è un emendamento al contratto, non una scelta di questo piano.
- **Dopo PR-0 (Task 1), `app/main.py`, `app/config.py`, `backend/tests/conftest.py` sono congelati**: nessun task successivo li riapre. Tutte le variabili d'ambiente di M2 **e** di M3 entrano in `config.py` una volta sola, in PR-0, con i valori di §5.2 del contratto.
- **`app/browser/whatsapp_page.py`, `whatsapp_selectors.py`, `services/wa_session.py`, `utils/phone_pseudonym.py`** sono patrimonio M1: si importano, non si modificano.
- **M2 non invia niente a nessuno.** Nessun task di questo piano apre un browser, tocca `wa_messages`, o scrive `locked_by`/`locked_at` (invariante I1 del contratto: M2 li **legge** soltanto).
- **Il canale Instagram è in produzione**: ogni modifica a codice condiviso porta il suo test di non-regressione **prima** della modifica. Vale in particolare per `app/main.py` (PR-0), `frontend/app/layout.tsx` e `frontend/components/LayoutShell.tsx`.
- **Numeri in chiaro solo ai confini** (P12): il numero vive nella richiesta HTTP e in `encrypted_phone`. Chiave interna `phone_hmac`. Nei log e nei report, forma mascherata — vedi §2.3 del contratto, che è un requisito con un test dedicato, non una raccomandazione.
- **Nessun `xfail`.** Commenti e docstring in **ASCII** (`gia'`, `e'`); i markdown usano gli accenti. Non-ASCII nei sorgenti sempre come escape (`"\u202a"`). Mai un monkeypatch che si autoriferisce.
- **Una sola suite pytest alla volta dentro questo worktree**; **un solo comando pesante alla volta a livello di macchina** — 7,4 GB totali, `npm run build` è già stato abbattuto da ram-guard a 2,7 GB e l'altro cantiere apre browser da 1,2 GB. `D:\dev\tools\ram-guard\guard.ps1 stato` prima di ogni build.
- **Branch/worktree dedicato**: `feat/whatsapp-m2-ingest-campagne`, da `main` aggiornato. Mai push diretto su `main`, mai commit nel worktree di M3.
- **Ordine di merge (§6.3): PR-0 → PR M2 → PR M3.** M2 merge per primo.
- **Migrazioni**: il numero riservato a M2 è **026**. Ad oggi M2 **non dovrebbe averne bisogno** (lo schema 025 copre già ingest e campagne): se resta un buco va bene, i buchi non fanno male e le collisioni sì. Se invece serve, vedi Task 13.
- **Fuori scope, con motivo:**
  - Invio, worker, cap runtime, guardie, opt-out da DOM, watcher: sono M3/M4.
  - Motore sequenze multi-step: schema completo, UI a **un solo step** (SDD Q29, decisione 24/07). Il branching si accende post-MVP senza migrazione.
  - UI cliente self-serve, flow builder, analytics ricche: fase 2 (F1/F2/F6).

## Decisioni sulle domande [S] della SDD

Il piano le chiude qui, esplicitamente, perché un implementatore non deve inventarsele.

| Q | Domanda | Decisione |
|---|---|---|
| **Q13** | Formato CSV accettato | UTF-8 con o senza BOM; separatore rilevato fra `,` e `;` (l'Excel italiano esporta `;`); header **obbligatorio**; colonna `numero` obbligatoria, `nome` opzionale, ogni altra colonna diventa un attributo |
| **Q14** | Normalizzazione numeri | `normalize_e164` di M1 con `default_country` da `WA_INGEST_DEFAULT_COUNTRY` (39). Numeri esteri accettati **se già in E.164** (`+…`) |
| **Q16** | Re-upload con attributi cambiati | **Aggiorna** gli attributi (gap-fill come `global_contacts`), **non** tocca `opted_out`/`do_not_contact` |
| **Q18** | Rimozione di un contatto dalla campagna | Sì, singola, **solo** se la riga non è sotto lock fresco (I1) e non è terminale |
| **Q21** | Ingest interrotto a metà | **Riga per riga, idempotente**: il re-upload sana. Nessuna transazione unica su 5.000 righe |
| **Q22** | Massimo contatti per campagna | Soft limit `WA_INGEST_MAX_ROWS` (5.000): oltre, l'ingest rifiuta il file con un messaggio che spiega il perché (con cap 100-200/giorno, 5.000 contatti sono mesi) |
| **Q23** | Contatti orfani | L'ingest crea **solo** i contatti della campagna. Niente anagrafica speculativa (minimizzazione) |
| **Q20** | Due tenant, stesso numero | Due `wa_contacts` distinti. Ogni query di M2 filtra per `tenant_id` — c'è un test adversarial dedicato |

---

## File Structure

| File | Stato | Responsabilità |
|---|---|---|
| `backend/app/api/tenants.py` | **Create** (PR-0 vuoto → riempito) | CRUD tenant |
| `backend/app/api/wa_numbers.py` | **Create** (PR-0 vuoto → riempito) | CRUD numeri + riattivazione + avvio login QR |
| `backend/app/api/wa_campaigns.py` | **Create** (PR-0 vuoto → riempito) | CRUD campagne, sequenze, start/pause/stop, KPI |
| `backend/app/api/wa_contacts.py` | **Create** (PR-0 vuoto → riempito) | Ingest CSV, lista contatti di una campagna, rimozione singola |
| `backend/app/api/wa_ops.py` | **Create** (PR-0, vuoto) | Scheletro per M3 — M2 lo crea e non lo riapre mai più |
| `backend/app/services/wa_template.py` | **Create** (PR-0) | `pick_wa_template`, `render_wa_template`, `validate_wa_template` (firme congelate, §2.4) |
| `backend/app/services/wa_csv.py` | **Create** | Parser CSV puro: dialetto, header, righe → dict. Nessun DB, nessuna rete |
| `backend/app/services/wa_ingest.py` | **Create** | Da righe a contatti: normalizza, pseudonimizza, cifra, dedupa, crea `wa_campaign_contacts`, produce il report |
| `backend/app/services/wa_campaign_service.py` | **Create** | Regole di campagna: `optout_enabled` condizionale, validazioni di start, ri-stampa di `next_action_at` |
| `backend/scripts/wa_seed_campaign.py` | **Create** (PR-0) | Seed per M3 (§7.4) |
| `backend/tests/factories_wa.py` | **Create** (PR-0) | Factory condivise: tenant, numero, contatto, campagna, step |
| `frontend/app/wa/layout.tsx` | **Create** | Shell del mondo WA: tema verde, nav propria |
| `frontend/app/wa/page.tsx` | **Create** | Home WA (elenco campagne) |
| `frontend/app/wa/numeri/page.tsx` | **Create** | Numeri: stato, cap, proxy, riattivazione |
| `frontend/app/wa/campagne/nuova/page.tsx` | **Create** | Creazione campagna + step 0 + upload CSV |
| `frontend/app/wa/campagne/[id]/page.tsx` | **Create** | Dettaglio: KPI, contatti, start/pause/stop |
| `frontend/app/canale/page.tsx` | **Create** | Picker di canale post-login (Instagram \| WhatsApp) |
| `frontend/lib/waApi.ts` | **Create** | Client REST del mondo WA, separato da `lib/api.ts` (che resta di Instagram) |
| `backend/tests/test_wa_csv.py`, `test_wa_ingest.py`, `test_wa_campaign_service.py`, `test_wa_api_*.py`, `test_wa_template.py` | **Create** | |
| `.superpowers/sdd/qa-m2-tests.md`, `qa-m2-adversarial.md` | **Create** | Liste Fase 4 |

---

### Task 1: PR-0 — l'impalcatura condivisa (si mergia su `main` da sola)

**Files:**
- Create: `backend/app/api/tenants.py`, `wa_numbers.py`, `wa_campaigns.py`, `wa_contacts.py`, `wa_ops.py` (solo il `router`, vuoti)
- Modify: `backend/app/main.py` (registrazione dei cinque router, in un colpo solo)
- Modify: `backend/app/config.py` e `.env.example` (le 20 variabili di M2 **e** M3)
- Create: `backend/app/services/wa_template.py` + `backend/tests/test_wa_template.py`
- Create: `backend/scripts/wa_seed_campaign.py`
- Create: `backend/tests/factories_wa.py`
- Modify: `backend/tests/conftest.py` (slot del DB di test + lock)

**Interfaces:**
- Produces: tutto ciò che M3 consuma il giorno 1 — le firme di `wa_template` (§2.4), lo script di seed (§7.4), le variabili di `settings`, i router registrati.

**Perché esiste** (§5): senza, M2 e M3 toccherebbero entrambi `main.py`, `config.py` e `conftest.py`, e M3 resterebbe fermo in attesa del renderer e del seed. Con PR-0, dopo il giorno 1 **nessuno dei due cantieri ha più motivo di aprire un file dell'altro**.

⚠️ **PR-0 si apre solo dopo che la CI su `main` è verde.** Con la CI rossa per motivi suoi, nessuno dei due cantieri può usarla per accorgersi di aver rotto qualcosa (§8.3).

- [ ] **Step 1: Worktree + branch**

**REQUIRED SUB-SKILL:** `superpowers:using-git-worktrees`. Branch `feat/whatsapp-pr0-impalcatura` da `main` aggiornato — **branch separato da quello di M2**, perché questa PR si mergia da sola e subito.

- [ ] **Step 2: Test di non-regressione su `main.py` PRIMA di toccarlo**

```python
# backend/tests/test_wa_router_registration.py
def test_i_router_instagram_restano_registrati():
    """Non-regressione: il canale IG e' in produzione. Si scrive PRIMA di
    toccare main.py."""
    from app.main import app
    paths = {r.path for r in app.routes}
    for atteso in ("/api/campaigns", "/api/accounts", "/api/followers", "/api/health"):
        assert any(p.startswith(atteso) for p in paths), f"{atteso} sparito"


def test_i_cinque_router_wa_sono_registrati():
    from app.main import app
    paths = {r.path for r in app.routes}
    for atteso in ("/api/tenants", "/api/wa/numbers", "/api/wa/campaigns",
                   "/api/wa/contacts", "/api/wa/ops"):
        assert any(p.startswith(atteso) for p in paths), f"{atteso} non registrato"
```

Run: `pytest backend/tests/test_wa_router_registration.py -v` → il primo passa, il secondo FAIL.

- [ ] **Step 3: Creare i cinque moduli router vuoti**

Ognuno esattamente così (esempio per `wa_numbers.py`), **niente altro**:

```python
# backend/app/api/wa_numbers.py
"""CRUD dei numeri WhatsApp. Scheletro creato in PR-0 e riempito da M2:
la registrazione in main.py avviene UNA volta sola, cosi' i due cantieri
paralleli non toccano mai lo stesso file (contratto §5)."""
from fastapi import APIRouter

router = APIRouter(prefix="/wa/numbers", tags=["wa-numbers"])
```

`wa_ops.py` è identico con `prefix="/wa/ops"`, `tags=["wa-ops"]`, e la docstring dice **"riempito da M3"**: M2 lo crea e non lo riapre mai più.

- [ ] **Step 4: Registrare tutto in `main.py`, una volta sola**

```python
# backend/app/main.py -- accanto agli include_router esistenti, INVARIATI
from app.api import tenants, wa_campaigns, wa_contacts, wa_numbers, wa_ops

app.include_router(tenants.router, prefix="/api", dependencies=_protected)
app.include_router(wa_numbers.router, prefix="/api", dependencies=_protected)
app.include_router(wa_campaigns.router, prefix="/api", dependencies=_protected)
app.include_router(wa_contacts.router, prefix="/api", dependencies=_protected)
app.include_router(wa_ops.router, prefix="/api", dependencies=_protected)
```

Run: `pytest backend/tests/test_wa_router_registration.py -v` → PASS.

- [ ] **Step 5: Le 20 variabili in `config.py`, con la provenienza accanto**

```python
# backend/app/config.py -- in coda alla classe Settings

    # --- Canale WhatsApp: ingest e campagne (M2) -------------------------
    # Prefisso applicato ai numeri senza '+' (SDD Q14). Un numero estero si
    # accetta solo se gia' in E.164.
    wa_ingest_default_country: str = "39"
    # Soft limit per file (SDD Q22): con cap 100-200/giorno, 5.000 contatti
    # sono MESI di campagna. Rifiutare e' piu' onesto che accettare.
    wa_ingest_max_rows: int = 5000
    # Tetto agli attributi liberi per contatto (SDD Q15).
    wa_ingest_max_attrs_bytes: int = 2048

    # --- Canale WhatsApp: invio (M3) -------------------------------------
    # Master switch fail-closed: nessun invio finche' non lo si accende a
    # mano. Nessun task di M2 lo tocca.
    wa_send_enabled: bool = False
    wa_daily_cap_default: int = 20              # SDD 10.3, warmup giorno 1-3
    # ATTENZIONE: proposta NON misurata (SDD 10.3). A6 si verifica solo con
    # la rampa di M5.
    wa_warmup_steps: str = "20,20,30,40,60,80,100"
    wa_send_delay_median_s: int = 90            # SDD 10.3
    wa_send_delay_sigma: float = 0.7            # SDD 10.3
    wa_session_min_msg: int = 8                 # SDD 10.3
    wa_session_max_msg: int = 15                # SDD 10.3
    wa_break_min_min: int = 20                  # SDD 10.3
    wa_break_max_min: int = 40                  # SDD 10.3
    wa_active_hours: str = "09:30-19:30"        # SDD 10.3, Europe/Rome
    # STIMATO, non misurato: finestra in cui la sincronizzazione post
    # riconnessione rende cieca la guardia (A9/FM16). Da rimisurare quando
    # SYNC_INDICATOR sara' catalogato.
    wa_resync_quarantine_min: int = 15
    wa_guard_tail_n: int = 40                   # default del POM
    wa_guard_history_min: int = 80              # default del POM
    # Stesso valore di campaign_orchestrator.LOCK_TIMEOUT_MINUTES.
    wa_lock_timeout_min: int = 20
    wa_max_failures_per_contact: int = 3        # SDD 8.2
    wa_stop_words: str = "stop,basta,cancellami,non scrivermi,unsubscribe,rimuovimi"
    wa_global_daily_cap: int = 200              # SDD Q70, safety valve macchina
```

`PHONE_HMAC_KEY` **esiste già** da M1 (`settings.phone_hmac_key`): non si duplica. Le stesse 20 righe, in forma `NOME=valore`, vanno in `.env.example` alla **radice del repo** (non in `backend/`).

- [ ] **Step 6: `wa_template.py` — test prima**

```python
# backend/tests/test_wa_template.py
import pytest

from app.services.wa_template import (TemplateRenderError, pick_wa_template,
                                      render_wa_template, validate_wa_template)


class _Step:
    def __init__(self, a, b=None, c=None, d=None):
        self.template_a, self.template_b = a, b
        self.template_c, self.template_d = c, d


def test_pick_legge_i_campi_WA_non_quelli_instagram():
    """template_renderer.pick_template legge base_message_template: su uno
    step WA prenderebbe stringa vuota SENZA sollevare. E' il motivo per cui
    questo modulo esiste (contratto §2.4)."""
    testo, variante = pick_wa_template(_Step("solo A"))
    assert (testo, variante) == ("solo A", "a")


def test_pick_sceglie_fra_le_varianti_compilate():
    varianti = {pick_wa_template(_Step("A", "B", None, "D"))[1] for _ in range(60)}
    assert varianti == {"a", "b", "d"}


def test_render_valorizza_nome_e_attributi():
    out = render_wa_template("Ciao {nome}, ordine {ultimo_ordine}.",
                             display_name="Marco",
                             attributes={"ultimo_ordine": "10/01/2026"})
    assert out == "Ciao Marco, ordine 10/01/2026."


def test_render_senza_nome_non_inventa_un_segnaposto():
    """Su Instagram il fallback e' '@username'. Su WhatsApp non esiste: si
    rende senza nome, non con un simbolo che il destinatario non capisce."""
    out = render_wa_template("Ciao {nome}, promo.", display_name=None, attributes=None)
    assert "@" not in out and "{" not in out


def test_render_solleva_su_placeholder_sconosciuto():
    with pytest.raises(TemplateRenderError):
        render_wa_template("Ciao {azienda}.", display_name="M", attributes={})


def test_render_solleva_su_attributo_vuoto_per_quel_contatto():
    """'il tuo ultimo ordine e' ' e' peggio di un messaggio non inviato."""
    with pytest.raises(TemplateRenderError):
        render_wa_template("Ordine {ultimo_ordine}.", display_name="M",
                           attributes={"ultimo_ordine": "   "})


def test_render_espande_lo_spintax_riusando_il_parser_esistente():
    out = {render_wa_template("{Ciao|Salve} {nome}.", display_name="M", attributes=None)
           for _ in range(40)}
    assert out == {"Ciao M.", "Salve M."}


def test_validate_elenca_i_placeholder_ignoti():
    assert validate_wa_template("Ciao {nome}, {ultimo_ordine} e {citta}.",
                                known_attributes={"ultimo_ordine"}) == ["citta"]
    assert validate_wa_template("{Ciao|Salve} {nome}.", known_attributes=set()) == []
```

- [ ] **Step 7: Implementare `wa_template.py`**

```python
# backend/app/services/wa_template.py
"""Rendering dei template del canale WhatsApp.

Esiste perche' template_renderer NON e' riusabile qui (contratto §2.4):
pick_template legge i campi Instagram (base_message_template) e su uno
WaSequenceStep prende stringa vuota senza sollevare; render_template
conosce solo {nome} e SOLLEVA su {ultimo_ordine}, cioe' esattamente i
placeholder che l'ingest raccoglie dalle colonne libere del CSV.

Lo spintax si RIUSA da template_renderer.resolve_spintax: una seconda
implementazione dello stesso parser e' una seconda occasione di divergere.
"""
import random
import re

from app.services.template_renderer import (RESIDUAL_PLACEHOLDER_RE,
                                            TemplateRenderError, resolve_spintax)

_NOME_RE = re.compile(r"\{nome\}|\[nome\]|\{name\}|\[name\]", re.IGNORECASE)
_ATTR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]{0,60})\}")


def pick_wa_template(step, rng: random.Random | None = None) -> tuple[str, str]:
    """(testo, variante) fra i template compilati dello step, pesi uguali.
    Stessa semantica di template_renderer.pick_template, sui campi WA."""
    r = rng or random
    candidati = [(step.template_a or "", "a")]
    for campo, lettera in (("template_b", "b"), ("template_c", "c"), ("template_d", "d")):
        valore = getattr(step, campo, None)
        if (valore or "").strip():
            candidati.append((valore, lettera))
    return r.choice(candidati)


def render_wa_template(template: str, *, display_name: str | None,
                       attributes: dict | None, rng: random.Random | None = None) -> str:
    """spintax -> {nome} -> attributi -> normalizzazione.

    Solleva TemplateRenderError se resta un placeholder sconosciuto o se un
    attributo atteso e' vuoto PER QUESTO contatto: meglio non mandare UN
    messaggio che mandarne uno con un buco dentro.
    """
    out = resolve_spintax(template, rng=rng)
    out = _NOME_RE.sub((display_name or "").strip(), out)

    attrs = attributes or {}
    def _sostituisci(m: re.Match) -> str:
        chiave = m.group(1)
        if chiave not in attrs:
            raise TemplateRenderError(f"Placeholder sconosciuto: {{{chiave}}}")
        valore = str(attrs[chiave] or "").strip()
        if not valore:
            raise TemplateRenderError(
                f"Attributo {{{chiave}}} vuoto per questo contatto: non si manda "
                "un messaggio con un buco dentro")
        return valore
    out = _ATTR_RE.sub(_sostituisci, out)

    residuo = RESIDUAL_PLACEHOLDER_RE.search(out)
    if residuo:
        raise TemplateRenderError(f"Placeholder non risolto: {residuo.group(0)!r}")
    out = re.sub(r"[ \t]{2,}", " ", out.replace("\r\n", "\n"))
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if not out:
        raise TemplateRenderError("Template vuoto dopo il rendering")
    return out


def validate_wa_template(template: str, *, known_attributes: set[str]) -> list[str]:
    """Placeholder NON risolvibili con le colonne note. Lista vuota =
    template valido. E' il gate al salvataggio di uno step (Task 7): un
    template con placeholder ignoti non si salva."""
    testo = resolve_spintax(template, rng=random.Random(0))
    testo = _NOME_RE.sub("x", testo)
    return [m.group(1) for m in _ATTR_RE.finditer(testo)
            if m.group(1) not in known_attributes]
```

Run: `pytest backend/tests/test_wa_template.py -v` → PASS (8 test).

- [ ] **Step 8: `factories_wa.py` — le factory condivise**

```python
# backend/tests/factories_wa.py
"""Factory dei test del canale WA, condivise fra M2 e M3 (contratto §5.1).
Modulo normale, NON un conftest: cosi' nessuno dei due cantieri ha motivo
di toccare backend/tests/conftest.py, che dopo PR-0 e' congelato."""
import uuid
from datetime import datetime

from app.models.tenant import Tenant
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaCampaignType, WaContact, WaContactStatus, WaNumber,
                           WaNumberStatus, WaSendCondition, WaSequenceStep)
from app.utils.crypto import encrypt
from app.utils.phone_pseudonym import hmac_phone


async def make_tenant(db, name: str = "Tenant Test") -> Tenant:
    t = Tenant(id=str(uuid.uuid4()), name=name, status="active")
    db.add(t)
    await db.flush()
    return t


async def make_number(db, tenant, *, label="Numero Test",
                      e164: str | None = None, status=WaNumberStatus.active) -> WaNumber:
    # phone_hmac e' UNIQUE GLOBALE: un numero fisso qui farebbe collidere
    # test diversi con un errore che sembra una regressione.
    e164 = e164 or f"+3933{uuid.uuid4().int % 10**8:08d}"
    n = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label=label,
                 phone_hmac=hmac_phone(e164), encrypted_phone=encrypt(e164),
                 status=status, daily_cap=20, warmup_day=1)
    db.add(n)
    await db.flush()
    return n


async def make_contact(db, tenant, *, e164: str | None = None,
                       display_name: str | None = "Marco",
                       attributes: dict | None = None) -> WaContact:
    e164 = e164 or f"+3934{uuid.uuid4().int % 10**8:08d}"
    c = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                  phone_hmac=hmac_phone(e164), encrypted_phone=encrypt(e164),
                  display_name=display_name, attributes=attributes)
    db.add(c)
    await db.flush()
    return c


async def make_campaign(db, tenant, number, *, name="Campagna Test",
                        tipo=WaCampaignType.marketing,
                        status=WaCampaignStatus.draft,
                        template="Ciao {nome}, promo attiva.") -> tuple:
    camp = WaCampaign(
        id=str(uuid.uuid4()), tenant_id=tenant.id, wa_number_id=number.id, name=name,
        campaign_type=tipo, status=status,
        optout_enabled=(tipo == WaCampaignType.marketing),
        optout_cta=("Scrivi STOP per non ricevere piu' messaggi."
                    if tipo == WaCampaignType.marketing else None),
        started_at=datetime.utcnow() if status == WaCampaignStatus.running else None,
    )
    db.add(camp)
    await db.flush()
    step = WaSequenceStep(id=str(uuid.uuid4()), campaign_id=camp.id, step_index=0,
                          template_a=template, send_condition=WaSendCondition.always,
                          wait_days=0)
    db.add(step)
    await db.flush()
    return camp, step


async def make_campaign_contact(db, campaign, contact, *,
                                status=WaContactStatus.queued) -> WaCampaignContact:
    """Rispetta il contratto di consegna §7.1: next_action_at NON e' mai
    NULL su una riga non terminale, e i campi di lock restano vuoti (I1)."""
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id,
                           contact_id=contact.id, status=status, current_step=-1,
                           next_action_at=datetime.utcnow(), failure_count=0)
    db.add(cc)
    await db.flush()
    return cc
```

- [ ] **Step 9: `conftest.py` — slot del DB di test e lock esclusivo**

```python
# backend/tests/conftest.py -- sostituisce la riga fissa del DATABASE_URL
# Il percorso e' relativo alla working directory: due run pytest nella STESSA
# directory si cancellano lo schema a vicenda col drop_all di sessione, e
# producono rossi che sembrano regressioni (28/07: tre run con 24, poi 1, poi
# 22 falliti, tutti fantasmi, contro 729 verdi in isolamento). Con lo slot,
# due run possono convivere dichiarando WA_TEST_DB_SLOT diversi; senza slot,
# il lock qui sotto fa fallire subito il secondo con un messaggio chiaro
# invece di lasciarlo sbagliare in silenzio.
_SLOT = os.environ.get("WA_TEST_DB_SLOT", "default")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./data/test_bot_{_SLOT}.db"
os.makedirs("data", exist_ok=True)

_LOCK_PATH = f"./data/test_bot_{_SLOT}.lock"


@pytest.fixture(scope="session", autouse=True)
def _suite_lock():
    """Un solo run pytest alla volta per slot. Il file resta se un run viene
    ucciso: si cancella a mano, ed e' comunque meglio di un'ora persa a
    inseguire rossi fantasma."""
    try:
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(
            f"Un'altra suite pytest sta girando sullo slot '{_SLOT}' "
            f"({_LOCK_PATH}). Usa WA_TEST_DB_SLOT=<nome> per uno slot tuo, "
            "oppure cancella il file se e' rimasto da un run ucciso."
        )
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        yield
    finally:
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass
```

⚠️ Il file `.lock` va in `.gitignore` insieme a `data/`. Verificare che `data/` sia già ignorata (lo è).

- [ ] **Step 10: Lo script di seed (contratto §7.4)**

Comportamento vincolante, per intero in §7.4 del contratto. I punti che l'implementatore non può negoziare:

- **idempotente** — get-or-create su `(tenant, phone_hmac)` e su `(tenant, campaign_name)`;
- **rifiuta di girare** se `DATABASE_URL` non è uno SQLite locale o non contiene `test`, salvo `--i-know-what-im-doing`;
- **stampa numeri mascherati**, mai in chiaro;
- `--force-number-active` stampa un warning grosso e non va usato per una prova d'invio vera;
- `--browser-profile` **non deve mai** puntare a `D:\dev\wa-poc\profile` (PoC-1, in corsa fino al 10/08). Lo script rifiuta esplicitamente quel percorso.

```python
# backend/scripts/wa_seed_campaign.py -- guardia di sicurezza, il resto e' CRUD
def _assert_db_di_test(url: str, forzato: bool) -> None:
    """L'08/07 i test hanno creato 110 campagne fantasma su Supabase
    PRODUZIONE. Uno script di seed e' la stessa arma con la sicura tolta."""
    if forzato:
        return
    if not (url.startswith("sqlite") or "test" in url.lower()):
        raise SystemExit(
            f"DATABASE_URL non sembra un database di test ({url[:40]}...). "
            "Se sai cosa stai facendo, ripeti con --i-know-what-im-doing."
        )


def _assert_profilo_non_poc1(path: str) -> None:
    if "wa-poc" in path.replace("/", "\\").lower():
        raise SystemExit(
            "Questo e' il profilo di PoC-1, in corsa fino al 10/08: aprirlo "
            "rischia un re-scan del QR e azzera 14 giorni di misura."
        )
```

- [ ] **Step 11: Suite completa + PR**

```bash
cd backend && pytest tests -q
```
Expected: verde (729 + i nuovi). Poi PR **piccola**, titolo `PR-0: impalcatura condivisa dei cantieri WhatsApp M2/M3`, che dichiara nel corpo: nessuna migrazione, nessun modello toccato, nessun file del canale Instagram modificato oltre alla registrazione dei router.

- [ ] **Step 12: Mergiare PR-0 prima di proseguire**

M3 è fermo finché questa non è su `main`. È l'unica dipendenza bloccante fra i due cantieri.

---

### Task 2: `wa_csv.py` — il parser, puro e difensivo

**Files:**
- Create: `backend/app/services/wa_csv.py`
- Test: `backend/tests/test_wa_csv.py`

**Interfaces:**
- Consumes: `settings.wa_ingest_max_rows`.
- Produces: `RigaCsv` (dataclass: `numero_riga: int`, `valori: dict[str, str]`), `CsvParseError`, `parse_wa_csv(contenuto: bytes) -> tuple[list[RigaCsv], list[str]]` (righe valide, nomi delle colonne attributo) — usata da Task 3.

**Puro di proposito:** niente DB, niente rete, niente logging del contenuto. Il parser è il posto dove i dati sporchi arrivano per primi, e deve essere provabile con venti file storti in venti millisecondi.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_csv.py
import pytest

from app.services.wa_csv import CsvParseError, parse_wa_csv


def test_separatore_virgola():
    righe, attrs = parse_wa_csv(b"numero,nome\n+393331112223,Marco\n")
    assert len(righe) == 1
    assert righe[0].valori["numero"] == "+393331112223"
    assert attrs == []


def test_separatore_punto_e_virgola_perche_excel_italiano():
    righe, _ = parse_wa_csv(b"numero;nome\n+393331112223;Marco\n")
    assert righe[0].valori["nome"] == "Marco"


def test_bom_utf8_non_rompe_l_header():
    """Excel salva con BOM: senza gestirlo, la prima colonna si chiama
    '\\ufeffnumero' e 'numero obbligatorio' fallisce su un file corretto."""
    righe, _ = parse_wa_csv("\ufeffnumero,nome\n+393331112223,Marco\n".encode())
    assert righe[0].valori["numero"] == "+393331112223"


def test_colonne_libere_diventano_attributi():
    righe, attrs = parse_wa_csv(b"numero,nome,ultimo_ordine,citta\n+39333,M,10/01,Roma\n")
    assert sorted(attrs) == ["citta", "ultimo_ordine"]
    assert righe[0].valori["citta"] == "Roma"


def test_header_senza_colonna_numero_fallisce_subito():
    with pytest.raises(CsvParseError) as exc:
        parse_wa_csv(b"telefono,nome\n+39333,M\n")
    assert "numero" in str(exc.value)


def test_file_vuoto_e_solo_header_falliscono_con_messaggi_diversi():
    with pytest.raises(CsvParseError):
        parse_wa_csv(b"")
    with pytest.raises(CsvParseError):
        parse_wa_csv(b"numero,nome\n")


def test_riga_corta_e_riga_lunga_non_uccidono_il_file():
    """Una riga storta e' UNA riga: il file non fallisce in blocco (Q21)."""
    righe, _ = parse_wa_csv(b"numero,nome\n+39333\n+39444,Anna,extra\n+39555,Luca\n")
    assert len(righe) == 3
    assert righe[0].valori.get("nome", "") == ""


def test_intestazioni_duplicate_sollevano():
    with pytest.raises(CsvParseError):
        parse_wa_csv(b"numero,nome,nome\n+39333,M,X\n")


def test_encoding_non_utf8_viene_letto_senza_esplodere():
    """Un CSV latin-1 con accenti non deve dare UnicodeDecodeError: si
    legge con errors='replace' e si va avanti."""
    righe, _ = parse_wa_csv("numero,nome\n+39333,Nicolò\n".encode("latin-1"))
    assert len(righe) == 1


def test_oltre_il_limite_di_righe_rifiuta_il_file(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_ingest_max_rows", 2)
    with pytest.raises(CsvParseError) as exc:
        parse_wa_csv(b"numero\n+391\n+392\n+393\n")
    assert "5.000" in str(exc.value) or "2" in str(exc.value)


def test_il_messaggio_di_errore_non_contiene_numeri_in_chiaro():
    """P12: nemmeno gli errori del parser possono stampare un numero."""
    with pytest.raises(CsvParseError) as exc:
        parse_wa_csv(b"telefono\n+393421460077\n")
    assert "3421460077" not in str(exc.value)
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_csv.py -v` → FAIL, modulo inesistente.

- [ ] **Step 3: Implementare il parser**

```python
# backend/app/services/wa_csv.py
"""Parsing difensivo del CSV di ingest. Puro: nessun DB, nessuna rete,
nessun log del contenuto (le righe contengono numeri di telefono).

Stile ereditato da import_resolver.py: il file non fallisce mai in blocco
per colpa di una riga storta -- una riga sbagliata e' UNA riga (SDD Q21).
Falliscono in blocco solo i problemi di STRUTTURA (header assente, colonna
numero mancante, intestazioni duplicate, file oltre il limite), perche' li'
non c'e' niente da salvare e proseguire produrrebbe solo rumore.
"""
import csv
import io
from dataclasses import dataclass

from app.config import settings

COLONNA_NUMERO = "numero"
COLONNA_NOME = "nome"


class CsvParseError(ValueError):
    """Problema di STRUTTURA del file. Non contiene mai dati di riga."""


@dataclass
class RigaCsv:
    numero_riga: int          # 1-based, header escluso: e' quello che l'admin vede
    valori: dict[str, str]


def _decodifica(contenuto: bytes) -> str:
    """UTF-8 con BOM gestito. Un file latin-1 non deve dare
    UnicodeDecodeError: si sostituiscono i caratteri illeggibili e si va
    avanti -- un accento storto in un nome non giustifica il rifiuto di
    5.000 contatti."""
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return contenuto.decode(encoding)
        except UnicodeDecodeError:
            continue
    return contenuto.decode("latin-1", errors="replace")


def _dialetto(prima_riga: str) -> str:
    """';' se ce ne sono piu' che virgole: e' l'export dell'Excel
    italiano, ed e' il caso piu' probabile dei file veri."""
    return ";" if prima_riga.count(";") > prima_riga.count(",") else ","


def parse_wa_csv(contenuto: bytes) -> tuple[list[RigaCsv], list[str]]:
    testo = _decodifica(contenuto).strip()
    if not testo:
        raise CsvParseError("File vuoto.")

    prima = testo.splitlines()[0]
    reader = csv.reader(io.StringIO(testo), delimiter=_dialetto(prima))
    header = [h.strip().lstrip("\ufeff").lower() for h in next(reader, [])]
    if not header:
        raise CsvParseError("File senza intestazione.")
    if len(set(header)) != len(header):
        raise CsvParseError("Intestazioni duplicate: ogni colonna deve avere un nome unico.")
    if COLONNA_NUMERO not in header:
        raise CsvParseError(
            f"Colonna '{COLONNA_NUMERO}' obbligatoria e assente. "
            f"Colonne trovate: {', '.join(header)}."
        )

    righe: list[RigaCsv] = []
    for i, valori in enumerate(reader, start=1):
        if not any((v or "").strip() for v in valori):
            continue        # riga vuota: non e' uno scarto, e' niente
        if len(righe) >= settings.wa_ingest_max_rows:
            raise CsvParseError(
                f"File oltre il limite di {settings.wa_ingest_max_rows} righe. "
                "Con un cap di 100-200 messaggi al giorno una lista cosi' lunga "
                "sono mesi di campagna: va spezzata."
            )
        # zip_longest a mano: una riga corta riempie di stringhe vuote, una
        # riga lunga scarta la coda. In entrambi i casi la riga SOPRAVVIVE:
        # sara' la validazione del numero a scartarla, con un motivo vero.
        valori = list(valori) + [""] * (len(header) - len(valori))
        righe.append(RigaCsv(numero_riga=i,
                             valori={h: (v or "").strip()
                                     for h, v in zip(header, valori)}))

    if not righe:
        raise CsvParseError("Il file ha l'intestazione ma nessuna riga di dati.")

    attributi = sorted(h for h in header if h not in (COLONNA_NUMERO, COLONNA_NOME))
    return righe, attributi
```

- [ ] **Step 4: Rilanciare i test** → PASS (11 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_csv.py backend/tests/test_wa_csv.py
git commit -m "feat(wa): parser CSV difensivo -- BOM, separatore italiano, righe storte non uccidono il file"
```

---

### Task 3: `wa_ingest.py` — da righe a contatti, con il report degli scarti

**Files:**
- Create: `backend/app/services/wa_ingest.py`
- Test: `backend/tests/test_wa_ingest.py`

**Interfaces:**
- Consumes: `wa_csv.parse_wa_csv` (Task 2), `phone_pseudonym.normalize_e164/hmac_phone/mask_phone` e `PhoneNormalizationError` (M1), `crypto.encrypt`, `settings.wa_ingest_default_country`, `settings.wa_ingest_max_attrs_bytes`.
- Produces: `Scarto` (dataclass: `riga: int`, `motivo: str`, `valore_mascherato: str`), `ReportIngest` (dataclass: `creati`, `aggiornati`, `gia_dnc`, `duplicati_nel_file`, `scarti: list[Scarto]`), `async ingerisci_csv(db, *, tenant_id, campaign_id, contenuto: bytes) -> ReportIngest` — usata da Task 4.

**Le tre regole del flusso** (SDD §7.1): numero invalido → riga scartata **con motivo**, mai "aggiustata" in silenzio; contatto `opted_out`/`do_not_contact` → **escluso e riportato**, mai re-incluso da un CSV nuovo (l'opt-out vince sull'ingest); duplicato dentro il file → dedup.

**Il mascheramento non è un dettaglio** (contratto §2.3): `PhoneNormalizationError` porta il numero in chiaro nel proprio messaggio, ed è corretto per un'eccezione. Ma **l'ingest è il primo chiamante**, e un `logger.error(str(exc))` scriverebbe il numero nei log aggirando `mask_phone`. Qui non si logga mai `str(exc)` e non si logga mai la riga grezza.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_ingest.py
import pytest

from app.services import wa_ingest
from tests.factories_wa import make_campaign, make_contact, make_number, make_tenant


async def _ctx(db):
    tenant = await make_tenant(db)
    number = await make_number(db, tenant)
    campaign, _ = await make_campaign(db, tenant, number)
    await db.commit()
    return tenant, campaign


@pytest.mark.asyncio
async def test_ingest_crea_contatti_e_righe_campagna(db_session):
    tenant, campaign = await _ctx(db_session)
    csv = b"numero,nome,ultimo_ordine\n+393331112223,Marco,10/01/2026\n3334445556,Anna,\n"
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    assert report.creati == 2
    assert report.scarti == []

    from sqlalchemy import func, select
    from app.models.wa import WaCampaignContact, WaContact, WaContactStatus
    assert await db_session.scalar(
        select(func.count(WaContact.id)).where(WaContact.tenant_id == tenant.id)) == 2
    righe = (await db_session.execute(
        select(WaCampaignContact).where(WaCampaignContact.campaign_id == campaign.id)
    )).scalars().all()
    assert len(righe) == 2
    # Contratto di consegna §7.1 + invarianti I1/I3
    for cc in righe:
        assert cc.status == WaContactStatus.queued
        assert cc.current_step == -1
        assert cc.next_action_at is not None
        assert cc.locked_by is None and cc.locked_at is None
        assert cc.failure_count == 0


@pytest.mark.asyncio
async def test_numero_senza_prefisso_prende_il_default_paese(db_session):
    tenant, campaign = await _ctx(db_session)
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=b"numero\n3334445556\n")
    assert report.creati == 1
    from sqlalchemy import select
    from app.models.wa import WaContact
    from app.utils.crypto import decrypt
    c = await db_session.scalar(select(WaContact).where(WaContact.tenant_id == tenant.id))
    assert decrypt(c.encrypted_phone) == "+393334445556"


@pytest.mark.asyncio
async def test_numeri_plausibilmente_sbagliati_vengono_scartati_non_aggiustati(db_session):
    """I casi negativi utili sono quelli PLAUSIBILI: '' e 'abc' non hanno mai
    intercettato niente, '+39 342 146 0077 ext. 12' si' -- ed era un numero
    che diventava un numero DIVERSO, accettato in silenzio."""
    tenant, campaign = await _ctx(db_session)
    csv = ("numero\n"
           "+39 342 146 0077 ext. 12\n"
           "+39 342 146 0077 (casa)\n"
           "0039 342 146 0078\n"
           "+39-342-146-0079\n"
           "342.146.0080\n"
           "+391\n"
           "+3934214600771234567890\n").encode()
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    assert report.creati + len(report.scarti) == 7
    # Nessun numero "aggiustato": tutto cio' che non e' normalizzabile in
    # modo NON ambiguo deve essere uno scarto con motivo.
    assert len(report.scarti) >= 3
    for s in report.scarti:
        assert s.motivo
        assert "0077" not in s.valore_mascherato or s.valore_mascherato.count("•") > 0


@pytest.mark.asyncio
async def test_duplicati_nel_file_contati_una_volta_sola(db_session):
    tenant, campaign = await _ctx(db_session)
    csv = b"numero,nome\n+393331112223,Marco\n+39 333 111 2223,Marco B\n"
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    assert report.creati == 1
    assert report.duplicati_nel_file == 1


@pytest.mark.asyncio
async def test_doppio_upload_dello_stesso_file_non_duplica_nulla(db_session):
    """Q21: l'ingest e' idempotente, il re-upload sana un import interrotto."""
    tenant, campaign = await _ctx(db_session)
    csv = b"numero,nome\n+393331112223,Marco\n"
    primo = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    secondo = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    assert primo.creati == 1 and secondo.creati == 0
    from sqlalchemy import func, select
    from app.models.wa import WaCampaignContact
    assert await db_session.scalar(select(func.count(WaCampaignContact.id))
                                   .where(WaCampaignContact.campaign_id == campaign.id)) == 1


@pytest.mark.asyncio
async def test_re_upload_aggiorna_gli_attributi_ma_non_l_optout(db_session):
    """Q16 + SDD 7.5.5: gli attributi si aggiornano, l'opt-out vince
    sull'ingest e non si riattiva MAI da un file."""
    tenant, campaign = await _ctx(db_session)
    await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=b"numero,citta\n+393331112223,Roma\n")
    from sqlalchemy import select
    from app.models.wa import WaContact, WaDncReason
    c = await db_session.scalar(select(WaContact).where(WaContact.tenant_id == tenant.id))
    c.opted_out = True
    c.do_not_contact = True
    c.dnc_reason = WaDncReason.optout
    await db_session.commit()

    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=b"numero,citta\n+393331112223,Milano\n")
    await db_session.refresh(c)
    assert c.opted_out is True          # non riattivato
    assert report.gia_dnc == 1
    assert c.attributes.get("citta") == "Milano"   # attributi aggiornati


@pytest.mark.asyncio
async def test_contatto_dnc_non_entra_in_campagna(db_session):
    tenant, campaign = await _ctx(db_session)
    contact = await make_contact(db_session, tenant, e164="+393331112223")
    contact.do_not_contact = True
    await db_session.commit()

    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=b"numero\n+393331112223\n")
    assert report.gia_dnc == 1
    from sqlalchemy import func, select
    from app.models.wa import WaCampaignContact
    assert await db_session.scalar(select(func.count(WaCampaignContact.id))
                                   .where(WaCampaignContact.campaign_id == campaign.id)) == 0


@pytest.mark.asyncio
async def test_attributi_oltre_il_limite_vengono_troncati_non_salvati_interi(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_ingest_max_attrs_bytes", 64)
    tenant, campaign = await _ctx(db_session)
    lungo = "x" * 5000
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=f"numero,note\n+393331112223,{lungo}\n".encode())
    assert report.creati == 1
    from sqlalchemy import select
    from app.models.wa import WaContact
    c = await db_session.scalar(select(WaContact).where(WaContact.tenant_id == tenant.id))
    import json
    assert len(json.dumps(c.attributes)) <= 200


@pytest.mark.asyncio
async def test_nessun_numero_in_chiaro_nei_log(db_session, caplog):
    """Contratto §2.3: il primo chiamante di PhoneNormalizationError e'
    questo. Un logger.error(str(exc)) scriverebbe il numero nei log."""
    tenant, campaign = await _ctx(db_session)
    with caplog.at_level("DEBUG"):
        await wa_ingest.ingerisci_csv(
            db_session, tenant_id=tenant.id, campaign_id=campaign.id,
            contenuto=b"numero\n+39 342 146 0077 ext. 12\n+393421460078\n")
    assert "3421460077" not in caplog.text
    assert "3421460078" not in caplog.text
    assert "+39342" not in caplog.text


@pytest.mark.asyncio
async def test_lo_scoping_per_tenant_non_si_rompe(db_session):
    """Q20: due tenant con lo stesso numero contatto = due wa_contacts
    distinti, e nessuna query deve incrociarli."""
    tenant_a, campaign_a = await _ctx(db_session)
    tenant_b = await make_tenant(db_session, name="Altro")
    number_b = await make_number(db_session, tenant_b)
    campaign_b, _ = await make_campaign(db_session, tenant_b, number_b)
    await db_session.commit()

    csv = b"numero\n+393331112223\n"
    a = await wa_ingest.ingerisci_csv(db_session, tenant_id=tenant_a.id,
                                      campaign_id=campaign_a.id, contenuto=csv)
    b = await wa_ingest.ingerisci_csv(db_session, tenant_id=tenant_b.id,
                                      campaign_id=campaign_b.id, contenuto=csv)
    assert a.creati == 1 and b.creati == 1
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

Run: `pytest backend/tests/test_wa_ingest.py -v` → FAIL, modulo inesistente.

- [ ] **Step 3: Implementare `wa_ingest.py`**

```python
# backend/app/services/wa_ingest.py
"""Ingest CSV -> contatti WhatsApp (SDD 7.1).

Tre regole, tutte con un test dedicato:
  1. un numero non normalizzabile in modo NON ambiguo si scarta con un
     motivo, non si aggiusta;
  2. un contatto opted_out/do_not_contact e' escluso e RIPORTATO -- mai
     re-incluso da un file nuovo (l'opt-out vince sull'ingest);
  3. il numero in chiaro non esce mai da questa funzione: ne' nei log, ne'
     nel report, ne' nei messaggi d'errore.

Riga per riga e idempotente (Q21): un import interrotto a meta' si sana
ricaricando lo stesso file.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.services.wa_csv import COLONNA_NOME, COLONNA_NUMERO, parse_wa_csv
from app.utils.crypto import encrypt
from app.utils.phone_pseudonym import (PhoneNormalizationError, hmac_phone,
                                       mask_phone, normalize_e164)


@dataclass
class Scarto:
    riga: int
    motivo: str
    valore_mascherato: str


@dataclass
class ReportIngest:
    creati: int = 0
    aggiornati: int = 0
    gia_dnc: int = 0
    duplicati_nel_file: int = 0
    scarti: list[Scarto] = field(default_factory=list)


def _maschera_grezzo(valore: str) -> str:
    """Un numero malformato spesso NON e' normalizzabile, quindi mask_phone
    non lo copre: si maschera a mano, primi 3 e ultimi 2. Il report va
    all'admin, ma resta un documento con dentro dati personali."""
    v = (valore or "").strip()
    if len(v) <= 5:
        return "•" * len(v)
    return f"{v[:3]}{'•' * max(3, len(v) - 5)}{v[-2:]}"


def _attributi(valori: dict, colonne_attributo: list[str]) -> dict | None:
    attrs = {k: valori.get(k, "") for k in colonne_attributo if valori.get(k, "")}
    if not attrs:
        return None
    # Tetto agli attributi (Q15): si tronca il singolo valore invece di
    # scartare il contatto -- il testo lungo e' un problema di chi ha
    # esportato il CSV, non un motivo per perdere un cliente.
    limite = int(settings.wa_ingest_max_attrs_bytes)
    while len(json.dumps(attrs)) > limite and attrs:
        piu_lungo = max(attrs, key=lambda k: len(str(attrs[k])))
        if len(str(attrs[piu_lungo])) <= 8:
            attrs.pop(piu_lungo)
        else:
            attrs[piu_lungo] = str(attrs[piu_lungo])[: max(8, len(str(attrs[piu_lungo])) // 2)]
    return attrs or None


async def ingerisci_csv(db, *, tenant_id: str, campaign_id: str,
                        contenuto: bytes) -> ReportIngest:
    from app.models.wa import (WaCampaign, WaCampaignContact, WaContact,
                               WaContactStatus)

    righe, colonne_attributo = parse_wa_csv(contenuto)
    report = ReportIngest()
    visti: set[str] = set()
    adesso = datetime.utcnow()

    for riga in righe:
        grezzo = riga.valori.get(COLONNA_NUMERO, "")
        try:
            e164 = normalize_e164(grezzo, default_country=settings.wa_ingest_default_country)
        except PhoneNormalizationError as exc:
            # MAI str(exc): contiene il numero in chiaro (contratto §2.3).
            motivo = type(exc).__name__ if not exc.args else _motivo_pulito(exc)
            report.scarti.append(Scarto(riga.numero_riga, motivo, _maschera_grezzo(grezzo)))
            logger.info(f"[WA ingest] riga {riga.numero_riga} scartata: {motivo}")
            continue

        pseudo = hmac_phone(e164)
        if pseudo in visti:
            report.duplicati_nel_file += 1
            continue
        visti.add(pseudo)

        contatto = await db.scalar(
            select(WaContact).where(WaContact.tenant_id == tenant_id,
                                    WaContact.phone_hmac == pseudo))
        attrs = _attributi(riga.valori, colonne_attributo)
        nome = riga.valori.get(COLONNA_NOME, "") or None

        if contatto is None:
            contatto = WaContact(tenant_id=tenant_id, phone_hmac=pseudo,
                                 encrypted_phone=encrypt(e164), display_name=nome,
                                 attributes=attrs, first_seen_at=adesso)
            db.add(contatto)
            await db.flush()
            report.creati += 1
        else:
            # Gap-fill (Q16): si aggiorna cio' che il file porta, non si
            # cancella cio' che c'era.
            if nome:
                contatto.display_name = nome
            if attrs:
                contatto.attributes = {**(contatto.attributes or {}), **attrs}
            report.aggiornati += 1

        if contatto.opted_out or contatto.do_not_contact:
            # L'opt-out vince sull'ingest, sempre e comunque (SDD 7.5.5).
            report.gia_dnc += 1
            continue

        esistente = await db.scalar(
            select(WaCampaignContact).where(
                WaCampaignContact.campaign_id == campaign_id,
                WaCampaignContact.contact_id == contatto.id))
        if esistente is None:
            db.add(WaCampaignContact(
                campaign_id=campaign_id, contact_id=contatto.id,
                status=WaContactStatus.queued, current_step=-1,
                # Contratto §7.2: MAI NULL su una riga non terminale (I3).
                next_action_at=adesso, failure_count=0,
            ))

    await db.flush()
    # Contatore denormalizzato: e' di M2 (contratto §4.1).
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is not None:
        from sqlalchemy import func
        campagna.total_contacts = await db.scalar(
            select(func.count(WaCampaignContact.id))
            .where(WaCampaignContact.campaign_id == campaign_id)) or 0
    await db.commit()

    logger.info(f"[WA ingest] campagna={campaign_id} creati={report.creati} "
                f"aggiornati={report.aggiornati} dnc={report.gia_dnc} "
                f"dup={report.duplicati_nel_file} scarti={len(report.scarti)}")
    return report


def _motivo_pulito(exc: PhoneNormalizationError) -> str:
    """Il messaggio dell'eccezione contiene il numero in chiaro: si tiene
    solo la PARTE DIAGNOSTICA, fino ai due punti. Se la forma cambia, si
    ripiega sul nome della classe -- mai sul messaggio intero."""
    testo = str(exc)
    return testo.split(":")[0].strip() if ":" in testo else type(exc).__name__
```

⚠️ **Nota per l'implementatore**: `_motivo_pulito` dipende dal formato dei messaggi di `phone_pseudonym.py`. Prima di scriverla, **leggere quel file** (righe 46-76) e verificare che ogni `raise` metta la parte diagnostica **prima** dei due punti. Se anche un solo messaggio mette il numero prima, questa funzione lo pubblica: in quel caso si passa a una mappa esplicita `tipo di errore → motivo`, senza toccare `phone_pseudonym.py` (patrimonio M1).

- [ ] **Step 4: Rilanciare i test** → PASS (10 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_ingest.py backend/tests/test_wa_ingest.py
git commit -m "feat(wa): ingest CSV -- dedup, DNC che vince sull'ingest, report scarti mascherato"
```

---

### Task 4: API di ingest e contatti

**Files:**
- Modify: `backend/app/api/wa_contacts.py`
- Test: `backend/tests/test_wa_api_contacts.py`

**Interfaces:**
- Consumes: `wa_ingest.ingerisci_csv` (Task 3).
- Produces: `POST /api/wa/contacts/ingest` (multipart: `campaign_id`, `file`), `GET /api/wa/contacts?campaign_id=…`, `DELETE /api/wa/contacts/{campaign_contact_id}`.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_api_contacts.py
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.factories_wa import (make_campaign, make_campaign_contact, make_contact,
                                make_number, make_tenant)


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_ingest_risponde_col_report(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()

    async with await _client() as client:
        r = await client.post(
            "/api/wa/contacts/ingest",
            data={"campaign_id": campaign.id},
            files={"file": ("lista.csv", b"numero,nome\n+393331112223,Marco\n", "text/csv")},
        )
    assert r.status_code in (200, 401)
    if r.status_code == 200:
        body = r.json()
        assert body["creati"] == 1
        assert body["scarti"] == []


@pytest.mark.asyncio
async def test_file_non_csv_rifiutato_con_422_non_500(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()
    async with await _client() as client:
        r = await client.post(
            "/api/wa/contacts/ingest",
            data={"campaign_id": campaign.id},
            files={"file": ("foto.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
    assert r.status_code in (401, 422)


@pytest.mark.asyncio
async def test_ingest_su_campagna_running_e_rifiutato(db_session):
    """Macchina a stati: la lista si carica in draft. Aggiungere contatti a
    una campagna che sta girando cambia il denominatore dei KPI sotto i
    piedi al worker."""
    from app.models.wa import WaCampaignStatus
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.running)
    await db_session.commit()
    async with await _client() as client:
        r = await client.post(
            "/api/wa/contacts/ingest",
            data={"campaign_id": campaign.id},
            files={"file": ("l.csv", b"numero\n+393331112223\n", "text/csv")},
        )
    assert r.status_code in (401, 409)


@pytest.mark.asyncio
async def test_rimozione_contatto_sotto_lock_fresco_rifiutata(db_session):
    """Invariante I1: M2 LEGGE i campi di lock e non li scrive. Una riga
    sotto lock e' in mano al worker di M3 in questo momento."""
    from app.api import wa_contacts
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    contact = await make_contact(db_session, tenant)
    cc = await make_campaign_contact(db_session, campaign, contact)
    cc.locked_by = "worker-vivo"
    cc.locked_at = datetime.utcnow()
    await db_session.commit()

    with pytest.raises(Exception):
        await wa_contacts.rimuovi_contatto(cc.id, db=db_session)

    cc.locked_at = datetime.utcnow() - timedelta(minutes=45)   # lock stale
    await db_session.commit()
    assert await wa_contacts.rimuovi_contatto(cc.id, db=db_session) == {"rimosso": True}
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

- [ ] **Step 3: Implementare gli endpoint**

```python
# backend/app/api/wa_contacts.py
"""Ingest e gestione contatti di una campagna WhatsApp."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaContact, WaContactStatus)
from app.services.wa_csv import CsvParseError
from app.services.wa_ingest import ingerisci_csv
from app.utils.phone_pseudonym import mask_phone
from app.utils.crypto import decrypt

router = APIRouter(prefix="/wa/contacts", tags=["wa-contacts"])

# 10 MB: 5.000 righe con dieci colonne stanno abbondantemente sotto. Serve a
# non tenere in memoria un file che nessuno vuole davvero caricare.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post("/ingest")
async def ingest(campaign_id: str = Form(...), file: UploadFile = File(...),
                 db=Depends(get_db)) -> dict:
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None:
        raise HTTPException(404, "campagna inesistente")
    if campagna.status != WaCampaignStatus.draft:
        raise HTTPException(
            409, f"la campagna e' in stato {campagna.status.value}: i contatti si "
                 "caricano quando e' in bozza")

    contenuto = await file.read()
    if len(contenuto) > MAX_UPLOAD_BYTES:
        raise HTTPException(422, "file troppo grande (limite 10 MB)")

    try:
        report = await ingerisci_csv(db, tenant_id=campagna.tenant_id,
                                     campaign_id=campaign_id, contenuto=contenuto)
    except CsvParseError as exc:
        # 422 e non 500: il file e' sbagliato, non il server. Il messaggio di
        # CsvParseError non contiene mai dati di riga (Task 2).
        raise HTTPException(422, str(exc))

    return {
        "creati": report.creati,
        "aggiornati": report.aggiornati,
        "gia_dnc": report.gia_dnc,
        "duplicati_nel_file": report.duplicati_nel_file,
        "scarti": [{"riga": s.riga, "motivo": s.motivo, "valore": s.valore_mascherato}
                   for s in report.scarti],
    }


@router.get("")
async def lista_contatti(campaign_id: str, limit: int = 200, offset: int = 0,
                         db=Depends(get_db)) -> dict:
    """Il numero torna SEMPRE mascherato (P12): la dashboard non ha motivo di
    vedere un numero intero, e un endpoint che lo espone e' un endpoint che
    prima o poi finisce in un log o in uno screenshot."""
    righe = (await db.execute(
        select(WaCampaignContact, WaContact)
        .join(WaContact, WaContact.id == WaCampaignContact.contact_id)
        .where(WaCampaignContact.campaign_id == campaign_id)
        .limit(min(limit, 500)).offset(offset)
    )).all()
    return {"contatti": [
        {
            "id": cc.id,
            "numero": mask_phone(decrypt(c.encrypted_phone)),
            "nome": c.display_name,
            "stato": cc.status.value,
            "tentativi_falliti": cc.failure_count,
            "ultimo_errore": cc.last_error,
            "opted_out": c.opted_out,
            "in_lavorazione": bool(cc.locked_by),
        }
        for cc, c in righe
    ]}


@router.delete("/{campaign_contact_id}")
async def rimuovi_contatto(campaign_contact_id: str, db=Depends(get_db)) -> dict:
    """Q18. Rifiuta se la riga e' sotto lock FRESCO: in quel momento e' in
    mano al worker di M3, e cancellarla sotto i suoi piedi significa un
    invio che scrive su una riga che non esiste piu'. M2 legge i campi di
    lock, non li scrive (invariante I1)."""
    cc = await db.scalar(select(WaCampaignContact)
                         .where(WaCampaignContact.id == campaign_contact_id))
    if cc is None:
        raise HTTPException(404, "riga inesistente")
    if cc.locked_by and cc.locked_at and cc.locked_at > (
            datetime.utcnow() - timedelta(minutes=int(settings.wa_lock_timeout_min))):
        raise HTTPException(409, "contatto in lavorazione dal worker: riprova fra poco")
    if cc.status in (WaContactStatus.opted_out, WaContactStatus.completed):
        raise HTTPException(409, f"riga in stato terminale ({cc.status.value}): "
                                 "non si rimuove, resta come storico")
    await db.delete(cc)
    await db.commit()
    return {"rimosso": True}
```

- [ ] **Step 4: Rilanciare i test** → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/wa_contacts.py backend/tests/test_wa_api_contacts.py
git commit -m "feat(wa): API ingest CSV con report scarti, lista contatti mascherata, rimozione con guardia lock"
```

---

### Task 5: CRUD tenant e numeri (con la riattivazione che oggi non esiste)

**Files:**
- Modify: `backend/app/api/tenants.py`, `backend/app/api/wa_numbers.py`
- Test: `backend/tests/test_wa_api_numbers.py`

**Interfaces:**
- Consumes: `wa_session.assisted_login`, `.check_session`, `.profile_dir_for` (M1).
- Produces: CRUD tenant; `GET/POST /api/wa/numbers`, `PATCH /api/wa/numbers/{id}`, `POST /api/wa/numbers/{id}/login`, `POST /api/wa/numbers/{id}/check`, `POST /api/wa/numbers/{id}/riattiva`.

**Il punto delicato è la riattivazione** (contratto §2.2). M1 ha chiuso di proposito la resurrezione automatica: `_persist_status` non fa più uscire un numero da `retired`/`suspended`, perché quegli stati li mette un operatore o la piattaforma e **non sono deducibili da una lettura del DOM**. Conseguenza: **oggi non esiste alcun modo di rimettere operativo un numero ritirato**, e questo task è quel modo.

Tre regole non negoziabili:
1. `retired|suspended` → **`pending_qr`**, mai → `active`. Un numero riattivato ripassa dalla verifica sessione, che guarda il browser vero. M2 non ha il diritto di dichiarare viva una sessione che non ha visto.
2. **Motivo scritto obbligatorio**, in append su `notes` con la data.
3. Azzera `sent_today`, `sent_date`, e riporta `warmup_day = 1`: un numero fermo da settimane riparte dalla rampa, non dal cap a cui era arrivato.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_api_numbers.py
import pytest

from app.api import wa_numbers
from app.models.wa import WaNumberStatus
from tests.factories_wa import make_number, make_tenant


@pytest.mark.asyncio
async def test_riattivazione_porta_a_pending_qr_non_ad_active(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.retired)
    n.sent_today, n.sent_date, n.warmup_day = 57, "2026-07-01", 7
    await db_session.commit()

    await wa_numbers.riattiva(n.id, motivo="numero rientrato dal cliente", db=db_session)
    await db_session.refresh(n)
    assert n.status == WaNumberStatus.pending_qr
    assert n.sent_today == 0 and n.sent_date is None and n.warmup_day == 1
    assert "numero rientrato dal cliente" in (n.notes or "")


@pytest.mark.asyncio
async def test_riattivazione_senza_motivo_rifiutata(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.suspended)
    await db_session.commit()
    with pytest.raises(Exception):
        await wa_numbers.riattiva(n.id, motivo="   ", db=db_session)


@pytest.mark.asyncio
async def test_riattivazione_su_numero_attivo_e_un_errore_non_un_no_op(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.active)
    await db_session.commit()
    with pytest.raises(Exception):
        await wa_numbers.riattiva(n.id, motivo="tanto per", db=db_session)


@pytest.mark.asyncio
async def test_il_numero_non_e_mai_esposto_in_chiaro(db_session):
    tenant = await make_tenant(db_session)
    await make_number(db_session, tenant, e164="+393421460077")
    await db_session.commit()
    elenco = await wa_numbers.lista(db=db_session)
    testo = str(elenco)
    assert "3421460077" not in testo
    assert "•" in testo


@pytest.mark.asyncio
async def test_patch_non_puo_scrivere_i_contatori_di_runtime(db_session):
    """Contratto §4.1: sent_today/sent_date/warmup_day sono di M3 in
    scrittura (tranne l'azzeramento in riattivazione). Un PATCH che li
    accetta e' una violazione del contratto, non una comodita'."""
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.sent_today = 5
    await db_session.commit()
    await wa_numbers.aggiorna(n.id, {"label": "nuovo nome", "sent_today": 0},
                              db=db_session)
    await db_session.refresh(n)
    assert n.label == "nuovo nome"
    assert n.sent_today == 5      # ignorato, non applicato
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

- [ ] **Step 3: Implementare — il cuore è `riattiva`**

```python
# backend/app/api/wa_numbers.py -- estratto vincolante
CAMPI_MODIFICABILI = {"label", "proxy_url", "daily_cap", "notes"}
# sent_today / sent_date / warmup_day / status NON sono qui: sono di M3 in
# scrittura (contratto §4.1). Un PATCH che li accettasse creerebbe due
# padroni per la stessa colonna, ed e' esattamente cio' che il contratto
# esiste per impedire.


@router.post("/{number_id}/riattiva")
async def riattiva(number_id: str, motivo: str = Body(..., embed=True),
                   db=Depends(get_db)) -> dict:
    """retired|suspended -> pending_qr (contratto §2.2).

    Mai -> active: la sessione potrebbe non esserci piu', e chi lo dice e'
    il browser (wa_session.check_session), non questo endpoint.
    """
    numero = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
    if numero is None:
        raise HTTPException(404, "numero inesistente")
    if numero.status not in (WaNumberStatus.retired, WaNumberStatus.suspended):
        raise HTTPException(
            409, f"il numero e' in stato {numero.status.value}: la riattivazione "
                 "esiste solo per 'retired' e 'suspended'")
    if not (motivo or "").strip():
        raise HTTPException(422, "il motivo e' obbligatorio: uno stato messo a mano "
                                 "si toglie a mano, lasciando traccia")

    numero.status = WaNumberStatus.pending_qr
    numero.sent_today = 0
    numero.sent_date = None
    numero.warmup_day = 1        # riparte dalla rampa, non dal cap raggiunto
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    numero.notes = f"{(numero.notes or '').rstrip()}\n[{stamp}] riattivato: {motivo.strip()}".strip()
    await db.commit()
    logger.warning(f"[WA] numero {number_id[:8]} riattivato -> pending_qr: {motivo.strip()}")
    return {"status": numero.status.value,
            "prossimo_passo": "avvia il login QR, poi verifica la sessione"}
```

Il resto del file è CRUD ordinario, con due obblighi: il numero torna **sempre** mascherato (`mask_phone(decrypt(...))`), e `POST /login` chiama `wa_session.assisted_login`, che apre un browser **visibile** — quindi va lanciato solo quando qualcuno è davanti allo schermo. `tenants.py` è CRUD semplice: `id`, `name`, `status`, `settings`, `created_at`.

- [ ] **Step 4: Rilanciare i test** → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/wa_numbers.py backend/app/api/tenants.py backend/tests/test_wa_api_numbers.py
git commit -m "feat(wa): CRUD tenant e numeri, riattivazione retired/suspended -> pending_qr con motivo"
```

---

### Task 6: campagne e sequenze — `optout_enabled` condizionale, template validati

**Files:**
- Create: `backend/app/services/wa_campaign_service.py`
- Modify: `backend/app/api/wa_campaigns.py`
- Test: `backend/tests/test_wa_campaign_service.py`

**Interfaces:**
- Consumes: `wa_template.validate_wa_template` (PR-0).
- Produces: `calcola_optout_enabled(tipo) -> bool`, `valida_step(template, *, colonne_note) -> None` (solleva `ValueError`), `async crea_campagna(db, dati) -> WaCampaign`; endpoint `POST/GET/PATCH /api/wa/campaigns`, `PUT /api/wa/campaigns/{id}/steps/0`.

**`optout_enabled` lo assegna M2, esplicitamente** (contratto §2.1). Il `server_default=true` a DB è la rete di sicurezza, non la regola: la regola della SDD è *"True **se** marketing"* — condizionale, quindi applicativa. Senza assegnazione esplicita, o la scrivono in due o non la scrive nessuno.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_campaign_service.py
import pytest

from app.models.wa import WaCampaignType
from app.services import wa_campaign_service as svc
from tests.factories_wa import make_number, make_tenant


def test_optout_e_attivo_per_marketing_e_spento_per_followup():
    assert svc.calcola_optout_enabled(WaCampaignType.marketing) is True
    assert svc.calcola_optout_enabled(WaCampaignType.followup) is False


@pytest.mark.asyncio
async def test_campagna_followup_ha_optout_false_A_DB_non_solo_nella_risposta(db_session):
    """Il server_default e' True: se il servizio non assegna esplicitamente,
    la riga a DB esce sbagliata anche con una risposta API giusta."""
    from sqlalchemy import select
    from app.models.wa import WaCampaign
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    camp = await svc.crea_campagna(db_session, {
        "tenant_id": tenant.id, "wa_number_id": number.id, "name": "follow",
        "campaign_type": WaCampaignType.followup, "template_a": "Ciao {nome}.",
    })
    riga = await db_session.scalar(select(WaCampaign).where(WaCampaign.id == camp.id))
    assert riga.optout_enabled is False


@pytest.mark.asyncio
async def test_marketing_senza_cta_rifiutata(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.crea_campagna(db_session, {
            "tenant_id": tenant.id, "wa_number_id": number.id, "name": "promo",
            "campaign_type": WaCampaignType.marketing, "template_a": "Ciao {nome}.",
            "optout_cta": "  ",
        })


def test_step_con_placeholder_ignoto_non_si_salva():
    with pytest.raises(ValueError) as exc:
        svc.valida_step("Ciao {nome}, ordine {ultimo_ordine}.", colonne_note=set())
    assert "ultimo_ordine" in str(exc.value)


def test_step_con_placeholder_coperto_dal_csv_si_salva():
    svc.valida_step("Ciao {nome}, ordine {ultimo_ordine}.",
                    colonne_note={"ultimo_ordine"})


def test_step_vuoto_non_si_salva():
    with pytest.raises(ValueError):
        svc.valida_step("   ", colonne_note=set())


@pytest.mark.asyncio
async def test_campagna_su_numero_di_un_altro_tenant_rifiutata(db_session):
    """Scoping: un numero appartiene a un tenant. Incrociarli e' il bug che
    manda i messaggi di un cliente dal numero di un altro."""
    tenant_a = await make_tenant(db_session, name="A")
    tenant_b = await make_tenant(db_session, name="B")
    number_b = await make_number(db_session, tenant_b)
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.crea_campagna(db_session, {
            "tenant_id": tenant_a.id, "wa_number_id": number_b.id, "name": "x",
            "campaign_type": WaCampaignType.followup, "template_a": "Ciao.",
        })
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

- [ ] **Step 3: Implementare il servizio**

```python
# backend/app/services/wa_campaign_service.py
"""Regole di campagna del canale WhatsApp.

Sta in un servizio e non nell'endpoint perche' queste regole valgono anche
per lo script di seed e per i test: una regola che vive dentro un handler
HTTP e' una regola che il resto del sistema puo' aggirare.
"""
from datetime import datetime

from sqlalchemy import select

from app.models.wa import (WaCampaign, WaCampaignStatus, WaCampaignType, WaNumber,
                           WaSendCondition, WaSequenceStep)
from app.services.wa_template import validate_wa_template


def calcola_optout_enabled(tipo: WaCampaignType) -> bool:
    """V10: marketing -> CTA "scrivi STOP" obbligatoria; follow-up -> no.
    Il server_default=true della migrazione 025 e' la rete di sicurezza; la
    regola vera e' condizionale, e quindi va scritta qui (contratto §2.1)."""
    return tipo == WaCampaignType.marketing


def valida_step(template: str, *, colonne_note: set[str]) -> None:
    """Un template con placeholder che il CSV non copre non si salva: se
    passasse, fallirebbe a tempo di invio, un contatto alla volta, in una
    campagna gia' partita."""
    if not (template or "").strip():
        raise ValueError("Il testo del messaggio non puo' essere vuoto.")
    ignoti = validate_wa_template(template, known_attributes=colonne_note)
    if ignoti:
        raise ValueError(
            "Placeholder non disponibili nella lista contatti: "
            + ", ".join(f"{{{x}}}" for x in ignoti)
            + ". Aggiungi la colonna al CSV oppure togli il segnaposto."
        )


async def crea_campagna(db, dati: dict) -> WaCampaign:
    tipo = dati["campaign_type"]
    numero = await db.scalar(select(WaNumber).where(WaNumber.id == dati["wa_number_id"]))
    if numero is None:
        raise ValueError("Numero inesistente.")
    if numero.tenant_id != dati["tenant_id"]:
        raise ValueError("Il numero appartiene a un altro tenant.")

    optout = dati.get("optout_enabled")
    if optout is None:
        optout = calcola_optout_enabled(tipo)      # esplicito, mai il default a DB
    cta = (dati.get("optout_cta") or "").strip() or None
    if optout and not cta:
        raise ValueError(
            "Una campagna con opt-out attivo deve avere una CTA: non si manda "
            "marketing senza via d'uscita."
        )

    valida_step(dati["template_a"], colonne_note=set(dati.get("colonne_note") or []))

    campagna = WaCampaign(
        tenant_id=dati["tenant_id"], wa_number_id=numero.id, name=dati["name"],
        campaign_type=tipo, status=WaCampaignStatus.draft,
        optout_enabled=bool(optout), optout_cta=cta,
        daily_limit=dati.get("daily_limit"),
        active_hours_start=dati.get("active_hours_start"),
        active_hours_end=dati.get("active_hours_end"),
        created_at=datetime.utcnow(),
    )
    db.add(campagna)
    await db.flush()
    db.add(WaSequenceStep(
        campaign_id=campagna.id, step_index=0, template_a=dati["template_a"],
        template_b=dati.get("template_b"), template_c=dati.get("template_c"),
        template_d=dati.get("template_d"),
        # MVP: un solo step, condizione fissa. Lo SCHEMA e' completo, il
        # motore multi-step si accende post-MVP senza migrazione (SDD Q29).
        send_condition=WaSendCondition.always, wait_days=0,
    ))
    await db.commit()
    return campagna
```

Gli endpoint di `wa_campaigns.py` sono il guscio: `POST` chiama `crea_campagna` e traduce `ValueError` in **422**; `PATCH` consente di modificare nome, cap, CTA, orari e template **solo in `draft`** (a campagna partita si passa da pausa); `PUT /steps/0` rivalida il template contro le colonne effettivamente presenti nei contatti già caricati.

- [ ] **Step 4: Rilanciare i test** → PASS (7 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_campaign_service.py backend/app/api/wa_campaigns.py backend/tests/test_wa_campaign_service.py
git commit -m "feat(wa): campagne e step 0 -- optout_enabled esplicito, template validati al salvataggio"
```

---

### Task 7: start / pausa / stop / resume, e la ri-stampa di `next_action_at`

**Files:**
- Modify: `backend/app/services/wa_campaign_service.py`, `backend/app/api/wa_campaigns.py`
- Test: `backend/tests/test_wa_campaign_lifecycle.py`

**Interfaces:**
- Produces: `async avvia(db, campaign_id) -> WaCampaign`, `async pausa(db, campaign_id)`, `async riprendi(db, campaign_id)`, `async ferma(db, campaign_id)`; endpoint `POST /api/wa/campaigns/{id}/{start|pause|resume|stop}`.

**Le due cose che questo task deve fare bene:**

1. **Le validazioni di start** (SDD §8.1): numero `active`, almeno uno step, almeno un contatto, e **nessun'altra campagna `running` sullo stesso numero** (decisione 23/07, Q2: max 1 campagna running per numero — è ciò che rende il pacing per-job sicuro).
2. **La ri-stampa di `next_action_at`** (contratto §7.2): allo start e a ogni resume, tutte le righe ancora `queued` prendono `next_action_at = adesso`. Una campagna ingerita e lasciata in bozza per tre settimane non deve presentarsi al worker come tremila righe scadute da giorni (SDD Q31: re-pacing al resume).

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_wa_campaign_lifecycle.py
from datetime import datetime, timedelta

import pytest

from app.models.wa import WaCampaignStatus, WaNumberStatus
from app.services import wa_campaign_service as svc
from tests.factories_wa import (make_campaign, make_campaign_contact, make_contact,
                                make_number, make_tenant)


async def _pronta(db):
    tenant = await make_tenant(db)
    number = await make_number(db, tenant)
    campaign, _ = await make_campaign(db, tenant, number)
    contact = await make_contact(db, tenant)
    cc = await make_campaign_contact(db, campaign, contact)
    await db.commit()
    return tenant, number, campaign, cc


@pytest.mark.asyncio
async def test_start_valida_e_porta_a_running(db_session):
    _, _, campaign, _ = await _pronta(db_session)
    await svc.avvia(db_session, campaign.id)
    await db_session.refresh(campaign)
    assert campaign.status == WaCampaignStatus.running
    assert campaign.started_at is not None


@pytest.mark.asyncio
async def test_start_ristampa_next_action_at_sulle_righe_queued(db_session):
    """Contratto §7.2: una campagna ingerita e lasciata in bozza tre
    settimane non deve presentarsi al worker come righe scadute da giorni."""
    _, _, campaign, cc = await _pronta(db_session)
    cc.next_action_at = datetime.utcnow() - timedelta(days=21)
    await db_session.commit()

    await svc.avvia(db_session, campaign.id)
    await db_session.refresh(cc)
    assert cc.next_action_at > datetime.utcnow() - timedelta(minutes=1)


@pytest.mark.asyncio
async def test_start_rifiutato_se_il_numero_non_e_active(db_session):
    _, number, campaign, _ = await _pronta(db_session)
    number.status = WaNumberStatus.qr_required
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.avvia(db_session, campaign.id)


@pytest.mark.asyncio
async def test_start_rifiutato_senza_contatti(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.avvia(db_session, campaign.id)


@pytest.mark.asyncio
async def test_una_sola_campagna_running_per_numero(db_session):
    """Decisione 23/07 (Q2): due campagne sullo stesso numero
    significherebbero ritmo doppio, e il pacing e' per-job."""
    tenant, number, campaign_a, _ = await _pronta(db_session)
    await svc.avvia(db_session, campaign_a.id)
    campaign_b, _ = await make_campaign(db_session, tenant, number, name="seconda")
    contact_b = await make_contact(db_session, tenant)
    await make_campaign_contact(db_session, campaign_b, contact_b)
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.avvia(db_session, campaign_b.id)


@pytest.mark.asyncio
async def test_doppio_start_non_e_un_no_op_silenzioso(db_session):
    _, _, campaign, _ = await _pronta(db_session)
    await svc.avvia(db_session, campaign.id)
    with pytest.raises(ValueError):
        await svc.avvia(db_session, campaign.id)


@pytest.mark.asyncio
async def test_resume_rispalma_ma_non_tocca_le_righe_terminali(db_session):
    from app.models.wa import WaContactStatus
    _, _, campaign, cc = await _pronta(db_session)
    await svc.avvia(db_session, campaign.id)
    await svc.pausa(db_session, campaign.id)
    cc.status = WaContactStatus.opted_out
    cc.next_action_at = None
    await db_session.commit()

    await svc.riprendi(db_session, campaign.id)
    await db_session.refresh(cc)
    assert cc.next_action_at is None          # terminale: non si risveglia
    assert cc.status == WaContactStatus.opted_out


@pytest.mark.asyncio
async def test_stop_non_cancella_niente(db_session):
    """'stopped' e' uno stato, non una cancellazione: i KPI restano."""
    from sqlalchemy import func, select
    from app.models.wa import WaCampaignContact
    _, _, campaign, _ = await _pronta(db_session)
    await svc.avvia(db_session, campaign.id)
    await svc.ferma(db_session, campaign.id)
    await db_session.refresh(campaign)
    assert campaign.status == WaCampaignStatus.stopped
    assert await db_session.scalar(select(func.count(WaCampaignContact.id))
                                   .where(WaCampaignContact.campaign_id == campaign.id)) == 1
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

- [ ] **Step 3: Implementare il ciclo di vita**

```python
# backend/app/services/wa_campaign_service.py -- in coda
async def _ristampa_next_action(db, campaign_id: str, quando: datetime) -> int:
    """Re-pacing (contratto §7.2, SDD Q31): tutte le righe ancora attive
    prendono un appuntamento nuovo. NON tocca le righe terminali, che hanno
    next_action_at NULL per una ragione."""
    from sqlalchemy import update
    from app.models.wa import WaCampaignContact, WaContactStatus

    res = await db.execute(
        update(WaCampaignContact)
        .where(WaCampaignContact.campaign_id == campaign_id,
               WaCampaignContact.status.in_([WaContactStatus.queued,
                                             WaContactStatus.in_sequence]))
        .values(next_action_at=quando)
    )
    return res.rowcount or 0


async def avvia(db, campaign_id: str) -> WaCampaign:
    from sqlalchemy import func
    from app.models.wa import WaCampaignContact

    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None:
        raise ValueError("Campagna inesistente.")
    if campagna.status not in (WaCampaignStatus.draft, WaCampaignStatus.paused):
        raise ValueError(f"La campagna e' gia' in stato {campagna.status.value}.")

    numero = await db.scalar(select(WaNumber).where(WaNumber.id == campagna.wa_number_id))
    if numero is None or numero.status != WaNumberStatus.active:
        raise ValueError(
            "Il numero non e' attivo: serve una sessione WhatsApp valida (QR) "
            "prima di far partire la campagna.")

    # Max 1 campagna running per numero (Q2, 23/07): con due, il pacing
    # per-job produrrebbe ritmo doppio sullo stesso numero.
    altra = await db.scalar(
        select(WaCampaign.id).where(WaCampaign.wa_number_id == numero.id,
                                    WaCampaign.status == WaCampaignStatus.running,
                                    WaCampaign.id != campagna.id))
    if altra:
        raise ValueError("Questo numero ha gia' una campagna in corso: mettila in "
                         "pausa prima di avviarne un'altra.")

    if not await db.scalar(select(func.count(WaSequenceStep.id))
                           .where(WaSequenceStep.campaign_id == campaign_id)):
        raise ValueError("La campagna non ha nessun messaggio.")
    if not await db.scalar(select(func.count(WaCampaignContact.id))
                           .where(WaCampaignContact.campaign_id == campaign_id)):
        raise ValueError("La campagna non ha contatti: carica prima la lista.")

    adesso = datetime.utcnow()
    campagna.status = WaCampaignStatus.running
    campagna.started_at = campagna.started_at or adesso
    await _ristampa_next_action(db, campaign_id, adesso)
    await db.commit()
    return campagna


async def pausa(db, campaign_id: str) -> WaCampaign:
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None or campagna.status != WaCampaignStatus.running:
        raise ValueError("Si mette in pausa solo una campagna in corso.")
    campagna.status = WaCampaignStatus.paused
    await db.commit()
    # NB: i job gia' accodati di M3 vedranno lo stato al prossimo controllo
    # (la mini-sessione ricontrolla a ogni messaggio, non solo all'avvio).
    return campagna


async def riprendi(db, campaign_id: str) -> WaCampaign:
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None or campagna.status != WaCampaignStatus.paused:
        raise ValueError("Si riprende solo una campagna in pausa.")
    return await avvia(db, campaign_id)


async def ferma(db, campaign_id: str) -> WaCampaign:
    """Stop definitivo. Non cancella niente: i contatti e i KPI restano, e
    la campagna diventa storico."""
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None:
        raise ValueError("Campagna inesistente.")
    if campagna.status in (WaCampaignStatus.completed, WaCampaignStatus.stopped):
        raise ValueError(f"La campagna e' gia' {campagna.status.value}.")
    campagna.status = WaCampaignStatus.stopped
    campagna.completed_at = datetime.utcnow()
    await db.commit()
    return campagna
```

**Nota di confine con M3:** `running → completed` e `running → error` sono di **M3** (contratto §4.1): M2 non le scrive mai. `completed_at` qui si valorizza solo sullo stop manuale.

- [ ] **Step 4: Rilanciare i test** → PASS (8 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_campaign_service.py backend/app/api/wa_campaigns.py backend/tests/test_wa_campaign_lifecycle.py
git commit -m "feat(wa): ciclo di vita campagna con validazioni di start e re-pacing di next_action_at"
```

---

### Task 8: KPI di campagna

**Files:**
- Modify: `backend/app/api/wa_campaigns.py`
- Test: `backend/tests/test_wa_api_kpi.py`

**Interfaces:**
- Produces: `GET /api/wa/campaigns/{id}/kpi`.

I KPI vengono dai contatori denormalizzati (`sent`, `replied`, `opted_out`, `failed`, `total_contacts`) — pattern IG, SDD §15.1. **M2 li legge e basta**: `sent`/`failed`/`opted_out` li scrive M3, `replied` M4 (contratto §4.1). L'unico contatore di M2 è `total_contacts`, scritto dall'ingest.

- [ ] **Step 1: Scrivere i test**

```python
# backend/tests/test_wa_api_kpi.py
import pytest

from app.api import wa_campaigns
from tests.factories_wa import (make_campaign, make_campaign_contact, make_contact,
                                make_number, make_tenant)


@pytest.mark.asyncio
async def test_kpi_su_campagna_vuota_non_divide_per_zero(db_session):
    """Il caso limite piu' banale e quello che rompe davvero le dashboard."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()
    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["tasso_risposta"] == 0
    assert kpi["tasso_optout"] == 0


@pytest.mark.asyncio
async def test_kpi_derivati_calcolati_sugli_inviati_non_sui_caricati(db_session):
    """Il tasso di risposta si misura su chi ha ricevuto, non su chi e' in
    lista: altrimenti una campagna appena partita sembra un disastro."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    campaign.total_contacts, campaign.sent, campaign.replied = 100, 20, 5
    campaign.opted_out, campaign.failed = 2, 3
    await db_session.commit()
    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["tasso_risposta"] == 25.0     # 5/20
    assert kpi["tasso_optout"] == 10.0       # 2/20
    assert kpi["da_inviare"] == 80


@pytest.mark.asyncio
async def test_kpi_segnala_la_soglia_di_allarme_optout(db_session):
    """SDD 10.3: oltre il 5% di opt-out la campagna va guardata. Il flag e'
    informativo: mettere in pausa e' una decisione di Tommaso."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    campaign.sent, campaign.opted_out = 100, 6
    await db_session.commit()
    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["allarme_optout"] is True
```

- [ ] **Step 2: Lanciare i test e verificare che falliscano**

- [ ] **Step 3: Implementare**

```python
# backend/app/api/wa_campaigns.py -- in coda
SOGLIA_ALLARME_OPTOUT_PCT = 5.0   # SDD 10.3, da confermare con dati veri (Q65)


@router.get("/{campaign_id}/kpi")
async def kpi(campaign_id: str, db=Depends(get_db)) -> dict:
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None:
        raise HTTPException(404, "campagna inesistente")

    inviati = campagna.sent or 0
    def _pct(n: int) -> float:
        return round(100.0 * n / inviati, 1) if inviati else 0.0

    return {
        "stato": campagna.status.value,
        "caricati": campagna.total_contacts or 0,
        "inviati": inviati,
        "da_inviare": max(0, (campagna.total_contacts or 0) - inviati),
        "risposti": campagna.replied or 0,
        "optout": campagna.opted_out or 0,
        "falliti": campagna.failed or 0,
        "tasso_risposta": _pct(campagna.replied or 0),
        "tasso_optout": _pct(campagna.opted_out or 0),
        "allarme_optout": _pct(campagna.opted_out or 0) > SOGLIA_ALLARME_OPTOUT_PCT,
        # Onesta' del dato: le risposte arrivate dopo la fine della campagna
        # possono sfuggire allo scan, quindi il tasso e' una stima per
        # DIFETTO (SDD 15.1). La UI lo dice, non lo nasconde.
        "nota": "Il tasso di risposta e' una stima per difetto: le risposte "
                "arrivate a campagna chiusa possono non essere conteggiate.",
    }
```

- [ ] **Step 4: Rilanciare i test** → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/wa_campaigns.py backend/tests/test_wa_api_kpi.py
git commit -m "feat(wa): KPI di campagna, derivati sugli inviati e soglia di allarme opt-out"
```

---

### Task 9: il mondo WhatsApp nel frontend — shell, tema, picker di canale, client REST

**Files:**
- Create: `frontend/lib/waApi.ts`
- Create: `frontend/app/wa/layout.tsx`, `frontend/app/wa/page.tsx`
- Create: `frontend/app/canale/page.tsx`
- Modify: `frontend/app/page.tsx` (redirect al picker) — **con test di non-regressione prima**

**Interfaces:**
- Consumes: gli endpoint dei Task 4-8.
- Produces: `waApi` (client tipizzato), il layout `/wa` che tutte le pagine successive usano.

**Mondi separati, non una vista mista** (SDD §6.3, review 23/07): stessa shell e stesso login, **picker di canale post-login**, poi un'interfaccia WhatsApp costruita da zero con **tema verde scuro** (proposta `#128C7E`) mentre Instagram resta sul suo. Il colore dice a colpo d'occhio *dove sono*. Nessuna pagina condivisa, nessun dato condiviso: `lib/api.ts` resta di Instagram e non si tocca.

**REQUIRED SUB-SKILL:** `frontend-design` prima di scrivere le pagine — il tema va deciso una volta e applicato, non improvvisato pagina per pagina.

- [ ] **Step 1: Non-regressione Instagram**

Prima di toccare `frontend/app/page.tsx`, verificare che le rotte Instagram esistenti rispondano ancora: `/campaigns`, `/accounts`, `/leads`, `/messages`, `/ops`, `/settings`. Il modo più economico in questo repo (che non ha test frontend) è un check manuale scritto nella lista funzionale della Fase 4, **più** il vincolo di non modificare `components/LayoutShell.tsx` né `app/layout.tsx` se non per aggiungere il ramo `/wa`.

- [ ] **Step 2: `waApi.ts` — client separato**

```typescript
// frontend/lib/waApi.ts
// Client REST del mondo WhatsApp. Separato da lib/api.ts di proposito: i due
// canali non condividono pagine ne' dati (SDD 6.3), e un client unico
// diventerebbe il primo punto in cui tornano a mescolarsi.
import { getAuthToken } from './api'

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000/api'

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(getAuthToken() ? { Authorization: `Bearer ${getAuthToken()}` } : {}),
      ...init?.headers,
    },
  })
  if (!res.ok) {
    // Il backend risponde 422 con un messaggio scritto per un umano (righe
    // scartate, placeholder mancanti): mostrarlo e' meglio di "errore 422".
    const detail = await res.json().catch(() => null)
    throw new Error(detail?.detail ?? `Errore ${res.status}`)
  }
  return res.json()
}

export type ScartoIngest = { riga: number; motivo: string; valore: string }
export type ReportIngest = {
  creati: number; aggiornati: number; gia_dnc: number
  duplicati_nel_file: number; scarti: ScartoIngest[]
}

export const waApi = {
  numeri: () => req<{ numeri: unknown[] }>('/wa/numbers'),
  riattivaNumero: (id: string, motivo: string) =>
    req(`/wa/numbers/${id}/riattiva`, { method: 'POST', body: JSON.stringify({ motivo }) }),
  campagne: () => req<{ campagne: unknown[] }>('/wa/campaigns'),
  campagna: (id: string) => req(`/wa/campaigns/${id}`),
  creaCampagna: (dati: unknown) =>
    req('/wa/campaigns', { method: 'POST', body: JSON.stringify(dati) }),
  kpi: (id: string) => req(`/wa/campaigns/${id}/kpi`),
  azione: (id: string, azione: 'start' | 'pause' | 'resume' | 'stop') =>
    req(`/wa/campaigns/${id}/${azione}`, { method: 'POST' }),
  contatti: (campaignId: string) => req(`/wa/contacts?campaign_id=${campaignId}`),
  rimuoviContatto: (id: string) => req(`/wa/contacts/${id}`, { method: 'DELETE' }),
  ingest: (campaignId: string, file: File) => {
    const fd = new FormData()
    fd.append('campaign_id', campaignId)
    fd.append('file', file)
    return req<ReportIngest>('/wa/contacts/ingest', { method: 'POST', body: fd })
  },
}
```

- [ ] **Step 3: Layout e tema**

`frontend/app/wa/layout.tsx` definisce il tema verde come variabili CSS locali al sottoalbero (`--wa-accent: #128C7E`), una nav propria (Campagne · Numeri · Nuova campagna) e nessun riferimento ai componenti di navigazione Instagram. Regola vincolante: **nessuna pagina sotto `/wa` importa da `lib/api.ts`** se non `getAuthToken`.

- [ ] **Step 4: Picker di canale**

`frontend/app/canale/page.tsx`: due card grandi, Instagram e WhatsApp, che portano a `/campaigns` e `/wa`. `frontend/app/page.tsx` redirige qui invece che alla home Instagram. **La preferenza si ricorda** in `localStorage` così chi usa un canale solo non paga un click in più, con un link "cambia canale" sempre visibile nella shell.

- [ ] **Step 5: Verifica lint e build**

```bash
cd frontend && npm run lint && npm run build
```
⚠️ **Un solo comando pesante alla volta a livello di macchina**: la build è già stata abbattuta da ram-guard a 2,7 GB. Controllare `D:\dev\tools\ram-guard\guard.ps1 stato` e coordinarsi col cantiere M3 prima di lanciarla.

- [ ] **Step 6: Commit**

```bash
git add frontend/lib/waApi.ts frontend/app/wa frontend/app/canale frontend/app/page.tsx
git commit -m "feat(wa-ui): mondo WhatsApp separato -- shell verde, picker canale, client REST dedicato"
```

---

### Task 10: pagina Numeri

**Files:**
- Create: `frontend/app/wa/numeri/page.tsx`

Cosa deve mostrare e fare, punto per punto:

- Tabella dei numeri: etichetta, **numero mascherato** (mai intero: l'API non lo espone nemmeno), stato con colore, cap giornaliero, `sent_today` di oggi, proxy sì/no, ultimo health-check.
- **Avviso esplicito quando manca il proxy**: è la minaccia T3 della SDD (numeri diversi che risultano correlati perché escono dallo stesso IP). Il backend logga un warning, ma un warning nei log non lo legge nessuno.
- Bottone **"Avvia login QR"** solo per `pending_qr`/`qr_required`, con avviso che apre un browser **visibile** sulla macchina che ospita il backend — quindi va premuto solo quando qualcuno è davanti a quello schermo.
- Bottone **"Verifica sessione"** su tutti gli stati tranne `retired`.
- Bottone **"Riattiva"** solo su `retired`/`suspended`, con **campo motivo obbligatorio** e testo che spiega cosa succede: il numero torna a `pending_qr`, i contatori si azzerano e il warmup riparte dal giorno 1 (contratto §2.2).
- Nessun campo modificabile fra `sent_today`, `sent_date`, `warmup_day`, `status`: sono di M3 in scrittura, e la UI non deve offrire una strada che l'API rifiuta.

- [ ] **Step 1: Implementare la pagina** riusando i componenti in `frontend/components/ui`.
- [ ] **Step 2: `npm run lint`** (build solo a fine batch frontend, per la RAM).
- [ ] **Step 3: Commit**

```bash
git add frontend/app/wa/numeri
git commit -m "feat(wa-ui): pagina numeri con riattivazione motivata e avviso proxy mancante"
```

---

### Task 11: creazione campagna e upload della lista — il report degli scarti è la pagina

**Files:**
- Create: `frontend/app/wa/campagne/nuova/page.tsx`

**È la schermata più importante di M2.** Un ingest che dice "caricati 240 contatti" e tace sui 60 scartati è un ingest che sembra funzionare: chi lo usa scopre il buco settimane dopo, guardando i KPI.

Flusso in tre passi, su una pagina sola:

1. **Campagna**: nome, tenant, numero (solo `active`), tipo (`marketing` | `followup`). Alla scelta del tipo, l'interruttore opt-out si **preseleziona da solo** e la CTA compare precompilata per marketing (`calcola_optout_enabled`, contratto §2.1). Resta modificabile: è togglabile per decisione, non per distrazione.
2. **Lista**: upload del CSV. Prima dell'upload, un pannello spiega il contratto del file — colonna `numero` obbligatoria, `nome` opzionale, ogni altra colonna diventa un segnaposto usabile nel testo — con un esempio scaricabile.
3. **Messaggio**: textarea con anteprima. I segnaposto disponibili sono **quelli veri della lista appena caricata**, mostrati come chip cliccabili. Il salvataggio chiama la validazione lato server e mostra i placeholder ignoti per nome (Task 6).

Il report di ingest, dopo l'upload:

```
✅  238 contatti caricati        (di cui 12 già presenti, aggiornati)
⏭️   9 esclusi: hanno detto STOP o sono in do-not-contact
🔁   4 duplicati nel file
⚠️  17 righe scartate                                    [mostra dettagli ▾]

     riga 34   +39•••••12   formato non riconoscibile
     riga 51   003•••••78   prefisso internazionale ambiguo
     ...                                        [scarica il report CSV]
```

Regole vincolanti della schermata:

- Le righe scartate si possono **scaricare come CSV** (numero riga + motivo + valore mascherato): è il file che l'admin rimanda al cliente per farsi correggere la lista.
- **Mai un numero intero a schermo.** L'API restituisce già la forma mascherata: la UI non deve ricostruirla.
- Un file rifiutato in blocco (422) mostra **il messaggio del backend**, che è scritto per un umano ("Colonna 'numero' obbligatoria e assente. Colonne trovate: telefono, nome"), non un "Errore 422".
- Il bottone "Avvia campagna" resta **disattivato** finché ci sono zero contatti caricati o zero messaggi, con accanto il motivo scritto: le stesse validazioni del Task 7, dette prima invece che dopo.

- [ ] **Step 1: Implementare la pagina**
- [ ] **Step 2: `npm run lint`**
- [ ] **Step 3: Commit**

```bash
git add frontend/app/wa/campagne/nuova
git commit -m "feat(wa-ui): creazione campagna con upload lista e report scarti scaricabile"
```

---

### Task 12: dettaglio campagna

**Files:**
- Create: `frontend/app/wa/campagne/[id]/page.tsx`

- Card KPI (caricati, inviati, da inviare, risposti, opt-out, falliti) più i due tassi, con **la nota di onestà del dato** visibile e non nascosta in un tooltip: il tasso di risposta è una stima per difetto.
- Badge di **allarme opt-out** oltre il 5%, con il suggerimento di mettere in pausa (l'azione resta di Tommaso, la UI non pausa da sola).
- Azioni: Avvia · Pausa · Riprendi · Stop, ognuna con conferma e con il messaggio d'errore del backend mostrato per intero quando la validazione rifiuta (es. "Questo numero ha già una campagna in corso").
- Tabella contatti paginata: numero mascherato, nome, stato, tentativi falliti, ultimo errore. Un contatto **in lavorazione** (`in_lavorazione: true`) mostra un'icona e **non è rimovibile** — il bottone c'è ma è disabilitato con la spiegazione, invece di far scoprire il 409 dopo il click.
- Nessuna azione che scriva su colonne di M3: niente "forza invio", niente "sblocca". Se serve sbloccare, è il cron di M3 a rilasciare i lock stale.

- [ ] **Step 1: Implementare la pagina**
- [ ] **Step 2: `npm run lint && npm run build`** (qui la build serve: è l'ultimo task frontend)
- [ ] **Step 3: Commit**

```bash
git add frontend/app/wa/campagne
git commit -m "feat(wa-ui): dettaglio campagna con KPI, azioni e contatti non rimovibili sotto lock"
```

---

### Task 13: migrazione 026 — solo se serve davvero

**Files:**
- Create (**condizionale**): `backend/alembic/versions/026_*.py`

Il numero **026** è riservato a M2 (contratto §6.1). Allo stato attuale **M2 non dovrebbe averne bisogno**: lo schema della 025 copre già ingest, contatti, campagne e sequenze, e questo piano non aggiunge colonne.

- [ ] **Step 1: Verificare di non averne aggiunte**

```bash
git diff main -- backend/app/models/ | head -40
```
Expected: **vuoto**. `app/models/wa.py` è congelato (Global Constraints): se il diff non è vuoto, o si toglie la colonna o si emenda il contratto (§9) prima di proseguire.

- [ ] **Step 2: Se il diff è vuoto, non fare niente**

Il numero 026 resta un buco. I buchi non fanno male, le collisioni sì — e M3 ne è già avvisato: la sua 027 nasce con `down_revision = "025"`.

- [ ] **Step 3: Se invece una colonna serve davvero**

Emendare il contratto §9 (data, cosa cambia, perché, chi l'ha chiesto), **avvisare il cantiere M3** perché deve ripuntare `027.down_revision` a `"026"`, scrivere la migrazione additiva, e provarla con il ciclo su-giù-su **prima su SQLite e poi su un Postgres vero** — la 025 non ha mai visto Postgres, e chi arriva primo è il primo a scoprirlo.

---

### Task 14: Fase 4 — chiusura modulo

**Files:**
- Create: `.superpowers/sdd/qa-m2-tests.md` (≥20 test funzionali UI)
- Create: `.superpowers/sdd/qa-m2-adversarial.md` (≥30 test adversarial)

Modelli da cui partire: `d:\dev\thevista-app-magazzino\.superpowers\sdd\qa-50-tests.md` e `qa-adversarial-tests.md`.

- [ ] **Step 1: Lista funzionale (≥20), eseguita dal QA agent via browser, una per una**

Copertura minima: login → picker canale → creazione tenant → creazione numero → login QR (schermata, non l'esecuzione) → creazione campagna marketing (opt-out preselezionato) → creazione campagna follow-up (opt-out spento) → upload CSV pulito → upload CSV sporco con report → download del report scarti → creazione step con segnaposto valido → tentativo con segnaposto ignoto → start rifiutato senza contatti → start riuscito → pausa → riprendi → stop → rimozione di un contatto → rimozione rifiutata su contatto sotto lock → KPI a zero → KPI con numeri → riattivazione di un numero `retired`.

- [ ] **Step 2: Lista adversarial (≥30), a criterio di PASS INVERTITO**

**PASS = il sistema si difende**: errore chiaro, nessuna scrittura sporca, invariante intatta. Un 500, un errore SQL grezzo a schermo, una scrittura parziale o un'invariante violata = **FAIL**, anche se "sembrava funzionare".

| Gruppo | Casi minimi |
|---|---|
| **CSV ostili** | 10 MB · 5.001 righe · solo header · header duplicato · BOM · UTF-16 · separatore misto · cella da 10.000 caratteri · null byte · formula `=cmd\|' /C calc'!A0` (CSV injection: deve restare testo) · newline dentro una cella quotata |
| **Numeri plausibilmente sbagliati** | `+39 342 146 0077 ext. 12` · `0039 342...` · `342.146.0077` · `+39-342-146-0077` · `39 342 146 0077` senza `+` · numero di 3 cifre · numero di 25 cifre · lo stesso numero in due formati diversi nello stesso file |
| **Concorrenza vera** | doppio upload dello **stesso** file in `Promise.all` (dedup deve reggere) · due start della stessa campagna in parallelo · start di due campagne sullo stesso numero in parallelo · rimozione di un contatto mentre M3 lo tiene sotto lock |
| **Scoping tenant** | campagna del tenant A che punta a un numero del tenant B · ingest con `campaign_id` di un altro tenant · lista contatti di una campagna altrui · KPI di una campagna altrui |
| **Macchina a stati** | ingest su campagna `running` · doppio start · stop di una campagna già `stopped` · modifica del template a campagna avviata · start con numero `qr_required` · riattivazione di un numero `active` |
| **Contratto con M3** | ogni riga creata dall'ingest ha `next_action_at` non NULL (I3) · nessuna riga creata da M2 ha `locked_by` valorizzato (I1) · un contatto `opted_out` non entra mai in una campagna, nemmeno da un file nuovo · `optout_enabled` a DB corrisponde al tipo campagna |
| **PII** | grep sui log dopo un ingest di 100 numeri veri: zero numeri interi · il report scarti non contiene numeri interi · la lista contatti API non contiene numeri interi · un `chat_title` numerico non viene mai mostrato |
| **Invarianti SQL a fine run** | `total_contacts` == conteggio reale delle righe · nessun `wa_contacts` orfano creato senza campagna (Q23) · nessun duplicato `(tenant_id, phone_hmac)` |

Il livello va **mescolato**: browser per ciò che la UI esprime, chiamata diretta all'API (script) per race, payload malformati e burst. Un adversarial fatto solo dalla UI non è adversarial.

- [ ] **Step 3: Fix loop fino al 100%** — "passano quasi tutti" = modulo non chiuso.

- [ ] **Step 4: Whole-branch review**

**REQUIRED SUB-SKILL:** `superpowers:requesting-code-review` su tutto il branch. In M1 è la fase che ha trovato il difetto peggiore, sfuggito a quattro batch di QA.

- [ ] **Step 5: Suite completa + build + PR**

```bash
cd backend && pytest tests -q
cd ../frontend && npm run lint && npm run build
```

- [ ] **Step 6: Commit finale delle liste**

```bash
git add .superpowers/sdd/qa-m2-tests.md .superpowers/sdd/qa-m2-adversarial.md
git commit -m "qa(wa-m2): liste funzionale e adversarial di fine modulo, fix loop chiuso al 100%"
```

---

## Stima

| Fase | Task | Sessioni di lavoro |
|---|---|---|
| **PR-0** (impalcatura condivisa, si mergia da sola) | 1 | ~0,5 |
| Ingest (parser, servizio, API) | 2-4 | ~1,5 |
| CRUD e ciclo di vita (tenant, numeri, campagne, start/stop, KPI) | 5-8 | ~1,5 |
| Frontend (shell, numeri, nuova campagna, dettaglio) | 9-12 | ~2 |
| Migrazione condizionale | 13 | ~0 |
| **Fase 4** (≥20 funzionali + ≥30 adversarial, fix loop, review) | 14 | **~1,5** |
| | | **~7 sessioni** |

## Come si implementa: agent-teams, un teammate per lato

M2 costruisce backend **e** frontend, e le due parti devono parlarsi mentre si scrivono — è il caso previsto dalla skill `sviluppo-modulo` (Fase 2). Non è un dettaglio organizzativo: il report di ingest, la lista dei segnaposto disponibili e i messaggi d'errore mostrati all'utente sono **la stessa decisione presa da due lati**, e due sessioni separate la prendono in due modi diversi.

- **Lead (chi esegue il piano):** possiede il contratto e i confini; risolve le contraddizioni fra i due teammate; fa i commit di integrazione.
- **Teammate backend:** Task 1-8, 13.
- **Teammate frontend:** Task 9-12, contro gli endpoint **già definiti** in questo piano (non aspetta che il backend li implementi: le firme sono qui).
- Entrambi: Task 14, insieme, perché la Fase 4 attraversa i due lati.

`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` è già attiva nei settings dal 29/07. Modello dei teammate: **Sonnet 5**.

## Cosa questo piano lascia aperto, e a chi

- **Q3, Q5, Q7, Q10, Q11, Q17, Q22, Q26, Q27, Q32, Q35** della SDD restano **[T]**: sono decisioni di prodotto di Tommaso e non bloccano M2 (le proposte della SDD reggono da sole per l'MVP).
- **Il flusso QR remoto** (SDD Q57, §7.6) qui è ridotto all'osso: si preme "avvia login" e il QR compare sul browser **della macchina che ospita il backend**. Va bene finché il numero è di Tommaso; per un cliente vero serve la pagina admin che mostra il QR con auto-refresh, ed è lavoro di M5 insieme al runbook di onboarding.
- **La retention di `attributes` e del report scarti** (Q92) aspetta la valutazione legale: oggi nulla si cancella da solo.
- **Nessun test automatico del frontend esiste in questo repo.** La verifica del mondo WA è quindi tutta nella Fase 4, eseguita da un QA agent via browser. Se in futuro si vorrà una rete più fitta, è backlog — non lo si improvvisa dentro questo cantiere.
