# M0 — PoC gate canale WhatsApp: piano esecutivo

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (consigliata) o superpowers:executing-plans per eseguire questo piano task-per-task. Gli step usano checkbox (`- [ ]`).

**Goal:** provare o smontare la fattibilità della strada A (browser Patchright su WhatsApp Web) con misure vere sul numero secondario di Primero, prima di scrivere una riga di codice di produzione.

**Architecture:** script Python usa-e-getta sotto `backend/scripts/poc_wa/`, isolati dall'app (non importano `app.*` salvo `patchright`), che pilotano un profilo Chromium persistente collegato come linked device. Nessuna migrazione, nessun modello, nessun endpoint. L'output del modulo non è software: è **un report con numeri** (`docs/whatsapp/poc-report.md`) + **un catalogo di selettori** (`docs/whatsapp/wa-dom-catalog.md`) che diventano l'input del POM in M1.

**Tech Stack:** Python 3.13, Patchright 1.58.2 (già in `requirements.txt`), psutil (da aggiungere), pytest 9 (solo per gli helper puri).

## Deviazioni dichiarate dallo standard `sviluppo-modulo`

Lo standard è scritto per moduli di prodotto. M0 non lo è. Cosa vale e cosa no:

| Regola standard | In M0 | Perché |
|---|---|---|
| Worktree isolato + branch + PR | **SÌ** | `feat/whatsapp-m0-poc`. Gli script si committano: sono la prova del report |
| Subagent implementer + reviewer per task | **SÌ** | invariato |
| TDD | **SOLO sugli helper puri** (`wa_lib.py`) | il resto è esplorazione di un DOM di terze parti che cambia sotto le mani: un test su un selettore ignoto è teatro |
| QA agent con E2E browser | **NO, sostituito dalla run reale** | l'E2E qui *è* il PoC. Il "QA" è Tommaso che guarda il telefono e conferma cosa è successo davvero |
| 20 test manuali UI + 30 adversarial | **NO** | non c'è UI in M0. Il protocollo di fine modulo si applica per intero a **M5** (DoD §15.2 dell'SDD) |
| Collaudo Tommaso solo a MVP | **ECCEZIONE: serve in M0** | i PoC-1/4 richiedono materialmente le sue mani sul telefono. Non è collaudo, è esecuzione |

## Global Constraints

Copiati verbatim dall'SDD v1.2 (`docs/whatsapp/SDD-whatsapp-channel.md`). Ogni task li eredita.

- **Numero di test = numero secondario di Primero, WhatsApp Business.** Non è un numero sacrificabile: è di un cliente.
- **Solo messaggi reali, mai messaggi di test** (Q60). Conseguenza operativa dura: **PoC-2 invia SOLO a chat controllate** — numeri di Tommaso/conoscenti già in rubrica sul secondario. Mai a un contatto Primero. Questo vincolo è implementato come **allowlist bloccante nel codice**, non come promemoria.
- **Nessun invio a contatti Primero in tutto M0.** Gli inbound reali dei clienti si usano **in sola lettura** (PoC-3).
- **Il reply-watcher non apre mai le chat** (aprire = marcare letto). Tutto PoC-3 legge dalla sola lista.
- **PoC-5 (volume) è fuori da M0** → rampa in M5. Nessuno script di stress in questo piano.
- **Solo testo, solo italiano.** Niente media, niente lingue extra.
- Numeri in chiaro **solo** dove servono per agire (deep-link, allowlist in env). Nei dump/artefatti su disco: **mascherati** (P12).
- Il profilo Chromium vive **fuori dal worktree** (path assoluto): il worktree può sparire dopo la PR, la sessione WhatsApp no.
- Gate duro: **PoC-1/2/3 falliti ⇒ strada A rimessa in discussione** prima di M1. Il piano finisce con un verdetto scritto, non con un merge.

---

## File structure

```
backend/scripts/poc_wa/                 ← tutto qui, usa-e-getta, non importato da app/
├── __init__.py
├── wa_lib.py            funzioni PURE (E.164, STOP-regex, masking, allowlist) — TDD
├── _common.py           lancio contesto persistente, artefatti, human_type, locator resilienti
├── poc1_login.py        PoC-1a: QR + login + marker sessione
├── poc1_heartbeat.py    PoC-1b: health-check sessione + campionamento RAM/CPU → CSV
├── poc3_dump_dom.py     PoC-3a: discovery struttura DOM lista chat (sola lettura)
├── poc3_scan.py         PoC-3b: scan strutturato lista chat + rilevamento inbound
├── poc2_open.py         PoC-2a: apertura chat per numero, 2 strategie, ZERO invii
├── poc2_send.py         PoC-2b: guardia pre-invio (misurata) + invio + spunte
└── poc4_coexist.py      PoC-4: invio pilotato mentre Tommaso usa il telefono

backend/tests/test_poc_wa_lib.py        test degli helper puri (gli unici testabili)
docs/whatsapp/wa-dom-catalog.md         catalogo selettori/segnali (output di PoC-3a)
docs/whatsapp/poc-report.md             report finale + verdetto GO/NO-GO
```

Artefatti runtime (**fuori dal repo**, mai committati):
```
D:\wa-poc\profile\        profilo Chromium persistente (= la sessione WhatsApp)
D:\wa-poc\artifacts\      screenshot, dump JSON, CSV heartbeat, log
```

---

## Task 0: Pre-flight (Tommaso, fisico — blocca tutto il resto)

Nessun codice. Se una di queste voci manca, i task successivi non sono eseguibili.

**Files:** nessuno.

- [ ] **Step 1: Verificare il numero**

Il numero secondario di Primero: SIM attiva, telefono acceso, **WhatsApp Business** installato e funzionante su quel telefono. Annotare il numero in E.164 (`+39…`).

- [ ] **Step 2: Contare le chat e censire le chat controllate**

Dal telefono: quante chat totali (serve per calibrare PoC-3, atteso 30-100) e **quali chat sono controllate** — cioè numeri di Tommaso o di conoscenti con cui è lecito scambiare messaggi di prova. **Servono ≥ 6 chat controllate distinte**, possibilmente in posizioni diverse della lista (una recente in cima, una vecchia raggiungibile solo con la ricerca).

Se sono meno di 6: aprire le chat mancanti **dal telefono, a mano**, scrivendo un messaggio vero a un conoscente. Il bot non deve mai creare una chat nuova (V2).

- [ ] **Step 3: Slot linked device**

Sul telefono: *Dispositivi collegati* → contare quanti slot sono occupati e liberarne uno se serve. Annotare il numero massimo di slot visto (risponde a Q54).

- [ ] **Step 4: Macchina**

PC di Tommaso. Per la durata di PoC-1 (14 giorni) **non va spento la notte** — o, se lo si spegne, i riavvii vanno annotati (fanno parte del criterio: ≥ 2 riavvii PC sopportati). Disattivare la sospensione automatica.

- [ ] **Step 5: Decisione proxy (registrarla, non subirla)**

Default M0: **nessun proxy**, si esce dall'IP residenziale di Tommaso. Motivo: WhatsApp Web da IP residenziale è il caso d'uso normale, mentre un proxy mal configurato è esso stesso un'anomalia; e V9 (1 proxy ↔ 2 numeri) serve alla correlazione multi-tenant, che in M0 non esiste. **Conseguenza dichiarata:** M0 non valida il layer proxy — resta aperto per M1/M3 (Q98). Se Tommaso preferisce testarlo subito, si passa `POC_WA_PROXY` e si annota nel report.

- [ ] **Step 6: Scrivere i messaggi**

Q4: i testi li scrive Tommaso. Servono **3 messaggi brevi, veri e sensati** da mandare alle chat controllate (PoC-2/4). Non "test 1 2 3": messaggi che una persona manderebbe davvero. Salvarli in `D:\wa-poc\messages.txt`, una riga per messaggio.

---

## Task 1: Helper puri + test (l'unico pezzo TDD-abile)

**Files:**
- Create: `backend/scripts/poc_wa/__init__.py`
- Create: `backend/scripts/poc_wa/wa_lib.py`
- Test: `backend/tests/test_poc_wa_lib.py`

**Interfaces:**
- Produces: `normalize_e164(raw: str, default_cc: str = "39") -> str | None` (cifre pure, senza `+`, formato deep-link) · `contains_stop(text: str) -> bool` · `mask_pii(text: str, keep: int = 40) -> str` · `AllowList.load() -> AllowList` con `.is_allowed(e164: str) -> bool` e `.assert_allowed(e164: str) -> None` (solleva `NotAllowed`)
- Consumes: nulla.

- [ ] **Step 1: Scrivere i test che falliscono**

