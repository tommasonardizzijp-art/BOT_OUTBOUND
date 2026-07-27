# M1 — Fondamenta canale WhatsApp — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **REQUIRED SUB-SKILL aggiuntiva (standard di Tommaso):** `sviluppo-modulo` — worktree isolato, reviewer dedicato per ogni task, QA agent dopo ogni funzione, e chiusura del modulo con ≥20 test manuali + ≥30 adversarial. Il modulo è chiuso quando **si difende**, non quando la suite è verde.

**Goal:** costruire le fondamenta del canale WhatsApp — schema dati, pseudonimizzazione dei numeri, il Page Object Model che eredita tutto ciò che M0 ha misurato sul DOM reale, e il login assistito locale — senza che il canale Instagram in produzione se ne accorga.

**Architecture:** tre strati indipendenti che non si conoscono tra loro. (1) **Dati**: una catena Alembic additiva `025` che crea `tenants` + 8 tabelle `wa_*`, più i modelli SQLAlchemy corrispondenti — zero `ALTER` su tabelle esistenti. (2) **Input umano condiviso**: `_human_type`/`_human_click` escono da `InstagramPage` e diventano un modulo puro `browser/human_input.py`, usato sia da Instagram (che deve regredire a zero) sia da WhatsApp; oggi lo stesso codice esiste in tre copie. (3) **Browser WhatsApp**: `WhatsAppWebPage`, che porta in produzione i selettori e le regole di M0, e `wa_session` per il login assistito locale.

**Tech Stack:** Python 3.13 · SQLAlchemy 2 async + Alembic · Patchright (Chromium persistente) · pytest + pytest-asyncio · Fernet (`cryptography`) per i numeri cifrati, HMAC-SHA256 per gli pseudonimi.

## Global Constraints

- **Interprete:** `D:\BOT OUTBOUND\backend\venv\Scripts\python.exe`. Non esiste venv dentro i worktree.
- **Test in un worktree:** `.env` non è versionato, quindi lì manca. Ogni run di pytest ha bisogno di `SECRET_KEY` e `JWT_SECRET` a env, altrimenti `app.config.Settings` fallisce la validazione pydantic prima ancora di raccogliere i test.
- **Browser Playwright/Patchright:** `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers`. **Mai su `C:`** — il disco di sistema è da 120 GB ed è già stretto.
- **Il canale Instagram non deve regredire.** È in produzione. Ogni task che tocca `app/browser/instagram_page.py` ha un test di non-regressione **prima** della modifica.
- **Commenti e docstring del codice in ASCII** (`gia'`, `e'`, `piu'`): è lo stile del repo e la console Windows è cp1252. I documenti markdown invece usano gli accenti normalmente.
- **Numeri di telefono:** mai in chiaro nei log, mai in chiaro come chiave. Chiave interna = `phone_hmac`; per il display si usa la forma mascherata `+39•••••077`.
- **Migrazioni:** additive. Si migra **prima** di far girare codice che dichiara colonne nuove (Postgres `42703` altrimenti).
- **Worktree isolato + branch dedicato + PR.** Mai push diretto su `master`.
- **RAM:** 1,2 GB per profilo browser misurati in M0, su una macchina da 7,4 GB. **Un numero WhatsApp alla volta** su questo PC: i test E2E non aprono due profili insieme.

---

## File Structure

**Nuovi:**

| File | Responsabilità |
|---|---|
| `backend/app/browser/human_input.py` | Digitazione e click umanizzati. Funzioni pure a livello di modulo, nessuno stato, nessun aggancio a IG o WA. |
| `backend/app/utils/phone_pseudonym.py` | `hmac_phone()`, `mask_phone()`, `normalize_e164()`. Nessun import di `app.models`. |
| `backend/alembic/versions/025_wa_channel_schema.py` | `tenants` + 8 tabelle `wa_*`. Additiva. |
| `backend/app/models/tenant.py` | Modello `Tenant`. |
| `backend/app/models/wa.py` | Gli 8 modelli `wa_*` (stessa area di dominio, cambiano insieme). |
| `backend/app/browser/whatsapp_page.py` | POM WhatsApp Web: sessione, apertura chat, guardia pre-invio, invio, spunte, scan lista. |
| `backend/app/browser/whatsapp_selectors.py` | Solo selettori e costanti DOM. Separato perché è la parte che WhatsApp romperà per prima: si aggiorna senza toccare la logica. |
| `backend/app/services/wa_session.py` | Stato sessione di un numero, login assistito locale, health-check. |

**Modificati:**

| File | Cosa cambia |
|---|---|
| `backend/app/browser/instagram_page.py` | `_human_type` e `_human_click` diventano deleghe sottili a `human_input`. Il comportamento non cambia. |
| `backend/app/config.py` | Nuovo setting `phone_hmac_key`. |
| `backend/app/models/__init__.py` | Import dei modelli nuovi (Alembic autogenerate e `Base.metadata` li devono vedere). |

**Nota di decomposizione:** `whatsapp_page.py` e `whatsapp_selectors.py` sono separati di proposito. `instagram_page.py` è arrivato a 1377 righe mescolando selettori, logica e comportamento umano, ed è il motivo per cui `_human_type` è finito duplicato in tre posti. Il POM WhatsApp non ripete quell'errore.

---

## Task 1: `human_input` — un solo posto per il comportamento umano

Oggi la stessa digitazione umanizzata esiste in **tre copie**: `InstagramPage._human_type` ([instagram_page.py:633](../../backend/app/browser/instagram_page.py)), la copia dichiarata negli script PoC (`scripts/poc_wa/_common.py:403`, che nel proprio docstring ammette di essere una copia), e quella che finirebbe nel POM WhatsApp se non si estraesse ora. Tre copie significano che una taratura anti-detect corretta in un posto resta sbagliata negli altri due.

**Files:**
- Create: `backend/app/browser/human_input.py`
- Create: `backend/tests/test_human_input.py`
- Modify: `backend/app/browser/instagram_page.py` (righe 25-43 e 633-691, più il call site di `_human_click` a 1035)

**Interfaces:**
- Produces:
  - `async def human_type(page, element, text: str, *, timing_multiplier: float = 1.0, newline_key: str = "Shift+Enter") -> None`
  - `async def human_click(page, element) -> None`
  - `def typo_char(char: str) -> str | None`
  - `QWERTY_ADJACENT: dict[str, str]`
- Consumes: niente (modulo foglia).

Il parametro `newline_key` è l'unica differenza reale tra i due canali: su Instagram Enter invia il DM, su WhatsApp pure, quindi entrambi vogliono `Shift+Enter`. Resta un parametro esplicito invece di una costante perché è **una regola del sito, non del nostro codice**, e va vista nella firma.

- [ ] **Step 1: Test di non-regressione su Instagram, PRIMA di toccare qualsiasi cosa**

Questo test deve passare sul codice attuale, non modificato. È la rete: se dopo l'estrazione passa ancora, IG non è regredito.

```python
# backend/tests/test_human_input.py
import asyncio
import pytest


class FakeKeyboard:
    def __init__(self):
        self.typed: list[str] = []
        self.pressed: list[str] = []

    async def type(self, text: str):
        self.typed.append(text)

    async def press(self, key: str):
        self.pressed.append(key)


class FakePage:
    def __init__(self):
        self.keyboard = FakeKeyboard()


class FakeElement:
    def __init__(self):
        self.clicked = False

    async def click(self):
        self.clicked = True


@pytest.mark.asyncio
async def test_instagram_human_type_ancora_digita_il_testo(monkeypatch):
    """Non-regressione IG: la digitazione resta corretta al netto dei typo."""
    from app.browser.instagram_page import InstagramPage

    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: asyncio.sleep(0))
    page, element = FakePage(), FakeElement()
    ig = InstagramPage.__new__(InstagramPage)
    ig._page = page
    ig._tm = 1.0

    await ig._human_type(element, "ciao come stai")

    assert element.clicked is True
    # I typo vengono corretti con Backspace: il numero di Backspace deve
    # corrispondere ai caratteri battuti in eccesso.
    battuti = "".join(page.keyboard.typed)
    backspace = page.keyboard.pressed.count("Backspace")
    assert len(battuti) - backspace == len("ciao come stai")
```

