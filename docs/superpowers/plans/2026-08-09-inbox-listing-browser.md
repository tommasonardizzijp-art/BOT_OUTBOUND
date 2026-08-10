# Listing inbox via browser — piano di implementazione

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **REQUIRED SUB-SKILL aggiuntiva (standard di Tommaso):** `sviluppo-modulo` — worktree isolato, implementer + reviewer dedicato per ogni task, QA agent dopo ogni funzione, e protocollo di chiusura modulo (Task 15).

**Goal:** aggiungere un secondo motore di raccolta contatti per le campagne `scrape_mode=dm_threads` che legge i contatti dall'inbox DM **via browser** (aprendo le chat), lasciando il motore API esistente byte per byte identico.

**Architecture:** un secondo ramo accanto al primo in `scrape_list.py:81`. Il nuovo motore vive in file suoi, decide da solo ritmo e pause, e condivide solo il governo a monte (stato campagna, kill-switch, resume) e il salvataggio a valle. Il pk non è ricavabile dal browser: si assegna una **targa provvisoria negativa** derivata dallo username, sostituita con quella vera durante l'arricchimento.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, Patchright/Playwright, ARQ, pytest, Next.js (frontend).

**Spec di riferimento:** `docs/superpowers/specs/2026-08-09-inbox-listing-browser-design.md` — leggerla per intero prima di iniziare.

## Global Constraints

- **Il motore API non si tocca.** Zero righe modificate in `app/services/scrape_inbox.py`, `app/services/inbox_source.py`, `_sample_page_delay`, `_inbox_page_delay`. Se un task sembra richiederlo, fermarsi e segnalare.
- **Branch dedicato + PR, mai push diretto su `main`.**
- **Migrazioni prima del codice**: `python -m scripts.migrate` prima di far girare il codice nuovo. Fermare bot e backend zombie prima (un `idle in transaction` blocca gli `ALTER TABLE`).
- **Suite pytest una alla volta**: sqlite condiviso. Usare `WA_TEST_DB_SLOT=<nome>` per uno slot proprio.
- **Playwright**: `PLAYWRIGHT_BROWSERS_PATH` NON va puntato su `D:` — il profilo PoC-1 usa `chromium-1208` su `C:`. Non toccare la variabile.
- **Prova del nove obbligatoria su ogni test**: reintrodurre il difetto e verificare che il test torni **rosso davvero**. Un test che non fallisce col bug rimesso non vale.
- **Niente `element.click()` diretto** nel motore: sempre `human_input.human_click`.
- **Loguru, mai `print()`**. Async ovunque. `Depends(get_db)`. Niente lazy loading ORM.
- **Segreti**: `.env` mai committato.
- Ogni test nuovo va in `backend/tests/`, nomi in italiano coerenti con i file esistenti (`test_inbox_*`).

## Struttura dei file

| File | Responsabilità | Azione |
|---|---|---|
| `backend/alembic/versions/031_inbox_browser_fields.py` | 4 colonne su `followers` | **creare** |
| `backend/app/models/follower.py` | dichiarare le 4 colonne | modificare (`:44` circa) |
| `backend/app/services/inbox_browser/targa.py` | targa provvisoria, funzione pura | **creare** |
| `backend/app/services/inbox_browser/testo.py` | normalizzazione nomi, stringhe localizzate, parsing lista e thread — tutte funzioni pure | **creare** |
| `backend/app/services/inbox_browser/riconoscimento.py` | archivio dei nomi noti + decisione riconosciuto/non riconosciuto | **creare** |
| `backend/app/services/inbox_browser/ritmo.py` | pause per zona, distribuzione troncata | **creare** |
| `backend/app/services/inbox_browser/pagina.py` | interazione col DOM: leggere righe, aprire chat, scorrere | **creare** |
| `backend/app/services/inbox_browser/salvataggio.py` | dedup per username, fusione, precedenza di stato | **creare** |
| `backend/app/services/scrape_inbox_browser.py` | il motore: macchina a stati, ciclo, stop | **creare** |
| `backend/app/services/scrape_list.py` | bivio sul motore | modificare (`:81-83`) |
| `backend/app/api/campaigns.py` | gate sul triplo di campi | modificare (dopo `:347`) |
| `backend/app/services/browser_bio.py` | sostituzione targa + verifica pk diverso | modificare (`:563-577`) |
| `backend/app/services/global_contact_service.py` | rifiuto targa provvisoria | modificare (`:82-104`) |
| `backend/app/api/leads.py` | export senza targhe provvisorie | modificare (`:363`, `:374`) |
| `frontend/app/campaigns/[id]/page.tsx` | riabilitare inbox browser, ingrigire bio API, fix default | modificare (`:736`, `:1084-1091`, `:1167-1194`) |

Le funzioni pure stanno in `inbox_browser/` **separate dal motore**: sono la parte testabile senza browser né DB, ed è dove vive quasi tutta la logica che può sbagliare.

---

### Task 0: Probe preliminari — due misure che sbloccano decisioni

Due scelte della spec poggiano su ipotesi non misurate. Vanno misurate **prima** di scriverci sopra, altrimenti si costruisce una macchina a stati su un segnale che non esiste.

**Files:**
- Create: `backend/scripts/probe_inbox_web_nonlette.py`
- Create: `backend/scripts/probe_inbox_web_requestfailed.py`

**Interfaces:**
- Produces: due esiti scritti nella spec — (a) come si riconosce una chat non letta dalla lista, (b) se le richieste fallite verso gli endpoint inbox sono un segnale usabile.

**Account da usare:** solo profili sacrificabili. **MAI `@michele.carozza`.** Usare `claudio.abbigliamentovincente`.

- [ ] **Step 1: Scrivere il probe delle chat non lette**

```python
# backend/scripts/probe_inbox_web_nonlette.py
"""Come si riconosce una chat NON LETTA dalla lista, senza aprirla?

La spec decide di aprire solo le chat gia' lette, per non bruciare il badge dei
non letti. Serve un segnale affidabile. Ipotesi da verificare: pallino colorato,
nome in grassetto (font-weight), aria-label dedicata.

Sola lettura, nessuna chat aperta.
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

ACCOUNT = "claudio.abbigliamentovincente"

JS = """() => {
    const righe = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.left < 660 && r.top > 200 && r.height > 50 && r.height < 130 && r.width > 250; });
    return righe.slice(0, 15).map(e => {
        const testo = e.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
        // peso del font sui nodi di testo: un non letto e' spesso in grassetto
        const pesi = [...e.querySelectorAll('span, div')]
          .filter(n => n.children.length === 0 && n.textContent.trim())
          .map(n => getComputedStyle(n).fontWeight);
        // pallini: elementi piccoli e tondi con background pieno
        const pallini = [...e.querySelectorAll('div, span')].filter(n => {
            const r = n.getBoundingClientRect(); const st = getComputedStyle(n);
            return r.width > 4 && r.width < 16 && Math.abs(r.width - r.height) < 3
                   && parseFloat(st.borderRadius) > 0
                   && st.backgroundColor !== 'rgba(0, 0, 0, 0)';
        }).length;
        const aria = e.getAttribute('aria-label');
        return {nome: testo[0] || null, pesi: [...new Set(pesi)], pallini, aria};
    });
}"""


def p(s):
    return str(s).encode("ascii", "replace").decode("ascii")


async def main():
    async with AsyncSessionLocal() as db:
        acct = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == ACCOUNT))).scalar_one_or_none()
    if acct is None:
        print(f"[X] account {ACCOUNT} non trovato")
        return
    session = BrowserSession(acct.id)
    await session.open()
    try:
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded")
        await page.wait_for_timeout(10000)
        for r in await page.evaluate(JS):
            print(f"  pallini={r['pallini']} pesi={r['pesi']} aria={p(r['aria'])[:40]}  {p(r['nome'])[:50]}")
    finally:
        await session.close()
        print("[OK] chiuso — nessuna chat aperta")


asyncio.run(main())
```

- [ ] **Step 2: Eseguire il probe delle chat non lette**

Run: `cd backend && ./venv/Scripts/python.exe scripts/probe_inbox_web_nonlette.py`

Atteso: una tabella di 15 righe. **Interpretazione**: se le righe non lette mostrano un `pallini >= 1` o un peso di font diverso dalle altre, il segnale esiste ed è utilizzabile. Se tutte le righe risultano identiche, il segnale **non** è distinguibile: fermarsi e riportare a Tommaso, come la spec prescrive — non indovinare.

- [ ] **Step 3: Scrivere il probe sulle richieste fallite**

```python
# backend/scripts/probe_inbox_web_requestfailed.py
"""Le richieste fallite sono un segnale usabile per distinguere "fine lista" da
"Instagram piantato"? La spec sospetta di no (rumore puro su una SPA).

Registra TUTTE le requestfailed durante 12 scroll, separando quelle verso gli
endpoint dell'inbox dal resto. Sola lettura, nessuna chat aperta.
"""
import asyncio, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

ACCOUNT = "claudio.abbigliamentovincente"
INBOX_ENDPOINT = re.compile(r"(direct_v2|graphql)", re.I)

JS_SCROLL = """() => {
    let box = null, best = 0;
    for (const e of document.querySelectorAll('div')) {
        const r = e.getBoundingClientRect();
        if (r.left > 700 || r.width < 200 || r.height < 300) continue;
        if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
        if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
    }
    if (!box) return null;
    box.scrollTop += box.clientHeight * 0.7;
    return box.scrollHeight;
}"""


async def main():
    async with AsyncSessionLocal() as db:
        acct = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == ACCOUNT))).scalar_one_or_none()
    session = BrowserSession(acct.id)
    await session.open()
    falliti = []
    try:
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("requestfailed", lambda r: falliti.append((r.url, r.failure)))
        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded")
        await page.wait_for_timeout(10000)
        falliti.clear()   # ignoriamo il caricamento iniziale
        for g in range(12):
            h = await page.evaluate(JS_SCROLL)
            await page.wait_for_timeout(2500)
            print(f"  giro {g + 1}: altezza={h}  falliti finora={len(falliti)}")
        print("-" * 60)
        inbox = [u for u, _ in falliti if INBOX_ENDPOINT.search(u)]
        print(f"  richieste fallite TOTALI      : {len(falliti)}")
        print(f"  di cui verso endpoint inbox   : {len(inbox)}")
        for u, f in falliti[:10]:
            print(f"    {f}  {u[:100]}")
    finally:
        await session.close()
        print("[OK] chiuso")


asyncio.run(main())
```

- [ ] **Step 4: Eseguire il probe sulle richieste fallite**

Run: `cd backend && ./venv/Scripts/python.exe scripts/probe_inbox_web_requestfailed.py`

**Interpretazione, e la decisione che ne consegue:**
- se `richieste fallite verso endpoint inbox == 0` durante uno scorrimento sano → il segnale ristretto è **pulito** e si può usare come discrimine
- se anche solo qualcuna fallisce durante uno scorrimento sano → il segnale è rumore anche ristretto: si adotta la **regola conservativa** della spec (altezza ferma + in fondo → fine lista; il "piantato" solo da eccezione vera della pagina)

- [ ] **Step 5: Scrivere gli esiti nella spec e commitare**

Aggiornare in `docs/superpowers/specs/2026-08-09-inbox-listing-browser-design.md`:
- la tabella "Fatti misurati" con le due righe nuove
- la sezione "Conferme di lettura" con il segnale trovato (o la richiesta di decisione a Tommaso se assente)
- la sezione "Fondo, lento o piantato" con la regola scelta

```bash
git add backend/scripts/probe_inbox_web_nonlette.py backend/scripts/probe_inbox_web_requestfailed.py docs/superpowers/specs/2026-08-09-inbox-listing-browser-design.md
git commit -m "probe: misura il segnale delle chat non lette e il rumore delle richieste fallite"
```

---

### Task 1: Migration 031 e colonne del modello

**Files:**
- Create: `backend/alembic/versions/031_inbox_browser_fields.py`
- Modify: `backend/app/models/follower.py:44` (dopo `contact_extra`)
- Test: `backend/tests/test_inbox_browser_migration.py`

**Interfaces:**
- Produces: `Follower.last_message_at: datetime | None`, `Follower.last_message_from: str | None` (`'us'`/`'them'`), `Follower.last_message_text: str | None`, `Follower.source_channel: str | None` (`'api'`/`'browser'`).

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_migration.py
"""Le 4 colonne del motore inbox browser esistono e sono tutte nullable.

Nullable non e' un dettaglio: le schede raccolte prima di questo lavoro non le
hanno, e devono restare valide.
"""
import pytest
from sqlalchemy import inspect

from app.models.follower import Follower


NUOVE = ("last_message_at", "last_message_from", "last_message_text", "source_channel")


@pytest.mark.parametrize("colonna", NUOVE)
def test_colonna_presente_e_nullable(colonna):
    col = inspect(Follower).columns[colonna]
    assert col.nullable is True, f"{colonna} deve essere nullable: le schede vecchie non ce l'hanno"


def test_le_colonne_preesistenti_non_sono_state_toccate():
    cols = inspect(Follower).columns
    assert cols["ig_user_id"].nullable is False
    assert cols["username"].nullable is False
    assert cols["full_name"].nullable is True
```

- [ ] **Step 2: Eseguire il test e verificare che fallisca**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_migration.py -q -p no:cacheprovider`
Atteso: FAIL con `KeyError: 'last_message_at'`

- [ ] **Step 3: Aggiungere le colonne al modello**

In `backend/app/models/follower.py`, subito dopo la riga `contact_extra` (attualmente `:45`):

```python
    # ── Motore inbox browser (migration 031) ──────────────────────────────
    # Popolati SOLO dal motore browser, a chat aperta. Nullable: le schede
    # raccolte via API non li hanno e restano valide.
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_message_from: Mapped[str | None] = mapped_column(String(10), nullable=True)   # 'us' | 'them'
    last_message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_channel: Mapped[str | None] = mapped_column(String(10), nullable=True)      # 'api' | 'browser'
```

- [ ] **Step 4: Scrivere la migration**