```python
# backend/tests/test_poc_wa_lib.py
"""Test degli helper puri dei PoC WhatsApp (M0).

Sono gli unici pezzi di M0 testabili in isolamento: tutto il resto tocca un DOM
di terze parti. `AllowList` in particolare NON e' un dettaglio: e' la guardia che
impedisce di mandare messaggi ai contatti veri di Primero (vincolo Q60).
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from poc_wa import wa_lib  # noqa: E402


@pytest.mark.parametrize("raw,expected", [
    ("+39 342 146 0077", "393421460077"),
    ("3421460077", "393421460077"),          # manca il prefisso -> default IT
    ("0039 342 1460077", "393421460077"),
    ("+1 415 555 0123", "14155550123"),      # estero: prefisso rispettato
    ("342-146.0077", "393421460077"),
    ("", None),
    ("abc", None),
    ("+39 12", None),                        # troppo corto per essere un numero
])
def test_normalize_e164(raw, expected):
    assert wa_lib.normalize_e164(raw) == expected


@pytest.mark.parametrize("text,expected", [
    ("STOP", True),
    ("stop", True),
    ("Stop.", True),
    ("basta grazie", True),
    ("CANCELLAMI", True),
    ("non scrivermi piu", True),
    ("non voglio piu ricevere messaggi", True),
    ("stopper", False),                      # parola intera, non substring
    ("mi fermo io", False),
    ("ok grazie", False),
    ("", False),
])
def test_contains_stop(text, expected):
    assert wa_lib.contains_stop(text) is expected


def test_mask_pii_nasconde_numeri_e_tronca():
    out = wa_lib.mask_pii("chiamami al 3421460077 domani", keep=100)
    assert "3421460077" not in out
    assert "<num>" in out
    assert wa_lib.mask_pii("x" * 500) .endswith("...")
    assert len(wa_lib.mask_pii("x" * 500, keep=40)) <= 43


def test_allowlist_blocca_i_non_autorizzati(monkeypatch):
    monkeypatch.setenv("POC_WA_ALLOWED_NUMBERS", "+39 342 146 0077, 3331112222")
    al = wa_lib.AllowList.load()
    assert al.is_allowed("393421460077") is True
    assert al.is_allowed("393331112222") is True
    assert al.is_allowed("395559998888") is False
    with pytest.raises(wa_lib.NotAllowed):
        al.assert_allowed("395559998888")


def test_allowlist_vuota_blocca_tutto(monkeypatch):
    """Fail-closed: allowlist non configurata => nessun invio possibile."""
    monkeypatch.delenv("POC_WA_ALLOWED_NUMBERS", raising=False)
    al = wa_lib.AllowList.load()
    assert al.is_allowed("393421460077") is False
```

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

```bash
cd backend && python -m pytest tests/test_poc_wa_lib.py -v
```
Atteso: FAIL — `ModuleNotFoundError: No module named 'poc_wa'`.

- [ ] **Step 3: Implementare `wa_lib.py`**

```python
# backend/scripts/poc_wa/wa_lib.py
"""Funzioni pure dei PoC WhatsApp (M0). Nessun import di app.*, nessun I/O di rete.

Sono qui perche' sono le uniche parti verificabili senza un browser: le altre
dipendono dal DOM di WhatsApp Web, che e' esattamente cio' che il PoC deve scoprire.
"""
import os
import re

DEFAULT_CC = "39"
MIN_DIGITS = 8   # sotto questa soglia non e' un numero mobile plausibile
MAX_DIGITS = 15  # E.164

# Parole/frasi di opt-out. MVP italiano (Q6). Il falso positivo e' accettato:
# meglio un opt-out di troppo che uno mancato (SDD 7.5 punto 6).
_STOP_PATTERNS = [
    r"\bstop\b",
    r"\bbasta\b",
    r"\bcancellami\b",
    r"\bdisiscrivimi\b",
    r"\bnon\s+scrivermi(\s+piu)?\b",
    r"\bnon\s+voglio\s+piu\s+ricevere\b",
    r"\brimuovimi\b",
]
_STOP_RE = re.compile("|".join(_STOP_PATTERNS), re.IGNORECASE)

# 6+ cifre consecutive = quasi certamente un numero di telefono in un dump.
_NUM_RE = re.compile(r"\d{6,}")


class NotAllowed(Exception):
    """Tentato invio verso un numero non in allowlist. In M0 e' un errore fatale."""


def normalize_e164(raw: str, default_cc: str = DEFAULT_CC) -> str | None:
    """'+39 342 146 0077' -> '393421460077' (cifre pure, formato deep-link WhatsApp).

    Restituisce None se l'input non e' un numero plausibile: chi chiama deve
    trattare il None come scarto, mai come 'numero vuoto'.
    """
    if not raw:
        return None
    s = raw.strip()
    has_plus = s.startswith("+") or s.startswith("00")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    elif not has_plus and not digits.startswith(default_cc):
        digits = default_cc + digits
    if not (MIN_DIGITS <= len(digits) <= MAX_DIGITS):
        return None
    return digits


def contains_stop(text: str) -> bool:
    """True se il testo contiene una richiesta di opt-out (parole intere)."""
    if not text:
        return False
    return bool(_STOP_RE.search(text))


def mask_pii(text: str, keep: int = 40) -> str:
    """Maschera i numeri e tronca: i dump di M0 girano su chat di clienti veri."""
    if text is None:
        return ""
    masked = _NUM_RE.sub("<num>", text)
    if len(masked) > keep:
        return masked[:keep] + "..."
    return masked


class AllowList:
    """Guardia bloccante degli invii M0 (vincolo Q60).

    Fail-closed per costruzione: se la variabile non e' configurata l'insieme e'
    vuoto e NESSUN numero passa. E' l'unica cosa che sta tra questo PoC e un
    messaggio di prova mandato a un cliente vero di Primero.
    """

    def __init__(self, numbers: set[str]):
        self._numbers = numbers

    @classmethod
    def load(cls, env_var: str = "POC_WA_ALLOWED_NUMBERS") -> "AllowList":
        raw = os.environ.get(env_var, "")
        nums = set()
        for chunk in raw.split(","):
            n = normalize_e164(chunk)
            if n:
                nums.add(n)
        return cls(nums)

    def is_allowed(self, e164: str) -> bool:
        return bool(e164) and e164 in self._numbers

    def assert_allowed(self, e164: str) -> None:
        if not self.is_allowed(e164):
            raise NotAllowed(
                f"Numero non in allowlist (ultime 4 cifre: …{str(e164)[-4:]}). "
                f"In M0 si scrive SOLO a chat controllate: aggiungilo a "
                f"POC_WA_ALLOWED_NUMBERS oppure fermati."
            )

    def __len__(self) -> int:
        return len(self._numbers)
```

E il package marker:

```python
# backend/scripts/poc_wa/__init__.py
"""Script PoC del canale WhatsApp (M0). Usa-e-getta: non importare da app/."""
```

- [ ] **Step 4: Eseguire i test e verificare che passino**

```bash
cd backend && python -m pytest tests/test_poc_wa_lib.py -v
```
Atteso: PASS (tutti). Poi la non-regressione della suite esistente:
```bash
cd backend && python -m pytest tests/ -q
```
Atteso: nessun test rotto rispetto a prima (M0 non tocca `app/`).

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/poc_wa/__init__.py backend/scripts/poc_wa/wa_lib.py backend/tests/test_poc_wa_lib.py
git commit -m "poc(wa): helper puri M0 (E164, STOP-regex, masking PII, allowlist fail-closed)"
```

---

## Task 2: Contesto browser persistente + artefatti

**Files:**
- Create: `backend/scripts/poc_wa/_common.py`
- Modify: `backend/requirements.txt` (aggiunta `psutil`)

**Interfaces:**
- Consumes: `wa_lib.mask_pii`
- Produces: `wa_context(headless: bool = False)` (async context manager → `(context, page)`) · `artifacts_dir() -> Path` · `snap(page, name) -> Path` · `log_event(kind: str, **fields) -> None` (append JSONL) · `human_type(page, element, text) -> None` · `first_locator(page, candidates: list[str], timeout_ms: int) -> tuple[Locator, str] | None`

- [ ] **Step 1: Aggiungere psutil**

```bash
cd backend && pip install psutil && pip show psutil | findstr /R "^Version"
```
Aggiungere a `backend/requirements.txt` la riga `psutil==<versione mostrata>` (in ordine alfabetico tra le dipendenze esistenti). Serve al campionamento RAM/CPU di PoC-1 (verifica A5).

- [ ] **Step 2: Scrivere `_common.py`**

```python
# backend/scripts/poc_wa/_common.py
"""Infrastruttura condivisa degli script PoC WhatsApp (M0).

Scelte deliberate, diverse da app/browser/context_manager.py:
- profilo su path ASSOLUTO fuori dal repo: la sessione WhatsApp deve
  sopravvivere alla cancellazione del worktree;
- nessuna dipendenza dal DB (niente account_id, niente proxy da tabella);
- NESSUNA iniezione di fingerprint: su WhatsApp Web un profilo Chromium
  vergine e persistente e' il caso normale, e in M0 vogliamo misurare la
  piattaforma, non il nostro layer anti-detect. La reintroduzione del
  fingerprint e' una decisione di M1 (rischio: puo' alterare la sessione).
"""
import asyncio
import json
import math
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from wa_lib import mask_pii  # type: ignore  # eseguito come script dalla sua cartella