- [ ] **Step 2: Eseguire il test sul codice NON modificato**

Run:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
SECRET_KEY=x JWT_SECRET=dummy-jwt-secret-for-local-tests-only-32chars \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m pytest tests/test_human_input.py -v
```
Expected: **PASS**. Se fallisce qui, il test è sbagliato — non il codice. Correggere il test prima di procedere: un test di non-regressione che non passa sul codice sano non protegge niente.

- [ ] **Step 3: Test del modulo nuovo (fallisce: il modulo non esiste)**

```python
# aggiungere a backend/tests/test_human_input.py
@pytest.mark.asyncio
async def test_human_type_batte_il_testo_e_corregge_i_typo(monkeypatch):
    from app.browser import human_input

    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: asyncio.sleep(0))
    page, element = FakePage(), FakeElement()

    await human_input.human_type(page, element, "ciao mondo")

    battuti = "".join(page.keyboard.typed)
    backspace = page.keyboard.pressed.count("Backspace")
    assert len(battuti) - backspace == len("ciao mondo")
    assert element.clicked is True


@pytest.mark.asyncio
async def test_human_type_usa_shift_enter_per_gli_a_capo(monkeypatch):
    """Un a-capo battuto come Enter INVIA il messaggio a meta'. Su entrambi i siti."""
    from app.browser import human_input

    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: asyncio.sleep(0))
    page, element = FakePage(), FakeElement()

    await human_input.human_type(page, element, "riga uno\nriga due")

    assert page.keyboard.pressed.count("Shift+Enter") == 1
    assert "Enter" not in page.keyboard.pressed


def test_typo_char_resta_sulla_tastiera_e_conserva_il_maiuscolo():
    from app.browser.human_input import QWERTY_ADJACENT, typo_char

    assert typo_char("1") is None          # cifra: nessun vicino, nessun typo
    for _ in range(50):
        wrong = typo_char("S")
        assert wrong is not None
        assert wrong.isupper()
        assert wrong.lower() in QWERTY_ADJACENT["s"]
```

- [ ] **Step 4: Eseguire, verificare che fallisca**

Run: stesso comando dello Step 2.
Expected: FAIL con `ModuleNotFoundError: No module named 'app.browser.human_input'`.

- [ ] **Step 5: Creare `human_input.py`**

Spostare il corpo **senza cambiarne la taratura**: le costanti numeriche (40-95 ms base, typo 8% su parole >3 lettere, lognormale σ=0.45, clamp 30-480 ms) sono state tarate contro un sistema anti-detect. Cambiarle "per pulizia" durante un'estrazione significa cambiare la firma comportamentale senza saperlo.

```python
# backend/app/browser/human_input.py
"""Comportamento umano di input, condiviso tra i canali browser.

Estratto da InstagramPage il 27/07 durante M1. Prima di questa estrazione lo
stesso codice esisteva in TRE copie: InstagramPage._human_type, la copia negli
script PoC di M0 (che nel proprio docstring dichiarava di essere una copia), e
quella che sarebbe finita nel POM WhatsApp. Tre copie significano che una
taratura anti-detect corretta in un posto resta sbagliata negli altri due.

NON cambiare le costanti numeriche senza una misura. Sono tarate su un utente
"digitale" (~100 WPM di picco) e la loro varianza E' la mitigazione: un ritardo
fisso e' varianza zero, cioe' la firma robotica piu' banale da misurare.
"""
import asyncio
import math
import random

# Tasti adiacenti su QWERTY, per generare typo plausibili.
QWERTY_ADJACENT: dict[str, str] = {
    'q': 'wa',   'w': 'qes',  'e': 'wrd',  'r': 'etf',  't': 'ryg',
    'y': 'tuh',  'u': 'yij',  'i': 'uok',  'o': 'ipl',  'p': 'ol',
    'a': 'qsz',  's': 'awdz', 'd': 'sefc', 'f': 'drgv', 'g': 'fthb',
    'h': 'gyun', 'j': 'huim', 'k': 'jiol', 'l': 'kop',
    'z': 'asx',  'x': 'zdc',  'c': 'xfv',  'v': 'cgb',  'b': 'vhn',
    'n': 'bhm',  'm': 'nj',
}


def typo_char(char: str) -> str | None:
    """Un tasto adiacente plausibile per char (conserva il maiuscolo), o None."""
    adjacent = QWERTY_ADJACENT.get(char.lower())
    if not adjacent:
        return None
    wrong = random.choice(adjacent)
    return wrong.upper() if char.isupper() else wrong


async def human_type(page, element, text: str, *, timing_multiplier: float = 1.0,
                     newline_key: str = "Shift+Enter") -> None:
    """Digita con velocita' variabile, pause tra le parole e typo corretti.

    Clicca l'elemento per dargli il focus, poi usa page.keyboard per tutto il
    resto: cosi' non si ri-localizza l'elemento a ogni carattere, cosa che
    fallisce se il DOM React del sito si ri-renderizza durante la digitazione.

    newline_key e' un parametro e non una costante perche' e' una regola DEL
    SITO, non del nostro codice: su IG e su WhatsApp Web un Enter nudo INVIA il
    messaggio, quindi un a-capo battuto come Enter spedisce meta' testo.
    """
    await element.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))

    base_ms = random.uniform(40, 95) * timing_multiplier

    for line_idx, line in enumerate(text.split('\n')):
        if line_idx > 0:
            await page.keyboard.press(newline_key)
            await asyncio.sleep(random.uniform(0.15, 0.5))

        words = line.split(' ')
        for i, word in enumerate(words):
            # Pausa di pensiero occasionale prima di una parola.
            if i > 0 and random.random() < 0.07:
                await asyncio.sleep(random.uniform(0.25, 1.0))

            for char_idx, char in enumerate(word):
                # Typo: ~8% per carattere in parole >3 lettere, mai sul primo o l'ultimo.
                if len(word) > 3 and 0 < char_idx < len(word) - 1 and random.random() < 0.08:
                    wrong = typo_char(char)
                    if wrong:
                        err_delay = random.lognormvariate(math.log(base_ms), 0.45)
                        await page.keyboard.type(wrong)
                        await asyncio.sleep(max(30, min(480, err_delay)) / 1000)
                        await asyncio.sleep(random.uniform(0.12, 0.40))   # se ne accorge
                        await page.keyboard.press("Backspace")
                        await asyncio.sleep(random.uniform(0.06, 0.20))   # prima di ribattere

                delay_ms = max(30, min(480, random.lognormvariate(math.log(base_ms), 0.45)))
                await page.keyboard.type(char)
                await asyncio.sleep(delay_ms / 1000)
                # Micro-pausa rara dentro una parola (rilettura, esitazione).
                if random.random() < 0.015:
                    await asyncio.sleep(random.uniform(0.2, 0.7))

            if i < len(words) - 1:
                await page.keyboard.type(' ')
                await asyncio.sleep(random.uniform(25, 80) / 1000)


