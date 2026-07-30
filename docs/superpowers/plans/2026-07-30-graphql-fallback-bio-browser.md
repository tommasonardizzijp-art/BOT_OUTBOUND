# GraphQL Fallback per Fase Bio browser — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando `web_profile_info` fallisce con l'errore-bug `HTTP 400 asset ig_business_category_subvertical deleted` (23/59 profili "agenzia scommesse"/svapo), recuperare la bio da un secondo canale intercettato passivamente — la risposta GraphQL (`PolarisProfilePageContentQuery`) che il browser di Instagram genera comunque durante lo stesso caricamento pagina.

**Architecture:** Nessuna nuova richiesta verso Instagram. Si aggiunge un secondo listener passivo sulle response `/api/graphql` dentro `_capture_web_profile_info`. Se `web_profile_info` non produce dati usabili (400/None), e una risposta GraphQL con `data.user` del profilo giusto è stata catturata durante la navigazione, la si normalizza nella forma di `web_profile_info` e la si usa. Il flusso principale (99% dei casi che oggi funzionano) resta identico bit-per-bit; il fetch attivo in-page di GraphQL è **vietato** (solo passivo).

**Tech Stack:** Python 3.13, Patchright/Playwright (browser), pytest + pytest-asyncio, SQLAlchemy async, SimpleNamespace shim.

**File di riferimento (audit già scritto):** `docs/audits/GRAPHQL_FALLBACK_BIO_BROWSER.md` — leggerlo prima: contiene la diagnosi e la giustificazione anti-detection.

## Global Constraints

- **Repo:** `D:\BOT OUTBOUND`, codice in `backend/`. CWD dei comandi = `D:\BOT OUTBOUND\backend`.
- **Worktree isolato SEMPRE** per questo lavoro (sviluppo-modulo Fase 1). Branch dedicato + PR, mai push diretto su master.
- **Una sola suite pytest alla volta** (DB sqlite condiviso + `phone_hmac` UNIQUE globale: run paralleli danno rossi falsi). Vedi memory `botoutbound-una-suite-pytest-alla-volta`.
- **Playwright browsers su D:**, mai C: → `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers`. Il profilo PoC va aperto con la build già installata lì (NON `PLAYWRIGHT_BROWSERS_PATH=D:` nudo — distrugge il profilo, vedi memory `botoutbound-playwright-profilo-versione`).
- **Passivo-only, hard rule:** GraphQL si LEGGE dalle response che il browser emette navigando. NON si costruisce né si fa fetch in-page di `/api/graphql` (replicherebbe `fb_dtsg`/`lsd`/`doc_id` = pattern anomalo + fragile). Il codice deve avere un commento che vieta il fetch attivo.
- **Non mascherare i rate-limit:** il fallback GraphQL scatta SOLO su fail non-rate-limit di `web_profile_info` (status ∉ {429,401,403}) o su None. Per 429/401/403 si continua a ritornare `{"__status": st}` così il breaker soft_block esistente funziona.
- **Anti-divergenza:** un unico shim (`web_user_to_shim`). GraphQL viene normalizzato NELLA forma di `web_profile_info`, non con uno shim parallelo, così `extract_contacts` e lo storage restano identici al path API.

---

## File Structure

- **Modify** `backend/app/services/browser_bio.py`
  - Aggiungi costante `_GRAPHQL_PATH`.
  - Aggiungi funzione pura `graphql_user_to_web_shape(gql_user: dict) -> dict` (Task 1).
  - Estendi `_capture_web_profile_info` (righe 98-165): secondo listener + fallback nei punti di fail (Task 2).
- **Create** `backend/tests/test_graphql_fallback_mapping.py` — unit sulla funzione pura (Task 1).
- **Create** `backend/tests/test_graphql_fallback_capture.py` — integrazione sul listener/fallback con page fake (Task 2).
- **Create** `backend/scripts/probe_graphql_fallback.py` — script di verifica LIVE dell'ipotesi sui 23 profili (Task 3). NON è un test pytest: gira a mano contro Instagram reale con un account loggato.