WA_URL = "https://web.whatsapp.com/"
PROFILE_DIR = Path(os.environ.get("POC_WA_PROFILE_DIR", r"D:\wa-poc\profile"))
ARTIFACTS_DIR = Path(os.environ.get("POC_WA_ARTIFACTS", r"D:\wa-poc\artifacts"))
PROXY_URL = os.environ.get("POC_WA_PROXY") or None


def artifacts_dir() -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(kind: str, **fields) -> None:
    """Append di una riga JSONL in artifacts/events.jsonl + echo a schermo.

    I campi di testo passano da mask_pii: questi artefatti riguardano chat di
    clienti veri e restano su disco per settimane.
    """
    safe = {k: (mask_pii(v, keep=120) if isinstance(v, str) else v) for k, v in fields.items()}
    rec = {"ts": _now(), "kind": kind, **safe}
    line = json.dumps(rec, ensure_ascii=False)
    (artifacts_dir() / "events.jsonl").open("a", encoding="utf-8").write(line + "\n")
    print(line)


def _parse_proxy(url: str) -> dict | None:
    p = urlparse(url.strip())
    if not p.hostname or not p.port:
        raise ValueError(f"POC_WA_PROXY malformato: {url!r}")
    out = {"server": f"{p.scheme or 'http'}://{p.hostname}:{p.port}"}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