async def human_click(page, element) -> None:
    """Clicca in un punto casuale dentro il bounding box dell'elemento.

    Il box si calcola subito prima del click: un'attesa lunga tra calcolo e
    click lascia che il layout si sposti e le coordinate diventino stale.
    """
    try:
        await element.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass
    box = await element.bounding_box()
    if not box:
        await element.click()
        return
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    await page.mouse.move(x, y, steps=random.randint(5, 15))
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.click(x, y)
```

- [ ] **Step 6: Eseguire i test del modulo nuovo**

Run: stesso comando dello Step 2.
Expected: PASS su tutti e quattro (il non-regressione IG passa ancora perché IG non è stato toccato).

- [ ] **Step 7: `InstagramPage` delega invece di duplicare**

In `backend/app/browser/instagram_page.py`:

1. Cancellare `_QWERTY_ADJACENT` (righe 25-34) e `_typo_char` (righe 36-43).
2. Sostituire il corpo di `_human_type` (righe 633-691) con:

```python
    async def _human_type(self, element, text: str) -> None:
        """Delega a browser.human_input (estratto in M1). Il comportamento non cambia:
        stesse costanti, stesso Shift+Enter. Qui resta solo il legame con lo stato
        dell'istanza (_page, _tm), che il modulo puro non deve conoscere."""
        await human_input.human_type(self._page, element, text,
                                     timing_multiplier=self._tm)
```

3. Sostituire il corpo di `_human_click` (righe 1035-1053) con:

```python
    async def _human_click(self, page, element) -> None:
        """Delega a browser.human_input (estratto in M1)."""
        await human_input.human_click(page, element)
```

4. Aggiungere l'import in cima al file: `from app.browser import human_input`.
5. Rimuovere `import math` **solo se** nessun'altra riga del file lo usa (`grep -n "math\." app/browser/instagram_page.py`). `random` resta: è usato in tutto il file.

- [ ] **Step 8: Eseguire il non-regressione IG + tutta la suite del canale IG**

Run:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
SECRET_KEY=x JWT_SECRET=dummy-jwt-secret-for-local-tests-only-32chars \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m pytest tests/ -q
```
Expected: PASS. **Zero test rotti.** Se un test IG cade, la delega non è equivalente: si torna allo Step 7, non si aggiusta il test.

- [ ] **Step 9: Commit**

```bash
git add backend/app/browser/human_input.py backend/app/browser/instagram_page.py backend/tests/test_human_input.py
git commit -m "refactor(browser): estrae human_input da InstagramPage, condiviso coi canali

Digitazione e click umanizzati erano in tre copie: InstagramPage, gli script
PoC di M0 e quella che sarebbe finita nel POM WhatsApp. Una taratura
anti-detect corretta in un posto restava sbagliata negli altri due.

Costanti invariate: sono tarate contro un sistema anti-detect, non si
'puliscono' durante un'estrazione. Test di non-regressione IG scritto ed
eseguito PRIMA della modifica."
```

---

## Task 2: `phone_pseudonym` — il numero non è mai la chiave

**Files:**
- Create: `backend/app/utils/phone_pseudonym.py`
- Create: `backend/tests/test_phone_pseudonym.py`
- Modify: `backend/app/config.py`

**Interfaces:**
- Produces:
  - `def normalize_e164(raw: str, default_country: str = "39") -> str` — solleva `ValueError` se il numero non è normalizzabile
  - `def hmac_phone(e164: str) -> str` — 64 caratteri esadecimali
  - `def mask_phone(e164: str) -> str` — `+39•••••077`
  - `class PhoneNormalizationError(ValueError)`
- Consumes: `app.config.settings.phone_hmac_key`

**Perché HMAC e non hash semplice.** Lo spazio dei numeri di telefono italiani è piccolo: uno SHA-256 nudo di un numero si inverte con un dizionario in pochi minuti. Serve una chiave segreta. E deve essere una chiave **dedicata**, non `SECRET_KEY`: quella cifra altre cose e un giorno la si ruota, e ruotare la chiave HMAC significa perdere l'aggancio a tutti gli pseudonimi già scritti a DB.

- [ ] **Step 1: Scrivere i test**

```python
# backend/tests/test_phone_pseudonym.py
import pytest

from app.utils.phone_pseudonym import (PhoneNormalizationError, hmac_phone,
                                       mask_phone, normalize_e164)


@pytest.mark.parametrize("raw,atteso", [
    ("+39 342 146 0077", "393421460077"),
    ("3421460077", "393421460077"),        # nazionale italiano, prefisso implicito
    ("0039 342 146 0077", "393421460077"),
    ("+39-342-146-0077", "393421460077"),
    ("\u202a+393421460077\u202c", "393421460077"),   # marcatori Unicode dai title WhatsApp
])
def test_normalize_e164_accetta_le_forme_reali(raw, atteso):
    assert normalize_e164(raw) == atteso


@pytest.mark.parametrize("raw", ["", "   ", "abc", "+39", "12", None])
def test_normalize_e164_rifiuta_invece_di_indovinare(raw):
    """Un numero non normalizzabile e' uno SCARTO dell'ingest, non un numero
    'quasi giusto': indovinare significa scrivere a uno sconosciuto."""
    with pytest.raises(PhoneNormalizationError):
        normalize_e164(raw)


def test_hmac_e_deterministico_e_lungo_64():
    a = hmac_phone("393421460077")
    assert a == hmac_phone("393421460077")
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_hmac_distingue_numeri_diversi():
    assert hmac_phone("393421460077") != hmac_phone("393421460078")


def test_hmac_non_contiene_il_numero():
    assert "3421460077" not in hmac_phone("393421460077")


def test_mask_mostra_solo_prefisso_e_ultime_tre():
    assert mask_phone("393421460077") == "+39\u2022\u2022\u2022\u2022\u2022077"


def test_mask_non_esplode_su_numero_corto():
    """mask_phone finisce nei log degli errori: se solleva li' dentro, nasconde
    l'errore vero che stava per essere loggato."""
    assert mask_phone("39") == "+39\u2022\u2022\u2022\u2022\u2022"
    assert mask_phone("") == ""
```

- [ ] **Step 2: Eseguire, verificare che fallisca**

Run:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
SECRET_KEY=x JWT_SECRET=dummy-jwt-secret-for-local-tests-only-32chars \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m pytest tests/test_phone_pseudonym.py -v
```
Expected: FAIL con `ModuleNotFoundError: No module named 'app.utils.phone_pseudonym'`.

- [ ] **Step 3: Aggiungere il setting**

In `backend/app/config.py`, accanto agli altri campi di `Settings`:

```python
    # Chiave HMAC per gli pseudonimi dei numeri di telefono (P12).
    # DEDICATA, non SECRET_KEY: ruotare SECRET_KEY e' un'operazione normale,
    # ruotare questa significa perdere l'aggancio a TUTTI i phone_hmac gia'
    # scritti a DB. Vanno tenute separate proprio per poter ruotare l'una
    # senza distruggere l'altra.
    phone_hmac_key: str = ""
```

Aggiungere a `backend/.env.example`:

```
# Chiave HMAC per gli pseudonimi dei numeri (canale WhatsApp).
# Generare con: python -c "import secrets; print(secrets.token_urlsafe(32))"
# NON ruotare senza una migrazione dei phone_hmac esistenti.
PHONE_HMAC_KEY=
```

- [ ] **Step 4: Implementare il modulo**

```python
# backend/app/utils/phone_pseudonym.py
"""Pseudonimizzazione dei numeri di telefono (SDD P12).

La chiave interna di un contatto WhatsApp e' l'HMAC del suo numero, mai il
numero. Il numero in chiaro esiste in due soli posti: cifrato con Fernet a DB
(`encrypted_phone`, decifrato solo al momento di aprire la chat) e nella
memoria del processo per la durata dell'invio.

HMAC e non SHA-256 nudo: lo spazio dei numeri italiani e' piccolo abbastanza da
essere invertito con un dizionario in minuti. Serve una chiave segreta.
"""
import hmac
import re
from hashlib import sha256