```python
# backend/alembic/versions/031_inbox_browser_fields.py
"""Campi raccolti dal motore inbox browser: last_message_at/from/text + source_channel.

Il motore browser apre le chat e legge dati che l'API non espone (testo integrale
dell'ultimo messaggio, chi l'ha scritto, data assoluta). source_channel serve a
sapere da dove arriva un dato: le schede raccolte via API hanno full_name NULL e
non sono riconoscibili dal nome visualizzato.

Tutte nullable: additiva, nessun ALTER distruttivo, le schede esistenti restano
valide con i campi vuoti.

Revision ID: 031
Revises: 030
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("followers", sa.Column("last_message_at", sa.DateTime(), nullable=True))
    op.add_column("followers", sa.Column("last_message_from", sa.String(length=10), nullable=True))
    op.add_column("followers", sa.Column("last_message_text", sa.Text(), nullable=True))
    op.add_column("followers", sa.Column("source_channel", sa.String(length=10), nullable=True))


def downgrade() -> None:
    # batch_alter_table: SQLite non ha DROP COLUMN nativo prima della 3.35
    # (stesso pattern di 028 e 030).
    with op.batch_alter_table("followers") as batch:
        batch.drop_column("last_message_at")
        batch.drop_column("last_message_from")
        batch.drop_column("last_message_text")
        batch.drop_column("source_channel")
```

- [ ] **Step 5: Applicare la migration e rieseguire il test**

Prima fermare bot e backend zombie (un `idle in transaction` blocca gli `ALTER TABLE`).

Run: `cd backend && ./venv/Scripts/python.exe -m scripts.migrate`
Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_migration.py -q -p no:cacheprovider`
Atteso: PASS (3 test)

- [ ] **Step 6: Prova del nove**

Rimuovere temporaneamente `nullable=True` da `last_message_at` nel modello (metterlo `nullable=False`), rieseguire il test: deve FALLIRE. Poi rimetterlo.

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/031_inbox_browser_fields.py backend/app/models/follower.py backend/tests/test_inbox_browser_migration.py
git commit -m "feat(inbox-browser): migration 031, i 4 campi raccolti a chat aperta"
```

---

### Task 2: La targa provvisoria

**Files:**
- Create: `backend/app/services/inbox_browser/__init__.py` (vuoto)
- Create: `backend/app/services/inbox_browser/targa.py`
- Test: `backend/tests/test_inbox_browser_targa.py`

**Interfaces:**
- Produces:
  - `targa_provvisoria(username: str) -> int` — sempre negativa, deterministica **fra processi**
  - `e_provvisoria(ig_user_id: int) -> bool`

- [ ] **Step 1: Scrivere il test, incluso quello su due processi**

```python
# backend/tests/test_inbox_browser_targa.py
"""La targa provvisoria: negativa, deterministica FRA PROCESSI, senza collisioni.

Il test di determinismo gira in un SOTTOPROCESSO apposta: hash() di Python e'
randomizzato per processo (PYTHONHASHSEED), quindi un test che chiama due volte
la funzione nello stesso processo PASSA anche con un'implementazione rotta che
darebbe numeri diversi a ogni riavvio del worker.
"""
import subprocess
import sys

from app.services.inbox_browser.targa import e_provvisoria, targa_provvisoria


def test_sempre_negativa():
    for u in ("lerocchette", "modando__palermo", "a", "x" * 200):
        assert targa_provvisoria(u) < 0


def test_deterministica_nello_stesso_processo():
    assert targa_provvisoria("lerocchette") == targa_provvisoria("lerocchette")


def test_deterministica_FRA_PROCESSI():
    """La guardia vera: hash() randomizzato passerebbe il test precedente."""
    codice = (
        "import sys; sys.path.insert(0, '.');"
        "from app.services.inbox_browser.targa import targa_provvisoria;"
        "print(targa_provvisoria('lerocchette'))"
    )
    valori = set()
    for _ in range(3):
        out = subprocess.run(
            [sys.executable, "-c", codice], capture_output=True, text=True, cwd=".",
        )
        assert out.returncode == 0, out.stderr
        valori.add(out.stdout.strip())
    assert len(valori) == 1, f"targa diversa fra processi: {valori}"


def test_normalizza_maiuscole_e_chiocciola():
    """Gli username in DB hanno gia' la chiocciola su alcuni account."""
    base = targa_provvisoria("lerocchette")
    assert targa_provvisoria("LeRocchette") == base
    assert targa_provvisoria("@lerocchette") == base
    assert targa_provvisoria("  lerocchette  ") == base


def test_username_diversi_targhe_diverse():
    n = 5000
    targhe = {targa_provvisoria(f"utente_{i}") for i in range(n)}
    assert len(targhe) == n, "collisione fra targhe provvisorie"


def test_riconoscimento_provvisoria():
    assert e_provvisoria(targa_provvisoria("lerocchette")) is True
    assert e_provvisoria(76561234567) is False   # pk reale Instagram
    assert e_provvisoria(0) is False


def test_sta_nel_bigint():
    """63 bit negati: deve stare in un BIGINT firmato."""
    for i in range(2000):
        t = targa_provvisoria(f"u{i}")
        assert -(2 ** 63) < t < 0
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_targa.py -q -p no:cacheprovider`
Atteso: FAIL con `ModuleNotFoundError: No module named 'app.services.inbox_browser'`

- [ ] **Step 3: Implementare**

```python
# backend/app/services/inbox_browser/targa.py
"""Targa provvisoria per i contatti raccolti dal browser.

Il canale browser non conosce il pk Instagram: dalla pagina del thread si ricava
lo username, non il numero (misurato — l'unico numero lungo accanto allo username
e' un segnaposto costante). Ma `ig_user_id` non e' un campo qualunque: e' sotto
UniqueConstraint(campaign_id, ig_user_id) ed e' la chiave di prenotazione
cross-account che impedisce a due account di scrivere alla stessa persona.

Quindi si assegna una targa PROVVISORIA, sostituita con quella vera durante
l'arricchimento (che naviga per username e riporta il pk).

NEGATIVA per costruzione: Instagram non assegna pk negativi, quindi la collisione
con una targa reale e' impossibile, non improbabile.

SHA-256 e non hash(): hash() e' randomizzato per processo (PYTHONHASHSEED), quindi
darebbe una targa diversa a ogni riavvio del worker -> una riga duplicata per ogni
riavvio. E non crc32: 32 bit collidono con probabilita' ~10^-3 su 3000 contatti, in
uno spazio che GlobalContact condivide fra TUTTE le campagne.
"""
from __future__ import annotations

import hashlib

# 63 bit: il valore negato sta sempre in un BIGINT firmato.
_MASCHERA = (1 << 63) - 1


def normalizza_username(username: str) -> str:
    """Minuscolo, senza chiocciola iniziale, senza spazi ai bordi.

    La chiocciola non e' teorica: alcuni account in DB hanno lo username salvato
    come '@michele.carozza'.
    """
    return (username or "").strip().lstrip("@").lower()


def targa_provvisoria(username: str) -> int:
    """Numero negativo stabile derivato dallo username. Mai zero."""
    normale = normalizza_username(username)
    digest = hashlib.sha256(normale.encode("utf-8")).digest()
    valore = int.from_bytes(digest[:8], "big") & _MASCHERA
    return -(valore or 1)   # il caso valore==0 e' irraggiungibile in pratica, ma 0 non e' negativo


def e_provvisoria(ig_user_id: int | None) -> bool:
    """True se la targa e' una nostra provvisoria (negativa), non un pk reale."""
    return ig_user_id is not None and ig_user_id < 0
```

- [ ] **Step 4: Eseguire i test**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_targa.py -q -p no:cacheprovider`
Atteso: PASS (7 test)

- [ ] **Step 5: Prova del nove — il test fra processi deve discriminare**

Sostituire temporaneamente il corpo di `targa_provvisoria` con l'implementazione ingenua:

```python
    return -(abs(hash(normalizza_username(username))) or 1)
```

Rieseguire: `test_deterministica_nello_stesso_processo` **passa** (è il punto), `test_deterministica_FRA_PROCESSI` **fallisce**. Se non fallisce, il test non discrimina e va corretto prima di proseguire. Poi ripristinare SHA-256.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/inbox_browser/ backend/tests/test_inbox_browser_targa.py
git commit -m "feat(inbox-browser): targa provvisoria negativa, deterministica fra processi"
```

---

### Task 3: Testo — normalizzazione, stringhe localizzate, parsing

**Files:**
- Create: `backend/app/services/inbox_browser/testo.py`
- Test: `backend/tests/test_inbox_browser_testo.py`

**Interfaces:**
- Produces:
  - `normalizza_nome(nome: str | None) -> str`
  - `e_segnaposto(nome: str) -> bool`
  - `LINGUE: dict[str, dict[str, str]]`
  - `analizza_riga_lista(testo_riga: str, lingua: str) -> RigaLista` (dataclass: `nome`, `anteprima`, `ultimo_nostro: bool | None`, `data_relativa`)
  - `estrai_username_thread(href_list: list[str], propri: set[str]) -> str | None`
  - `estrai_ultimo_messaggio(testo_pagina: str, lingua: str) -> str | None`

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_testo.py
"""Parsing e normalizzazione: la parte che sbaglia in silenzio.

Il caso peggiore dell'intero modulo e' qui: se il prefisso "Tu:" non viene
riconosciuto (perche' l'interfaccia e' in inglese), OGNI chat risulta "ha
risposto". Nessun errore, solo dati falsi.
"""
import pytest

from app.services.inbox_browser.testo import (
    LINGUE, analizza_riga_lista, e_segnaposto, estrai_ultimo_messaggio,
    estrai_username_thread, normalizza_nome,
)


# ── normalizzazione ────────────────────────────────────────────────────────
def test_normalizza_maiuscole_e_spazi():
    assert normalizza_nome("  Bruzzo   Abbigliamento ") == normalizza_nome("bruzzo abbigliamento")


def test_normalizza_rimuove_emoji_e_spazi_invisibili():
    assert normalizza_nome("Fashion​Style \U0001F3AF") == normalizza_nome("FashionStyle")


def test_normalizza_none_e_vuoto():
    assert normalizza_nome(None) == ""
    assert normalizza_nome("   ") == ""


# ── segnaposto multilingua ─────────────────────────────────────────────────
@pytest.mark.parametrize("nome", ["Utente Instagram", "utente instagram", "Instagram User", "INSTAGRAM USER"])
def test_segnaposto_riconosciuto_in_due_lingue(nome):
    assert e_segnaposto(nome) is True


@pytest.mark.parametrize("nome", ["Bruzzo Abbigliamento", "Patrizia Salvia", "Instagram Marketing Srl"])
def test_nome_vero_non_e_segnaposto(nome):
    assert e_segnaposto(nome) is False


# ── riga della lista ───────────────────────────────────────────────────────
def test_riga_con_prefisso_nostro_italiano():
    riga = "KIDS Mstore Civitanova Marche\nTu: Procedo con i consigli?\n22 sett"
    r = analizza_riga_lista(riga, "it")
    assert r.nome == "KIDS Mstore Civitanova Marche"
    assert r.ultimo_nostro is True
    assert r.data_relativa == "22 sett"


def test_riga_con_prefisso_nostro_inglese():
    riga = "KIDS Mstore\nYou: Shall I proceed?\n3w"
    r = analizza_riga_lista(riga, "en")
    assert r.ultimo_nostro is True


def test_riga_senza_prefisso_ha_risposto():
    riga = "Bruzzo Abbigliamento\nGrazie siamo gia' seguiti\n2 sett"
    r = analizza_riga_lista(riga, "it")
    assert r.ultimo_nostro is False


def test_LINGUA_SBAGLIATA_non_deve_mentire():
    """Il fallimento piu' insidioso: riga italiana letta come inglese.

    Deve dichiarare 'non lo so' (None), MAI 'ha risposto' (False): un False
    silenzioso classificherebbe ogni chat come risposta.
    """
    riga = "KIDS Mstore\nTu: Procedo con i consigli?\n22 sett"
    r = analizza_riga_lista(riga, "en")
    assert r.ultimo_nostro is None, "con la lingua sbagliata deve ammettere di non sapere"


def test_lingua_non_prevista_solleva():
    with pytest.raises(KeyError):
        analizza_riga_lista("qualcosa", "de")


# ── thread aperto ──────────────────────────────────────────────────────────
def test_username_thread_ignora_i_link_di_servizio():
    href = ["/reels/", "/explore/", "/claudio.abbigliamentovincente/", "/lerocchettebyelena/"]
    propri = {"claudio.abbigliamentovincente"}
    assert estrai_username_thread(href, propri) == "lerocchettebyelena"


def test_username_thread_nessun_candidato():
    assert estrai_username_thread(["/reels/", "/explore/"], set()) is None


def test_username_thread_scarta_se_ambiguo():
    """Piu' candidati = thread di gruppo o menzione: meglio nessuno che quello sbagliato."""
    href = ["/reels/", "/tizio/", "/caio/"]
    assert estrai_username_thread(href, set()) is None


def test_estrai_ultimo_messaggio_si_ferma_al_campo_di_scrittura():
    pagina = (
        "modando__palermo\nVisualizza profilo\n9 feb 2026, 20:28\n"
        "Ciao! Stavo guardando il vostro profilo.\n"
        "Grazie siamo gia' seguiti\n"
        "Scrivi un messaggio..."
    )
    assert estrai_ultimo_messaggio(pagina, "it") == "Grazie siamo gia' seguiti"