@asynccontextmanager
async def wa_context(headless: bool = False):
    """Apre il profilo persistente su web.whatsapp.com e restituisce (context, page).

    headless=False di default: in M0 vogliamo VEDERE cosa succede, e il QR va
    inquadrato col telefono.
    """
    from patchright.async_api import async_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # Lock lasciati da una sessione uccisa male: senza rimuoverli Chromium
    # inoltra il lancio a un PID fantasma ed esce subito (lezione IG).
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = PROFILE_DIR / lock
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    kwargs = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        locale="it-IT",
        timezone_id="Europe/Rome",
        args=args,
        ignore_default_args=["--enable-automation"],
    )
    if PROXY_URL:
        kwargs["proxy"] = _parse_proxy(PROXY_URL)
    else:
        args.append("--no-proxy-server")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(**kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(WA_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            yield context, page
        finally:
            await context.close()


async def snap(page, name: str) -> Path:
    """Screenshot diagnostico. ATTENZIONE: contiene PII (schermate di chat vere).
    Restano in artifacts/, mai nel repo (Q48 li tratta come materiale sensibile)."""
    path = artifacts_dir() / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{name}.png"
    await page.screenshot(path=str(path), full_page=False)
    return path


async def first_locator(page, candidates: list[str], timeout_ms: int = 4000):
    """Prova N selettori in ordine, restituisce (locator, selettore_che_ha_funzionato).

    Il DOM di WhatsApp Web e' offuscato e cambia: nessuno script di M0 deve
    dipendere da UN selettore. Quale ha funzionato finisce nel catalogo.
    """
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc, sel
        except Exception:
            continue
    return None


def _typo_char(char: str) -> str | None:
    """Vicino di tastiera QWERTY, per il typo simulato."""
    neighbors = {
        "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr", "f": "dg",
        "g": "fh", "h": "gj", "i": "uo", "l": "kp", "m": "n", "n": "bm",
        "o": "ip", "p": "ol", "q": "wa", "r": "et", "s": "ad", "t": "ry",
        "u": "yi", "v": "cb", "w": "qe", "y": "tu", "z": "x",
    }
    opts = neighbors.get(char.lower())
    return random.choice(opts) if opts else None


async def human_type(page, element, text: str) -> None:
    """Digitazione umanizzata: copia adattata di InstagramPage._human_type
    (backend/app/browser/instagram_page.py:633).

    Copiata e non importata di proposito: InstagramPage e' accoppiato al flusso IG.
    L'estrazione del modulo condiviso `human_input` e' un task di M1 (SDD sez. 6):
    questa copia e' il banco di prova di cosa deve contenere.

    Differenza rispetto a IG: su WhatsApp Web Enter invia il messaggio, quindi gli
    a-capo si battono con Shift+Enter — stesso comportamento, va confermato in PoC-2.
    """
    await element.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    base_ms = random.uniform(40, 95)

    for line_idx, line in enumerate(text.split("\n")):
        if line_idx > 0:
            await page.keyboard.press("Shift+Enter")
            await asyncio.sleep(random.uniform(0.15, 0.5))
        words = line.split(" ")
        for i, word in enumerate(words):
            if i > 0 and random.random() < 0.07:
                await asyncio.sleep(random.uniform(0.25, 1.0))
            for char_idx, char in enumerate(word):
                if len(word) > 3 and 0 < char_idx < len(word) - 1 and random.random() < 0.08:
                    wrong = _typo_char(char)
                    if wrong:
                        await page.keyboard.type(wrong)
                        await asyncio.sleep(max(30, min(480, random.lognormvariate(math.log(base_ms), 0.45))) / 1000)
                        await asyncio.sleep(random.uniform(0.12, 0.40))
                        await page.keyboard.press("Backspace")
                        await asyncio.sleep(random.uniform(0.06, 0.20))
                delay_ms = max(30, min(480, random.lognormvariate(math.log(base_ms), 0.45)))
                await page.keyboard.type(char)
                await asyncio.sleep(delay_ms / 1000)
                if random.random() < 0.015:
                    await asyncio.sleep(random.uniform(0.2, 0.7))
            if i < len(words) - 1:
                await page.keyboard.type(" ")
                await asyncio.sleep(random.uniform(25, 80) / 1000)
```

- [ ] **Step 3: Verifica di import (non c'è altro da testare senza browser)**

```bash
cd backend/scripts/poc_wa && python -c "import _common; print(_common.PROFILE_DIR, _common.ARTIFACTS_DIR)"
```
Atteso: stampa i due path assoluti, nessuna eccezione.

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/poc_wa/_common.py backend/requirements.txt
git commit -m "poc(wa): contesto Chromium persistente fuori-repo, artefatti mascherati, typing umano"
```

---

## Task 3: PoC-1a — login QR e marker di sessione

**Files:**
- Create: `backend/scripts/poc_wa/poc1_login.py`

**Interfaces:**
- Consumes: `_common.wa_context`, `_common.snap`, `_common.log_event`, `_common.first_locator`
- Produces: evento `session_established` in `events.jsonl`; file `artifacts/session_start.txt` con l'istante del login (base per contare i giorni di PoC-1)

- [ ] **Step 1: Scrivere lo script**

```python
# backend/scripts/poc_wa/poc1_login.py
"""PoC-1a — login iniziale via QR sul numero secondario Primero.

Da eseguire UNA VOLTA, con il telefono in mano. Da qui parte il cronometro dei
14 giorni: ogni re-scan richiesto dopo questo momento e' un dato di PoC-1.

Uso:  python poc1_login.py
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from _common import artifacts_dir, first_locator, log_event, snap, wa_context

# Candidati per "sono loggato": la lista chat e' visibile.
CHATLIST_CANDIDATES = [
    "#pane-side",
    "[data-testid='chat-list']",
    "div[aria-label*='Elenco chat']",
    "div[aria-label*='Chat list']",
    "[role='grid']",
]
# Candidati per "serve il QR".
QR_CANDIDATES = [
    "canvas[aria-label*='Scan']",
    "[data-testid='qrcode']",
    "canvas",
]


async def main() -> None:
    async with wa_context(headless=False) as (context, page):
        found = await first_locator(page, CHATLIST_CANDIDATES, timeout_ms=8000)
        if found:
            _, sel = found
            log_event("already_logged_in", selector=sel)
            print("Sessione gia' attiva: nessun QR necessario.")
        else:
            qr = await first_locator(page, QR_CANDIDATES, timeout_ms=15000)
            if not qr:
                await snap(page, "poc1-schermata-ignota")
                log_event("login_unknown_screen")
                raise SystemExit(
                    "Ne' lista chat ne' QR: schermata non prevista. "
                    "Guarda lo screenshot in artifacts/ e catalogala."
                )
            _, qr_sel = qr
            log_event("qr_shown", selector=qr_sel)
            print("Inquadra il QR col telefono (Dispositivi collegati). Attendo fino a 3 minuti…")
            got = await first_locator(page, CHATLIST_CANDIDATES, timeout_ms=180000)
            if not got:
                await snap(page, "poc1-login-fallito")
                raise SystemExit("Login non completato entro 3 minuti.")
            _, sel = got
            log_event("login_ok", selector=sel)

        # Marker: da qui si contano i giorni di sessione viva.
        marker = artifacts_dir() / "session_start.txt"
        if not marker.exists():
            marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        await snap(page, "poc1-logged-in")
        log_event("session_established", marker=str(marker))
        print("OK. Non chiudere il profilo a mano: da ora gira poc1_heartbeat.py ogni giorno.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Eseguirlo con Tommaso presente (telefono in mano)**

```bash
cd backend/scripts/poc_wa && python poc1_login.py
```
Atteso: si apre Chromium, compare il QR, dopo la scansione appare la lista chat, viene scritto `session_start.txt`.
Se compare una schermata diversa (aggiornamento forzato, interstitial): **è un dato**, va nello screenshot e nel catalogo (Q43).

- [ ] **Step 3: Verificare dal telefono**

Sul telefono: *Dispositivi collegati* → deve comparire un nuovo dispositivo Chrome/Windows. Annotare come viene mostrato e quanti slot restano (Q54).

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/poc_wa/poc1_login.py
git commit -m "poc(wa): PoC-1a login QR + marker inizio sessione"
```

---

## Task 4: PoC-1b — heartbeat sessione + RAM/CPU (gira 14 giorni)

**Files:**
- Create: `backend/scripts/poc_wa/poc1_heartbeat.py`

**Interfaces:**
- Consumes: `_common.wa_context`, `_common.first_locator`, `_common.log_event`
- Produces: `artifacts/heartbeat.csv` con colonne `ts,giorni_da_login,sessione_viva,selettore,rss_mb,cpu_pct,note`

- [ ] **Step 1: Scrivere lo script**

```python
# backend/scripts/poc_wa/poc1_heartbeat.py
"""PoC-1b — la sessione e' ancora viva? e quanto costa tenerla aperta?

Da lanciare almeno 1 volta al giorno per 14 giorni (Task 4, step 2). Ogni run:
apre il profilo, guarda se c'e' la lista chat o il QR, campiona RAM/CPU dei
processi Chromium di QUESTO profilo, scrive una riga nel CSV, chiude.

Il criterio GO di PoC-1: 14 giorni senza re-scan, >= 5 riavvii browser e >= 2
riavvii PC sopportati. Ogni riga di questo CSV e' un riavvio browser.

Uso:  python poc1_heartbeat.py [--nota "riavviato il PC"]
"""
import argparse
import asyncio
import csv
from datetime import datetime, timezone
from pathlib import Path

import psutil

from _common import PROFILE_DIR, artifacts_dir, first_locator, log_event, snap, wa_context
from poc1_login import CHATLIST_CANDIDATES, QR_CANDIDATES

CSV_PATH = None  # impostato in main()


def _sample_profile_processes() -> tuple[float, float]:
    """RSS totale (MB) e CPU% dei processi Chromium legati a QUESTO profilo.

    Il filtro sulla cmdline evita di contare il Chrome personale di Tommaso.
    """
    needle = str(PROFILE_DIR).lower()
    rss = 0
    cpu = 0.0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
            if needle in cmd:
                rss += proc.memory_info().rss
                cpu += proc.cpu_percent(interval=0.1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return round(rss / (1024 * 1024), 1), round(cpu, 1)


async def main(nota: str) -> None:
    global CSV_PATH
    CSV_PATH = artifacts_dir() / "heartbeat.csv"
    marker = artifacts_dir() / "session_start.txt"
    start = datetime.fromisoformat(marker.read_text(encoding="utf-8")) if marker.exists() else None

    async with wa_context(headless=False) as (context, page):
        alive = await first_locator(page, CHATLIST_CANDIDATES, timeout_ms=20000)
        if alive:
            viva, sel = True, alive[1]
        else:
            qr = await first_locator(page, QR_CANDIDATES, timeout_ms=5000)
            viva, sel = False, (qr[1] if qr else "schermata-ignota")
            await snap(page, "poc1-sessione-persa")
        rss_mb, cpu_pct = _sample_profile_processes()
        giorni = (datetime.now(timezone.utc) - start).days if start else -1

        new_file = not CSV_PATH.exists()
        with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["ts", "giorni_da_login", "sessione_viva", "selettore", "rss_mb", "cpu_pct", "note"])
            w.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                giorni, int(viva), sel, rss_mb, cpu_pct, nota,
            ])
        log_event("heartbeat", giorni=giorni, viva=viva, rss_mb=rss_mb, cpu_pct=cpu_pct, nota=nota)
        if not viva:
            print("!! SESSIONE PERSA — annota cosa e' successo prima (aggiornamenti, riavvii, telefono offline).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--nota", default="")
    args = ap.parse_args()
    asyncio.run(main(args.nota))
```

- [ ] **Step 2: Protocollo di esecuzione (14 giorni, in parallelo ai task 5-9)**

- 1 run al giorno minimo: `python poc1_heartbeat.py`
- dopo ogni riavvio del PC: `python poc1_heartbeat.py --nota "dopo riavvio PC"`
- almeno **2 riavvii PC** e **5 riavvii browser** nella finestra (ogni run è un riavvio browser)
- se in un giorno il telefono resta spento/offline a lungo, annotarlo (`--nota "telefono offline 6h"`) → risponde a Q55
- **PoC-1 non blocca i task 5-9**: quelli girano dentro questa finestra e sono anche l'"uso quotidiano" che il criterio richiede

- [ ] **Step 3: Verifica del primo campionamento**

```bash
cd backend/scripts/poc_wa && python poc1_heartbeat.py --nota "primo run"
type D:\wa-poc\artifacts\heartbeat.csv
```
Atteso: una riga con `sessione_viva=1` e `rss_mb` > 0. Se `rss_mb` è 0 il filtro sulla cmdline non ha agganciato i processi → correggere prima di andare avanti, altrimenti A5 resta non misurata.

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/poc_wa/poc1_heartbeat.py
git commit -m "poc(wa): PoC-1b heartbeat sessione + campionamento RAM/CPU per profilo"
```

---

## Task 5: PoC-3a — discovery del DOM della lista chat (sola lettura)

**Files:**
- Create: `backend/scripts/poc_wa/poc3_dump_dom.py`
- Create: `docs/whatsapp/wa-dom-catalog.md`

**Interfaces:**
- Consumes: `_common.wa_context`, `_common.log_event`, `wa_lib.mask_pii`
- Produces: `artifacts/dom_dump_<ts>.json` (struttura candidata delle righe) + il catalogo markdown compilato a mano dal dump

**Vincolo assoluto di questo task: NON si apre nessuna chat.** Aprire = marcare letto = interferire con il lavoro di Primero.

- [ ] **Step 1: Scrivere lo script di discovery**

```python
# backend/scripts/poc_wa/poc3_dump_dom.py
"""PoC-3a — che informazione espone la lista chat, senza aprire nulla?

Non assume selettori: cammina il pannello laterale e raccoglie, per ogni riga
candidata, gli attributi utili (role, aria-label, data-*, testi). Il risultato
serve a compilare docs/whatsapp/wa-dom-catalog.md, che sara' la base del POM in M1.

I testi vengono mascherati e troncati: qui dentro ci sono conversazioni vere di
clienti di Primero.

Uso:  python poc3_dump_dom.py
"""
import asyncio
import json
from datetime import datetime

from _common import artifacts_dir, log_event, snap, wa_context
from wa_lib import mask_pii

PANE_CANDIDATES = ["#pane-side", "[data-testid='chat-list']", "div[aria-label*='Elenco chat']", "[role='grid']"]

# Cammina il pannello e descrive i primi N nodi "riga" plausibili.
JS_DUMP = """
(paneSel) => {
  const pane = document.querySelector(paneSel);
  if (!pane) return {error: 'pane non trovato', sel: paneSel};
  const rows = Array.from(pane.querySelectorAll("[role='listitem'], [role='row'], [data-testid='cell-frame-container']"));
  const describe = (el) => {
    const attrs = {};
    for (const a of el.attributes) {
      if (a.name.startsWith('data-') || a.name.startsWith('aria-') || a.name === 'role' || a.name === 'title') {
        attrs[a.name] = a.value;
      }
    }
    return {tag: el.tagName.toLowerCase(), attrs, text: (el.innerText || '').slice(0, 200)};
  };
  return {
    sel: paneSel,
    rowCount: rows.length,
    rows: rows.slice(0, 12).map(r => ({
      self: describe(r),
      children: Array.from(r.querySelectorAll('span[title], span[aria-label], [data-icon], [aria-label]'))
                     .slice(0, 25).map(describe),
    })),
  };
}
"""


async def main() -> None:
    async with wa_context(headless=False) as (context, page):
        await page.wait_for_timeout(5000)  # lascia idratare la lista
        dump = None
        for sel in PANE_CANDIDATES:
            dump = await page.evaluate(JS_DUMP, sel)
            if not dump.get("error") and dump.get("rowCount"):
                break
        if not dump or dump.get("error"):
            await snap(page, "poc3-pane-non-trovato")
            raise SystemExit(f"Pannello chat non trovato con nessun candidato: {dump}")

        # Mascheramento: testi e attributi title/aria-label contengono nomi e numeri.
        def scrub(node):
            node["text"] = mask_pii(node.get("text", ""), keep=80)
            for k in list(node.get("attrs", {})):
                node["attrs"][k] = mask_pii(node["attrs"][k], keep=60)
            return node

        for row in dump["rows"]:
            scrub(row["self"])
            row["children"] = [scrub(c) for c in row["children"]]

        out = artifacts_dir() / f"dom_dump_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        log_event("dom_dump", pane=dump["sel"], rows=dump["rowCount"], file=str(out))
        print(f"Dump scritto in {out} — {dump['rowCount']} righe viste.")
        print("Ora compila docs/whatsapp/wa-dom-catalog.md guardando questo file.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Eseguire e leggere il dump**

```bash
cd backend/scripts/poc_wa && python poc3_dump_dom.py
```
Atteso: `rowCount` coerente con le chat visibili a schermo (**non** con le 30-100 totali: se è molto minore, la lista **virtualizza** → è la risposta a Q40, va scritta nel catalogo).

- [ ] **Step 3: Compilare il catalogo**

Creare `docs/whatsapp/wa-dom-catalog.md` con questa struttura, riempita **dai dati del dump**, non a memoria:

```markdown
# Catalogo DOM WhatsApp Web — rilevato in M0

> Data rilevazione: <data> · Versione WhatsApp Web: <se visibile> · Lingua interfaccia: it
> Ogni voce riporta il selettore scelto E l'alternativa di fallback. Se una voce non
> e' stata trovata, si scrive "NON TROVATO": e' un dato per il gate, non una lacuna da nascondere.

| Elemento | Selettore primario | Fallback | Note/robustezza |
|---|---|---|---|
| Pannello lista chat | | | |
| Riga chat | | | |
| Titolo chat (nome o numero) | | | Q19: stabile nel tempo? |
| Badge non letti | | | Q41: testo/aria-label o solo CSS? |
| Preview ultimo messaggio | | | troncata a quanti caratteri? |
| Direzione ultimo messaggio (in/out) | | | Q42: icona spunta presente sugli outbound? |
| Timestamp riga | | | formato |
| Casella di ricerca | | | |
| Composer messaggio | | | |
| Pulsante invio | | | |
| Spunte messaggio inviato (orologio/1/2) | | | Q39 |
| Interstitial "aggiorna WhatsApp Web" | | | Q43 |
| Popup/promo dismissibili | | | Q46 |

## Virtualizzazione della lista (Q40)
Righe nel DOM a riposo: <n> · chat totali sul numero: <n> · scroll necessario per vederle tutte: <sì/no, quanto>

## Titolo chat: nome vs numero (Q19)
Contatto in rubrica: <cosa mostra> · Contatto non in rubrica: <cosa mostra>
```

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/poc_wa/poc3_dump_dom.py docs/whatsapp/wa-dom-catalog.md
git commit -m "poc(wa): PoC-3a discovery DOM lista chat (sola lettura, PII mascherata) + catalogo"
```

---

## Task 6: PoC-3b — scan strutturato e rilevamento inbound senza aprire chat

**Files:**
- Create: `backend/scripts/poc_wa/poc3_scan.py`
- Modify: `docs/whatsapp/wa-dom-catalog.md` (sezione virtualizzazione/titoli, con i dati veri)

**Interfaces:**
- Consumes: catalogo del Task 5, `_common.wa_context`, `wa_lib.mask_pii`, `wa_lib.contains_stop`
- Produces: `artifacts/scan_<ts>.json` — lista di `{title_masked, unread_count, preview_masked, has_stop, last_is_outbound, position}` + evento `scan_done`

- [ ] **Step 1: Scrivere lo scanner usando i selettori catalogati**

```python
# backend/scripts/poc_wa/poc3_scan.py
"""PoC-3b — rilevamento inbound dalla SOLA lista chat.

Sostituire i selettori sotto con quelli catalogati nel Task 5 (wa-dom-catalog.md).
Regola non negoziabile: nessun click su una riga chat. Se serve un click per
capire qualcosa, PoC-3 e' NO-GO e va scritto nel report.

Uso:  python poc3_scan.py            # uno scan
      python poc3_scan.py --loop 15  # uno scan ogni 15 minuti (simula il watcher)
"""
import argparse
import asyncio
import json
from datetime import datetime

from _common import artifacts_dir, log_event, snap, wa_context
from wa_lib import contains_stop, mask_pii

# <<< DA COMPILARE DAL CATALOGO (Task 5) >>>
PANE_SEL = "#pane-side"
ROW_SEL = "[role='listitem']"
TITLE_SEL = "span[title]"
UNREAD_SEL = "span[aria-label*='non lett']"
PREVIEW_SEL = "[data-testid='last-msg-status'], span[dir='ltr']"
OUTBOUND_ICON_SEL = "[data-icon='status-dblcheck'], [data-icon='status-check'], [data-icon='status-time']"

JS_SCAN = """
(sels) => {
  const pane = document.querySelector(sels.pane);
  if (!pane) return {error: 'pane non trovato'};
  const rows = Array.from(pane.querySelectorAll(sels.row));
  return rows.map((r, i) => {
    const t = r.querySelector(sels.title);
    const u = r.querySelector(sels.unread);
    const p = r.querySelector(sels.preview);
    const o = r.querySelector(sels.outIcon);
    return {
      position: i,
      title: t ? (t.getAttribute('title') || t.innerText || '') : '',
      unread_raw: u ? (u.getAttribute('aria-label') || u.innerText || '') : '',
      preview: p ? (p.innerText || '') : '',
      last_is_outbound: !!o,
    };
  });
}
"""


def _parse_unread(raw: str) -> int:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else (1 if raw else 0)


async def scan_once(page) -> list[dict]:
    rows = await page.evaluate(JS_SCAN, {
        "pane": PANE_SEL, "row": ROW_SEL, "title": TITLE_SEL,
        "unread": UNREAD_SEL, "preview": PREVIEW_SEL, "outIcon": OUTBOUND_ICON_SEL,
    })
    if isinstance(rows, dict) and rows.get("error"):
        raise SystemExit(f"Scan fallito: {rows['error']} — ricontrolla i selettori del catalogo.")
    out = []
    for r in rows:
        out.append({
            "position": r["position"],
            "title_masked": mask_pii(r["title"], keep=40),
            "title_is_number": r["title"].replace(" ", "").replace("+", "").isdigit(),
            "unread_count": _parse_unread(r["unread_raw"]),
            "preview_masked": mask_pii(r["preview"], keep=60),
            "has_stop": contains_stop(r["preview"]),
            "last_is_outbound": r["last_is_outbound"],
        })
    return out


async def main(loop_minutes: int) -> None:
    async with wa_context(headless=False) as (context, page):
        while True:
            await page.wait_for_timeout(4000)
            rows = await scan_once(page)
            unread = [r for r in rows if r["unread_count"] > 0]
            path = artifacts_dir() / f"scan_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            log_event("scan_done", righe=len(rows), non_letti=len(unread),
                      titoli_numerici=sum(1 for r in rows if r["title_is_number"]),
                      stop_visti=sum(1 for r in rows if r["has_stop"]))
            if not loop_minutes:
                return
            await page.wait_for_timeout(loop_minutes * 60 * 1000)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="minuti tra uno scan e l'altro")
    args = ap.parse_args()
    asyncio.run(main(args.loop))
```

- [ ] **Step 2: Raccogliere i 20 inbound del criterio GO**

Protocollo (dentro la finestra di PoC-1):
1. lanciare `python poc3_scan.py --loop 15` e lasciarlo girare in una giornata lavorativa;
2. far arrivare **≥ 15 inbound dalle chat controllate** (Tommaso e conoscenti scrivono al secondario), di cui **almeno 2 contenenti "STOP"** e almeno 2 con un messaggio intermedio dopo lo STOP (verifica del miss noto: la preview mostra solo l'ultimo messaggio);
3. contare gli **inbound spontanei reali** dei clienti Primero intercettati (servono ≥ 5, in sola lettura);
4. per ogni inbound: è comparso nello scan successivo? Il `title_masked` corrisponde? È `title_is_number` per i non in rubrica?

- [ ] **Step 3: Verificare che il watcher NON marchi letto (criterio invertito)**

Dopo una tornata di scan con chat non lette: **dal telefono**, controllare che le chat siano ancora segnate come non lette. Se anche una sola risulta letta, PoC-3 è **NO-GO** — l'intera strategia "leggi senza aprire" cade e va scritto nel report.

- [ ] **Step 4: Registrare gli esiti nel catalogo**

Compilare in `wa-dom-catalog.md` le sezioni *Virtualizzazione* e *Titolo chat: nome vs numero* con i numeri veri, e aggiungere: quanti dei 20 inbound sono stati rilevati al primo ciclo, quanti STOP intercettati / quanti persi per messaggio intermedio.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/poc_wa/poc3_scan.py docs/whatsapp/wa-dom-catalog.md
git commit -m "poc(wa): PoC-3b scan lista chat + rilevamento inbound senza aprire le chat"
```

---

## Task 7: PoC-2a — apertura chat per numero, due strategie, zero invii

**Files:**
- Create: `backend/scripts/poc_wa/poc2_open.py`

**Interfaces:**
- Consumes: `wa_lib.normalize_e164`, `wa_lib.AllowList`, `_common.*`
- Produces: `artifacts/open_results.csv` (`ts,strategia,numero_masked,esito,ms,segnale_cronologia,note`); costanti `SEARCH_SEL`, `open_by_deeplink(page, e164)`, `open_by_search(page, e164)` riusate dal Task 8

- [ ] **Step 1: Scrivere lo script (nessun invio possibile: non tocca il composer)**

```python
# backend/scripts/poc_wa/poc2_open.py
"""PoC-2a — si apre una chat esistente PER NUMERO in modo deterministico?

Confronta due strategie:
  A) deep-link  https://web.whatsapp.com/send?phone=<e164>
  B) ricerca interna nella lista chat

Misura: successo/fallimento, millisecondi, e che segnale distingue
"chat con cronologia" da "chat inesistente" (serve alla guardia V2 dell'SDD).

Questo script NON invia: non tocca il composer. L'allowlist protegge comunque
dal caso "deep-link a un numero sbagliato apre una chat nuova con un cliente".

Uso:  python poc2_open.py --numeri "+39...,+39..." --strategia deeplink|search|both
"""
import argparse
import asyncio
import csv
import time
from datetime import datetime, timezone

from _common import artifacts_dir, first_locator, log_event, snap, wa_context
from wa_lib import AllowList, normalize_e164

SEARCH_SEL = ["[data-testid='chat-list-search']", "div[contenteditable='true'][data-tab='3']",
              "div[aria-label*='Cerca']", "div[aria-label*='Search']"]
COMPOSER_SEL = ["div[contenteditable='true'][data-tab='10']", "div[aria-label*='Scrivi un messaggio']",
                "div[aria-label*='Type a message']", "footer div[contenteditable='true']"]
# Messaggi gia' presenti nella conversazione = la chat ha cronologia (guardia V2).
HISTORY_SEL = ["div.message-in", "div.message-out", "[data-testid='msg-container']", "[role='row']"]
NO_CHAT_SEL = ["text=Il numero di telefono condiviso tramite url non è valido",
               "text=Phone number shared via url is invalid", "[data-testid='popup-contents']"]


async def _history_signal(page) -> str:
    found = await first_locator(page, HISTORY_SEL, timeout_ms=5000)
    if found:
        count = await page.locator(found[1]).count()
        return f"cronologia:{found[1]}:{count}"
    missing = await first_locator(page, NO_CHAT_SEL, timeout_ms=2000)
    return f"nessuna-cronologia:{missing[1] if missing else 'nessun-segnale'}"


async def open_by_deeplink(page, e164: str) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    await page.goto(f"https://web.whatsapp.com/send?phone={e164}", wait_until="domcontentloaded", timeout=60000)
    ok = await first_locator(page, COMPOSER_SEL, timeout_ms=25000)
    ms = (time.perf_counter() - t0) * 1000
    return bool(ok), ms, await _history_signal(page)


async def open_by_search(page, e164: str) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    box = await first_locator(page, SEARCH_SEL, timeout_ms=10000)
    if not box:
        return False, (time.perf_counter() - t0) * 1000, "casella-ricerca-non-trovata"
    await box[0].click()
    await page.keyboard.type(e164, delay=60)
    await page.wait_for_timeout(2500)
    await page.keyboard.press("Enter")
    ok = await first_locator(page, COMPOSER_SEL, timeout_ms=15000)
    ms = (time.perf_counter() - t0) * 1000
    return bool(ok), ms, await _history_signal(page)


async def main(numeri: list[str], strategia: str) -> None:
    allow = AllowList.load()
    if not len(allow):
        raise SystemExit("POC_WA_ALLOWED_NUMBERS non configurata: mi fermo (fail-closed).")

    path = artifacts_dir() / "open_results.csv"
    new = not path.exists()
    async with wa_context(headless=False) as (context, page):
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "strategia", "numero_masked", "esito", "ms", "segnale_cronologia", "note"])
            for raw in numeri:
                e164 = normalize_e164(raw)
                if not e164:
                    print(f"scarto: {raw!r} non normalizzabile")
                    continue
                allow.assert_allowed(e164)  # anche in sola apertura: niente sorprese
                for strat in (["deeplink", "search"] if strategia == "both" else [strategia]):
                    fn = open_by_deeplink if strat == "deeplink" else open_by_search
                    try:
                        ok, ms, segnale = await fn(page, e164)
                        note = ""
                    except Exception as e:
                        ok, ms, segnale, note = False, -1, "eccezione", f"{type(e).__name__}: {e}"[:160]
                        await snap(page, f"poc2-open-fail-{strat}")
                    w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), strat,
                                f"…{e164[-4:]}", "OK" if ok else "KO", round(ms), segnale, note])
                    log_event("open_chat", strategia=strat, esito=ok, ms=round(ms), segnale=segnale)
                    await page.wait_for_timeout(3000)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--numeri", required=True, help="numeri separati da virgola (solo chat controllate)")
    ap.add_argument("--strategia", default="both", choices=["deeplink", "search", "both"])
    args = ap.parse_args()
    asyncio.run(main([n for n in args.numeri.split(",") if n.strip()], args.strategia))
```

- [ ] **Step 2: Eseguire su tutte le chat controllate, entrambe le strategie**

```bash
cd backend/scripts/poc_wa
set POC_WA_ALLOWED_NUMBERS=+39...,+39...,+39...
python poc2_open.py --numeri "%POC_WA_ALLOWED_NUMBERS%" --strategia both
```
Ripetere in momenti diversi della giornata e con chat in posizioni diverse della lista (una in cima, una vecchia). **≥ 20 aperture per strategia.**

- [ ] **Step 3: Test del caso "chat inesistente" (guardia V2)**

Una sola volta, con un numero **tuo** mai usato su WhatsApp o senza cronologia: eseguire con `--strategia deeplink` e registrare **esattamente** cosa mostra la pagina (screenshot + `segnale_cronologia`). È la risposta a Q37/Q38 e definisce come la guardia V2 riconoscerà "niente cronologia → non scrivere".

- [ ] **Step 4: Verdetto di apertura**

Dal CSV: percentuale OK per strategia e ms medi. **Criterio: ≥ 90% su almeno una delle due strategie**, altrimenti PoC-2 NO-GO. La strategia vincente diventa quella del POM in M1 e va scritta nel catalogo.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/poc_wa/poc2_open.py
git commit -m "poc(wa): PoC-2a apertura chat per numero (deep-link vs ricerca) con misure, zero invii"
```

---

## Task 8: PoC-2b — guardia pre-invio misurata + invio + spunte

**Files:**
- Create: `backend/scripts/poc_wa/poc2_send.py`

**Interfaces:**
- Consumes: `poc2_open.open_by_deeplink|open_by_search`, `_common.human_type`, `wa_lib.AllowList`, `wa_lib.contains_stop`
- Produces: `artifacts/send_results.csv` (`ts,numero_masked,guardia_ms,inbound_letti,stop_trovato,inviato,spunta,totale_ms,note`)

Questo è il task più delicato del piano: è l'unico che **scrive** su WhatsApp. Tre guardie in fila: allowlist fail-closed, `--send` esplicito, STOP che ferma tutto.

- [ ] **Step 1: Scrivere lo script**

```python
# backend/scripts/poc_wa/poc2_send.py
"""PoC-2b — guardia pre-invio + invio reale + verifica spunte.

Sequenza per ogni destinatario (identica a quella che il sender di M3 dovra' fare):
  1. apre la chat con la strategia vincente di PoC-2a;
  2. GUARDIA PRE-INVIO: legge i messaggi inbound successivi all'ultimo messaggio
     nostro e cerca uno STOP -> cronometrata, target <= 2s (SDD 13, PoC-2);
  3. se STOP -> NON invia, registra e passa oltre;
  4. altrimenti digita in modo umano e invia;
  5. legge la spunta dell'ultimo messaggio inviato (orologio/1/2) -> Q39.

TRE GUARDIE prima di scrivere a qualcuno:
  - allowlist fail-closed (POC_WA_ALLOWED_NUMBERS);
  - flag --send esplicito (senza, e' dry-run: apre, misura la guardia, non invia);
  - STOP trovato = stop assoluto.

Uso:  python poc2_send.py --numero "+39..." --messaggio-file D:\\wa-poc\\messages.txt [--send]
"""
import argparse
import asyncio
import csv
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from _common import artifacts_dir, first_locator, human_type, log_event, snap, wa_context
from poc2_open import COMPOSER_SEL, open_by_deeplink
from wa_lib import AllowList, contains_stop, mask_pii, normalize_e164

# Legge la coda di messaggi in fondo alla conversazione: gli inbound dopo
# l'ultimo outbound nostro. Budget fisso: cio' che e' renderizzato, niente scroll
# infinito (SDD: budget = visibili + 1-2 scroll).
JS_TAIL = """
() => {
  const rows = Array.from(document.querySelectorAll('div.message-in, div.message-out'));
  const tail = [];
  for (let i = rows.length - 1; i >= 0 && tail.length < 30; i--) {
    const el = rows[i];
    const isOut = el.classList.contains('message-out');
    if (isOut) break;                     // fermati al nostro ultimo messaggio
    tail.push((el.innerText || '').slice(0, 300));
  }
  return tail.reverse();
}
"""
TICK_SEL = ["[data-icon='status-dblcheck']", "[data-icon='status-check']", "[data-icon='status-time']"]


async def guardia_pre_invio(page) -> tuple[list[str], float, bool]:
    """Ritorna (inbound_dopo_ultimo_nostro, millisecondi, stop_trovato)."""
    t0 = time.perf_counter()
    tail = await page.evaluate(JS_TAIL)
    ms = (time.perf_counter() - t0) * 1000
    stop = any(contains_stop(t) for t in tail)
    return tail, ms, stop


async def leggi_spunta(page) -> str:
    found = await first_locator(page, TICK_SEL, timeout_ms=8000)
    return found[1] if found else "nessuna-spunta-letta"


async def main(numero: str, messaggio: str, send: bool) -> None:
    allow = AllowList.load()
    e164 = normalize_e164(numero)
    if not e164:
        raise SystemExit(f"Numero non normalizzabile: {numero!r}")
    allow.assert_allowed(e164)   # fail-closed: solleva se non e' una chat controllata

    path = artifacts_dir() / "send_results.csv"
    new = not path.exists()
    async with wa_context(headless=False) as (context, page):
        t_start = time.perf_counter()
        ok, open_ms, segnale = await open_by_deeplink(page, e164)
        if not ok:
            await snap(page, "poc2-send-apertura-fallita")
            raise SystemExit(f"Chat non aperta ({segnale}): niente invio.")
        if "nessuna-cronologia" in segnale:
            raise SystemExit("Chat senza cronologia: V2 vieta di scrivere. Stop.")

        tail, guardia_ms, stop = await guardia_pre_invio(page)
        inviato, spunta, note = False, "", ""
        if stop:
            note = "STOP trovato nella coda inbound: invio annullato"
        elif not send:
            note = "dry-run (nessun --send)"
        else:
            comp = await first_locator(page, COMPOSER_SEL, timeout_ms=10000)
            if not comp:
                await snap(page, "poc2-composer-non-trovato")
                raise SystemExit("Composer non trovato: catalogare il selettore prima di riprovare.")
            await human_type(page, comp[0], messaggio)
            await asyncio.sleep(random.uniform(0.4, 1.2))
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2500)
            spunta = await leggi_spunta(page)
            inviato = True

        totale_ms = (time.perf_counter() - t_start) * 1000
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "numero_masked", "guardia_ms", "inbound_letti", "stop_trovato",
                            "inviato", "spunta", "totale_ms", "note"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), f"…{e164[-4:]}",
                        round(guardia_ms), len(tail), int(stop), int(inviato), spunta,
                        round(totale_ms), note])
        log_event("send_attempt", guardia_ms=round(guardia_ms), inbound=len(tail), stop=stop,
                  inviato=inviato, spunta=spunta, totale_ms=round(totale_ms),
                  ultimo_inbound=mask_pii(tail[-1] if tail else "", keep=60))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--numero", required=True)
    ap.add_argument("--messaggio-file", required=True, help="file con i messaggi veri scritti da Tommaso")
    ap.add_argument("--send", action="store_true", help="senza questo flag e' dry-run")
    args = ap.parse_args()
    righe = [r.strip() for r in Path(args.messaggio_file).read_text(encoding="utf-8").splitlines() if r.strip()]
    if not righe:
        raise SystemExit("File messaggi vuoto: scrivi i testi veri prima (Task 0 step 6).")
    asyncio.run(main(args.numero, random.choice(righe), args.send))