from app.config import settings

# Marcatori di direzione del testo che WhatsApp infila negli attributi `title`.
# Invisibili a schermo, ma un numero che li contiene non normalizza (misurato
# in M0, 27/07).
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_NON_CIFRE = re.compile(r"[^\d+]")


class PhoneNormalizationError(ValueError):
    """Il numero non e' normalizzabile in E.164.

    Deliberatamente un'eccezione e non un valore di ritorno None: un numero
    "quasi giusto" non esiste. Chi normalizza scrivera' a quel numero, e
    indovinare significa scrivere a uno sconosciuto.
    """


def normalize_e164(raw: str, default_country: str = "39") -> str:
    """Da qualunque forma scritta da un umano a `393421460077` (senza '+')."""
    if not raw or not isinstance(raw, str):
        raise PhoneNormalizationError(f"numero vuoto o non testuale: {raw!r}")

    s = _NON_CIFRE.sub("", _BIDI.sub("", raw).strip())
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("00"):
        s = s[2:]
    s = s.replace("+", "")

    if not s.isdigit():
        raise PhoneNormalizationError(f"caratteri non numerici: {raw!r}")
    # Numero nazionale italiano: il prefisso lo mettiamo noi.
    if len(s) == 10 and s.startswith("3"):
        s = default_country + s
    if not (11 <= len(s) <= 15):
        raise PhoneNormalizationError(f"lunghezza fuori range E.164 ({len(s)}): {raw!r}")
    return s