def test_estrai_ultimo_messaggio_senza_delimitatore():
    assert estrai_ultimo_messaggio("solo\nrighe\nsparse", "it") is None
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_testo.py -q -p no:cacheprovider`
Atteso: FAIL con `ModuleNotFoundError` su `testo`

- [ ] **Step 3: Implementare**

```python
# backend/app/services/inbox_browser/testo.py
"""Parsing del testo dell'inbox web: funzioni pure, nessun browser, nessun DB.

Qui vive il fallimento piu' insidioso del modulo. Le stringhe che leggiamo
dipendono dalla LINGUA DELL'INTERFACCIA DELL'ACCOUNT, non da una nostra
impostazione. Se il prefisso "Tu:" non viene riconosciuto perche' l'account e' in
inglese, OGNI chat risulta "ha risposto": nessun errore, solo dati falsi che poi
guidano anche i diversivi anti-ban.

Per questo `ultimo_nostro` e' un tri-stato: True / False / None. None significa
"non lo so" e non va mai confuso con False.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

LINGUE: dict[str, dict[str, str]] = {
    "it": {
        "prefisso_nostro": "Tu:",
        "segnaposto": "utente instagram",
        "campo_scrittura": "Scrivi un messaggio...",
    },
    "en": {
        "prefisso_nostro": "You:",
        "segnaposto": "instagram user",
        "campo_scrittura": "Message...",
    },
}

_SEGNAPOSTO = {v["segnaposto"] for v in LINGUE.values()}
_INVISIBILI = re.compile(r"[​-‏ - ﻿]")
_SPAZI = re.compile(r"\s+")


def normalizza_nome(nome: str | None) -> str:
    """Forma canonica per il confronto: minuscolo, senza emoji, spazi compattati.

    Gli emoji vanno tolti perche' Instagram li lascia nei nomi profilo e la
    stessa persona puo' comparire con o senza a seconda di dove leggiamo.
    """
    if not nome:
        return ""
    testo = _INVISIBILI.sub("", str(nome))
    testo = "".join(c for c in testo if unicodedata.category(c) not in ("So", "Sk", "Cf"))
    return _SPAZI.sub(" ", testo).strip().lower()


def e_segnaposto(nome: str | None) -> bool:
    """Profilo cancellato o disattivato: si ignora senza aprire la chat."""
    return normalizza_nome(nome) in _SEGNAPOSTO


@dataclass
class RigaLista:
    nome: str | None
    anteprima: str | None
    ultimo_nostro: bool | None   # None = lingua non riconosciuta, NON "ha risposto"
    data_relativa: str | None


def analizza_riga_lista(testo_riga: str, lingua: str) -> RigaLista:
    """Scompone il testo di una riga della lista chat.

    Solleva KeyError se la lingua non e' prevista: meglio fermarsi che indovinare.
    """
    voci = LINGUE[lingua]
    righe = [r.strip() for r in (testo_riga or "").split("\n") if r.strip()]
    if not righe:
        return RigaLista(None, None, None, None)

    nome = righe[0]
    anteprima = righe[1] if len(righe) > 1 else None
    data = righe[-1] if len(righe) > 2 else None

    ultimo_nostro: bool | None = None
    if anteprima:
        if anteprima.startswith(voci["prefisso_nostro"]):
            ultimo_nostro = True
        elif any(anteprima.startswith(v["prefisso_nostro"]) for v in LINGUE.values()):
            # Il prefisso c'e' ma e' di un'ALTRA lingua: l'interfaccia non e'
            # quella che credevamo. Dichiarare False qui significherebbe marcare
            # come "ha risposto" un messaggio nostro.
            ultimo_nostro = None
        else:
            ultimo_nostro = False

    return RigaLista(nome=nome, anteprima=anteprima, ultimo_nostro=ultimo_nostro, data_relativa=data)


def estrai_username_thread(href_list: list[str], propri: set[str]) -> str | None:
    """Lo username dell'interlocutore dagli href a segmento singolo.

    Ritorna None se i candidati sono zero o PIU' DI UNO: piu' candidati significa
    thread di gruppo, menzione o post condiviso, e prendere "l'ultimo" salverebbe
    la persona sbagliata senza nessun errore.
    """
    servizio = {"reels", "explore", "direct", "stories", "p", "accounts"}
    candidati = []
    for href in href_list or []:
        parti = [p for p in (href or "").split("/") if p]
        if len(parti) != 1:
            continue
        u = parti[0].lower()
        if u in servizio or u in {p.lower().lstrip("@") for p in propri}:
            continue
        if u not in candidati:
            candidati.append(u)
    return candidati[0] if len(candidati) == 1 else None


def estrai_ultimo_messaggio(testo_pagina: str, lingua: str) -> str | None:
    """L'ultimo messaggio della conversazione: la riga prima del campo di scrittura."""
    delimitatore = LINGUE[lingua]["campo_scrittura"]
    righe = [r.strip() for r in (testo_pagina or "").split("\n") if r.strip()]
    try:
        i = len(righe) - 1 - righe[::-1].index(delimitatore)
    except ValueError:
        return None
    return righe[i - 1] if i > 0 else None
```

- [ ] **Step 4: Eseguire i test**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_testo.py -q -p no:cacheprovider`
Atteso: PASS (15 test)

- [ ] **Step 5: Prova del nove sul caso peggiore**

In `analizza_riga_lista`, sostituire il ramo tri-stato con la versione ingenua:

```python
        ultimo_nostro = anteprima.startswith(voci["prefisso_nostro"])
```

Rieseguire: `test_LINGUA_SBAGLIATA_non_deve_mentire` deve **fallire** (otterrebbe `False` invece di `None`). È il test che protegge dal fallimento silenzioso: se non fallisce, non serve a niente. Poi ripristinare.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/inbox_browser/testo.py backend/tests/test_inbox_browser_testo.py
git commit -m "feat(inbox-browser): parsing testo con stringhe localizzate e tri-stato sull'autore"
```

---

### Task 4: Riconoscimento — chi è già in archivio

**Files:**
- Create: `backend/app/services/inbox_browser/riconoscimento.py`
- Test: `backend/tests/test_inbox_browser_riconoscimento.py`

**Interfaces:**
- Consumes: `normalizza_nome`, `e_segnaposto` da Task 3.
- Produces:
  - `class ArchivioNomi`: `__init__(self, nomi: list[str | None])`, `e_riconosciuto(self, nome: str | None) -> bool`, `aggiungi(self, nome: str | None) -> None`
  - `class ContatoreZona`: attributo `zona`, metodo `registra(self, riconosciuto: bool) -> str` che ritorna `'piena'` o `'rapida'`

**Regola fondante da rispettare:** il riconoscimento decide **solo il ritmo**. Non decide se aprire, e non autorizza mai a scrivere.

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_riconoscimento.py
"""Riconoscimento per nome e contatore di zona.

Il difetto che questi test proteggono e' quello che affossava il disegno
precedente: in cima alla lista ci sono i DM appena inviati (tutti noti), quindi
il contatore va subito a 10. Se da quel momento si smettesse di aprire, il motore
raccoglierebbe ZERO a regime.
"""
from app.services.inbox_browser.riconoscimento import ArchivioNomi, ContatoreZona


def test_nome_presente_e_riconosciuto():
    a = ArchivioNomi(["Bruzzo Abbigliamento", "Patrizia Salvia"])
    assert a.e_riconosciuto("bruzzo  abbigliamento") is True


def test_nome_assente_non_e_riconosciuto():
    a = ArchivioNomi(["Bruzzo Abbigliamento"])
    assert a.e_riconosciuto("Max Fashion") is False


def test_nome_ambiguo_non_vale_come_riconoscimento():
    """Due schede con lo stesso nome: non possiamo sapere quale sia."""
    a = ArchivioNomi(["Fashion Style", "Fashion Style", "Bruzzo"])
    assert a.e_riconosciuto("Fashion Style") is False


def test_nomi_vuoti_non_creano_un_falso_riconoscimento():
    """I contatti raccolti via API hanno full_name=None: se collassassero tutti
    sulla stringa vuota, una riga senza nome risulterebbe 'nota'."""
    a = ArchivioNomi([None, None, None, "Bruzzo"])
    assert a.e_riconosciuto(None) is False
    assert a.e_riconosciuto("") is False


def test_segnaposto_mai_riconosciuto():
    a = ArchivioNomi(["Utente Instagram", "Bruzzo"])
    assert a.e_riconosciuto("Utente Instagram") is False


def test_parte_in_zona_piena():
    assert ContatoreZona().zona == "piena"


def test_dieci_riconosciuti_passano_a_rapida():
    c = ContatoreZona()
    for _ in range(9):
        assert c.registra(True) == "piena"
    assert c.registra(True) == "rapida"


def test_un_solo_non_riconosciuto_azzera_il_contatore():
    c = ContatoreZona()
    for _ in range(9):
        c.registra(True)
    c.registra(False)
    for _ in range(9):
        assert c.registra(True) == "piena"


def test_tre_sconosciuti_su_dieci_tornano_a_piena():
    c = ContatoreZona()
    for _ in range(10):
        c.registra(True)
    assert c.zona == "rapida"
    c.registra(False)
    c.registra(True)
    c.registra(False)
    assert c.registra(False) == "piena"


def test_due_sconosciuti_su_dieci_restano_in_rapida():
    c = ContatoreZona()
    for _ in range(10):
        c.registra(True)
    c.registra(False)
    for _ in range(8):
        c.registra(True)
    assert c.registra(False) == "rapida"


def test_la_zona_non_decide_se_aprire():
    """Guardia sull'invariante piu' importante del modulo.

    ContatoreZona NON deve esporre nessun metodo del tipo 'devo aprire?': la
    regola fondante e' che una riga non riconosciuta si apre SEMPRE. Se qualcuno
    aggiunge quel metodo, sta reintroducendo il difetto che azzerava la raccolta.
    """
    metodi = {m for m in dir(ContatoreZona) if not m.startswith("_")}
    vietati = {"deve_aprire", "salta", "apre", "skip"}
    assert not (metodi & vietati), f"la zona non decide le aperture: {metodi & vietati}"
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_riconoscimento.py -q -p no:cacheprovider`
Atteso: FAIL con `ModuleNotFoundError` su `riconoscimento`

- [ ] **Step 3: Implementare**

```python
# backend/app/services/inbox_browser/riconoscimento.py
"""Riconoscimento per nome visualizzato e contatore di zona.

REGOLA FONDANTE: il riconoscimento decide SOLO il ritmo. Una riga non
riconosciuta si apre SEMPRE, in qualunque zona.

Il disegno precedente aveva una modalita' che non apriva niente, con rientro a 3
sconosciuti su 10. Due revisori indipendenti hanno dimostrato che raccoglieva
ZERO a regime: la lista e' ordinata per messaggio piu' recente, in cima ci sono i
~100 DM appena inviati (tutti noti), il contatore arrivava a 10 entro le prime
dieci righe, e da li' 1-2 sconosciuti ogni 10 non superavano mai la soglia.
"""
from __future__ import annotations

from collections import Counter, deque

from app.services.inbox_browser.testo import e_segnaposto, normalizza_nome

NOTI_PER_ZONA_RAPIDA = 10
FINESTRA = 10
SCONOSCIUTI_PER_ZONA_PIENA = 3


class ArchivioNomi:
    """I nomi gia' in archivio, in forma normalizzata.

    Un nome vale come riconoscimento solo se e' UNICO: i nomi visualizzati di
    Instagram non sono univoci, e riconoscere per un nome ripetuto significa
    saltare una persona diversa credendola gia' presa.
    """

    def __init__(self, nomi: list[str | None]):
        conteggio = Counter(n for n in (normalizza_nome(x) for x in nomi or []) if n)
        self._unici = {n for n, k in conteggio.items() if k == 1}

    def e_riconosciuto(self, nome: str | None) -> bool:
        normale = normalizza_nome(nome)
        if not normale or e_segnaposto(nome):
            return False
        return normale in self._unici

    def aggiungi(self, nome: str | None) -> None:
        """Un nome appena raccolto entra nell'archivio."""
        normale = normalizza_nome(nome)
        if normale and not e_segnaposto(nome):
            self._unici.add(normale)


class ContatoreZona:
    """Governa SOLO il ritmo: 'piena' (si aprono chat nuove) o 'rapida'
    (si attraversa una zona gia' lavorata).

    Deliberatamente NON espone nessun metodo che dica se aprire una riga.
    """

    def __init__(self) -> None:
        self.zona = "piena"
        self._noti_di_fila = 0
        self._finestra: deque[bool] = deque(maxlen=FINESTRA)

    def registra(self, riconosciuto: bool) -> str:
        self._finestra.append(riconosciuto)

        if self.zona == "piena":
            self._noti_di_fila = self._noti_di_fila + 1 if riconosciuto else 0
            if self._noti_di_fila >= NOTI_PER_ZONA_RAPIDA:
                self.zona = "rapida"
                self._finestra.clear()
        else:
            sconosciuti = sum(1 for r in self._finestra if not r)
            if sconosciuti >= SCONOSCIUTI_PER_ZONA_PIENA:
                self.zona = "piena"
                self._noti_di_fila = 0
                self._finestra.clear()

        return self.zona
```

- [ ] **Step 4: Eseguire i test**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_riconoscimento.py -q -p no:cacheprovider`
Atteso: PASS (11 test)

- [ ] **Step 5: Prova del nove**

In `ArchivioNomi.__init__` togliere il filtro sull'unicità, sostituendo con `self._unici = set(conteggio)`: `test_nome_ambiguo_non_vale_come_riconoscimento` deve **fallire**. Ripristinare.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/inbox_browser/riconoscimento.py backend/tests/test_inbox_browser_riconoscimento.py
git commit -m "feat(inbox-browser): riconoscimento per nome unico, la zona governa solo il ritmo"
```

---

### Task 5: Il ritmo per zona

**Files:**
- Create: `backend/app/services/inbox_browser/ritmo.py`
- Test: `backend/tests/test_inbox_browser_ritmo.py`

**Interfaces:**
- Produces: `campiona_pausa(zona: str) -> float` (secondi), `PARAMETRI: dict[str, dict]`

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_ritmo.py
"""Il ritmo: distribuzione troncata, mai clampata, e differenziata per zona.

Il clamp non scarta la coda: la SCHIACCIA sui bound. Sul motore API ci finiva il
45% dei ritardi, su due valori fissi. Due picchi netti sono una firma piu'
riconoscibile di un ritardo costante.
"""
import statistics
from collections import Counter