```

- [ ] **Step 2: Dry-run su tutte le chat controllate (nessun messaggio parte)**

```bash
cd backend/scripts/poc_wa
python poc2_send.py --numero "+39..." --messaggio-file D:\wa-poc\messages.txt
```
Atteso: `inviato=0`, `guardia_ms` valorizzato. **Verificare subito il numero chiave: `guardia_ms` ≤ 2000.** Se sfora sistematicamente, la strategia opt-out va rivista prima di M3 (è scritto nell'SDD §13) — segnalarlo, non nasconderlo in una media.

- [ ] **Step 3: Test negativo della guardia (il più importante)**

1. Da una chat controllata, Tommaso (o il conoscente) scrive **"STOP"** al secondario, seguito da un altro messaggio qualsiasi ("scherzo ahah").
2. Lanciare `poc2_send.py --numero <quella chat> --send`.
3. **Atteso: nessun messaggio inviato**, `stop_trovato=1`. Se parte lo stesso, la guardia non funziona → **PoC-2 NO-GO su opt-out**, e va scritto in maiuscolo nel report: è la garanzia strutturale su cui poggia tutto il disegno GDPR.

- [ ] **Step 4: 20 invii reali su ≥ 6 chat controllate**

Con `--send`, distribuiti su almeno 2 giornate, alternando chat in cima e chat vecchie. Ogni invio usa un messaggio vero dal file. Verificare a campione **dal telefono** che i messaggi siano arrivati come messaggi normali.
**Criterio GO: 20/20 riusciti**, `spunta` leggibile in ≥ 18/20.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/poc_wa/poc2_send.py
git commit -m "poc(wa): PoC-2b guardia pre-invio cronometrata + invio umanizzato + lettura spunte"
```