def hmac_phone(e164: str) -> str:
    """HMAC-SHA256 esadecimale del numero normalizzato."""
    key = (settings.phone_hmac_key or "").encode("utf-8")
    if not key:
        raise RuntimeError(
            "PHONE_HMAC_KEY non impostata: senza, gli pseudonimi sarebbero "
            "invertibili con un dizionario. Genera con: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return hmac.new(key, e164.encode("utf-8"), sha256).hexdigest()


def mask_phone(e164: str) -> str:
    """`+39•••••077` — forma da log e da display admin.

    Non solleva mai: finisce dentro i messaggi d'errore, e un'eccezione qui
    nasconderebbe l'errore vero che si stava per loggare.
    """
    if not e164:
        return ""
    s = str(e164).lstrip("+")
    prefisso, resto = s[:2], s[2:]
    return f"+{prefisso}" + "\u2022" * 5 + resto[-3:]
```

- [ ] **Step 5: Eseguire i test**

Run: come Step 2, ma servirà anche `PHONE_HMAC_KEY` a env:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
SECRET_KEY=x JWT_SECRET=dummy-jwt-secret-for-local-tests-only-32chars \
PHONE_HMAC_KEY=chiave-di-test-non-usare-in-produzione \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m pytest tests/test_phone_pseudonym.py -v
```
Expected: PASS (9 test).

- [ ] **Step 6: Commit**

```bash
git add backend/app/utils/phone_pseudonym.py backend/tests/test_phone_pseudonym.py backend/app/config.py backend/.env.example
git commit -m "feat(wa): pseudonimizzazione dei numeri con HMAC dedicato

Chiave interna = HMAC del numero, mai il numero. Chiave DEDICATA e non
SECRET_KEY: ruotare SECRET_KEY e' normale, ruotare questa perde l'aggancio a
tutti i phone_hmac gia' a DB.

normalize_e164 solleva invece di tornare None: un numero 'quasi giusto' non
esiste, indovinare significa scrivere a uno sconosciuto. mask_phone invece non
solleva mai, perche' finisce dentro i messaggi d'errore."
```

---

## Task 3: schema dati — `tenants` + 8 tabelle `wa_*`

**Files:**
- Create: `backend/alembic/versions/025_wa_channel_schema.py`
- Create: `backend/app/models/tenant.py`
- Create: `backend/app/models/wa.py`
- Create: `backend/tests/test_wa_models.py`
- Modify: `backend/app/models/__init__.py`

**Interfaces:**
- Consumes: `app.database.Base`, `app.utils.phone_pseudonym.hmac_phone` (solo nei test).
- Produces: `Tenant`, `WaNumber`, `WaContact`, `WaCampaign`, `WaSequenceStep`, `WaCampaignContact`, `WaMessage`, `WaInboundEvent`, più gli enum `WaNumberStatus`, `WaCampaignStatus`, `WaContactStatus`, `WaMessageStatus`, `WaSendCondition`, `WaMatchedBy`, `WaDeliveryCheck`, `WaDncReason`.

Lo schema è in SDD §5.2 e va seguito lì. Regola vincolante di §5.3: **nessun `ALTER` su tabelle esistenti**. Il canale IG in produzione non si accorge di questa migrazione.

- [ ] **Step 1: Verificare la testa della catena Alembic**

Run:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
grep -h "^revision\|^down_revision" alembic/versions/024_message_template_d.py
```
Expected: `revision = "024"`, `down_revision = "023"` ⇒ la nuova migrazione è `025` con `down_revision = "024"`. Se la testa non è 024, usare quella vera: la catena non si indovina.

- [ ] **Step 2: Scrivere i test dei modelli e degli invarianti**

```python
# backend/tests/test_wa_models.py
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.wa import WaContact, WaNumber, WaNumberStatus
from app.utils.phone_pseudonym import hmac_phone


async def _tenant(session: AsyncSession) -> Tenant:
    t = Tenant(id=str(uuid.uuid4()), name="Primero")
    session.add(t)
    await session.flush()
    return t


@pytest.mark.asyncio
async def test_contatto_unico_per_tenant_e_numero(db_session: AsyncSession):
    """UNIQUE(tenant_id, phone_hmac): due tenant possono avere lo stesso
    contatto, lo stesso tenant no. Senza questo, un doppio upload del CSV
    crea due contatti e la persona riceve il messaggio due volte."""
    t = await _tenant(db_session)
    h = hmac_phone("393421460077")
    db_session.add(WaContact(id=str(uuid.uuid4()), tenant_id=t.id, phone_hmac=h,
                             encrypted_phone="x"))
    await db_session.flush()
    db_session.add(WaContact(id=str(uuid.uuid4()), tenant_id=t.id, phone_hmac=h,
                             encrypted_phone="x"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_stesso_numero_su_due_tenant_e_permesso(db_session: AsyncSession):
    t1, t2 = await _tenant(db_session), await _tenant(db_session)
    h = hmac_phone("393421460077")
    db_session.add_all([
        WaContact(id=str(uuid.uuid4()), tenant_id=t1.id, phone_hmac=h, encrypted_phone="x"),
        WaContact(id=str(uuid.uuid4()), tenant_id=t2.id, phone_hmac=h, encrypted_phone="x"),
    ])
    await db_session.flush()   # nessuna eccezione: sono clienti diversi


@pytest.mark.asyncio
async def test_numero_wa_nasce_in_pending_qr(db_session: AsyncSession):
    t = await _tenant(db_session)
    n = WaNumber(id=str(uuid.uuid4()), tenant_id=t.id, label="Primero sede",
                 phone_hmac=hmac_phone("393421460077"), encrypted_phone="x")
    db_session.add(n)
    await db_session.flush()
    assert n.status == WaNumberStatus.pending_qr
    assert n.sent_today == 0


@pytest.mark.asyncio
async def test_contatto_nasce_contattabile(db_session: AsyncSession):
    t = await _tenant(db_session)
    c = WaContact(id=str(uuid.uuid4()), tenant_id=t.id,
                  phone_hmac=hmac_phone("393421460077"), encrypted_phone="x")
    db_session.add(c)
    await db_session.flush()
    assert c.opted_out is False and c.do_not_contact is False


@pytest.mark.asyncio
async def test_chat_title_e_nullable(db_session: AsyncSession):
    """chat_title resta NULL quando il titolo della chat e' un NUMERO (contatto
    non in rubrica del cliente): salvarlo metterebbe il numero in chiaro a DB,
    violando P12. In quel caso il matching usa gia' phone_hmac."""
    t = await _tenant(db_session)
    c = WaContact(id=str(uuid.uuid4()), tenant_id=t.id,
                  phone_hmac=hmac_phone("393421460077"), encrypted_phone="x")
    db_session.add(c)
    await db_session.flush()
    assert c.chat_title is None
```

Serve una fixture `db_session`. Se `backend/tests/conftest.py` non la espone già, aggiungerla lì (il `conftest` crea già lo schema su SQLite per sessione):

```python
@pytest.fixture
async def db_session():
    """Sessione su SQLite di test, con rollback a fine test: nessun test
    vede le scritture di un altro."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    await eng.dispose()
```

- [ ] **Step 3: Eseguire, verificare che fallisca**

Run:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
SECRET_KEY=x JWT_SECRET=dummy-jwt-secret-for-local-tests-only-32chars \
PHONE_HMAC_KEY=chiave-di-test-non-usare-in-produzione \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m pytest tests/test_wa_models.py -v
```
Expected: FAIL con `ModuleNotFoundError: No module named 'app.models.tenant'`.

- [ ] **Step 4: Scrivere i modelli**

`backend/app/models/tenant.py`:

```python
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TenantStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"


class Tenant(Base):
    """Il cliente della piattaforma. Non e' solo WhatsApp: esiste da qui in poi
    anche per il canale IG, quando si unifichera' la UI."""
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[TenantStatus] = mapped_column(
        SAEnum(TenantStatus, name="tenant_status"),
        default=TenantStatus.active, nullable=False)
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=datetime.utcnow, nullable=False)
```

`backend/app/models/wa.py` — gli 8 modelli, seguendo SDD §5.2 colonna per colonna e lo stile di `app/models/campaign.py` (`Mapped`, `mapped_column`, enum `str`+`enum.Enum`).

> **L'elenco delle colonne non è ricopiato qui, ed è una scelta.** SDD §5.2 le elenca già tutte, tabella per tabella, con tipo e nota. Duplicarle in questo piano creerebbe una **seconda fonte di verità** su uno schema di 8 tabelle: il giorno che una colonna cambia, una delle due copie resta indietro e nessuno sa quale sia quella giusta. L'implementatore apre SDD §5.2 e la traduce. Quello che **non** può ricavare da lì, perché sono decisioni di implementazione e non di schema, è tutto scritto qui sotto.

Vincoli che il modello **deve** esprimere, perché sono invarianti del dominio e non dettagli:

```python
# Estratto dei vincoli non negoziabili — il file completo segue SDD 5.2.

class WaNumberStatus(str, enum.Enum):
    pending_qr = "pending_qr"
    active = "active"
    qr_required = "qr_required"
    disconnected = "disconnected"
    cooldown = "cooldown"
    suspended = "suspended"
    retired = "retired"


class WaContact(Base):
    __tablename__ = "wa_contacts"
    __table_args__ = (
        # Un doppio upload dello stesso CSV non deve creare due contatti:
        # sarebbe la stessa persona contattata due volte.
        UniqueConstraint("tenant_id", "phone_hmac", name="uq_wa_contacts_tenant_phone"),
    )
    # chat_title NULLABLE e mai popolato se il titolo e' un numero (P12).
    chat_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WaSequenceStep(Base):
    __tablename__ = "wa_sequence_steps"
    __table_args__ = (
        UniqueConstraint("campaign_id", "step_index", name="uq_wa_steps_campaign_index"),
    )


class WaCampaignContact(Base):
    __tablename__ = "wa_campaign_contacts"
    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id", name="uq_wa_camp_contact"),
        # next_action_at e' la colonna su cui gira il tick del sequence engine:
        # senza indice, ogni tick e' una scansione completa della tabella.
        Index("ix_wa_campaign_contacts_next_action", "next_action_at"),
    )
```

Registrare i nuovi modelli in `backend/app/models/__init__.py`: se non li importa, `Base.metadata` non li vede e `create_all` nei test non crea le tabelle.

- [ ] **Step 5: Scrivere la migrazione 025**

```python
# backend/alembic/versions/025_wa_channel_schema.py
"""Schema del canale WhatsApp: tenants + 8 tabelle wa_*.

Interamente ADDITIVA: nessun ALTER su tabelle esistenti (SDD 5.3). Il canale
Instagram in produzione non si accorge di questa migrazione.

Revision ID: 025
Revises: 024
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    # ... le 8 tabelle wa_*, colonne come da SDD 5.2, nell'ordine delle FK:
    #     wa_numbers -> wa_contacts -> wa_campaigns -> wa_sequence_steps
    #     -> wa_campaign_contacts -> wa_messages -> wa_inbound_events
    op.create_unique_constraint("uq_wa_contacts_tenant_phone", "wa_contacts",
                                ["tenant_id", "phone_hmac"])
    op.create_index("ix_wa_campaign_contacts_next_action", "wa_campaign_contacts",
                    ["next_action_at"])


def downgrade() -> None:
    # Ordine inverso: le FK non permettono di droppare un padre prima dei figli.
    for tabella in ("wa_inbound_events", "wa_messages", "wa_campaign_contacts",
                    "wa_sequence_steps", "wa_campaigns", "wa_contacts",
                    "wa_numbers", "tenants"):
        op.drop_table(tabella)
```

**Gli enum si scrivono come `sa.String` con un vincolo applicativo, non come `sa.Enum` nativo di Postgres.** Motivo: un `ENUM` Postgres va alterato con `ALTER TYPE` a ogni valore nuovo, e la state machine dei numeri (§8.3) ne guadagnerà. Il modello SQLAlchemy usa comunque `SAEnum`, che valida lato Python.

- [ ] **Step 6: Eseguire i test dei modelli**

Run: come Step 3.
Expected: PASS (5 test).

- [ ] **Step 7: Verificare che la migrazione giri davvero, in su e in giù**

Una migrazione che non è mai stata eseguita non è una migrazione: è un file.

Run:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
DATABASE_URL="sqlite:///./data/test_migration.db" SECRET_KEY=x \
JWT_SECRET=dummy-jwt-secret-for-local-tests-only-32chars \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m alembic upgrade head && \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m alembic downgrade 024 && \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m alembic upgrade head
```
Expected: tre comandi verdi. Il ciclo su-giù-su prova che il `downgrade` non è decorativo.

⚠️ **Su un DB Postgres reale non lanciare questo ciclo.** Il downgrade droppa le tabelle. Su Supabase si esegue **solo** `upgrade head`, e con l'avvertenza nota del repo sui lock `idle in transaction`.

- [ ] **Step 8: Commit**

```bash
git add backend/alembic/versions/025_wa_channel_schema.py backend/app/models/ backend/tests/test_wa_models.py
git commit -m "feat(wa): schema del canale -- tenants + 8 tabelle wa_*

Migrazione 025 interamente additiva: nessun ALTER su tabelle esistenti, il
canale IG in produzione non se ne accorge.

Due vincoli sono nello schema e non nel codice applicativo, perche' sono
invarianti di dominio: UNIQUE(tenant_id, phone_hmac) -- un doppio upload dello
stesso CSV non deve contattare la stessa persona due volte -- e l'indice su
next_action_at, che e' la colonna su cui gira il tick del sequence engine.

chat_title resta nullable: se il titolo della chat e' un numero (contatto non
in rubrica del cliente) NON si salva, perche' sarebbe il numero in chiaro a DB."
```

---

## Task 4: `WhatsAppWebPage` — il POM che eredita M0

Questo è il task che porta in produzione la conoscenza pagata in M0. **Riferimento obbligatorio prima di scrivere una riga:** SDD §6.4 (tredici regole, ognuna un errore già commesso) e `docs/whatsapp/wa-dom-catalog.md`.

**Files:**
- Create: `backend/app/browser/whatsapp_selectors.py`
- Create: `backend/app/browser/whatsapp_page.py`
- Create: `backend/tests/test_whatsapp_page.py`

**Interfaces:**
- Consumes: `human_input.human_type`, `phone_pseudonym.mask_phone`
- Produces:
  - `class WhatsAppWebPage` con:
    - `async def session_state(self) -> Literal["logged_in", "qr_required", "unknown"]`
    - `async def open_chat(self, e164: str) -> OpenResult`
    - `async def load_history(self, minimo: int = 80) -> HistoryInfo`
    - `async def read_inbound_tail(self, n: int = 40) -> list[str] | None` — `None` = **nessuna bolla agganciata** (cecità), diverso da `[]` (nessun inbound)
    - `async def sync_state(self) -> Literal["synced", "syncing", "unknown"]`
    - `async def send_text(self, text: str) -> None`
    - `async def read_last_tick(self) -> str`
    - `async def scan_chat_list(self) -> list[ChatRow]`
  - `@dataclass OpenResult(ok: bool, ms: float, signal: str)`
  - `@dataclass HistoryInfo(ok: bool, before: int, after: int, rounds: int, exhausted: bool)`
  - `@dataclass ChatRow(position, title, title_is_number, unread_count, preview, last_is_outbound, outgoing_state, muted)`

**Il POM non decide se inviare.** Espone segnali; la politica (guardia opt-out, cap, opt-out persistito) sta in `wa_sender`, che è M3. Un POM che decide è un POM che non si può testare.

- [ ] **Step 1: Test sui segnali di direzione — la regola asimmetrica**

È la regola più importante del canale: sbagliarla significa mandare a chi ha detto STOP.

```python
# backend/tests/test_whatsapp_page.py
import pytest

from app.browser.whatsapp_page import classify_direction


@pytest.mark.parametrize("segnali,atteso", [
    # (aria_tu, tail_icon, data_id) -> "out" | "in"
    ((True,  "tail-out", "A5" + "x" * 30), "out"),   # tutti concordi: nostro
    ((False, "tail-in",  "3A" + "x" * 18), "in"),    # tutti concordi: loro
    ((False, None,       "A5" + "x" * 30), "out"),   # solo il data_id: basta
    ((False, None,       None),            "in"),    # nessun segnale -> inbound
    ((True,  "tail-in",  "A5" + "x" * 30), "in"),    # DISCORDANTI -> inbound
    ((False, "tail-in",  "A5" + "x" * 30), "in"),    # DISCORDANTI -> inbound
])
def test_direzione_in_dubbio_vale_inbound(segnali, atteso):
    """Asimmetria deliberata: un messaggio e' 'nostro' solo se ALMENO UN segnale
    dice OUT e NESSUNO dice IN.

    I due errori non costano uguale. Trattare un nostro messaggio come inbound
    = si legge qualcosa in piu' e al peggio non si invia. Trattare un loro
    messaggio come nostro = la guardia salta lo STOP e si scrive a chi aveva
    chiesto di smettere. Il secondo e' irreversibile.
    """
    aria_tu, tail_icon, data_id = segnali
    assert classify_direction(aria_tu=aria_tu, tail_icon=tail_icon, data_id=data_id) == atteso
```

- [ ] **Step 2: Test sulla sentinella di cecità**

```python
@pytest.mark.asyncio
async def test_tail_none_quando_il_dom_non_aggancia_nulla(monkeypatch):
    """None (cecita') non e' [] (silenzio). Se un selettore si rompe e il POM
    tornasse [], il chiamante concluderebbe 'nessuno STOP' e invierebbe SEMPRE,
    sembrando funzionare. E' esattamente il bug che M0 ha evitato con la
    sentinella."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageSenzaBolle:
        async def evaluate(self, _script, *_a):
            return None

    pom = WhatsAppWebPage(PageSenzaBolle())
    assert await pom.read_inbound_tail() is None


@pytest.mark.asyncio
async def test_tail_vuota_e_diversa_da_tail_assente():
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageSenzaInbound:
        async def evaluate(self, _script, *_a):
            return []

    assert await WhatsAppWebPage(PageSenzaInbound()).read_inbound_tail() == []
```

- [ ] **Step 3: Test sulla cronologia esaurita vs non sincronizzata (A9/FM16)**

```python
@pytest.mark.asyncio
async def test_sync_state_unknown_finche_il_selettore_non_e_catalogato():
    """A9: WhatsApp Web non sincronizza tutte le chat subito. Su una chat non
    ancora sincronizzata la guardia non legge un silenzio, legge il VUOTO.

    Il selettore dell'indicatore non e' ancora catalogato: catturarlo richiede
    un re-scan del QR, che azzererebbe PoC-1. Quindi in M1 sync_state() esiste
    con la sua interfaccia e torna 'unknown', ed e' la POLITICA (M3) a decidere
    cosa fare di 'unknown'. Quello che NON si fa e' far finta che 'unknown'
    sia 'synced'."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageQualunque:
        async def evaluate(self, _s, *_a):
            return None

        def locator(self, _sel):
            raise AssertionError("nessun selettore catalogato: non si inventa")

    assert await WhatsAppWebPage(PageQualunque()).sync_state() == "unknown"
```

- [ ] **Step 4: Eseguire, verificare che falliscano**

Run:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
SECRET_KEY=x JWT_SECRET=dummy-jwt-secret-for-local-tests-only-32chars \
PHONE_HMAC_KEY=chiave-di-test-non-usare-in-produzione \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m pytest tests/test_whatsapp_page.py -v
```
Expected: FAIL con `ModuleNotFoundError: No module named 'app.browser.whatsapp_page'`.

- [ ] **Step 5: Scrivere `whatsapp_selectors.py`**

Portare i selettori **verificati** da `scripts/poc_wa/` e da `wa-dom-catalog.md`. Ogni costante porta in commento il numero misurato in M0: è quello che permette a chi la aggiornerà tra sei mesi di sapere se è ancora vera.

```python
# backend/app/browser/whatsapp_selectors.py
"""Selettori DOM di WhatsApp Web. SOLO costanti, nessuna logica.

Separato da whatsapp_page.py di proposito: e' la parte che WhatsApp rompera'
per prima, e deve poter essere aggiornata senza rileggere la logica.

Ogni costante porta la misura fatta in M0 (27/07). Chi la aggiorna deve poter
capire se e' ancora vera. Fonte: docs/whatsapp/wa-dom-catalog.md.

TRE COSE CHE NON ESISTONO, e che sono state cercate a lungo:
  div.message-in / div.message-out  -> 0 nodi su 35 messaggi
  data-icon='status-*'              -> mai esistito, le spunte sono aria-label
  [role='listitem']                 -> le righe della lista sono [role='row']
"""

CHATLIST = ["#pane-side"]
QR = ["canvas[aria-label*='scan']", "div[data-ref]"]
SEARCH = ["div[contenteditable='true'][data-tab='3']"]
COMPOSER = ["div[contenteditable='true'][data-tab='10']"]

# Spunte: aria-label LOCALIZZATO IN ITALIANO. Rompere su un cliente non
# italiano e' un quando, non un se (SDD A4).
TICKS = ["[aria-label*='Consegnato']", "[aria-label*='Letto']"]

MSG_CONTAINER = "#main [data-testid='msg-container']"
ROW = "[role='row']"
# Le INTESTAZIONI di sezione ('Chat', 'Gruppi in comune') sono [role='row']
# identiche alle chat: si filtrano su questo.
ROW_MARKER = "[data-testid='cell-frame-title']"
UNREAD_BADGE = "[data-testid='icon-unread-count']"
PREVIEW = "[data-testid='last-msg-status']"

# data_id: 20 caratteri con prefisso '3A' -> inbound; 32 con 'A5' -> outbound.
# Copertura 100% su 35 messaggi, coerente 12/12 con i tail. NON documentato da
# WhatsApp: si usa come segnale, mai da solo.
DATA_ID_IN_PREFIX, DATA_ID_IN_LEN = "3A", 20
DATA_ID_OUT_PREFIX, DATA_ID_OUT_LEN = "A5", 32

# Indicatore di sincronizzazione (A9/FM16): NON CATALOGATO.
# Si cattura alla prima riconnessione: farlo apposta richiede un re-scan del QR,
# che azzera PoC-1. Finche' e' vuoto, sync_state() torna 'unknown'.
SYNC_INDICATOR: list[str] = []
```

- [ ] **Step 6: Scrivere `whatsapp_page.py`**

Portare da `scripts/poc_wa/poc2_open.py`, `poc2_send.py`, `poc3_scan.py`, `_common.py`, con questi cambi obbligatori rispetto al PoC:

1. `open_chat` usa **solo** la ricerca. Nessun deep-link, nemmeno come fallback: su un numero senza chat ne creerebbe una nuova (V2).
2. `read_inbound_tail` **non** chiama `load_history` da sola. In M0 erano insieme; separarle serve a M3 per la **ri-lettura pre-invio** (finestra TOCTOU, §6.4 punto 2): la seconda lettura costa poco perché la cronologia è già caricata.
3. `classify_direction` è una **funzione pura a livello di modulo**, non un metodo: è la regola più importante del canale e deve essere testabile senza browser.
4. `sync_state()` torna `"unknown"` finché `SYNC_INDICATOR` è vuoto. **Non** torna `"synced"`.
5. Ogni log passa da `mask_phone`. Il numero in chiaro esiste solo ai confini.

```python
# In cima a backend/app/browser/whatsapp_page.py:
from app.browser import human_input, whatsapp_selectors as sel
from app.utils.phone_pseudonym import mask_phone


def classify_direction(*, aria_tu: bool, tail_icon: str | None,
                       data_id: str | None) -> str:
    """'out' se ALMENO UN segnale dice OUT e NESSUNO dice IN. Altrimenti 'in'.

    Asimmetrica di proposito: leggere un messaggio in piu' costa un po' di
    tempo, saltarne uno costa un opt-out violato.
    """
    dice_in = False
    dice_out = False
    if aria_tu:
        dice_out = True
    if tail_icon == "tail-out":
        dice_out = True
    elif tail_icon == "tail-in":
        dice_in = True
    if data_id:
        if data_id.startswith(sel.DATA_ID_OUT_PREFIX) and len(data_id) == sel.DATA_ID_OUT_LEN:
            dice_out = True
        elif data_id.startswith(sel.DATA_ID_IN_PREFIX) and len(data_id) == sel.DATA_ID_IN_LEN:
            dice_in = True
    return "out" if (dice_out and not dice_in) else "in"
```

- [ ] **Step 7: Eseguire i test**

Run: come Step 4.
Expected: PASS (9 test: 6 parametrizzati di direzione + 2 di sentinella + 1 di sync).

- [ ] **Step 8: QA agent — E2E reale sul browser**

Come da `sviluppo-modulo` Fase 3, il reviewer approva il codice, il QA agent prova che **funziona davvero**. Qui l'E2E è particolare e va detto esplicitamente al QA agent:

- `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers`;
- si usa il **profilo esistente** di M0 (`D:\dev\wa-poc\profile`), che ha la sessione viva: aprirne uno nuovo significa un re-scan del QR e **azzera PoC-1**;
- **sola lettura**: `session_state`, `open_chat` su un numero dell'allowlist, `load_history`, `read_inbound_tail`, `scan_chat_list`. **Nessun `send_text`.** In M1 non si manda niente a nessuno: gli invii sono M3, dopo cap e guardie;
- il daemon di M0 (`wa_daemon.py`) va **fermato prima** (`{"tipo":"stop"}` in `D:\dev\wa-poc\comandi\`): il profilo si apre una volta sola.

- [ ] **Step 9: Commit**

```bash
git add backend/app/browser/whatsapp_page.py backend/app/browser/whatsapp_selectors.py backend/tests/test_whatsapp_page.py
git commit -m "feat(wa): POM WhatsApp Web, eredita i selettori e le regole di M0

Selettori separati dalla logica: sono la parte che WhatsApp rompera' per prima
e devono potersi aggiornare senza rileggere il resto. Ogni costante porta la
misura fatta in M0.

Tre cose che il PoC ha imparato a caro prezzo e che qui NON si ripetono:
apertura solo via ricerca (il deep-link crea chat nuove e viola V2); tail None
= cecita', diverso da [] = silenzio (senza la sentinella un selettore rotto
farebbe inviare SEMPRE); direzione asimmetrica, in dubbio vale inbound.

read_inbound_tail e' separato da load_history: serve a M3 per rileggere la coda
subito prima di premere invio, che e' la finestra TOCTOU da ~20s misurata in M0.

sync_state() torna 'unknown' e non 'synced': il selettore dell'indicatore non
e' catalogato perche' catturarlo richiede un re-scan del QR che azzera PoC-1."
```

---

## Task 5: `wa_session` — login assistito locale e health-check

**Decisione 27/07:** il browser gira sul PC di Tommaso. **Niente pagina admin che mostra il QR da remoto** — quella nasceva dal modello "managed service su VPS" e oggi non serve. Ma sessione e QR stanno **dietro un'interfaccia**, perché "in futuro lo eseguono i clienti a casa loro" è uno scenario dichiarato, e se lo si mura adesso quel giorno si riscrive tutto.

**Files:**
- Create: `backend/app/services/wa_session.py`
- Create: `backend/tests/test_wa_session.py`

**Interfaces:**
- Consumes: `WhatsAppWebPage.session_state()`, `WaNumber`, `WaNumberStatus`
- Produces:
  - `async def check_session(number_id: str) -> WaNumberStatus`
  - `async def assisted_login(number_id: str, timeout_s: int = 180) -> WaNumberStatus`
  - `def profile_dir_for(number_id: str) -> Path` — convenzione `data/browser_profiles/wa_<id>`

- [ ] **Step 1: Test sulla transizione di stato**

```python
# backend/tests/test_wa_session.py
import pytest

from app.models.wa import WaNumberStatus
from app.services.wa_session import stato_da_segnale


@pytest.mark.parametrize("segnale,atteso", [
    ("logged_in",   WaNumberStatus.active),
    ("qr_required", WaNumberStatus.qr_required),
    ("unknown",     WaNumberStatus.disconnected),
])
def test_stato_da_segnale(segnale, atteso):
    assert stato_da_segnale(segnale) == atteso


def test_schermata_ignota_non_diventa_active():
    """'unknown' e' una schermata che non abbiamo riconosciuto: un interstitial,
    un aggiornamento, un ban. Mapparla su active farebbe partire gli invii
    contro una pagina che non e' WhatsApp."""
    assert stato_da_segnale("unknown") != WaNumberStatus.active


def test_profile_dir_e_per_numero():
    from app.services.wa_session import profile_dir_for

    a, b = profile_dir_for("num-a"), profile_dir_for("num-b")
    assert a != b
    assert a.name == "wa_num-a"
```

- [ ] **Step 2: Eseguire, verificare che fallisca**

Run:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
SECRET_KEY=x JWT_SECRET=dummy-jwt-secret-for-local-tests-only-32chars \
PHONE_HMAC_KEY=chiave-di-test-non-usare-in-produzione \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m pytest tests/test_wa_session.py -v
```
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.wa_session'`.

- [ ] **Step 3: Implementare**

Punti obbligati, ognuno da una misura di M0:

```python
# backend/app/services/wa_session.py
"""Stato della sessione WhatsApp di un numero, e login assistito locale.

DEPLOYMENT (decisione 27/07): il browser gira sul PC di Tommaso, il QR si
inquadra di persona. Niente pagina admin per il QR da remoto. Ma la funzione
di login sta dietro un'interfaccia perche' "in futuro lo eseguono i clienti a
casa loro" e' uno scenario dichiarato.

DUE COSE MISURATE IN M0 CHE QUESTO MODULO DEVE RISPETTARE:

1. Il PROCESSO browser puo' morire senza che muoia la SESSIONE WhatsApp.
   Successo il 27/07: browser caduto dopo 16 minuti, ma alla riapertura la
   lista chat era li' e nessun QR e' stato chiesto. Quindi un browser morto
   NON significa qr_required: si riapre e si guarda, non si allarma il cliente.

2. Un profilo si apre UNA VOLTA SOLA. Aprire un secondo browser sullo stesso
   user-data-dir e' il modo piu' rapido di corrompere il profilo e perdere la
   sessione -- cioe' provocare proprio il re-scan che si vuole evitare.
   Da qui il lock per-numero.
"""

def stato_da_segnale(segnale: str) -> WaNumberStatus:
    """Mappa il segnale del POM sullo stato del numero (SDD 8.3).

    'unknown' -> disconnected, MAI active: e' una schermata che non abbiamo
    riconosciuto (interstitial, aggiornamento, ban). Trattarla da sessione
    valida farebbe partire gli invii contro una pagina che non e' WhatsApp.
    """
    return {
        "logged_in": WaNumberStatus.active,
        "qr_required": WaNumberStatus.qr_required,
    }.get(segnale, WaNumberStatus.disconnected)
```

Il lock per-numero riusa il pattern già presente in `app/browser/context_manager.py:32` (`_get_account_lock`): stessa forma, chiave `wa_<number_id>`.

- [ ] **Step 4: Eseguire i test**

Run: come Step 2.
Expected: PASS (3 test).

- [ ] **Step 5: Suite intera + commit**

Run:
```bash
cd "D:/BOT OUTBOUND/.claude/worktrees/<worktree-m1>/backend" && \
SECRET_KEY=x JWT_SECRET=dummy-jwt-secret-for-local-tests-only-32chars \
PHONE_HMAC_KEY=chiave-di-test-non-usare-in-produzione \
"D:/BOT OUTBOUND/backend/venv/Scripts/python.exe" -m pytest tests/ -q
```
Expected: tutti verdi, **incluso il canale Instagram**.

```bash
git add backend/app/services/wa_session.py backend/tests/test_wa_session.py
git commit -m "feat(wa): stato sessione e login assistito locale

Deployment deciso il 27/07: il browser gira sul PC di Tommaso, il QR si
inquadra di persona. Niente pagina admin per il QR da remoto, ma la funzione
sta dietro un'interfaccia: 'in futuro lo eseguono i clienti a casa loro' e' uno
scenario dichiarato, non un'ipotesi.

Due regole vengono da misure di M0. Un browser morto non e' un logout: il
27/07 il processo e' caduto dopo 16 minuti e la sessione era ancora viva, si
riapre e si guarda invece di allarmare il cliente. E un profilo si apre una
volta sola: il lock per-numero evita il secondo browser sullo stesso
user-data-dir, che e' il modo piu' rapido di provocare il re-scan che si
vuole evitare."
```

---

## Chiusura del modulo (skill `sviluppo-modulo`, Fase 4)

M1 non ha UI, quindi i "test manuali dalla UI" diventano **test manuali da script**, eseguiti dal QA agent come li eseguirebbe una persona. Le liste si salvano in `.superpowers/sdd/qa-wa-m1-tests.md` e `qa-wa-m1-adversarial.md`, partendo dai modelli in `d:\dev\thevista-app-magazzino\.superpowers\sdd\`.

- [ ] **≥20 test funzionali.** Coprire almeno: migrazione su/giù/su · unicità contatto per tenant · stesso numero su due tenant · normalizzazione dei formati reali di numero · determinismo dell'HMAC · mascheramento nei log · `session_state` sul profilo vero · apertura chat per numero · caricamento cronologia · lettura coda inbound · scan lista senza aprire nulla · non-regressione IG completa.
- [ ] **≥30 test adversarial**, criterio di PASS **invertito** (passa se il sistema si difende). Categorie obbligatorie, calate su questo modulo:
  - **numeri ostili**: `+39`, `0000000000`, 30 cifre, numero con marcatori Unicode bidi, numero con caratteri arabo-indiani, `+39 342 146 0077 ext. 12`;
  - **concorrenza vera** (`asyncio.gather`, non sequenziale): due `assisted_login` sullo stesso `number_id` — deve vincerne uno solo; due ingest dello stesso contatto in parallelo — l'`UNIQUE` deve reggere e l'errore deve essere pulito, non un 500;
  - **tampering**: `phone_hmac` scritto a mano che collide, `chat_title` con un numero di telefono dentro (**non deve mai essere salvato**), `tenant_id` di un altro tenant;
  - **DOM ostile** (con page finte): `evaluate` che torna `None`, `[]`, una lista di dict senza le chiavi attese, una stringa al posto della lista, un'eccezione — in **nessuno** di questi casi `read_inbound_tail` può tornare qualcosa che il chiamante legga come "nessuno STOP";
  - **macchina a stati**: `qr_required` → `active` senza login, doppio login sullo stesso numero, `check_session` su un numero `retired`;
  - **segreti**: `PHONE_HMAC_KEY` assente ⇒ deve sollevare un errore leggibile, **mai** ripiegare su un hash senza chiave;
  - **invarianti via SQL a fine run**: nessun `wa_contacts.chat_title` che corrisponda a una regex di numero di telefono; nessun duplicato `(tenant_id, phone_hmac)`; nessun `encrypted_phone` in chiaro.
- [ ] **Fix loop fino al 100%.** "Quasi tutti" = modulo non chiuso.
- [ ] **Final whole-branch review** (`superpowers:requesting-code-review`).
- [ ] **Nessun collaudo di Tommaso su M1**: il suo collaudo è solo a MVP.

---

## Note di esecuzione

- **Worktree nuovo.** Questo piano non si esegue in `whatsapp-m0-poc`: lì c'è PoC-1 in corsa e artefatti vivi. `superpowers:using-git-worktrees`, branch `feat/whatsapp-m1-fondamenta`.
- **PoC-1 continua a girare durante M1.** Il daemon di M0 va fermato solo per i test E2E che aprono il profilo, e riavviato subito dopo: il conteggio dei giorni è dato dal marker `session_start.txt`, non dal processo, ma gli inbound di PoC-3b si raccolgono solo se il daemon gira.
- **Ordine dei task.** 1 → 2 → 3 sono indipendenti tra loro e potrebbero essere parallelizzati; **non farlo**: `sviluppo-modulo` vieta i subagent in parallelo sull'implementazione. Il 4 dipende dall'1 (`human_type`) e dal 2 (`mask_phone`). Il 5 dipende dal 3 (`WaNumberStatus`) e dal 4 (`session_state`).
- **Cosa questo piano NON contiene, deliberatamente:** invio reale (M3), ingest CSV e mappatura colonne (M2), watcher (M4), frontend (M2). Ognuno avrà il suo piano. In particolare la **memoria propria del watcher** — la conseguenza di FM17, l'inbound letto dall'umano che sparisce — è M4: qui c'è solo lo schema che la reggerà.