import pytest

from app.services.inbox_browser.ritmo import PARAMETRI, campiona_pausa

N = 4000


@pytest.fixture(scope="module")
def campioni():
    return {z: [campiona_pausa(z) for _ in range(N)] for z in ("piena", "rapida")}


@pytest.mark.parametrize("zona", ["piena", "rapida"])
def test_dentro_i_bound(campioni, zona):
    p = PARAMETRI[zona]
    lo = min(p["normale"][0], p["sosta"][0], p["stacco"][0])
    hi = max(p["normale"][1], p["sosta"][1], p["stacco"][1])
    assert all(lo <= d <= hi for d in campioni[zona])


@pytest.mark.parametrize("zona", ["piena", "rapida"])
def test_nessuna_pila_su_un_singolo_valore(campioni, zona):
    """Il difetto del clamp: niente deve accumularsi su un valore preciso."""
    comuni = Counter(round(d, 3) for d in campioni[zona]).most_common(1)[0][1]
    assert comuni / N < 0.02, f"{comuni / N:.1%} dei valori su un unico punto"


def test_la_zona_rapida_e_davvero_piu_rapida(campioni):
    assert statistics.median(campioni["rapida"]) < statistics.median(campioni["piena"]) / 2


@pytest.mark.parametrize("zona", ["piena", "rapida"])
def test_varianza_ampia(campioni, zona):
    d = campioni[zona]
    assert statistics.stdev(d) / statistics.mean(d) > 0.30


def test_le_tre_modalita_compaiono_tutte(campioni):
    p = PARAMETRI["piena"]
    d = campioni["piena"]
    assert any(x <= p["normale"][1] for x in d)
    assert any(p["sosta"][0] <= x <= p["sosta"][1] for x in d)
    assert any(x >= p["stacco"][0] for x in d)


def test_zona_sconosciuta_solleva():
    with pytest.raises(KeyError):
        campiona_pausa("turbo")
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_ritmo.py -q -p no:cacheprovider`
Atteso: FAIL con `ModuleNotFoundError` su `ritmo`

- [ ] **Step 3: Implementare**

```python
# backend/app/services/inbox_browser/ritmo.py
"""Pause fra una chat e l'altra, differenziate per zona.

Il ritmo NON e' uniforme sulla lista: pause piene dove si aprono chat nuove,
ritmo rapido dove si attraversa una zona gia' lavorata. E' quello che fa una
persona: scorre in fretta cio' che ha gia' visto e si ferma su cio' che le
interessa. Il throughput dipende quasi per intero da qui — l'apertura di una chat
costa mezzo secondo (misurato), le pause costano dieci volte tanto.

La distribuzione e' lognormale TRONCATA per riestrazione, mai clampata: il clamp
accumula la coda esattamente sui bound (misurato sul motore API: il 45% dei
ritardi finiva su due valori fissi), e due picchi netti sono una firma piu'
riconoscibile di un ritardo costante.
"""
from __future__ import annotations

import math
import random

PARAMETRI: dict[str, dict] = {
    # zona piena: si aprono chat nuove, ci si ferma a leggere
    "piena": {
        "normale": (1.0, 4.0),
        "sosta": (10.0, 30.0),
        "stacco": (120.0, 300.0),
        "p_sosta": 0.10,
        "p_stacco": 0.02,
    },
    # zona rapida: si attraversa cio' che e' gia' stato raccolto
    "rapida": {
        "normale": (0.4, 1.2),
        "sosta": (10.0, 30.0),
        "stacco": (120.0, 300.0),
        "p_sosta": 0.025,
        "p_stacco": 0.02,
    },
}

SIGMA = 0.9


def _troncata(lo: float, hi: float, sigma: float = SIGMA) -> float:
    """Lognormale troncata su [lo, hi] per riestrazione.

    Mediana sulla media geometrica sqrt(lo*hi): centro naturale in scala
    logaritmica, quindi il troncamento taglia code simmetriche e accetta circa
    due volte su tre.
    """
    if hi <= lo:
        return float(lo)
    mediana = math.sqrt(lo * hi)
    for _ in range(20):
        d = random.lognormvariate(0, sigma) * mediana
        if lo <= d <= hi:
            return d
    return mediana


def campiona_pausa(zona: str) -> float:
    """Secondi di attesa prima della prossima riga. Solleva KeyError su zona ignota."""
    p = PARAMETRI[zona]
    sorte = random.random()
    if sorte < p["p_stacco"]:
        return _troncata(*p["stacco"])
    if sorte < p["p_stacco"] + p["p_sosta"]:
        return _troncata(*p["sosta"])
    return _troncata(*p["normale"])
```

- [ ] **Step 4: Eseguire i test**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_ritmo.py -q -p no:cacheprovider`
Atteso: PASS (9 test)

- [ ] **Step 5: Prova del nove**

Sostituire il corpo di `_troncata` con la versione clampata:

```python
    return min(hi, max(lo, random.lognormvariate(0, sigma) * ((lo + hi) / 2)))
```

Rieseguire: `test_nessuna_pila_su_un_singolo_valore` deve **fallire** su entrambe le zone. Ripristinare.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/inbox_browser/ritmo.py backend/tests/test_inbox_browser_ritmo.py
git commit -m "feat(inbox-browser): ritmo per zona, lognormale troncata mai clampata"
```

---

### Task 6: Salvataggio — dedup per username e fusione

**Files:**
- Create: `backend/app/services/inbox_browser/salvataggio.py`
- Test: `backend/tests/test_inbox_browser_salvataggio.py`

**Interfaces:**
- Consumes: `targa_provvisoria`, `e_provvisoria`, `normalizza_username` da Task 2.
- Produces:
  - `@dataclass DatiContatto`: `username: str`, `nome: str | None`, `last_message_at: datetime | None`, `last_message_from: str | None`, `last_message_text: str | None`
  - `async def salva_contatto(db, campaign_id: str, dati: DatiContatto) -> str` → `'creato'` | `'aggiornato'`
  - `def stato_vincente(a: FollowerStatus, b: FollowerStatus) -> FollowerStatus`

**Perché il dedup è sullo username e non sulla targa:** i contatti raccolti via API hanno `full_name=None` (`scrape_inbox.py:179`), non sono riconoscibili dal nome, quindi le loro chat vengono riaperte. Arrivando con una targa provvisoria diversa dalla targa vera che hanno già, un dedup basato sulla targa **non scatterebbe** e produrrebbe una riga duplicata per ogni contatto già presente — e su una campagna con arricchimento attivo quella riga può portare a un **secondo DM**.

> **Nota per l'implementatore**: le fixture per DB e campagna esistono già nella suite. Cercarle in `backend/tests/conftest.py` e riusare esattamente quelle, senza inventarne di nuove. Adattare i nomi dei parametri delle funzioni di test a quelle fixture.

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_salvataggio.py
"""Dedup per username, fusione, precedenza di stato.

La fusione non e' un caso limite: e' l'esito NORMALE, perche' ogni contatto
raccolto via API ha full_name=None e verra' riaperto.
"""
from datetime import datetime

import pytest
from sqlalchemy import select

from app.models.follower import Follower, FollowerStatus
from app.services.inbox_browser.salvataggio import (
    DatiContatto, salva_contatto, stato_vincente,
)
from app.services.inbox_browser.targa import targa_provvisoria


def _dati(username="lerocchette", nome="Elena Rocchetti", testo="ciao"):
    return DatiContatto(
        username=username,
        nome=nome,
        last_message_at=datetime(2026, 8, 1, 12, 0),
        last_message_from="them",
        last_message_text=testo,
    )


@pytest.mark.asyncio
async def test_primo_salvataggio_crea(db_session, campagna):
    esito = await salva_contatto(db_session, campagna.id, _dati())
    assert esito == "creato"
    f = (await db_session.execute(
        select(Follower).where(Follower.campaign_id == campagna.id)
    )).scalar_one()
    assert f.username == "lerocchette"
    assert f.ig_user_id == targa_provvisoria("lerocchette")
    assert f.source_channel == "browser"


@pytest.mark.asyncio
async def test_secondo_salvataggio_aggiorna_non_duplica(db_session, campagna):
    await salva_contatto(db_session, campagna.id, _dati(testo="primo"))
    esito = await salva_contatto(db_session, campagna.id, _dati(testo="secondo"))
    assert esito == "aggiornato"
    righe = (await db_session.execute(
        select(Follower).where(Follower.campaign_id == campagna.id)
    )).scalars().all()
    assert len(righe) == 1
    assert righe[0].last_message_text == "secondo"


@pytest.mark.asyncio
async def test_contatto_gia_raccolto_via_API_non_viene_duplicato(db_session, campagna):
    """Il caso che rende la fusione la norma: targa VERA gia' in DB, nessun nome."""
    db_session.add(Follower(
        campaign_id=campagna.id, ig_user_id=76561234567, username="lerocchette",
        full_name=None, status=FollowerStatus.pending, source_channel="api",
    ))
    await db_session.commit()

    esito = await salva_contatto(db_session, campagna.id, _dati())
    assert esito == "aggiornato"
    righe = (await db_session.execute(
        select(Follower).where(Follower.campaign_id == campagna.id)
    )).scalars().all()
    assert len(righe) == 1, "una riga duplicata qui puo' portare a un SECONDO DM"
    assert righe[0].ig_user_id == 76561234567, "la targa vera non si sovrascrive con una provvisoria"
    assert righe[0].full_name == "Elena Rocchetti", "il nome mancante viene riempito"


@pytest.mark.asyncio
async def test_uno_stato_avanzato_non_torna_indietro(db_session, campagna):
    """Un contatto gia' contattato NON deve tornare mandabile."""
    db_session.add(Follower(
        campaign_id=campagna.id, ig_user_id=76561234567, username="lerocchette",
        status=FollowerStatus.sent, source_channel="api",
    ))
    await db_session.commit()

    await salva_contatto(db_session, campagna.id, _dati())
    f = (await db_session.execute(
        select(Follower).where(Follower.campaign_id == campagna.id)
    )).scalar_one()
    assert f.status == FollowerStatus.sent, "un sent tornato pending riceve un secondo DM"


@pytest.mark.asyncio
async def test_username_normalizzato_nel_confronto(db_session, campagna):
    await salva_contatto(db_session, campagna.id, _dati(username="lerocchette"))
    esito = await salva_contatto(db_session, campagna.id, _dati(username="LeRocchette"))
    assert esito == "aggiornato"


@pytest.mark.asyncio
async def test_username_vuoto_solleva(db_session, campagna):
    with pytest.raises(ValueError):
        await salva_contatto(db_session, campagna.id, _dati(username="  "))


def test_precedenza_di_stato():
    assert stato_vincente(FollowerStatus.pending, FollowerStatus.sent) == FollowerStatus.sent
    assert stato_vincente(FollowerStatus.sent, FollowerStatus.pending) == FollowerStatus.sent
    assert stato_vincente(FollowerStatus.pending, FollowerStatus.pending) == FollowerStatus.pending
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_salvataggio.py -q -p no:cacheprovider`
Atteso: FAIL con `ModuleNotFoundError` su `salvataggio`

- [ ] **Step 3: Implementare**

```python
# backend/app/services/inbox_browser/salvataggio.py
"""Salvataggio dei contatti raccolti dal browser: dedup per USERNAME.

Perche' non per targa: i contatti raccolti via API hanno full_name=None
(scrape_inbox.py:179), quindi non sono riconoscibili dal nome e le loro chat
vengono riaperte. Arrivano con una targa provvisoria diversa dalla targa vera che
hanno gia' in archivio: un dedup sulla targa non scatterebbe e creerebbe una riga
duplicata per OGNI contatto gia' presente. Su una campagna con arricchimento
attivo, quella riga duplicata puo' portare a un secondo DM alla stessa persona.

Questo NON sostituisce UniqueConstraint(campaign_id, ig_user_id), che resta a
proteggere il percorso API: sono due reti a maglie diverse.

La lookup e' ESPLICITA, non si tenta l'INSERT lasciando parlare il vincolo:
sui due percorsi della Fase Bio quell'eccezione e' gestita in modi diversi, e in
uno dei due blocca il batch per sempre (browser_bio.py:1362 fa break senza
marcare il follower, e la selezione e' limit(1) senza ORDER BY: il giro dopo
ripesca la stessa riga).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.models.follower import Follower, FollowerStatus
from app.services.inbox_browser.targa import (
    e_provvisoria, normalizza_username, targa_provvisoria,
)

# Ordine di avanzamento: piu' avanti = vince in una fusione.
_ORDINE = {
    FollowerStatus.pending: 0,
    FollowerStatus.bio_scraped: 1,
    FollowerStatus.message_generated: 2,
    FollowerStatus.sending: 3,
    FollowerStatus.sent: 4,
    FollowerStatus.replied: 5,
    FollowerStatus.failed: 5,
    FollowerStatus.skipped: 5,
}


def stato_vincente(a: FollowerStatus, b: FollowerStatus) -> FollowerStatus:
    """In una fusione lo stato piu' avanzato vince SEMPRE.

    Un contatto gia' `sent` che tornasse `pending` riceverebbe un secondo DM.
    """
    return a if _ORDINE.get(a, 0) >= _ORDINE.get(b, 0) else b


@dataclass
class DatiContatto:
    username: str
    nome: str | None
    last_message_at: datetime | None
    last_message_from: str | None   # 'us' | 'them' | None
    last_message_text: str | None


async def salva_contatto(db, campaign_id: str, dati: DatiContatto) -> str:
    """Crea o aggiorna il contatto. Ritorna 'creato' o 'aggiornato'."""
    username = normalizza_username(dati.username)
    if not username:
        raise ValueError("username vuoto: il contatto non e' identificabile")

    esistente = (await db.execute(
        select(Follower).where(
            Follower.campaign_id == campaign_id,
            Follower.username == username,
        )
    )).scalar_one_or_none()

    if esistente is None:
        db.add(Follower(
            campaign_id=campaign_id,
            ig_user_id=targa_provvisoria(username),
            username=username,
            full_name=dati.nome,
            is_private=False,
            is_verified=False,
            status=FollowerStatus.pending,
            last_message_at=dati.last_message_at,
            last_message_from=dati.last_message_from,
            last_message_text=dati.last_message_text,
            source_channel="browser",
        ))
        await db.commit()
        return "creato"

    # Fusione: si integra, non si sovrascrive.
    if not esistente.full_name and dati.nome:
        esistente.full_name = dati.nome
    esistente.last_message_at = dati.last_message_at or esistente.last_message_at
    esistente.last_message_from = dati.last_message_from or esistente.last_message_from
    esistente.last_message_text = dati.last_message_text or esistente.last_message_text
    esistente.status = stato_vincente(esistente.status, FollowerStatus.pending)
    esistente.updated_at = datetime.utcnow()

    # La targa VERA non si tocca mai: sostituirla con una provvisoria
    # sgancerebbe il contatto da GlobalContact e dalle prenotazioni.
    if e_provvisoria(esistente.ig_user_id):
        atteso = targa_provvisoria(username)
        if esistente.ig_user_id != atteso:
            logger.info(
                f"[InboxBrowser] @{username}: targa provvisoria riallineata "
                f"({esistente.ig_user_id} -> {atteso})"
            )
            esistente.ig_user_id = atteso

    await db.commit()
    return "aggiornato"
```