---

## Task 9: PoC-4 — coesistenza bot / umano

**Files:**
- Create: `backend/scripts/poc_wa/poc4_coexist.py`

**Interfaces:**
- Consumes: `poc2_send.guardia_pre_invio`, `poc2_open.open_by_deeplink`, `_common.*`
- Produces: `artifacts/coexist_results.csv` (`ts,scenario,esito,dettaglio`)

- [ ] **Step 1: Scrivere lo script degli scenari**

```python
# backend/scripts/poc_wa/poc4_coexist.py
"""PoC-4 — il bot e l'umano sullo stesso numero, contemporaneamente.

Quattro scenari, tutti su chat controllate. Tommaso tiene in mano il telefono del
numero secondario (fa l'umano-business); il "contatto" e' un suo secondo numero.

  S1  bot invia mentre l'umano ha WhatsApp aperto sul telefono (chat diversa)
  S2  bot invia mentre l'umano ha APERTA LA STESSA chat            -> Q75
  S3  l'umano scrive nella chat 1-2 secondi prima che il bot invii -> check C4
  S4  l'umano legge la risposta prima dello scan del bot           -> Q73

Non automatizza l'umano: e' un cronometro con protocollo. Lo script chiede
conferma a schermo tra uno scenario e l'altro.

Uso:  python poc4_coexist.py --numero "+39..." --messaggio-file D:\\wa-poc\\messages.txt --scenario S1
"""
import argparse
import asyncio
import csv
import random
from datetime import datetime, timezone
from pathlib import Path

from _common import artifacts_dir, first_locator, human_type, log_event, snap, wa_context
from poc2_open import COMPOSER_SEL, open_by_deeplink
from poc2_send import guardia_pre_invio, leggi_spunta
from wa_lib import AllowList, normalize_e164

ISTRUZIONI = {
    "S1": "Apri WhatsApp sul telefono su una chat DIVERSA e tienilo acceso. Poi premi Invio qui.",
    "S2": "Apri sul telefono ESATTAMENTE la chat di destinazione e tienila aperta. Poi premi Invio qui.",
    "S3": "Scrivi tu un messaggio in quella chat dal telefono, poi entro 2 secondi premi Invio qui.",
    "S4": "Fai scrivere una risposta dal secondo telefono, LEGGILA dal telefono del secondario, poi premi Invio qui.",
}


async def main(numero: str, messaggio: str, scenario: str) -> None:
    allow = AllowList.load()
    e164 = normalize_e164(numero)
    allow.assert_allowed(e164)

    print(f"\n=== {scenario} ===\n{ISTRUZIONI[scenario]}")
    input("> ")

    async with wa_context(headless=False) as (context, page):
        ok, _, segnale = await open_by_deeplink(page, e164)
        if not ok:
            raise SystemExit(f"Chat non aperta: {segnale}")
        tail, guardia_ms, stop = await guardia_pre_invio(page)
        dettaglio = f"inbound_in_coda={len(tail)} guardia_ms={round(guardia_ms)} stop={stop}"

        if scenario == "S4":
            # Non si invia: si osserva solo se un inbound gia' letto dall'umano
            # resta rilevabile (il badge unread sparisce -> il watcher lo perde?).
            await snap(page, "poc4-S4-dopo-lettura-umana")
            esito = "osservato"
        elif stop:
            esito = "invio-annullato-da-STOP"
        else:
            comp = await first_locator(page, COMPOSER_SEL, timeout_ms=10000)
            await human_type(page, comp[0], messaggio)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3000)
            dettaglio += f" spunta={await leggi_spunta(page)}"
            esito = "inviato"
        await snap(page, f"poc4-{scenario}")

        path = artifacts_dir() / "coexist_results.csv"
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "scenario", "esito", "dettaglio"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), scenario, esito, dettaglio])
        log_event("coexist", scenario=scenario, esito=esito, dettaglio=dettaglio)

    print("\nOra GUARDA IL TELEFONO e annota nel report:")
    print(" - il messaggio e' comparso mentre guardavi? come si vede?")
    print(" - il telefono ha mostrato notifiche/anomalie? la sessione e' rimasta collegata?")
    print(" - c'e' stato un doppio messaggio o un ordine strano?")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--numero", required=True)
    ap.add_argument("--messaggio-file", required=True)
    ap.add_argument("--scenario", required=True, choices=["S1", "S2", "S3", "S4"])
    args = ap.parse_args()
    righe = [r.strip() for r in Path(args.messaggio_file).read_text(encoding="utf-8").splitlines() if r.strip()]
    asyncio.run(main(args.numero, random.choice(righe), args.scenario))
```