Nessuna modifica a `config.py`: il fallback è comportamento interno senza nuovi parametri (degrada in sicurezza; niente kill-switch necessario perché non aggiunge traffico). Se in review si decide un kill-switch, aggiungere `bio_browser_graphql_fallback_enabled: bool = True` in `config.py` e gaterei il blocco — ma di default NON serve.

---

## Task 1 — Funzione pura `graphql_user_to_web_shape`

**Files:**
- Modify: `backend/app/services/browser_bio.py` (aggiungi dopo `web_user_to_shim`, ~riga 96)
- Test: `backend/tests/test_graphql_fallback_mapping.py`

**Interfaces:**
- Consumes: niente (funzione pura, nessun IO).
- Produces: `graphql_user_to_web_shape(gql_user: dict) -> dict` — ritorna un dict nella FORMA di `web_profile_info` (`id`, `edge_followed_by.count`, `edge_follow.count`), pronto per `web_user_to_shim(...)`.

**Perché serve (delta di forma, verificato nell'audit):**
| campo GraphQL | campo web_profile_info atteso da `web_user_to_shim` |
|---|---|
| `pk` (str) | `id` |
| `follower_count` (int flat) | `edge_followed_by.count` |
| `following_count` (int flat) | `edge_follow.count` |
| `username`,`full_name`,`biography`,`is_private`,`is_verified`,`external_url`,`bio_links` | stessi nomi (passano invariati) |

Senza normalizzazione, `web_user_to_shim` leggerebbe `edge_followed_by.count` da un dict GraphQL che non ce l'ha → `follower_count=None` (dato perso silenziosamente).

- [ ] **Step 1: Scrivi il test che fallisce**

Crea `backend/tests/test_graphql_fallback_mapping.py`:

```python
"""GraphQL PolarisProfilePageContentQuery -> forma web_profile_info -> shim.

Verifica che il payload GraphQL (forma FLAT: pk/follower_count) venga normalizzato
nella forma web_profile_info (annidata: id/edge_followed_by.count) cosi' l'unico shim
`web_user_to_shim` produca gli stessi contatti del path API (anti-divergenza).
"""
from app.services.browser_bio import graphql_user_to_web_shape, web_user_to_shim
from app.utils.contact_extract import extract_contacts


def _sample_graphql_user() -> dict:
    # Forma reale osservata (audit 2026-07-29, profilo planetwinpiromallo).
    return {
        "pk": "77905145792",
        "username": "planetwinpiromallo",
        "full_name": "Planetwin Piromallo",
        "biography": "Via conte piromallo 40/42\nSan sebastiano al vesuvio scrivi info@pw.it",
        "follower_count": 658,
        "following_count": 92,
        "is_private": False,
        "is_verified": False,
        "external_url": "",
        "bio_links": [],
    }


def test_shape_normalizes_flat_counts_to_nested():
    web_shaped = graphql_user_to_web_shape(_sample_graphql_user())
    assert web_shaped["id"] == "77905145792"
    assert web_shaped["edge_followed_by"]["count"] == 658
    assert web_shaped["edge_follow"]["count"] == 92
    # I campi con nome gia' coincidente restano.
    assert web_shaped["username"] == "planetwinpiromallo"
    assert web_shaped["biography"].startswith("Via conte")


def test_shim_reads_counts_after_shape():
    shim = web_user_to_shim(graphql_user_to_web_shape(_sample_graphql_user()))
    assert shim.pk == "77905145792"
    assert shim.follower_count == 658      # sarebbe None senza la normalizzazione
    assert shim.following_count == 92
    assert shim.username == "planetwinpiromallo"


def test_contacts_extracted_end_to_end():
    shim = web_user_to_shim(graphql_user_to_web_shape(_sample_graphql_user()))
    c = extract_contacts(shim)
    # Email dal regex sulla bio (GraphQL non espone business_email, come web_profile_info).
    assert c.email == "info@pw.it"


def test_missing_and_empty_keys_are_safe():
    for g in ({}, {"username": "x"}, {"pk": None, "follower_count": None, "bio_links": None}):
        web_shaped = graphql_user_to_web_shape(g)
        shim = web_user_to_shim(web_shaped)
        c = extract_contacts(shim)
        assert c.email is None
        assert shim.follower_count is None


def test_id_falls_back_to_id_key_if_no_pk():
    # Difesa: se un giorno GraphQL usasse 'id' invece di 'pk', non perdiamo il pk.
    web_shaped = graphql_user_to_web_shape({"id": "42", "username": "z"})
    assert web_shaped["id"] == "42"
```

- [ ] **Step 2: Esegui il test, verifica che fallisce**

Run: `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers python -m pytest tests/test_graphql_fallback_mapping.py -v`
Expected: FAIL con `ImportError: cannot import name 'graphql_user_to_web_shape'`.

- [ ] **Step 3: Implementa la funzione**

In `backend/app/services/browser_bio.py`, subito dopo `web_user_to_shim` (dopo riga ~95, prima di `_capture_web_profile_info`):

```python
def graphql_user_to_web_shape(gql_user: dict) -> dict:
    """Normalizza il dict `data.user` della query GraphQL interna di IG
    (`PolarisProfilePageContentQuery`) nella FORMA di `web_profile_info`, cosi'
    `web_user_to_shim` resta l'UNICO shim (anti-divergenza col path API).

    Delta di forma noti (GraphQL FLAT -> web_profile_info ANNIDATO):
      - pk              -> id
      - follower_count  -> edge_followed_by.count
      - following_count -> edge_follow.count
    Gli altri campi (username, full_name, biography, is_private, is_verified,
    external_url, bio_links) hanno gia' gli stessi nomi e passano invariati.
    Pura e testabile: nessun IO. Robusta a chiavi mancanti/None.
    """
    g = gql_user or {}
    shaped = dict(g)  # copia: preserva i campi col nome gia' coincidente
    shaped["id"] = g.get("pk") or g.get("id")
    shaped["edge_followed_by"] = {"count": g.get("follower_count")}
    shaped["edge_follow"] = {"count": g.get("following_count")}
    return shaped
```

- [ ] **Step 4: Esegui il test, verifica che passa**

Run: `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers python -m pytest tests/test_graphql_fallback_mapping.py -v`
Expected: 5 passed.

- [ ] **Step 5: Typecheck (se il progetto lo usa) e commit**

```bash
git add backend/app/services/browser_bio.py backend/tests/test_graphql_fallback_mapping.py
git commit -m "feat(bio-browser): normalizzatore GraphQL->web_profile_info per fallback bio"
```

---

## Task 2 — Listener passivo GraphQL + fallback in `_capture_web_profile_info`

**Files:**
- Modify: `backend/app/services/browser_bio.py` — costante `_GRAPHQL_PATH` (dopo riga 45) + funzione `_capture_web_profile_info` (righe 98-165)
- Test: `backend/tests/test_graphql_fallback_capture.py`

**Interfaces:**
- Consumes: `graphql_user_to_web_shape` (Task 1); l'oggetto `raw_page` di Playwright (`.on`, `.remove_listener`, `.goto`, `.evaluate`).
- Produces: `_capture_web_profile_info(raw_page, username, timeout_s=8.0)` con contratto di ritorno INVARIATO verso `fetch_and_store_bio_browser`:
  - dict `data.user` (forma web_profile_info) in caso di successo — **ora anche da GraphQL**, già normalizzato;
  - `{"__status": st}` se `web_profile_info` fallisce con `st ∈ {429,401,403}` e NON c'è recupero GraphQL (il chiamante lo tratta come soft_block);
  - `None` se nessun canale ha dati.

**Regole di attivazione del fallback (encodate nel codice):**
1. Il listener GraphQL resta armato per TUTTA la durata della funzione (rimosso solo nel `finally`), indipendentemente dall'esito di `web_profile_info`. → risolve il rischio "race window" (il 400 arriva presto ma la GraphQL arriva più tardi nella stessa pagina).
2. Cattura GraphQL SOLO se `data.user` esiste, `username` combacia (case-insensitive) con quello richiesto — evita di catturare l'utente loggato o un altro user object — e contiene `biography` o `follower_count`.
3. Il fallback si usa SOLO se `web_profile_info` non ha dato dati usabili E il fail non è rate-limit (status ∉ {429,401,403}) o è None. Per 429/401/403 si ritorna `{"__status": st}` (soft_block preservato).
4. Vietato fetch attivo di GraphQL.

- [ ] **Step 1: Scrivi i test che falliscono**

Crea `backend/tests/test_graphql_fallback_capture.py`. Usa una `FakePage` che simula Playwright: registra i listener, all'atto di `goto` emette le response che vogliamo, e risponde all'`evaluate` (fetch in-page di web_profile_info) con lo status configurato.

```python
"""Integrazione: _capture_web_profile_info recupera da GraphQL passivo quando
web_profile_info fallisce col bug 400, senza fare NESSUNA nuova richiesta.
"""
import asyncio
import pytest

from app.services import browser_bio
from app.services.browser_bio import _capture_web_profile_info


class FakeResponse:
    def __init__(self, url, status, payload):
        self.url = url
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class FakePage:
    """Simula il minimo di Playwright usato da _capture_web_profile_info.

    - on/remove_listener: registra il callback 'response'.
    - goto: emette (await) le response programmate in `self.responses`.
    - evaluate: ritorna `self.evaluate_result` (simula il fetch in-page di
      web_profile_info: dict con __status per un HTTP fail, oppure il body).
    """
    def __init__(self, responses, evaluate_result):
        self.responses = responses
        self.evaluate_result = evaluate_result
        self._on_response = None
        self.evaluate_calls = []

    def on(self, event, cb):
        if event == "response":
            self._on_response = cb

    def remove_listener(self, event, cb):
        if event == "response" and self._on_response is cb:
            self._on_response = None

    async def goto(self, url, **kw):
        for r in self.responses:
            if self._on_response is not None:
                await self._on_response(r)

    async def evaluate(self, script, args):
        self.evaluate_calls.append(args)
        return self.evaluate_result


def _gql_response(username):
    return FakeResponse(
        "https://www.instagram.com/api/graphql",
        200,
        {"data": {"user": {
            "pk": "999", "username": username, "full_name": "Bet Shop",
            "biography": "scrivi info@bet.it", "follower_count": 100,
            "following_count": 5, "is_private": False, "is_verified": False,
            "external_url": "", "bio_links": [],
        }}},
    )


@pytest.mark.asyncio
async def test_fallback_used_when_web_profile_info_400():
    # web_profile_info non intercettato passivamente + fetch in-page = 400 (bug asset).
    page = FakePage(
        responses=[_gql_response("betshop")],
        evaluate_result={"__status": 400},
    )
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.5)
    assert user is not None
    assert not user.get("__status")             # NON e' propagato come errore
    assert user["id"] == "999"                  # forma web_profile_info (normalizzata)
    assert user["edge_followed_by"]["count"] == 100
    assert len(page.evaluate_calls) == 1        # UN solo fetch (web_profile_info), NESSUN fetch GraphQL


@pytest.mark.asyncio
async def test_web_profile_info_success_ignores_graphql():
    # Passa una response web_profile_info 200: il primary path vince, GraphQL ignorato.
    wpi = FakeResponse(
        "https://www.instagram.com/api/v1/users/web_profile_info/?username=betshop",
        200,
        {"data": {"user": {"id": "1", "username": "betshop", "biography": "x",
                            "edge_followed_by": {"count": 7}, "edge_follow": {"count": 2}}}},
    )
    page = FakePage(responses=[wpi, _gql_response("betshop")], evaluate_result={"__status": 400})
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.5)
    assert user["id"] == "1"                    # dal web_profile_info, non dal GraphQL (999)
    assert len(page.evaluate_calls) == 0        # colto passivo: nessun fetch in-page


@pytest.mark.asyncio
async def test_rate_limit_not_masked_by_graphql():
    # 429 su web_profile_info: DEVE propagarsi come __status (soft_block), NON usare GraphQL.
    page = FakePage(responses=[_gql_response("betshop")], evaluate_result={"__status": 429})
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.5)
    assert user == {"__status": 429}


@pytest.mark.asyncio
async def test_graphql_of_wrong_user_ignored():
    # Una GraphQL per un ALTRO username non deve essere usata come fallback.
    page = FakePage(responses=[_gql_response("qualcunaltro")], evaluate_result={"__status": 400})
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.5)
    assert user == {"__status": 400}            # nessun recupero: torna il fail originale


@pytest.mark.asyncio
async def test_no_data_anywhere_returns_none():
    page = FakePage(responses=[], evaluate_result=None)
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.3)
    assert user is None
```

- [ ] **Step 2: Esegui i test, verifica che falliscono**

Run: `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers python -m pytest tests/test_graphql_fallback_capture.py -v`
Expected: FAIL (il fallback GraphQL non esiste ancora: `test_fallback_used_when_web_profile_info_400` torna `{"__status":400}` invece del user).

- [ ] **Step 3: Aggiungi la costante `_GRAPHQL_PATH`**

In `browser_bio.py`, dopo la riga 45 (`_WEB_PROFILE_PATH = ...`):

```python
# Endpoint interno del client web IG: il browser lo chiama da solo navigando il
# profilo (query `PolarisProfilePageContentQuery`). Lo INTERCETTIAMO passivamente
# come fallback quando web_profile_info fallisce col bug 400 "asset ...subvertical
# deleted". VIETATO fare fetch attivo di questo endpoint (replicherebbe fb_dtsg/
# lsd/doc_id = pattern anomalo + fragile): solo lettura passiva. Vedi
# docs/audits/GRAPHQL_FALLBACK_BIO_BROWSER.md.
_GRAPHQL_PATH = "/api/graphql"
```

- [ ] **Step 4: Riscrivi `_capture_web_profile_info` (righe 98-165)**

Sostituisci l'INTERA funzione con questa versione (docstring aggiornata, secondo listener, tre punti di fallback). Nota i tre punti in cui si tenta il recupero GraphQL: (a) dopo un `__status` non-rate-limit; (b) se il fetch in-page solleva; (c) se non c'è comunque nessun dato prima di `return None`.

```python
async def _capture_web_profile_info(raw_page, username: str, timeout_s: float = 8.0) -> dict | None:
    """Naviga al profilo e cattura il JSON di web_profile_info.

    Strategia (dalla piu' "umana" alla piu' esplicita):
      1. Listener passivo sulle response: se il JS di IG spara web_profile_info lo
         intercettiamo (nessuna chiamata extra). In PARALLELO ascoltiamo anche le
         response /api/graphql (PolarisProfilePageContentQuery), che il browser
         genera comunque, come FALLBACK.
      2. Se non colto entro timeout, fetch IN-PAGE di web_profile_info (cookie
         reali, x-ig-app-id web).
      3. FALLBACK: se web_profile_info fallisce con un errore NON-rate-limit
         (tipicamente il bug 400 "asset ...subvertical deleted" su certi account
         business) oppure non da' dati, e durante la navigazione abbiamo catturato
         passivamente una risposta GraphQL del profilo giusto, la normalizziamo
         nella forma di web_profile_info e la usiamo. NON facciamo MAI un fetch
         attivo di /api/graphql (solo lettura passiva).

    Ritorna il dict `data.user` (forma web_profile_info, eventualmente da GraphQL
    gia' normalizzata), oppure {"__status": st} su fail rate-limit, oppure None.
    Non solleva su errori di parsing.
    """
    captured: dict = {}

    async def _on_response(resp):
        try:
            if _WEB_PROFILE_PATH in resp.url and resp.status == 200:
                body = await resp.json()
                u = (((body or {}).get("data") or {}).get("user"))
                if u:
                    captured["user"] = u
            elif (_GRAPHQL_PATH in resp.url and resp.status == 200
                  and "graphql_user" not in captured):
                body = await resp.json()
                u = (((body or {}).get("data") or {}).get("user"))
                # Solo il profilo GIUSTO (username combacia) e con i campi bio:
                # /api/graphql serve molte query diverse; ci interessa solo quella
                # del profilo navigato.
                if (isinstance(u, dict) and u.get("username")
                        and str(u["username"]).lower() == username.lower()
                        and ("biography" in u or "follower_count" in u)):
                    captured["graphql_user"] = u
        except Exception:
            pass  # response non-JSON o gia' consumata: ignora

    def _graphql_fallback():
        """Ritorna il user GraphQL normalizzato se catturato, altrimenti None."""
        g = captured.get("graphql_user")
        if g:
            logger.info(f"[BioBrowser] @{username}: uso fallback GraphQL passivo (web_profile_info non utilizzabile)")
            return graphql_user_to_web_shape(g)
        return None

    raw_page.on("response", _on_response)
    try:
        url = f"https://www.instagram.com/{username}/"
        await raw_page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Attendi l'intercettazione passiva (polling breve).
        waited = 0.0
        while waited < timeout_s and "user" not in captured:
            await asyncio.sleep(0.4)
            waited += 0.4

        if "user" in captured:
            return captured["user"]

        # Fallback esplicito: fetch in-page di web_profile_info.
        try:
            result = await raw_page.evaluate(
                """async (args) => {
                    const [username, appId] = args;
                    const r = await fetch(
                        `/api/v1/users/web_profile_info/?username=${encodeURIComponent(username)}`,
                        { headers: { 'x-ig-app-id': appId }, credentials: 'include' }
                    );
                    if (!r.ok) return { __status: r.status };
                    return await r.json();
                }""",
                [username, WEB_APP_ID],
            )
            if isinstance(result, dict):
                if result.get("__status"):
                    st = result["__status"]
                    # 429/401/403 = rate-limit reale: propaga come soft_block, NON
                    # mascherare con GraphQL (altrimenti si martella cieco).
                    if st not in (429, 401, 403):
                        gql = _graphql_fallback()
                        if gql is not None:
                            return gql
                    logger.warning(f"[BioBrowser] @{username}: web_profile_info fetch HTTP {st}")
                    return {"__status": st}
                u = (((result or {}).get("data") or {}).get("user"))
                if u:
                    return u
        except Exception as e:
            logger.warning(f"[BioBrowser] @{username}: fetch in-page fallito ({type(e).__name__}: {e})")

        # Ne' passivo ne' fetch in-page hanno dato dati usabili: ultima spiaggia GraphQL.
        return _graphql_fallback()
    finally:
        try:
            raw_page.remove_listener("response", _on_response)
        except Exception:
            pass
```

- [ ] **Step 5: Esegui i test, verifica che passano**

Run: `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers python -m pytest tests/test_graphql_fallback_capture.py tests/test_browser_bio_mapping.py -v`
Expected: tutti passed (incluso il mapping preesistente = nessuna regressione sul path primario).

- [ ] **Step 6: Regressione mirata sul modulo bio-browser**

Run: `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers python -m pytest tests/test_bio_browser_regression.py tests/test_scrape_bios_browser_session.py tests/test_browser_bio_lock_release.py tests/test_bio_softblock_keeps_dm.py -v`
Expected: tutti passed. In particolare `test_bio_softblock_keeps_dm` deve restare verde (prova che il 429 non è mascherato dal fallback).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/browser_bio.py backend/tests/test_graphql_fallback_capture.py
git commit -m "feat(bio-browser): fallback GraphQL passivo su bug 400 web_profile_info"
```

---

## Task 3 — Verifica LIVE dell'ipotesi (probe reale, NON pytest)

**Scopo:** i test sopra provano la MECCANICA con dati finti. Questo task prova le IPOTESI di fatto che il piano assume ma che l'audit ha verificato su **un solo** profilo (`planetwinpiromallo`):
1. la GraphQL `data.user` viene davvero emessa passivamente per i 23 profili falliti (non solo per 1);
2. contiene davvero `biography`+`follower_count`+`pk` per quei profili;
3. `username` nella GraphQL combacia con quello richiesto (il match del listener non scarta tutto).

**Files:**
- Create: `backend/scripts/probe_graphql_fallback.py`

**Interfaces:**
- Consumes: `BrowserSession`, `_capture_web_profile_info`, `graphql_user_to_web_shape`, `web_user_to_shim`, `extract_contacts`.

- [ ] **Step 1: Scrivi lo script di probe**

Crea `backend/scripts/probe_graphql_fallback.py`:

```python
"""Probe LIVE del fallback GraphQL (NON un test pytest).

Uso (a mano, con un account loggato e Playwright su D:):
    PLAYWRIGHT_BROWSERS_PATH=D:\\dev\\.playwright-browsers \\
    python -m scripts.probe_graphql_fallback <account_id> user1 user2 ...

Per ogni username apre il profilo, esegue _capture_web_profile_info e stampa da
DOVE e' arrivato il dato (web_profile_info vs GraphQL fallback) e i campi chiave.
Serve a confermare che il fallback recupera davvero i 23 profili business falliti,
NON solo l'uno gia' verificato nell'audit. Nessuna scrittura su DB.
"""
import asyncio
import sys

from app.browser.context_manager import BrowserSession
from app.services.browser_bio import (
    _capture_web_profile_info, graphql_user_to_web_shape, web_user_to_shim,
)
from app.utils.contact_extract import extract_contacts


async def main(account_id: str, usernames: list[str]) -> None:
    session = BrowserSession(account_id, headless=False)
    await session.open()
    await session.page.ensure_logged_in(account_id, allow_login=False)
    raw_page = await session.page._get_page()

    ok_web = ok_gql = fail = 0
    try:
        for uname in usernames:
            user = await _capture_web_profile_info(raw_page, uname, timeout_s=8.0)
            if user is None:
                print(f"[FAIL ] @{uname}: nessun dato (ne' web ne' GraphQL)")
                fail += 1
                continue
            if isinstance(user, dict) and user.get("__status"):
                print(f"[FAIL ] @{uname}: HTTP {user['__status']} (rate-limit, non mascherato)")
                fail += 1
                continue
            # 'id' presente perche' normalizzato: distinguo web da gql guardando se
            # e' arrivato con la forma flat originale non e' possibile qui (gia'
            # normalizzato), quindi ristampo solo i campi finali.
            shim = web_user_to_shim(user)
            c = extract_contacts(shim)
            print(f"[OK   ] @{uname}: pk={shim.pk} followers={shim.follower_count} "
                  f"bio_len={len(shim.biography or '')} email={c.email}")
            ok_web += 1  # (il conteggio web-vs-gql preciso si legge dai log INFO di _capture_web_profile_info)
            await asyncio.sleep(6.0)  # ritmo umano tra profili
    finally:
        await session.close()

    print(f"\n== Riepilogo: {ok_web} risolti, {fail} falliti su {len(usernames)} ==")
    print("(cerca 'uso fallback GraphQL passivo' nei log INFO per contare i recuperi GraphQL)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python -m scripts.probe_graphql_fallback <account_id> user1 [user2 ...]")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
```

- [ ] **Step 2: Prepara la lista dei 23 profili falliti**

I 23 username sono quelli della campagna BORDERLINE (`30f68a3f-9300-46c5-bc67-75805067a694`) categoria "agenzia scommesse". Estraili dal DB (i Follower `skipped` con `skip_reason` che riporta l'errore, o i pending falliti). Query di riferimento (adatta i nomi colonna al reale — probe, non commit):

```bash
python -c "import asyncio; from app.database import AsyncSessionLocal; from sqlalchemy import select; from app.models.follower import Follower; \
async def m():\
  import os;\
  async with AsyncSessionLocal() as db:\
    rows=(await db.execute(select(Follower.username).where(Follower.campaign_id=='30f68a3f-9300-46c5-bc67-75805067a694'))).all();\
    print(' '.join(r[0] for r in rows[:30]));\
asyncio.run(m())"
```

(Se la query non gira così com'è, aprila in uno script temporaneo nello scratchpad — l'obiettivo è solo ottenere la lista di username.)

- [ ] **Step 3: Esegui la probe su almeno 6 profili falliti**

Run (account loggato reale, finestra visibile):
```bash
PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers python -m scripts.probe_graphql_fallback <account_id> <user1> <user2> <user3> <user4> <user5> <user6>
```
Expected: la maggioranza esce `[OK]` con `followers` e `bio_len>0`, e nei log INFO compare `uso fallback GraphQL passivo` per i profili col bug 400.

**Criterio di decisione (il vero test delle ipotesi):**
- **≥5 su 6 recuperati via GraphQL** → ipotesi confermata su campione ampio: il fallback vale, procedi al merge.
- **Recuperi sporadici / campi mancanti su alcuni** → la GraphQL non è uniforme sui 23. Annota QUALI campi mancano e su quali profili; valuta se il dato parziale è comunque utile (basta `biography`+`pk` per il DM) prima di decidere.
- **Nessun recupero** → l'ipotesi "GraphQL emessa passivamente" non regge oltre il profilo dell'audit. NON mergiare: la modifica sarebbe codice morto. Riporta l'evidenza.

- [ ] **Step 4: Registra l'esito**

Scrivi l'esito (numeri reali) in coda a `docs/audits/GRAPHQL_FALLBACK_BIO_BROWSER.md` sezione "8. Verifica live". Commit:

```bash
git add docs/audits/GRAPHQL_FALLBACK_BIO_BROWSER.md backend/scripts/probe_graphql_fallback.py
git commit -m "test(bio-browser): probe live fallback GraphQL + esito su N profili"
```

---

## Adversarial checklist (proporzionata: backend puro, no UI)

Il protocollo standard "20 UI + 30 adversarial" è per moduli con UI. Qui è un fallback di parsing interno: l'adversarial è già nei test di Task 2 + questi casi da aggiungere a `test_graphql_fallback_capture.py` se non coperti. Aggiungili come test extra prima della PR:

- [ ] GraphQL con `data.user = null` → non deve essere catturato (nessun crash, torna il fail originale).
- [ ] GraphQL con `data.user` ma `username` in maiuscolo/diverso case → il match case-insensitive lo accetta se è lo stesso utente, lo scarta se è un altro.
- [ ] `resp.json()` che solleva (body non-JSON su `/api/graphql`) → ingoiato, non propaga.
- [ ] Due response GraphQL per lo stesso profilo → la seconda NON sovrascrive (`"graphql_user" not in captured` guard); si tiene la prima valida.
- [ ] web_profile_info 200 con `data.user` ma anche GraphQL presente → vince web_profile_info (primary path intatto).
- [ ] `remove_listener` che solleva nel `finally` → ingoiato (già coperto dal try/except, verificare).

Ogni caso PASSA se il sistema **si difende**: nessuna eccezione propagata, nessun dato del profilo sbagliato usato, il rate-limit mai mascherato.

---

## Self-Review (eseguita in fase di scrittura)

1. **Copertura spec:** audit sez. 5 (proposta passiva) → Task 2. Sez. 6.1/6.2/6.4 (no nuovo traffico) → garantito dal solo-listener + `evaluate_calls==0`/`==1` nei test. Sez. 6.3 (fragilità) → degrado sicuro: `_graphql_fallback` torna None → contratto `None`/`__status` invariato. Sez. 6.5 (copertura dati) → `graphql_user_to_web_shape` + `/info/` a valle (invariato). Sez. 6.6 (no fetch attivo, solo su fail) → costante commentata + guard rate-limit. I tre buchi della seconda opinione: race window → listener nel `finally` + poll pieno 8s; campione=1 → Task 3 con criterio ≥5/6; china fetch attivo → commento di divieto su `_GRAPHQL_PATH`.
2. **Placeholder scan:** nessun TODO/TBD; ogni step ha codice reale.
3. **Type consistency:** `graphql_user_to_web_shape(dict)->dict` usato in Task 2 esattamente com'è definito in Task 1; contratto di ritorno di `_capture_web_profile_info` invariato → `fetch_and_store_bio_browser` non va toccato.

---

## Execution Handoff

Piano salvato. Due opzioni di esecuzione:

1. **Subagent-Driven (consigliato)** — un subagent fresco per task, review tra un task e l'altro. Task 1 e 2 sono TDD puri; Task 3 è manuale (richiede account IG loggato + browser) → lo esegue Tommaso o una sessione con browser, non un subagent headless.
2. **Inline** — esegui Task 1+2 in questa sessione con checkpoint; Task 3 resta manuale.

Nota: Task 3 (probe live) è il vero verificatore delle ipotesi e va fatto PRIMA del merge — Task 1+2 possono essere completi e verdi ma essere codice morto se l'ipotesi GraphQL non regge oltre il profilo dell'audit.