- [ ] **Step 4: Eseguire i test**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_salvataggio.py -q -p no:cacheprovider`
Atteso: PASS (7 test)

- [ ] **Step 5: Prova del nove**

Sostituire la lookup per username con una per targa:

```python
    esistente = (await db.execute(select(Follower).where(
        Follower.campaign_id == campaign_id,
        Follower.ig_user_id == targa_provvisoria(username),
    ))).scalar_one_or_none()
```

Rieseguire: `test_contatto_gia_raccolto_via_API_non_viene_duplicato` deve **fallire** trovando due righe. Ripristinare.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/inbox_browser/salvataggio.py backend/tests/test_inbox_browser_salvataggio.py
git commit -m "feat(inbox-browser): dedup per username e fusione con precedenza di stato"
```

---

### Task 7: Il gate di configurazione sull'API

**Files:**
- Modify: `backend/app/api/campaigns.py` — inserire **dopo** il blocco `enrichment_level` (attualmente termina a `:347`)
- Test: `backend/tests/test_inbox_browser_gate.py`

**Interfaces:**
- Produces: `def valida_combinazione_motori(inbox_engine: str, bio_engine: str, enrichment_level: str) -> str | None` in `backend/app/services/inbox_browser/gate.py` — ritorna il messaggio d'errore, o `None` se la combinazione è valida.

**Perché il gate va sullo stato finale e non su un campo alla volta:** i tre campi sono indipendenti e applicati in sequenza sullo stesso oggetto (`campaigns.py:329`, `:338`, `:347`). Un controllo scritto su `campaign.inbox_engine` (valore in DB) si aggira con un solo PATCH `{"inbox_engine": "browser"}`, che non tocca gli altri due e lascia la campagna in uno stato incoerente.

**Perché serve anche `enrichment_level`:** la Fase Bio **non** è governata da `bio_engine` ma da `enrichment_level`, dichiarato «ortogonale» (`campaign.py:46-48`). La guardia `enrichment_blocca_la_fase_bio` sta a `scrape_bios.py:82` e fa `return None` **prima** del dispatch su `bio_engine` (`:114`). Il default sulle campagne nuove è `'none'` (`campaign.py:182-184`): senza questo gate, la configurazione di default porta la targa provvisoria fino a `GlobalContact` e al dedup anti-doppio-DM.

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_gate.py
"""Il gate sulla combinazione dei tre motori.

Il vincolo non e' su bio_engine soltanto: la Fase Bio e' governata da
enrichment_level, che e' ortogonale, e' controllato PRIMA di bio_engine
(scrape_bios.py:82 contro :114) e vale 'none' di default sulle campagne nuove.
"""
import pytest

from app.services.inbox_browser.gate import valida_combinazione_motori


def test_combinazione_valida():
    assert valida_combinazione_motori("browser", "browser", "contacts") is None
    assert valida_combinazione_motori("browser", "browser", "bio") is None


def test_inbox_api_non_e_vincolato():
    """Il motore API resta libero: nessuna regressione sul percorso esistente."""
    assert valida_combinazione_motori("api", "api", "none") is None
    assert valida_combinazione_motori("api", "browser", "none") is None


def test_browser_con_arricchimento_none_e_rifiutato():
    """Il buco trovato in revisione: e' la configurazione DI DEFAULT."""
    msg = valida_combinazione_motori("browser", "browser", "none")
    assert msg is not None
    assert "arricchimento" in msg.lower()


def test_browser_con_bio_engine_api_e_rifiutato():
    msg = valida_combinazione_motori("browser", "api", "contacts")
    assert msg is not None
    assert "browser" in msg.lower()


def test_entrambi_gli_errori_insieme_producono_un_messaggio():
    assert valida_combinazione_motori("browser", "api", "none") is not None


@pytest.mark.parametrize("livello", ["bio", "contacts"])
def test_tutti_i_livelli_non_none_sono_ammessi(livello):
    assert valida_combinazione_motori("browser", "browser", livello) is None
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_gate.py -q -p no:cacheprovider`
Atteso: FAIL con `ModuleNotFoundError` su `gate`

- [ ] **Step 3: Implementare la funzione pura**

```python
# backend/app/services/inbox_browser/gate.py
"""Vincolo di configurazione fra i tre motori di una campagna.

Il motore inbox browser assegna una targa PROVVISORIA (il pk non e' ricavabile
dalla pagina). Quella targa e' un ponte: viene sostituita con la targa vera
durante l'arricchimento, che naviga per username e riporta il pk.

Se l'arricchimento non avviene, il ponte non viene mai attraversato e la targa
provvisoria arriva fino a GlobalContact e al dedup anti-doppio-DM: la stessa
persona raccolta via API in un'altra campagna avrebbe una chiave diversa e
potrebbe ricevere DUE messaggi.

Due condizioni, non una:
- enrichment_level != 'none', altrimenti la Fase Bio non parte affatto
  (scrape_bios.py:82, PRIMA del dispatch su bio_engine a :114) — ed e' il default
  sulle campagne nuove (campaign.py:182-184);
- bio_engine == 'browser', perche' l'arricchimento API interroga PER PK
  (profile_lookup.py:49, user_info_v1(pk)) e su una targa provvisoria cercherebbe
  una persona inesistente.
"""
from __future__ import annotations

from app.models.campaign import ENRICHMENT_NONE


def valida_combinazione_motori(
    inbox_engine: str, bio_engine: str, enrichment_level: str
) -> str | None:
    """Ritorna il messaggio d'errore, o None se la combinazione e' valida."""
    if inbox_engine != "browser":
        return None

    problemi: list[str] = []
    if enrichment_level == ENRICHMENT_NONE:
        problemi.append(
            "il livello di arricchimento non puo' essere 'nessuno' (serve 'bio' o 'contatti'): "
            "senza arricchimento i contatti restano con un identificativo provvisorio, "
            "che aggirerebbe la protezione contro il doppio invio alla stessa persona"
        )
    if bio_engine != "browser":
        problemi.append(
            "l'arricchimento deve avvenire via browser: quello via API interroga Instagram "
            "con l'identificativo numerico, che sui contatti raccolti dal browser non esiste ancora"
        )
    if not problemi:
        return None
    return "Campagna con raccolta inbox via browser: " + "; ".join(problemi) + "."
```

- [ ] **Step 4: Eseguire i test della funzione pura**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_gate.py -q -p no:cacheprovider`
Atteso: PASS (8 test)

- [ ] **Step 5: Innestare il gate nell'API**

In `backend/app/api/campaigns.py`, **dopo** il blocco `if data.enrichment_level is not None:` (che oggi termina con `campaign.enrichment_level = data.enrichment_level`, riga `:347`), inserire:

```python
    # Gate sulla combinazione FINALE dei tre motori, non su un campo alla volta.
    # I tre campi sono indipendenti e applicati in sequenza sullo stesso oggetto
    # (:329, :338, :347): un controllo sul valore in DB si aggirerebbe con un solo
    # PATCH {"inbox_engine": "browser"}, che non tocca gli altri due e lascia la
    # campagna incoerente.
    from app.services.inbox_browser.gate import valida_combinazione_motori
    errore_motori = valida_combinazione_motori(
        campaign.inbox_engine, campaign.bio_engine, campaign.enrichment_level,
    )
    if errore_motori:
        raise HTTPException(status_code=400, detail=errore_motori)
```

Lo stesso controllo va aggiunto in **creazione** campagna, dopo `inbox_engine=data.inbox_engine` (`:202`), sui valori del payload.

- [ ] **Step 6: Scrivere il test di integrazione sull'API**

Aggiungere in `backend/tests/test_inbox_browser_gate.py` (adattando le fixture a quelle della suite):

```python
@pytest.mark.asyncio
async def test_PATCH_singolo_non_aggira_il_gate(client, campagna_api_none):
    """Il caso che un gate scritto male lascerebbe passare.

    Campagna con inbox='api', bio='api', arricchimento='none'. Un solo PATCH che
    cambia il motore inbox deve essere RIFIUTATO: da solo produrrebbe una
    combinazione incoerente.
    """
    resp = await client.put(
        f"/api/campaigns/{campagna_api_none.id}", json={"inbox_engine": "browser"},
    )
    assert resp.status_code == 400, f"combinazione incoerente accettata: {resp.text}"


@pytest.mark.asyncio
async def test_PATCH_completo_e_accettato(client, campagna_api_none):
    resp = await client.put(
        f"/api/campaigns/{campagna_api_none.id}",
        json={"inbox_engine": "browser", "bio_engine": "browser", "enrichment_level": "contacts"},
    )
    assert resp.status_code == 200, resp.text
```

- [ ] **Step 7: Eseguire i test e la prova del nove**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_gate.py tests/test_inbox_engine_switch_adversarial.py -q -p no:cacheprovider`
Atteso: PASS — inclusi i test adversarial esistenti sul cambio motore, che **non devono** regredire.

**Prova del nove**: spostare la chiamata a `valida_combinazione_motori` *dentro* il blocco `if data.inbox_engine is not None:`, usando `data.bio_engine` invece di `campaign.bio_engine`. `test_PATCH_singolo_non_aggira_il_gate` deve **fallire**. Ripristinare.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/inbox_browser/gate.py backend/app/api/campaigns.py backend/tests/test_inbox_browser_gate.py
git commit -m "feat(inbox-browser): gate sulla combinazione finale dei tre motori"
```

---

### Task 8: La pagina — leggere righe, aprire chat, scorrere

**Files:**
- Create: `backend/app/services/inbox_browser/pagina.py`
- Test: `backend/tests/test_inbox_browser_pagina.py` (test sulle funzioni pure e su una pagina finta, **senza** browser reale)

**Interfaces:**
- Consumes: `analizza_riga_lista`, `estrai_username_thread`, `estrai_ultimo_messaggio` da Task 3.
- Produces:
  - `async def leggi_righe_visibili(page, lingua: str) -> list[RigaVisibile]` — dataclass con `indice`, `nome`, `ultimo_nostro`, `non_letta`
  - `async def apri_riga(page, indice: int, nome_atteso: str) -> str | None` — ritorna lo username, o `None` se la verifica post-click fallisce
  - `async def scorri(page) -> StatoScorrimento` — dataclass con `altezza`, `al_fondo`
  - `async def decidi_fine_lista(page, falliti_inbox: list) -> str` — `'continua'` | `'fine'` | `'piantato'`