- [ ] **Step 2: Eseguire i 4 scenari, ognuno almeno 2 volte**

```bash
cd backend/scripts/poc_wa
python poc4_coexist.py --numero "+39..." --messaggio-file D:\wa-poc\messages.txt --scenario S1
```
(idem S2, S3, S4). **Criteri GO: nessun doppio messaggio; il telefono non viene disconnesso; il check C4 vede l'outbound umano recente in S3.**

- [ ] **Step 3: Osservare l'away-message del Business (Q78)**

Se il numero secondario ha attivo un messaggio di assenza automatico: verificare se compare come outbound nella conversazione e se lo scan lo scambia per attività umana. Registrare.

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/poc_wa/poc4_coexist.py
git commit -m "poc(wa): PoC-4 scenari di coesistenza bot/umano con protocollo manuale"
```

---

## Task 10: Report finale e verdetto GO/NO-GO

**Files:**
- Create: `docs/whatsapp/poc-report.md`
- Modify: `docs/whatsapp/SDD-whatsapp-channel.md` (§3.2 assunzioni verificate, §17 domande [PoC] chiuse)
- Modify: `docs/project/PROGRESS.md`

- [ ] **Step 1: Scrivere il report dai dati, non dai ricordi**

```markdown
# PoC report — canale WhatsApp M0