**Vincoli non negoziabili di questo task:**
1. **Mai `element.click()`**: sempre `human_input.human_click`.
2. **Verifica post-click**: dopo aver aperto, si confronta il nome nell'intestazione del thread con quello della riga cliccata. Se non combaciano, **non si salva niente e non si avanza**. `human_click` clicca su coordinate (`human_input.py:99-107`): fra il calcolo del riquadro e la pressione passa un movimento a 5-15 passi più una pausa di 50-150 ms, e se in quel momento arriva un DM la lista scorre di una posizione e il click atterra sulla riga accanto — senza nessun errore.
3. **Mai riusare un riferimento a una riga attraverso una pausa**: fra due aperture può passare una sosta di 10-30 s o uno stacco di 2-5 minuti, e la lista è virtualizzata. La riga va **ri-risolta immediatamente prima** del click.
4. **Passo di scorrimento inferiore a una schermata** (0.6-0.8 dell'altezza visibile, randomizzato): sopra il buffer renderizzato le righe in mezzo non entrano mai nel DOM e si perdono **in silenzio**.

- [ ] **Step 1: Scrivere i test sulle funzioni pure**

```python
# backend/tests/test_inbox_browser_pagina.py
"""Interazione con la pagina: qui si testano le parti pure e la logica di
decisione. Il browser reale e' coperto dal QA agent (Task 15), non da pytest.
"""
import pytest

from app.services.inbox_browser.pagina import (
    PASSO_SCROLL_MAX, decidi_da_segnali, nome_combacia,
)


# ── verifica post-click ────────────────────────────────────────────────────
def test_nome_combacia_ignora_maiuscole_e_spazi():
    assert nome_combacia("Bruzzo  Abbigliamento", "bruzzo abbigliamento") is True


def test_nome_non_combacia_blocca():
    assert nome_combacia("Bruzzo Abbigliamento", "Max Fashion") is False


def test_nome_mancante_non_combacia_mai():
    """Meglio rinunciare a una riga che salvare dati attribuiti alla persona sbagliata."""
    assert nome_combacia(None, "Bruzzo") is False
    assert nome_combacia("Bruzzo", None) is False


# ── passo di scorrimento ───────────────────────────────────────────────────
def test_il_passo_non_supera_una_schermata():
    """Sopra il buffer renderizzato le righe si perdono IN SILENZIO."""
    assert PASSO_SCROLL_MAX <= 0.8


# ── fine lista / lento / piantato ──────────────────────────────────────────
def test_altezza_cresciuta_significa_continua():
    assert decidi_da_segnali(altezza_prima=1152, altezza_dopo=1872,
                             al_fondo=False, falliti_inbox=0, attese_esaurite=False) == "continua"


def test_altezza_ferma_ma_attese_non_esaurite_significa_continua():
    """La lentezza normale non deve mai essere scambiata per la fine."""
    assert decidi_da_segnali(altezza_prima=5112, altezza_dopo=5112,
                             al_fondo=True, falliti_inbox=0, attese_esaurite=False) == "continua"


def test_attese_esaurite_in_fondo_senza_fallimenti_e_fine():
    assert decidi_da_segnali(altezza_prima=5112, altezza_dopo=5112,
                             al_fondo=True, falliti_inbox=0, attese_esaurite=True) == "fine"


def test_richieste_inbox_fallite_significano_piantato():
    """Non si dichiara completata una lista che potrebbe avere altro sotto."""
    assert decidi_da_segnali(altezza_prima=5112, altezza_dopo=5112,
                             al_fondo=True, falliti_inbox=3, attese_esaurite=True) == "piantato"


def test_ferma_ma_non_in_fondo_e_anomalia():
    assert decidi_da_segnali(altezza_prima=5112, altezza_dopo=5112,
                             al_fondo=False, falliti_inbox=0, attese_esaurite=True) == "piantato"
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_pagina.py -q -p no:cacheprovider`
Atteso: FAIL con `ModuleNotFoundError` su `pagina`

- [ ] **Step 3: Implementare**

```python
# backend/app/services/inbox_browser/pagina.py
"""Interazione col DOM dell'inbox web.

Tre vincoli misurati sul campo, non ipotizzati:

1. LISTA VIRTUALIZZATA. Instagram tiene nel DOM solo le righe vicine al viewport
   e rimuove le altre (misurato: il conteggio righe oscilla fra 72 e 96 mentre
   l'altezza cresce in modo monotono). Scorrere a salti piu' grandi del buffer fa
   perdere righe IN SILENZIO: nessun errore, solo contatti mancanti.

2. NESSUN INDICATORE DI CARICAMENTO. Misurato: 0 spinner su 10 giri di scroll. Il
   segnale utile e' l'ALTEZZA del contenitore, che cresce a ogni caricamento
   riuscito (1152 -> 1872 -> ... -> 5112). Il numero di righe NON e' utilizzabile.

3. IL CLICK E' PER COORDINATE. human_click calcola il riquadro, muove il mouse in
   5-15 passi, attende 50-150 ms, poi preme (human_input.py:99-107). Se in quella
   finestra arriva un DM, la lista scorre di una posizione e si apre la chat
   accanto: mouse.click riesce sempre, nessun errore. Da qui la verifica
   post-click obbligatoria.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from loguru import logger

from app.browser import human_input
from app.services.inbox_browser.testo import (
    analizza_riga_lista, estrai_username_thread, normalizza_nome,
)

# Sotto una schermata: sopra il buffer renderizzato si perdono righe in silenzio.
PASSO_SCROLL_MIN = 0.6
PASSO_SCROLL_MAX = 0.8

# Attese a pazienza crescente prima di dichiarare qualcosa sulla fine lista.
ATTESE_S = (1, 2, 4, 8, 16)

_JS_RIGHE = """(nRighe) => {
    const righe = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.left < 660 && r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; });
    return righe.slice(0, nRighe).map((e, i) => ({indice: i, testo: e.innerText}));
}"""

_JS_CONTENITORE = """() => {
    let box = null, best = 0;
    for (const e of document.querySelectorAll('div')) {
        const r = e.getBoundingClientRect();
        if (r.left > 700 || r.width < 200 || r.height < 300) continue;
        if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
        if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
    }
    if (!box) return null;
    return {altezza: box.scrollHeight, top: box.scrollTop, visibile: box.clientHeight,
            alFondo: (box.scrollHeight - box.scrollTop - box.clientHeight) < 50};
}"""

_JS_HREF_THREAD = """() => [...document.querySelectorAll('a[href^="/"]')]
    .map(e => e.getAttribute('href'))"""

_JS_HEADER_THREAD = """() => {
    const t = [...document.querySelectorAll('span, div')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.left > 660 && r.top < 130 && e.children.length === 0
               && e.textContent.trim().length > 1; })
      .map(e => e.textContent.trim());
    return [...new Set(t)];
}"""


@dataclass
class RigaVisibile:
    indice: int
    nome: str | None
    ultimo_nostro: bool | None
    testo_grezzo: str


@dataclass
class StatoScorrimento:
    altezza: int | None
    al_fondo: bool


def nome_combacia(atteso: str | None, trovato: str | None) -> bool:
    """Verifica post-click. Se uno dei due manca, NON combacia: meglio rinunciare
    a una riga che salvare dati attribuiti alla persona sbagliata."""
    a, b = normalizza_nome(atteso), normalizza_nome(trovato)
    return bool(a) and bool(b) and a == b


async def leggi_righe_visibili(page, lingua: str, quante: int = 30) -> list[RigaVisibile]:
    """Le righe attualmente nel DOM. Da rileggere a ogni passo di scorrimento."""
    grezze = await page.evaluate(_JS_RIGHE, quante)
    fuori = []
    for r in grezze:
        analizzata = analizza_riga_lista(r["testo"], lingua)
        fuori.append(RigaVisibile(
            indice=r["indice"], nome=analizzata.nome,
            ultimo_nostro=analizzata.ultimo_nostro, testo_grezzo=r["testo"],
        ))
    return fuori


async def apri_riga(page, indice: int, nome_atteso: str, lingua: str) -> str | None:
    """Apre la riga e ritorna lo username, oppure None se la verifica fallisce.

    La riga viene ri-risolta QUI, immediatamente prima del click: mai riusare un
    riferimento preso prima di una pausa.
    """
    handle = await page.evaluate_handle(
        """(idx) => [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
             .filter(e => { const r = e.getBoundingClientRect();
               return r.left < 660 && r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; })[idx] || null""",
        indice,
    )
    elemento = handle.as_element()
    if elemento is None:
        return None

    await human_input.human_click(page, elemento)
    await page.wait_for_timeout(1500)

    header = await page.evaluate(_JS_HEADER_THREAD)
    nome_trovato = header[0] if header else None
    if not nome_combacia(nome_atteso, nome_trovato):
        logger.warning(
            f"[InboxBrowser] verifica post-click fallita: atteso {nome_atteso!r}, "
            f"aperto {nome_trovato!r} — la lista si e' riordinata, riga non salvata"
        )
        return None

    href = await page.evaluate(_JS_HREF_THREAD)
    return estrai_username_thread(href, propri=set())


async def scorri(page) -> StatoScorrimento:
    """Un passo di scorrimento, sempre inferiore a una schermata."""
    frazione = random.uniform(PASSO_SCROLL_MIN, PASSO_SCROLL_MAX)
    await page.evaluate(
        """(f) => {
            let box = null, best = 0;
            for (const e of document.querySelectorAll('div')) {
                const r = e.getBoundingClientRect();
                if (r.left > 700 || r.width < 200 || r.height < 300) continue;
                if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
                if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
            }
            if (box) box.scrollTop += box.clientHeight * f;
        }""",
        frazione,
    )
    stato = await page.evaluate(_JS_CONTENITORE)
    if stato is None:
        return StatoScorrimento(altezza=None, al_fondo=False)
    return StatoScorrimento(altezza=stato["altezza"], al_fondo=stato["alFondo"])


def decidi_da_segnali(
    altezza_prima: int | None, altezza_dopo: int | None,
    al_fondo: bool, falliti_inbox: int, attese_esaurite: bool,
) -> str:
    """'continua' | 'fine' | 'piantato'. Funzione pura: qui vive la decisione.

    Dichiarare "esaurita" una lista solo lenta fa perdere IN SILENZIO tutti i
    contatti che stavano sotto: nel dubbio si continua.
    """
    if altezza_prima is not None and altezza_dopo is not None and altezza_dopo > altezza_prima:
        return "continua"
    if not attese_esaurite:
        return "continua"
    if falliti_inbox > 0:
        return "piantato"
    return "fine" if al_fondo else "piantato"
```

- [ ] **Step 4: Eseguire i test**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_pagina.py -q -p no:cacheprovider`
Atteso: PASS (10 test)

- [ ] **Step 5: Prova del nove**

In `apri_riga`, togliere la verifica post-click (ritornare lo username senza confrontare i nomi). Aggiungere un test che simula il disallineamento e verificare che **fallisca** senza la verifica e **passi** con essa. Ripristinare.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/inbox_browser/pagina.py backend/tests/test_inbox_browser_pagina.py
git commit -m "feat(inbox-browser): pagina con verifica post-click e passo di scorrimento limitato"
```

---

### Task 9: Il motore

**Files:**
- Create: `backend/app/services/scrape_inbox_browser.py`
- Test: `backend/tests/test_scrape_inbox_browser.py`

**Interfaces:**
- Consumes: tutto quanto prodotto dai Task 2-8.
- Produces: `async def run_inbox_browser_list(campaign_id: str, db, campaign) -> int | None` — **stesso contratto di `run_inbox_list`**: ritorna i secondi di defer al session-break (il worker solleva `Retry(defer=...)`), `None` se completata o interrotta.

**Da riusare senza modificarli:** `BrowserSession` (lock cross-processo di profilo), `is_halted`, `emit_event`, `is_challenge_exception`, `isolate_challenged_account`, `_single_inbox_account` da `scrape_inbox.py:88` (pura lettura DB). **NON** riusare `build_inbox_source` (fa il login instagrapi, che su questi account vanifica lo scopo) né `inbox_collect` (la sua firma non trasporta i campi nuovi, e modificarla toccherebbe il motore API).

- [ ] **Step 1: Scrivere i test sul ciclo**

```python
# backend/tests/test_scrape_inbox_browser.py
"""Il motore: ciclo, stop, regola fondante.

Il test piu' importante e' quello sulla regola fondante: la sequenza "10 righe
note in cima" (i DM appena inviati) NON deve azzerare la raccolta.
"""
import pytest

from app.services.inbox_browser.riconoscimento import ArchivioNomi, ContatoreZona
from app.services.scrape_inbox_browser import decide_se_aprire


def test_una_riga_non_riconosciuta_si_apre_SEMPRE_anche_in_zona_rapida():
    """REGOLA FONDANTE. Se questo test cade, il motore raccoglie zero a regime."""
    archivio = ArchivioNomi(["Noto Uno", "Noto Due"])
    contatore = ContatoreZona()
    for _ in range(10):
        contatore.registra(True)
    assert contatore.zona == "rapida"
    assert decide_se_aprire("Sconosciuto Mai Visto", archivio, contatore.zona) is True


def test_una_riga_riconosciuta_non_si_apre():
    archivio = ArchivioNomi(["Noto Uno"])
    assert decide_se_aprire("Noto Uno", ArchivioNomi(["Noto Uno"]), "piena") is False


def test_un_segnaposto_non_si_apre_mai():
    """Profili cancellati: aprirli e' tempo perso."""
    assert decide_se_aprire("Utente Instagram", ArchivioNomi([]), "piena") is False


def test_scenario_che_affossava_il_disegno_precedente():
    """10 note in cima, poi un nuovo ogni 10: TUTTI i nuovi vanno raccolti."""
    archivio = ArchivioNomi([f"Noto {i}" for i in range(50)])
    contatore = ContatoreZona()
    aperte = 0
    for blocco in range(5):
        for i in range(9):
            nome = f"Noto {blocco * 10 + i}"
            if decide_se_aprire(nome, archivio, contatore.zona):
                aperte += 1
            contatore.registra(archivio.e_riconosciuto(nome))
        nuovo = f"Nuovo {blocco}"
        if decide_se_aprire(nuovo, archivio, contatore.zona):
            aperte += 1
        contatore.registra(archivio.e_riconosciuto(nuovo))
    assert aperte == 5, f"persi {5 - aperte} contatti nuovi su 5"
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_scrape_inbox_browser.py -q -p no:cacheprovider`
Atteso: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implementare il motore**

Il file è lungo; questa è la struttura obbligatoria, con la funzione pura da cui partire:

```python
# backend/app/services/scrape_inbox_browser.py
"""Fase Lista via browser per scrape_mode=dm_threads, inbox_engine=browser.

Motore SEPARATO da scrape_inbox.py (API), che non viene toccato. Condivide solo
il governo a monte (list_followers: stato campagna, kill-switch, resume) e il
salvataggio a valle.

REGOLA FONDANTE: una riga non riconosciuta si apre SEMPRE. La zona governa solo
il ritmo. Vedi decide_se_aprire.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from app.models.campaign import CampaignStatus
from app.models.follower import Follower
from app.services.bot_state_service import is_halted
from app.services.inbox_browser.pagina import (
    apri_riga, decidi_da_segnali, leggi_righe_visibili, scorri, ATTESE_S,
)
from app.services.inbox_browser.riconoscimento import ArchivioNomi, ContatoreZona
from app.services.inbox_browser.ritmo import campiona_pausa
from app.services.inbox_browser.salvataggio import DatiContatto, salva_contatto
from app.services.inbox_browser.testo import e_segnaposto
from app.services.scrape_inbox import _single_inbox_account   # sola lettura DB
from app.services.scraper import is_challenge_exception, isolate_challenged_account
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, ScraperError

DURATA_SESSIONE_MIN = 30 * 60
DURATA_SESSIONE_MAX = 55 * 60


def decide_se_aprire(nome: str | None, archivio: ArchivioNomi, zona: str) -> bool:
    """REGOLA FONDANTE del modulo.

    Una riga si apre SEMPRE, tranne in due casi: e' gia' riconosciuta, oppure e'
    un segnaposto (profilo cancellato). La ZONA non compare in questa decisione,
    ed e' voluto: nel disegno precedente la zona 'rapida' non apriva niente, e
    questo faceva raccogliere ZERO contatti a regime, perche' in cima alla lista
    ci sono sempre i DM appena inviati (tutti noti).

    Il parametro `zona` resta nella firma solo per rendere esplicito che non
    influenza il risultato: non rimuoverlo pensando sia inutile, e non usarlo.
    """
    if e_segnaposto(nome):
        return False
    return not archivio.e_riconosciuto(nome)
```

Il ciclo principale deve, nell'ordine:

1. `_single_inbox_account(db, campaign.id)` per l'account (solleva se non è esattamente uno).
2. `BrowserSession(account.id)` con `await session.open()` — il lock cross-processo è suo.
3. `page.goto("https://www.instagram.com/direct/inbox/")`; se l'URL finisce su login → `ScraperError` esplicito, **nessun** tentativo di login automatico.
4. Registrare `page.on("requestfailed")` filtrando sugli endpoint inbox (vedi esito del Task 0).
5. Costruire `ArchivioNomi` dai `full_name` già presenti in campagna.
6. Ciclo: leggere le righe visibili → per ognuna `decide_se_aprire` → se sì `apri_riga` con verifica post-click → se lo username torna, `salva_contatto` → `contatore.registra(...)` → `await asyncio.sleep(campiona_pausa(contatore.zona))`.
7. Esaurite le righe visibili: `scorri`, poi `decidi_da_segnali` con le attese crescenti `ATTESE_S`.
8. Controllare a ogni giro: `is_halted` → `BotHaltedError`; stato campagna diverso da `listing`/`listing_break` → uscita; `list_target` raggiunto (**contando i contatti distinti**, non le righe); durata sessione superata → session-break con `return secondi`.
9. `finally`: `await session.close()` sempre.
10. Eventi: `scrape_start`, `scrape_batch`, `scrape_complete`. Se la sessione chiude con **zero** contatti nuovi, l'evento lo dichiara (livello `warn`).
11. Eccezioni: `is_challenge_exception` → `isolate_challenged_account` e stop; `'piantato'` → chiusura pulita, campagna **non** completata, evento di errore.

- [ ] **Step 4: Eseguire i test**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_scrape_inbox_browser.py -q -p no:cacheprovider`
Atteso: PASS (4 test)

- [ ] **Step 5: Prova del nove sulla regola fondante**

In `decide_se_aprire` aggiungere `if zona == "rapida": return False`. `test_scenario_che_affossava_il_disegno_precedente` deve **fallire** con "persi 5 contatti nuovi su 5". È il test che vale l'intero modulo. Ripristinare.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scrape_inbox_browser.py backend/tests/test_scrape_inbox_browser.py
git commit -m "feat(inbox-browser): il motore, con la regola fondante sotto test"
```

---

### Task 10: L'innesto in scrape_list.py

**Files:**
- Modify: `backend/app/services/scrape_list.py:81-83`
- Test: `backend/tests/test_inbox_browser_innesto.py`

**Interfaces:**
- Consumes: `run_inbox_browser_list` da Task 9.

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_innesto.py
"""Il bivio: 'api' va dove e' sempre andato, 'browser' al motore nuovo.

Il test sul percorso API e' una guardia di NON REGRESSIONE: il motore esistente
non deve cambiare comportamento.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.campaign import CampaignStatus


def _campagna(engine):
    return SimpleNamespace(
        id="c1", scrape_mode="dm_threads", inbox_engine=engine,
        status=CampaignStatus.listing,
    )


@pytest.mark.asyncio
async def test_engine_api_va_al_motore_esistente():
    from app.services import scrape_list
    with patch("app.services.scrape_inbox.run_inbox_list", new=AsyncMock(return_value=None)) as api, \
         patch("app.services.scrape_inbox_browser.run_inbox_browser_list", new=AsyncMock()) as browser:
        await scrape_list._dispatch_inbox("c1", None, _campagna("api"))
        api.assert_awaited_once()
        browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_browser_va_al_motore_nuovo():
    from app.services import scrape_list
    with patch("app.services.scrape_inbox.run_inbox_list", new=AsyncMock()) as api, \
         patch("app.services.scrape_inbox_browser.run_inbox_browser_list", new=AsyncMock(return_value=None)) as browser:
        await scrape_list._dispatch_inbox("c1", None, _campagna("browser"))
        browser.assert_awaited_once()
        api.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_assente_usa_api():
    """Retrocompatibilita': una campagna senza il campo non deve cambiare motore."""
    from app.services import scrape_list
    campagna = SimpleNamespace(id="c1", scrape_mode="dm_threads", status=CampaignStatus.listing)
    with patch("app.services.scrape_inbox.run_inbox_list", new=AsyncMock(return_value=None)) as api:
        await scrape_list._dispatch_inbox("c1", None, campagna)
        api.assert_awaited_once()
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Atteso: FAIL con `AttributeError: module 'app.services.scrape_list' has no attribute '_dispatch_inbox'`

- [ ] **Step 3: Modificare `scrape_list.py`**

Sostituire le righe `81-83`, che oggi sono:

```python
        if getattr(campaign, "scrape_mode", "followers") == "dm_threads":
            from app.services.scrape_inbox import run_inbox_list
            return await run_inbox_list(campaign_id, db, campaign)
```

con:

```python
        if getattr(campaign, "scrape_mode", "followers") == "dm_threads":
            return await _dispatch_inbox(campaign_id, db, campaign)
```

e aggiungere, sopra `list_followers`:

```python
async def _dispatch_inbox(campaign_id: str, db, campaign) -> int | None:
    """Bivio fra i due motori di raccolta inbox.

    'api' (default) va dove e' sempre andato: run_inbox_list resta INTOCCATO.
    'browser' va al motore nuovo, che ha ritmo, pause e criteri tutti suoi.

    Entrambi rispettano lo stesso contratto: secondi di defer al session-break,
    None se completata o interrotta.
    """
    if getattr(campaign, "inbox_engine", "api") == "browser":
        from app.services.scrape_inbox_browser import run_inbox_browser_list
        return await run_inbox_browser_list(campaign_id, db, campaign)
    from app.services.scrape_inbox import run_inbox_list
    return await run_inbox_list(campaign_id, db, campaign)
```

- [ ] **Step 4: Eseguire i test, inclusa la regressione sul motore API**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider -k "inbox or scrape_list"`
Atteso: PASS — **tutti** i test inbox esistenti devono restare verdi senza modifiche.

- [ ] **Step 5: Verificare che il motore API non sia stato toccato**

Run: `cd backend && git diff --stat app/services/scrape_inbox.py app/services/inbox_source.py`
Atteso: **nessun output**. Se compare qualcosa, fermarsi: il vincolo globale è stato violato.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scrape_list.py backend/tests/test_inbox_browser_innesto.py
git commit -m "feat(inbox-browser): bivio sul motore, il percorso API resta intoccato"
```

---

### Task 11: Sostituzione della targa e verifica dell'identità

**Files:**
- Modify: `backend/app/services/browser_bio.py:563-577`
- Test: `backend/tests/test_inbox_browser_sostituzione_targa.py`

**Interfaces:**
- Consumes: `e_provvisoria` da Task 2.

**Perché va qui e non altrove:** è l'unico punto in cui il pk vero e il follower coesistono (`shim.pk`, da `browser_bio.py:152` e `:194`). Il file compare fra i "riusi" della spec, ma **questa modifica è necessaria e va dichiarata**.

**Requisito di atomicità:** la sostituzione deve stare **nello stesso commit** che rilascia il lock. Oggi `browser_bio.py:563-577` rilascia `locked_by_account_id` nello stesso commit che porta lo stato a `bio_scraped` (= mandabile): se la sostituzione avvenisse dopo, esisterebbe una finestra in cui un worker DM claima una riga con targa ancora provvisoria e prenota la chiave sbagliata.

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_sostituzione_targa.py
"""La targa provvisoria diventa vera durante l'arricchimento.

E il caso peggiore del progetto: uno username riassegnato dopo un rename farebbe
arricchire e scrivere i dati di un ESTRANEO sulla scheda sbagliata, e poi gli
manderebbe il DM. Nessun passaggio solleva un errore da solo.
"""
import pytest

from app.services.browser_bio import decidi_sostituzione_targa


def test_provvisoria_viene_sostituita():
    assert decidi_sostituzione_targa(targa_attuale=-123456, pk_vero=76561234567) == "sostituisci"


def test_targa_vera_uguale_non_si_tocca():
    assert decidi_sostituzione_targa(targa_attuale=76561234567, pk_vero=76561234567) == "invariata"


def test_targa_vera_DIVERSA_ferma_tutto():
    """Username riassegnato dopo un rename: stiamo guardando un'altra persona."""
    assert decidi_sostituzione_targa(targa_attuale=76561234567, pk_vero=99988877766) == "identita_cambiata"


def test_pk_mancante_non_sostituisce():
    assert decidi_sostituzione_targa(targa_attuale=-123456, pk_vero=None) == "invariata"


def test_pk_non_numerico_non_sostituisce():
    assert decidi_sostituzione_targa(targa_attuale=-123456, pk_vero="non_un_numero") == "invariata"
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Atteso: FAIL con `ImportError: cannot import name 'decidi_sostituzione_targa'`

- [ ] **Step 3: Aggiungere la funzione pura in `browser_bio.py`**

Sopra `fetch_and_store_bio_browser`:

```python
def decidi_sostituzione_targa(targa_attuale: int | None, pk_vero) -> str:
    """'sostituisci' | 'invariata' | 'identita_cambiata'.

    Il terzo esito e' il caso peggiore del modulo inbox browser: il contatto ha
    gia' una targa VERA e Instagram ne restituisce una DIVERSA. Significa che lo
    username ha cambiato proprietario (rename + riassegnazione), quindi stiamo
    guardando il profilo di un estraneo. Senza questo controllo ne salveremmo bio
    e contatti sulla scheda sbagliata e gli manderemmo il DM: nessun passaggio
    solleva un errore da solo, perche' il profilo esiste davvero e lo username
    combacia con quello richiesto (la guardia a :261-262 non scatta).
    """
    from app.services.inbox_browser.targa import e_provvisoria

    try:
        pk = int(pk_vero)
    except (TypeError, ValueError):
        return "invariata"
    if e_provvisoria(targa_attuale):
        return "sostituisci"
    if targa_attuale != pk:
        return "identita_cambiata"
    return "invariata"
```

- [ ] **Step 4: Innestare nel flusso**

In `fetch_and_store_bio_browser`, **prima** del blocco che rilascia il lock (oggi `:563-577`), inserire:

```python
    esito_targa = decidi_sostituzione_targa(follower.ig_user_id, shim.pk)
    if esito_targa == "identita_cambiata":
        logger.error(
            f"[BioBrowser] @{follower.username}: pk diverso da quello registrato "
            f"({follower.ig_user_id} -> {shim.pk}). Username riassegnato dopo un rename: "
            "non scrivo nulla."
        )
        follower.status = FollowerStatus.skipped
        follower.skip_reason = "identita_cambiata"
        follower.locked_by_account_id = None
        follower.locked_at = None
        await db.commit()
        return "skipped", None
    if esito_targa == "sostituisci":
        # NELLO STESSO commit che rilascia il lock: se avvenisse dopo, esisterebbe
        # una finestra in cui un worker DM claima una riga gia' mandabile con la
        # targa ancora provvisoria e prenota la chiave sbagliata.
        follower.ig_user_id = int(shim.pk)
```

`upsert_lead` (`:570`) riceve così la targa **vera**, mai la provvisoria.

- [ ] **Step 5: Eseguire i test**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_sostituzione_targa.py tests/test_bio_browser_regression.py -q -p no:cacheprovider`
Atteso: PASS, **inclusi** i test di regressione della Fase Bio browser.

- [ ] **Step 6: Prova del nove**

Togliere il ramo `identita_cambiata` (ritornare sempre `"invariata"` quando la targa è vera): `test_targa_vera_DIVERSA_ferma_tutto` deve **fallire**. Ripristinare.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/browser_bio.py backend/tests/test_inbox_browser_sostituzione_targa.py
git commit -m "feat(inbox-browser): sostituzione targa nello stesso commit del lock, con verifica identita"
```

---

### Task 12: Le guardie difensive

**Files:**
- Modify: `backend/app/services/global_contact_service.py:82-104`
- Modify: `backend/app/api/leads.py:363`, `:374`
- Test: `backend/tests/test_inbox_browser_guardie.py`

**Perché servono anche col gate del Task 7:** il primo presidio è già saltato una volta in revisione (il vincolo era ancorato al campo sbagliato). Queste guardie reggono anche se il gate venisse aggirato da un percorso futuro.

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_guardie.py
"""Difesa in profondita': l'anagrafica globale rifiuta le targhe provvisorie.

Se una targa provvisoria arrivasse in GlobalContact, la protezione anti-doppio-DM
cross-campagna non riconoscerebbe la persona (chiave diversa da quella registrata
via API) e potrebbe mandarle un secondo messaggio.
"""
import pytest

from app.services.global_contact_service import targa_ammessa_in_anagrafica


def test_targa_vera_ammessa():
    assert targa_ammessa_in_anagrafica(76561234567) is True


def test_targa_provvisoria_rifiutata():
    assert targa_ammessa_in_anagrafica(-8834567123) is False


def test_zero_e_none_rifiutati():
    assert targa_ammessa_in_anagrafica(0) is False
    assert targa_ammessa_in_anagrafica(None) is False
```

- [ ] **Step 2: Eseguire e verificare il fallimento**

Atteso: FAIL con `ImportError`

- [ ] **Step 3: Implementare la guardia**

In `backend/app/services/global_contact_service.py`, sopra `upsert_lead`:

```python
def targa_ammessa_in_anagrafica(ig_user_id: int | None) -> bool:
    """L'anagrafica globale accetta solo pk Instagram reali.

    Le targhe provvisorie del motore inbox browser (negative) non devono entrare:
    la stessa persona raccolta via API avrebbe una chiave diversa, e la protezione
    contro il doppio DM cross-campagna non la riconoscerebbe.

    Difesa in profondita': il gate di configurazione (inbox_browser/gate.py) gia'
    impedisce che un contatto arrivi qui senza arricchimento, ma quel presidio e'
    gia' saltato una volta in revisione perche' era ancorato al campo sbagliato.
    """
    return ig_user_id is not None and ig_user_id > 0
```

e all'inizio di `upsert_lead`:

```python
    if not targa_ammessa_in_anagrafica(ig_user_id):
        logger.warning(
            f"[GlobalContact] @{username}: identificativo provvisorio ({ig_user_id}), "
            "lead non registrato in anagrafica — il contatto non e' ancora stato arricchito"
        )
        return
```

- [ ] **Step 4: Proteggere l'export CSV**

In `backend/app/api/leads.py`, dove `ig_user_id` viene scritto come prima colonna (`:363` e `:374`), sostituire il valore con stringa vuota quando la targa è provvisoria:

```python
        # Le targhe provvisorie non escono in un file che si apre in Excel:
        # un numero negativo nella colonna identificativo e' solo confusione.
        ig_esportabile = row.ig_user_id if (row.ig_user_id or 0) > 0 else ""
```

- [ ] **Step 5: Eseguire i test**

Run: `cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_guardie.py -q -p no:cacheprovider`
Atteso: PASS (3 test)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/global_contact_service.py backend/app/api/leads.py backend/tests/test_inbox_browser_guardie.py
git commit -m "feat(inbox-browser): anagrafica ed export rifiutano le targhe provvisorie"
```

---

### Task 13: Il frontend

**Files:**
- Modify: `frontend/app/campaigns/[id]/page.tsx:736` (default divergente)
- Modify: `frontend/app/campaigns/[id]/page.tsx:1084-1091` (pulsante inbox browser)
- Modify: `frontend/app/campaigns/[id]/page.tsx:1167-1194` (pulsanti bio_engine)

- [ ] **Step 1: Correggere il default divergente**

Riga `:736` legge `campaign.inbox_engine ?? 'browser'` mentre il backend ha default `'api'` (`campaign.py:174`). Su una campagna col campo nullo la UI mostra uno stato **diverso da quello reale**. Sostituire con `?? 'api'`.

- [ ] **Step 2: Riabilitare il pulsante del motore inbox browser**

Alle righe `1084-1091` il pulsante ha `disabled` **cablato**, `cursor-not-allowed` e `title="L'estrazione dell'inbox usa sempre l'API: il motore browser è stato rimosso."`. Rimuovere `disabled`, l'attributo `title` e le classi di disabilitazione; riscrivere l'etichetta (oggi `🛡️ Browser (non disponibile)` e `Deprecato — l'inbox usa sempre l'API`) con testo che descriva il motore. Aggiornare anche il paragrafo esplicativo a `:1065-1067`.

- [ ] **Step 3: Disabilitare il pulsante di arricchimento API quando l'inbox è su browser**

Ai pulsanti `bio_engine` (`:1167-1194`, `handleBioEngineSwitch('api')` a `:1171`), aggiungere sul pulsante API:

```tsx
disabled={(campaign.inbox_engine ?? 'api') === 'browser'}
title={(campaign.inbox_engine ?? 'api') === 'browser'
  ? "Con la raccolta inbox via browser l'arricchimento deve avvenire via browser: quello via API interroga Instagram con l'identificativo numerico, che sui contatti appena raccolti non esiste ancora."
  : undefined}
```

e le classi di disabilitazione condizionali coerenti con lo stile già usato a `:1084-1091`.

- [ ] **Step 4: Verificare a mano**

Aprire una campagna `dm_threads`, cambiare il motore inbox su browser, verificare che il pulsante di arricchimento API diventi grigio e non cliccabile, e che il messaggio compaia al passaggio del mouse. Verificare che su una campagna con motore API il pulsante resti attivo.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/campaigns/\[id\]/page.tsx
git commit -m "feat(inbox-browser): riabilita il motore inbox browser, ingrigisce l'arricchimento API"
```

---

### Task 14: Auto-defer sul cambio motore a caldo

**Files:**
- Modify: `backend/app/services/scrape_inbox_browser.py` (controllo a inizio ciclo)
- Test: `backend/tests/test_inbox_browser_defer.py`

**Perché serve:** `scrape_bios.py:188-199` ha già un auto-defer (`ENGINE_SWITCH_DEFER`) per quando il motore cambia mentre il job gira — senza, il lock arq in-progress resta appeso e blocca anche un nuovo avvio. Qui il problema è **peggiore**: una sessione dura 30-55 minuti contro una singola pagina API. Inoltre la guardia di `campaigns.py:322-326` protegge solo gli stati fermi (`draft/ready/paused/error`), ma una campagna in listing passa per `listing_break`, che **non** è fra quelli.

- [ ] **Step 1: Scrivere il test**

```python
# backend/tests/test_inbox_browser_defer.py
"""Se il motore cambia mentre la sessione gira, si esce subito e pulito."""
from types import SimpleNamespace

from app.services.scrape_inbox_browser import motore_ancora_nostro


def test_motore_invariato_prosegue():
    assert motore_ancora_nostro(SimpleNamespace(inbox_engine="browser")) is True


def test_motore_cambiato_interrompe():
    assert motore_ancora_nostro(SimpleNamespace(inbox_engine="api")) is False


def test_campo_assente_interrompe():
    """Default 'api': se il campo sparisce non siamo piu' noi a dover girare."""
    assert motore_ancora_nostro(SimpleNamespace()) is False
```

- [ ] **Step 2: Implementare**

```python
def motore_ancora_nostro(campaign) -> bool:
    """True se la campagna e' ancora impostata sul motore browser.

    Va controllato a ogni giro del ciclo, dopo il refresh della campagna: una
    sessione dura 30-55 minuti e nel frattempo il motore puo' essere cambiato.
    Proseguire significherebbe raccogliere con un motore che l'utente ha spento.
    """
    return getattr(campaign, "inbox_engine", "api") == "browser"
```

Nel ciclo del motore, dopo `await db.refresh(campaign)`:

```python
            if not motore_ancora_nostro(campaign):
                logger.info("[InboxBrowser] motore cambiato durante la sessione — esco pulito")
                emit_event(campaign_id, "scrape_stopped",
                           "Motore inbox cambiato durante la raccolta: sessione interrotta",
                           level="warn")
                return None
```

- [ ] **Step 3: Estendere la guardia di stato nell'API**

In `campaigns.py:322-326`, aggiungere `CampaignStatus.listing_break` agli stati che **bloccano** il cambio di `inbox_engine` (oggi la lista è `draft/ready/paused/error`, e `listing_break` non ne fa parte pur essendo uno stato di lavoro in corso).

- [ ] **Step 4: Eseguire i test e commitare**

```bash
cd backend && WA_TEST_DB_SLOT=inbox31 ./venv/Scripts/python.exe -m pytest tests/test_inbox_browser_defer.py tests/test_inbox_engine_switch_adversarial.py -q -p no:cacheprovider
git add backend/app/services/scrape_inbox_browser.py backend/app/api/campaigns.py backend/tests/test_inbox_browser_defer.py
git commit -m "feat(inbox-browser): uscita pulita se il motore cambia durante la sessione"
```

---

### Task 15: Chiusura del modulo — protocollo di fine modulo

**REQUIRED SUB-SKILL:** `sviluppo-modulo`, fase 4. Un modulo è chiuso quando **si difende dagli attacchi**, non quando la suite è verde.

**Files:**
- Create: `.superpowers/sdd/qa-inbox-browser-tests.md` (minimo 20 test manuali UI)
- Create: `.superpowers/sdd/qa-inbox-browser-adversarial.md` (minimo 30 test adversarial)

Partire dai modelli riusabili in `d:\dev\thevista-app-magazzino\.superpowers\sdd\qa-50-tests.md` e `qa-adversarial-tests.md`, non da zero.

- [ ] **Step 1: Scrivere la lista dei test manuali UI (minimo 20)**

Scritti come li eseguirebbe Tommaso dalla UI, passo per passo. Devono coprire almeno: creazione campagna con motore browser; il pulsante di arricchimento API che diventa grigio; il rifiuto della combinazione con arricchimento "nessuno"; avvio, pausa e ripresa; la campagna che riprende dalla cima; gli eventi in interfaccia; l'avviso "nulla di nuovo"; i contatti che compaiono con nome, data e ultimo messaggio; l'export CSV senza numeri negativi.

- [ ] **Step 2: Scrivere la lista adversarial (minimo 30)**

Categorie obbligatorie, con il **criterio di PASS invertito** — passa se il sistema **si difende** (errore chiaro, nessuna scrittura sporca, invariante intatta); un 500, un errore DB grezzo, una scrittura parziale o un'invariante violata = FAIL anche se "sembrava funzionare":

- concorrenza: due sessioni sullo stesso account (`Promise.all` vero, non sequenziale); listing e arricchimento sovrapposti; `release_stale_locks` che scatta durante la sostituzione della targa
- targa: rename dello username fra due sessioni; username riassegnato a un terzo; determinismo su due processi; targa vera già presente in campagna
- riconoscimento: 10 note in cima; zona a macchia di leopardo al 10% di nuovi; nomi omonimi; nomi con emoji e spazi invisibili; segnaposto in italiano e inglese; archivio vuoto
- lingua: interfaccia in inglese; interfaccia in una lingua non prevista
- lista: inbox vuota; una sola chat; fondo raggiunto; connessione staccata a metà (fisicamente); riordino durante il click
- stato: kill-switch globale a metà; motore cambiato a metà; campagna messa in pausa a metà; tetto giornaliero raggiunto a metà
- dati: `list_target` con righe duplicate; export CSV; `GlobalContact` con targa provvisoria forzata via script
- **livello tool/API diretto**: un adversarial fatto solo dalla UI non è adversarial. Le race, i payload malformati e i burst vanno fatti con script che chiamano direttamente le API.
- invarianti verificate via SQL a fine run: nessun `Follower` con targa provvisoria in `global_contacts`; nessun username duplicato nella stessa campagna; nessun contatto `sent` tornato `pending`.

- [ ] **Step 3: Il QA agent esegue tutti i test, uno per uno**

Test normali (unit/integration) + E2E reale dal browser. **Fix loop fino al 100%**: "quasi tutti" significa modulo non chiuso.

- [ ] **Step 4: Review finale dell'intero branch**

**REQUIRED SUB-SKILL:** `superpowers:requesting-code-review`.

- [ ] **Step 5: Aggiornare la documentazione**

- `docs/project/PROGRESS.md`: sezione datata con il lavoro svolto
- `docs/architecture/OVERVIEW.md`: i file nuovi in `inbox_browser/`
- `docs/architecture/DATABASE.md`: le 4 colonne della migration 031
- `INDEX.md` se cambia la struttura
- `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md`: sezione datata con root cause, file toccati e comportamento atteso, più l'indice in `MEMORY.md`

- [ ] **Step 6: PR**

Branch dedicato, PR verso `main`, mai push diretto. Nel corpo della PR: cosa è stato misurato, cosa è stato scartato e perché, e la nota che il motore API non è stato toccato (`git diff --stat` su `scrape_inbox.py` e `inbox_source.py` deve essere vuoto).

---

## Self-review del piano

**Copertura della spec.** Ogni sezione della spec ha un task: regola fondante → Task 4 e 9; il riconoscimento non autorizza a scrivere → Task 4 e 9; targa provvisoria → Task 2; funzione hash specificata → Task 2; dedup per username e fusione → Task 6; gate su tre campi → Task 7; le due trappole silenziose → Task 8 (click) e Task 11 (rename); ritmo per zona → Task 5; virtualizzazione e passo di scorrimento → Task 8; fondo/lento/piantato → Task 8 e Task 0; conferme di lettura → Task 0 (misura) e Task 9 (applicazione); stringhe localizzate → Task 3; migration → Task 1; guardie difensive → Task 12; frontend → Task 13; cambio motore a caldo → Task 14; collaudo → Task 15.

**Due punti restano aperti per costruzione**, ed è deliberato: il segnale delle chat non lette e l'utilizzabilità delle richieste fallite si **misurano nel Task 0** e i loro esiti entrano nella spec prima che il Task 8 e il Task 9 li usino. Se il segnale delle chat non lette non esistesse, il Task 0 prescrive di **fermarsi e chiedere a Tommaso**, non di indovinare.

**Coerenza dei nomi fra i task.** `targa_provvisoria` / `e_provvisoria` / `normalizza_username` (Task 2) sono usate con questi nomi nei Task 6, 11, 12. `normalizza_nome` / `e_segnaposto` (Task 3) nei Task 4 e 8. `ArchivioNomi` / `ContatoreZona` (Task 4) nel Task 9. `campiona_pausa` (Task 5) nel Task 9. `DatiContatto` / `salva_contatto` (Task 6) nel Task 9. `decidi_da_segnali` / `apri_riga` / `scorri` / `leggi_righe_visibili` (Task 8) nel Task 9. `run_inbox_browser_list` (Task 9) nel Task 10.

**Nessun placeholder**: ogni step contiene il codice reale o il comando esatto. Il solo punto in cui il piano descrive invece di mostrare è il ciclo principale del Task 9, dove l'elenco numerato di undici punti sostituisce un blocco di codice: è deliberato, perché quel ciclo dipende dall'esito del Task 0 e da fixture della suite che l'implementatore deve leggere sul posto. Tutte le sue parti decisionali sono però funzioni pure già scritte per esteso nei task precedenti.

## Handoff

**Piano completo e salvato in `docs/superpowers/plans/2026-08-09-inbox-listing-browser.md`. Due modalità di esecuzione:**

**1. Subagent-driven (consigliata, ed è lo standard di Tommaso)** — un subagente fresco per task, reviewer dedicato e QA agent dopo ogni funzione, revisione fra un task e l'altro.

**2. Esecuzione inline** — i task si eseguono in questa sessione con checkpoint di revisione.

**Quale preferisci?**