> Periodo: <data inizio> → <data fine> · Numero: secondario Primero (WhatsApp Business)
> Fonti: artifacts/heartbeat.csv, open_results.csv, send_results.csv, coexist_results.csv, events.jsonl

## Verdetto

| PoC | Criterio | Misurato | Esito |
|---|---|---|---|
| PoC-1 sessione | 14gg, ≥5 riavvii browser, ≥2 riavvii PC, nessun re-scan | | GO / NO-GO |
| PoC-2 apertura | ≥90% aperture OK su una strategia | | |
| PoC-2 invio | 20/20 su ≥6 chat controllate | | |
| PoC-2 guardia | ≤ 2s per invio | | |
| PoC-2 STOP | invio bloccato dallo STOP in coda | | |
| PoC-3 rilevamento | 20/20 inbound entro 1 ciclo, ≥5 spontanei reali | | |
| PoC-3 non-lettura | nessuna chat marcata letta dal watcher | | |
| PoC-4 coesistenza | nessun doppio invio, nessun logout | | |

**Decisione:** <strada A confermata → si procede con M1> / <strada A in discussione: cosa ha fallito e perché>

## Numeri che servono a M1-M3
- Costo medio di un invio (apertura + guardia + typing + verifica): <n> s → cap giornalieri realistici (Q50)
- Costo della guardia pre-invio: mediana <n> ms, p95 <n> ms (Q73)
- RAM per sessione: mediana <n> MB, picco <n> MB → sessioni contemporanee sostenibili sul PC attuale (A5)
- Virtualizzazione lista: <sì/no>, scroll necessario: <n> (Q40)
- Strategia di apertura scelta: <deep-link | ricerca> — motivo: <dati>

## Cosa si è rotto e come si è presentato
<selettori mancati, interstitial, popup, comportamenti inattesi: uno per riga, con lo screenshot>

## Domande §17 chiuse da M0
<elenco Q chiuse con la risposta misurata>

## Domande ancora aperte
<Q rimaste, con il motivo per cui M0 non le ha chiuse>
```

- [ ] **Step 2: Riportare gli esiti nell'SDD**

In §3.2: aggiornare la colonna *Verifica* delle assunzioni A1-A5, A7 con l'esito reale (verificata / smentita / parziale). In §17: marcare **CHIUSA (M0)** le domande [PoC] con la risposta misurata — Q19, Q37-Q47, Q49-Q51, Q53-Q55, Q64, Q69, Q73, Q75. Le domande che M0 non ha potuto chiudere restano aperte **con il motivo scritto**.

- [ ] **Step 3: Aggiornare PROGRESS.md**

Voce `[2026-XX-XX] WhatsApp M0 — PoC gate: <esito>` con il link al report.

- [ ] **Step 4: Commit + PR**

```bash
git add docs/whatsapp/poc-report.md docs/whatsapp/SDD-whatsapp-channel.md docs/whatsapp/wa-dom-catalog.md docs/project/PROGRESS.md
git commit -m "poc(wa): report M0 + verdetto GO/NO-GO + SDD aggiornato con le assunzioni verificate"
git push -u origin feat/whatsapp-m0-poc
gh pr create --title "M0 — PoC gate canale WhatsApp" --body "Script PoC usa-e-getta + report con le misure. Gate: vedi verdetto nel report."
```

- [ ] **Step 5: Gate — fermarsi qui**

Se PoC-1, PoC-2 o PoC-3 sono NO-GO, **M1 non parte**: si riapre la scelta della strada (B con occhi aperti, o rinuncia) con Tommaso. Il gate è il punto del piano, non un adempimento.

---

## Self-review (fatta in scrittura)

**Copertura §13 dell'SDD:** PoC-1 → Task 3+4 · PoC-2 apertura → Task 7 · PoC-2 invio/guardia/spunte → Task 8 · PoC-3 → Task 5+6 · PoC-4 → Task 9 · report+selettori → Task 10. PoC-5 fuori scope per decisione 24/07 (rampa M5).

**Rischi noti di questo piano, dichiarati:**
1. I selettori nei Task 6/7/8 sono **candidati plausibili, non verità**: il Task 5 esiste apposta per sostituirli con quelli veri. Un implementer che li prende per buoni senza fare il Task 5 lavora su sabbia.
2. PoC-1 dura 14 giorni: è il **cammino critico** dell'intero M0. Va avviato per primo, i task 5-9 girano dentro la sua finestra.
3. Il vincolo "solo chat controllate" rende PoC-2 più debole dell'originale (6+ chat invece di 50): l'apertura su cronologie di terzi resta parzialmente non provata fino a M5.
4. Nessun proxy e nessun fingerprint in M0 (Task 0 step 5, `_common.py`): M0 misura WhatsApp Web, non il nostro layer anti-detect. Entrambi tornano in M1/M3.
