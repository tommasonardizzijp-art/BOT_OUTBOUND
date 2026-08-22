# Username come chiave di identità di prima classe — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rendere lo username normalizzato una chiave d'identità legittima accanto al pk, così che i contatti raccolti dal browser siano contattabili senza una passata di arricchimento dedicata, e la targa si ancori al pk vero al primo DM.

**Architecture:** Il canale browser già identifica le persone per username — `targa_provvisoria()` è lo SHA-256 dello username normalizzato reso negativo. Oggi quella targa è trattata come di serie B: `targa_ammessa_in_anagrafica()` la rifiuta, quindi il contatto non prenota, non entra in anagrafica e **non riceve il DM**. Questo piano la promuove a targa legittima, aggiunge la colonna che fa da ponte fra le due rappresentazioni della stessa persona (`username_norm` con UNIQUE), e chiude l'anello facendo salvare il pk vero dalla visita che il DM fa comunque.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend, `backend/venv`), pytest, Next.js 15 + TypeScript (frontend).

**Spec:** questo documento. Nasce dalla sessione del 22/08/2026; la sezione «Fatti verificati» riporta le prove con file e riga.

---

## Global Constraints

- Branch dedicato + PR, mai push diretto su `main`.
- `loguru`, mai `print()`. Async ovunque, `Depends(get_db)`, niente lazy loading ORM.
- Python: `backend/venv/Scripts/python.exe`. Test: `venv/Scripts/python.exe -m pytest`.
- **Una suite pytest alla volta** (sqlite condiviso — memory `botoutbound-una-suite-pytest-alla-volta`).
- **Migrazione prima del codice**: `python -m scripts.migrate` prima di far girare codice nuovo; ferma bot e backend zombie prima (un `idle in transaction` blocca gli `ALTER TABLE`).
- Ultima migrazione presente: `038_inbox_discesa_senza_lavoro.py`. La prossima è **039**.
- Frontend senza test runner: verifica con `npx tsc --noEmit` + `npm run build`.

---

## Fatti verificati (le prove, non le impressioni)

| Affermazione | Prova |
|---|---|
| La targa provvisoria **è** lo username | `inbox_browser/targa.py` — `-SHA256(normalizza_username(u))` |
| Il pk dal browser è **gratis**, zero richieste in più | `browser_bio.graphql_user_to_web_shape` mappa `pk -> id` dalla risposta GraphQL che la pagina fa da sé; `instagram_page.py:399-401` la legge in ascolto passivo |
| `send_dm` **ha già** il pk e lo butta | `dm_harvest.py` contiene **zero** occorrenze di `ig_user_id` |
| Oggi la targa provvisoria impedisce l'invio | `reservation.try_reserve` → `targa_ammessa_in_anagrafica` False → `skipped` (`campaign_orchestrator.py:486-492`) |
| Non esiste un blocco anti-doppio-DM permanente | `_legacy_global_contact_placeholder` (`campaign_orchestrator.py:1468`) **non è chiamato da nessuno**; `contact_reservations` ha TTL 30 min e viene rilasciata dopo l'invio (`:737`) |
| Il segnaposto dei profili chiusi finisce nel campo username | log reale 22/08: `[InboxLista] @utente instagram esiste gia' con una targa REALE diversa` |
| Il riconoscitore esiste ma è cablato su un motore solo | `inbox_browser/testo.py::e_segnaposto` usato in `inbox_browser/riconoscimento.py`, **non** in `scrape_inbox.py` |
| `Follower.username` è `NOT NULL`; `GlobalContact.username` è nullable | `models/follower.py:30`, `models/global_contact.py:13` |

### Decisioni prese da Tommaso in sessione

1. Il dedup anti-doppio-DM cross-campagna **non si ricostruisce**: follow-up e clienti diversi sullo stesso contatto sono casi d'uso legittimi.
2. Il rischio "handle riassegnato" è accettato: **si manda comunque**.
3. Il perdente di un handle riassegnato **non si cancella**: va messo in uno stato che dice «esiste, ma va ri-arricchito prima di contattarlo».
4. La scheda contatto **non si sovrascrive** con i dati della persona nuova: si marca e si logga.
5. Regola finale del pulsante: **AI accesa → «Solo DM» spento. AI spenta → tutte e tre le opzioni, su tutte le sorgenti.**

### Effetto collaterale voluto

Con lo username chiave legittima, una campagna **import** può creare i contatti direttamente dal file (senza pk) e prendere il pk vero durante la visita che il DM fa comunque: **cade la passata di risoluzione**, una visita per contatto invece di due. È la richiesta da cui è partita la sessione. Ricade nella Task 7.

---

## File Structure

| File | Responsabilità | Azione |
|---|---|---|
| `backend/app/services/inbox_browser/targa.py` | `handle_valido()`: la forma di uno username Instagram reale. Sta qui accanto a `normalizza_username`, che è già l'autorità sulla forma dell'handle. | Modifica |
| `backend/app/services/scrape_inbox.py` | Scarta in ingresso i segnaposto (motore inbox API). | Modifica |
| `backend/app/services/dm_harvest.py` | Salva il pk vero dopo l'invio. | Modifica |
| `backend/alembic/versions/039_username_norm_global_contacts.py` | Colonna `username_norm` + indice UNIQUE parziale. | Crea |
| `backend/app/models/global_contact.py` | Il campo nuovo. | Modifica |
| `backend/app/services/global_contact_service.py` | `targa_ammessa_in_anagrafica` accetta le provvisorie; match su pk **oppure** `username_norm`. | Modifica |
| `backend/app/services/reservation.py` | Il lease accetta le targhe provvisorie. | Modifica |
| `backend/app/services/campaign_orchestrator.py` | Lease perso → rimanda, non scarta. Nomi veri. Codice morto via. | Modifica |
| `backend/app/services/inbox_browser/gate.py` | Il divieto di configurazione decade. | Modifica |
| `frontend/lib/arricchimento.ts` + le due pagine campagne | Pulsante: solo AI accesa. | Crea/Modifica |

---

### Task 1: La forma di uno username reale (prerequisito)

Senza questa, tutti i profili chiusi collassano in un unico contatto appena lo username diventa chiave.

**Files:**
- Modify: `backend/app/services/inbox_browser/targa.py`
- Test: `backend/tests/test_handle_valido.py`

**Interfaces:**
- Produces: `handle_valido(username: str | None) -> bool`

- [ ] **Step 1: Scrivi il test che fallisce**

Crea `backend/tests/test_handle_valido.py`:

```python
"""La forma di uno username Instagram reale.

Perche' un controllo sulla FORMA e non sulla lista dei segnaposto: `e_segnaposto`
(inbox_browser/testo.py) confronta con un insieme costruito da `LINGUE`, che oggi
contiene solo italiano e inglese. Il segnaposto dipende dalla lingua
dell'interfaccia dell'ACCOUNT, non da una nostra impostazione: su un account in
spagnolo o tedesco quel filtro non scatta e non lo si scopre da nessun errore.

La forma invece non dipende dalla lingua. Instagram ammette negli username solo
lettere, cifre, punto e underscore: qualunque cosa contenga uno spazio non e' un
handle, e' un nome visualizzato finito nella casella sbagliata (log reale del
22/08: `[InboxLista] @utente instagram ...`).
"""
import pytest

from app.services.inbox_browser.targa import handle_valido


@pytest.mark.parametrize("u", [
    "borderline_grow",
    "mario.rossi",
    "shop123",
    "a",
    "@conchiocciola",       # la chiocciola la toglie normalizza_username
    "  Spazi.Ai.Bordi  ",   # i bordi li toglie normalizza_username
    "MAIUSCOLO",
])
def test_handle_reale_e_valido(u):
    assert handle_valido(u) is True


@pytest.mark.parametrize("u", [
    "utente instagram",     # segnaposto IT — il caso reale del 22/08
    "instagram user",       # segnaposto EN
    "usuario de instagram",  # segnaposto ES: la lista lingue NON lo conosce, la forma si'
    "nome con spazi",
    "",
    "   ",
    None,
    "@",
    "ha-un-trattino",       # il trattino non e' ammesso da Instagram
    "ha/uno/slash",
])
def test_non_e_un_handle(u):
    assert handle_valido(u) is False


def test_lunghezza_massima():
    # Instagram si ferma a 30 caratteri: oltre, non e' un handle.
    assert handle_valido("a" * 30) is True
    assert handle_valido("a" * 31) is False
```

- [ ] **Step 2: Esegui e verifica che fallisca**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_handle_valido.py -v`
Expected: FAIL — `ImportError: cannot import name 'handle_valido'`

- [ ] **Step 3: Implementa**

In `backend/app/services/inbox_browser/targa.py`, dopo `normalizza_username`:

```python
# Instagram ammette lettere, cifre, punto e underscore. Niente spazi, niente
# trattini. Max 30 caratteri.
_FORMA_HANDLE = _re.compile(r"^[a-z0-9._]{1,30}$")


def handle_valido(username: str | None) -> bool:
    """True se la stringa ha la forma di uno username Instagram reale.

    Serve a tenere fuori dalla chiave d'identita' i SEGNAPOSTO dei profili chiusi
    o disattivati, che Instagram mostra uguali per tutti ("Utente di Instagram",
    "Instagram User", e l'equivalente in ogni altra lingua) e che finiscono nel
    campo username (log reale 22/08: `[InboxLista] @utente instagram ...`).

    Perche' la FORMA e non l'insieme dei segnaposto: `e_segnaposto`
    (inbox_browser/testo.py) confronta con `LINGUE`, che contiene solo IT e EN, e
    il segnaposto dipende dalla lingua dell'interfaccia dell'ACCOUNT — non nostra.
    Su un account in spagnolo quel filtro non scatta e nessun errore lo segnala.
    Uno spazio in mezzo invece esclude un handle in QUALUNQUE lingua.

    Finche' la chiave era il pk questo non mordeva: N profili chiusi diventavano N
    righe distinte, brutte ma separate. Con lo username chiave, senza questo
    controllo collasserebbero tutti in un contatto solo, mescolando cronologia e
    contatti di persone diverse. E sarebbero comunque righe morte: l'invio naviga
    su `instagram.com/<username>/`, quindi un contatto che si chiama
    "utente instagram" non ricevera' mai un DM.
    """
    return bool(_FORMA_HANDLE.match(normalizza_username(username)))
```

E in cima al file, accanto a `import hashlib`:

```python
import re as _re
```

- [ ] **Step 4: Esegui e verifica che passi**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_handle_valido.py -v`
Expected: PASS (18 test)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/inbox_browser/targa.py backend/tests/test_handle_valido.py
git commit -m "feat(identita): handle_valido() riconosce i segnaposto dalla forma, non dalla lingua"
```

---

### Task 2: Scartare i segnaposto in ingresso (motore inbox API)

**Files:**
- Modify: `backend/app/services/scrape_inbox.py:113-127`
- Test: `backend/tests/test_handle_valido.py` (stessa suite)

**Interfaces:**
- Consumes: `handle_valido()` dalla Task 1.

**Nota:** non farli entrare, invece di lasciarli entrare e poi fonderli. Un contatto con username segnaposto non è contattabile in nessun caso (l'invio naviga per username), quindi salvarlo produce solo righe morte.

- [ ] **Step 1: Scrivi il test che fallisce**

Aggiungi in fondo a `backend/tests/test_handle_valido.py`:

```python
# -- Ingresso del motore inbox API -----------------------------------------

from app.services.scrape_inbox import _classifica_pagina  # noqa: E402


def test_segnaposto_non_entra_in_lista():
    """Il segnaposto va scartato PRIMA di diventare una riga."""
    esito = _classifica_pagina(
        fresh=[(111, "borderline_grow"), (222, "utente instagram"), (333, "shop_ok")],
        targa_per_username={},
    )
    nuovi = [u for _pk, u in esito.nuovi]
    assert "borderline_grow" in nuovi
    assert "shop_ok" in nuovi
    assert "utente instagram" not in nuovi
    assert esito.segnaposto_scartati == 1


def test_due_profili_chiusi_non_diventano_una_collisione():
    """Due profili chiusi condividono il segnaposto: senza il filtro sarebbero
    una falsa 'collisione username' (il warning reale del 22/08)."""
    esito = _classifica_pagina(
        fresh=[(444, "utente instagram"), (555, "utente instagram")],
        targa_per_username={},
    )
    assert esito.nuovi == []
    assert esito.collisioni_username == []
    assert esito.segnaposto_scartati == 2
```

**Prima di scrivere il test, apri `backend/app/services/scrape_inbox.py:104-128`**: la funzione che classifica la pagina oggi potrebbe avere un nome o una firma diversi da `_classifica_pagina(fresh, targa_per_username)`. Usa il nome e la firma reali; se la firma differisce, adatta la chiamata nel test — **non rinominare la funzione di produzione per far combaciare il test**.

- [ ] **Step 2: Esegui e verifica che fallisca**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_handle_valido.py -v -k "segnaposto or profili_chiusi"`
Expected: FAIL — `segnaposto_scartati` non esiste

- [ ] **Step 3: Implementa**

In `backend/app/services/scrape_inbox.py`, aggiungi il contatore alla dataclass `EsitoPagina` (riga ~84, accanto a `collisioni_username`):

```python
    segnaposto_scartati: int = 0
```

E nel ciclo `for pk, username in fresh:` (riga ~114), come **prima** istruzione del corpo:

```python
        # Profilo chiuso/disattivato: Instagram mostra a tutti lo stesso segnaposto,
        # che qui arriva nel campo username. Non entra: non e' contattabile (l'invio
        # naviga per username) e con lo username come chiave d'identita' tutti i
        # profili chiusi collasserebbero in un contatto solo.
        if not handle_valido(username):
            esito.segnaposto_scartati += 1
            continue
```

Import in cima al file:

```python
from app.services.inbox_browser.targa import handle_valido
```

- [ ] **Step 4: Fai emergere il conteggio nel log**

Dove oggi si logga il ciclo delle collisioni (riga ~436-440), aggiungi dopo:

```python
            if esito.segnaposto_scartati:
                logger.info(
                    f"[InboxLista] {esito.segnaposto_scartati} profili chiusi/disattivati "
                    "scartati (username segnaposto, non contattabili)."
                )
```

**Silenzio no:** senza questa riga il filtro scarta contatti senza dire quanti, e un domani un filtro troppo largo taglierebbe lead veri senza lasciare traccia.

- [ ] **Step 5: Esegui la suite e verifica che passi**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_handle_valido.py -v`
Expected: PASS

- [ ] **Step 6: Verifica il rosso**

Commenta le tre righe del filtro allo Step 3, rilancia: i due test nuovi DEVONO tornare rossi. Rimettile.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/scrape_inbox.py backend/tests/test_handle_valido.py
git commit -m "fix(inbox): i profili chiusi non entrano piu' in lista come falsi rename"
```

---

### Task 3: L'harvest post-invio salva il pk vero

**Files:**
- Modify: `backend/app/services/dm_harvest.py`
- Test: `backend/tests/test_dm_harvest_targa.py`

**Interfaces:**
- Consumes: `browser_bio.decidi_sostituzione_targa(targa_attuale, pk_vero) -> 'sostituisci'|'invariata'|'identita_cambiata'` (esiste già, usata dalla Fase Bio).
- Produces: dopo un invio riuscito, `follower.ig_user_id` porta il pk reale.

**Nota per chi implementa — tre vincoli non negoziabili:**
1. Gira **dopo** la marcatura `sent`: un guasto qui non deve mai toccare la contabilità dell'invio. Il modulo **non solleva mai** (è il suo contratto, riga 1-9).
2. Prima di scrivere la targa vera, verifica che **nessun'altra riga della stessa campagna** la porti già: `UniqueConstraint(campaign_id, ig_user_id)`. È lo stesso presidio già scritto in `browser_bio.py:570-598` — leggilo e replica la scelta (skip + segnalazione, **mai** un merge indovinato).
3. Su `identita_cambiata` **non si sovrascrive la scheda** (decisione 4 di Tommaso): si marca e si logga.

- [ ] **Step 1: Scrivi i test che falliscono**

Crea `backend/tests/test_dm_harvest_targa.py`:

```python
"""L'harvest post-invio ancora la targa provvisoria al pk vero.

Perche' esiste: `send_dm` apre gia' il profilo e cattura gia' il payload GraphQL
(instagram_page.py:399-401) — il pk e' li' dentro, gratis, a ogni invio. Prima di
questo lavoro `dm_harvest` salvava bio e conteggi e BUTTAVA il pk (zero occorrenze
di ig_user_id nel modulo): una targa provvisoria restava provvisoria per sempre,
anche dopo dieci DM, e la finestra "handle riassegnato" non si chiudeva mai.

Con l'ancoraggio al primo invio quella finestra dura un solo DM.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services.dm_harvest import harvest_profile_into_follower
from app.services.inbox_browser.targa import targa_provvisoria


class _FakeDb:
    def __init__(self, altre_righe=None):
        self.committed = False
        self._altre = altre_righe or []
    async def commit(self):
        self.committed = True
    async def rollback(self):
        pass
    async def execute(self, _stmt):
        righe = self._altre
        class _R:
            def scalar_one_or_none(self_inner):
                return righe[0] if righe else None
        return _R()


def _follower(targa, username="borderline_grow"):
    return SimpleNamespace(
        id="f1", campaign_id="c1", username=username, ig_user_id=targa,
        full_name=None, biography=None, follower_count=None, following_count=None,
        external_url=None, is_private=False, is_verified=False,
        status=None, skip_reason=None, locked_by_account_id="acc1", locked_at="x",
        updated_at=None,
    )


def _payload(pk, username="borderline_grow"):
    return {"id": str(pk), "username": username, "biography": "growshop a Savona"}


def test_targa_provvisoria_diventa_quella_vera():
    f = _follower(targa_provvisoria("borderline_grow"))
    db = _FakeDb()
    assert asyncio.run(harvest_profile_into_follower(db, f, _payload(12345))) is True
    assert f.ig_user_id == 12345


def test_targa_gia_vera_e_uguale_resta_invariata():
    f = _follower(12345)
    db = _FakeDb()
    asyncio.run(harvest_profile_into_follower(db, f, _payload(12345)))
    assert f.ig_user_id == 12345


def test_identita_cambiata_non_sovrascrive_la_scheda():
    """Handle riassegnato: si e' gia' mandato il DM (scelta di Tommaso), ma i dati
    dello sconosciuto NON finiscono sulla scheda del contatto del cliente."""
    f = _follower(12345)
    f.biography = "bio del contatto vero"
    db = _FakeDb()
    asyncio.run(harvest_profile_into_follower(db, f, _payload(99999)))
    assert f.ig_user_id == 12345                    # targa non toccata
    assert f.biography == "bio del contatto vero"   # scheda non sporcata
    assert f.skip_reason == "handle_riassegnato"    # ma evidente


def test_targa_vera_gia_su_altra_riga_non_fonde():
    """UniqueConstraint(campaign_id, ig_user_id): scrivere qui solleverebbe.
    Stessa scelta di browser_bio: skip + segnalazione, mai un merge indovinato."""
    f = _follower(targa_provvisoria("borderline_grow"))
    db = _FakeDb(altre_righe=[SimpleNamespace(id="f2", username="altro")])
    asyncio.run(harvest_profile_into_follower(db, f, _payload(12345)))
    assert f.ig_user_id != 12345
    assert f.skip_reason == "targa_gia_presente_su_altra_riga"


def test_payload_senza_pk_non_rompe_nulla():
    f = _follower(targa_provvisoria("borderline_grow"))
    db = _FakeDb()
    asyncio.run(harvest_profile_into_follower(db, f, {"username": "borderline_grow"}))
    assert f.ig_user_id == targa_provvisoria("borderline_grow")


def test_non_solleva_mai():
    """Contratto del modulo: gira dopo 'sent', un guasto non tocca l'invio."""
    f = _follower(targa_provvisoria("x"))
    class _Esplode:
        async def commit(self): raise RuntimeError("db giu'")
        async def rollback(self): pass
        async def execute(self, _s): raise RuntimeError("db giu'")
    assert asyncio.run(harvest_profile_into_follower(_Esplode(), f, _payload(1))) is False
```

- [ ] **Step 2: Esegui e verifica che falliscano**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_dm_harvest_targa.py -v`
Expected: FAIL — la targa resta provvisoria, `skip_reason` mai valorizzato

- [ ] **Step 3: Implementa**

In `backend/app/services/dm_harvest.py`, dentro il `try`, **subito dopo** `shim = web_user_to_shim(graphql_user_to_web_shape(payload))` e **prima** del ciclo su `_CAMPI`:

```python
        # Ancoraggio della targa. La visita l'abbiamo gia' pagata per mandare il DM e
        # il pk e' dentro il payload catturato: prima di questo blocco veniva buttato,
        # e una targa provvisoria restava provvisoria per sempre.
        from app.services.browser_bio import decidi_sostituzione_targa

        esito_targa = decidi_sostituzione_targa(follower.ig_user_id, getattr(shim, "pk", None))

        if esito_targa == "identita_cambiata":
            # Lo username ha cambiato proprietario: il profilo appena visitato e' di
            # un'altra persona. Il DM e' gia' partito (scelta esplicita: si accetta
            # il caso raro invece di bloccare gli invii), ma i dati dello sconosciuto
            # NON vanno sulla scheda del contatto del cliente: e' la scheda che poi
            # usa lui, e un dato sbagliato li' non si nota mai piu'.
            logger.error(
                f"[Harvest] @{username}: pk diverso da quello registrato "
                f"({follower.ig_user_id} -> {shim.pk}). Handle riassegnato: "
                "non scrivo nulla, il contatto va ri-arricchito prima di ricontattarlo."
            )
            follower.skip_reason = "handle_riassegnato"
            follower.updated_at = datetime.utcnow()
            await db.commit()
            return False

        if esito_targa == "sostituisci":
            # UniqueConstraint(campaign_id, ig_user_id): se un'altra riga della stessa
            # campagna porta gia' il pk vero, scrivere qui solleverebbe. Stessa scelta
            # di browser_bio.py:570-598 — skip e segnalazione, mai un merge indovinato.
            from sqlalchemy import select
            from app.models.follower import Follower

            bersaglio = (await db.execute(
                select(Follower).where(
                    Follower.campaign_id == follower.campaign_id,
                    Follower.ig_user_id == int(shim.pk),
                    Follower.id != follower.id,
                )
            )).scalar_one_or_none()
            if bersaglio is not None:
                logger.error(
                    f"[Harvest] @{username}: la targa vera {shim.pk} e' gia' su un'altra "
                    "riga della campagna. Non fondo automaticamente: segnalo e lascio."
                )
                follower.skip_reason = "targa_gia_presente_su_altra_riga"
                follower.updated_at = datetime.utcnow()
                await db.commit()
                return False
            follower.ig_user_id = int(shim.pk)
            scritto_targa = True
        else:
            scritto_targa = False
```

Poi, dove oggi c'è `scritto = False` prima del ciclo su `_CAMPI`, sostituisci con:

```python
        scritto = scritto_targa
```

**Non toccare** il resto della funzione: il `try/except` che garantisce «non solleva mai» deve continuare a racchiudere tutto.

- [ ] **Step 4: Esegui e verifica che passino**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_dm_harvest_targa.py -v`
Expected: PASS (6 test)

- [ ] **Step 5: Verifica il rosso**

Commenta il blocco `if esito_targa == "sostituisci":` (lasciando `scritto_targa = False`), rilancia: `test_targa_provvisoria_diventa_quella_vera` e `test_targa_vera_gia_su_altra_riga_non_fonde` DEVONO tornare rossi. Rimetti.

- [ ] **Step 6: Non rompere l'harvest esistente**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/ -q -k "harvest"`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/dm_harvest.py backend/tests/test_dm_harvest_targa.py
git commit -m "feat(dm): l'invio ancora la targa al pk vero invece di buttarlo"
```

---

### Task 4: La colonna ponte fra le due rappresentazioni

**Files:**
- Create: `backend/alembic/versions/039_username_norm_global_contacts.py`
- Modify: `backend/app/models/global_contact.py`

**Interfaces:**
- Produces: `GlobalContact.username_norm: str | None`, con indice UNIQUE parziale (solo dove non NULL).

**Perché serve:** la targa provvisoria dedup già i contatti browser fra loro (stesso username → stesso hash negativo). Quello che **non** dedup è la stessa persona vista dai due canali: pk reale da API, hash negativo da browser. `username_norm` è il ponte che li fa combaciare, e l'UNIQUE è ciò che impedisce alle due rappresentazioni di coesistere come righe separate — l'incidente dei 32 doppioni su 34.

- [ ] **Step 1: Scrivi la migrazione**

Crea `backend/alembic/versions/039_username_norm_global_contacts.py`. **Copia intestazione, `revision`/`down_revision` e stile da `038_inbox_discesa_senza_lavoro.py`** — `down_revision` deve puntare a `038`.

```python
def upgrade():
    op.add_column("global_contacts", sa.Column("username_norm", sa.Text(), nullable=True))
    # Backfill dalla colonna username gia' presente, normalizzata come in
    # inbox_browser/targa.py: minuscolo, senza chiocciola, senza spazi ai bordi.
    op.execute("""
        UPDATE global_contacts
        SET username_norm = lower(trim(ltrim(trim(username), '@')))
        WHERE username IS NOT NULL AND trim(username) <> ''
    """)
    # I doppioni pre-esistenti impedirebbero l'indice UNIQUE: azzera username_norm
    # su tutti tranne il piu' vecchio di ogni gruppo. Non cancella righe e non tocca
    # `username`: il dato resta leggibile, perde solo il ruolo di chiave.
    op.execute("""
        UPDATE global_contacts SET username_norm = NULL
        WHERE id NOT IN (
            SELECT MIN(id) FROM global_contacts
            WHERE username_norm IS NOT NULL GROUP BY username_norm
        ) AND username_norm IS NOT NULL
    """)
    op.create_index(
        "ux_global_contacts_username_norm", "global_contacts", ["username_norm"],
        unique=True, sqlite_where=sa.text("username_norm IS NOT NULL"),
        postgresql_where=sa.text("username_norm IS NOT NULL"),
    )


def downgrade():
    op.drop_index("ux_global_contacts_username_norm", table_name="global_contacts")
    op.drop_column("global_contacts", "username_norm")
```

**Attenzione al `MIN(id)`:** `id` è un UUID stringa, quindi «il più vecchio» qui è lessicografico, non cronologico. Va bene — serve solo un criterio deterministico per scegliere UN vincitore per gruppo. Se preferisci il vero più vecchio, usa una sottoquery su `created_at`; **non** lasciare la scelta non deterministica.

- [ ] **Step 2: Il campo nel modello**

In `backend/app/models/global_contact.py`, dopo `username`:

```python
    # Ponte fra le due rappresentazioni della stessa persona: pk reale (canale API)
    # e targa provvisoria (canale browser, = hash dello username). UNIQUE parziale:
    # un handle = un contatto, ma NULL ammesso per le righe senza handle valido.
    username_norm: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 2b: Censisci la colonna nel test delle migrazioni — NON saltare**

`backend/tests/test_wa_migration.py` fabbrica lo stato «prod già a 024» con `create_all` sui
modelli correnti, poi **toglie a mano** le colonne che le migrazioni successive introducono.
`username_norm` esiste già nel modello (Step 2), quindi `create_all` la crea, e poi la 039
proverebbe ad aggiungerla di nuovo: **`duplicate column name`, 7 test rossi**.

Non è teoria: è successo esattamente così alla migration **038**, ed è stato scoperto solo
eseguendo la CI in locale (PR **#108**, 22/08). Se quella PR non è ancora mergiata, il file che
modifichi qui potrebbe non contenere ancora la riga della 038 — **verifica ed evita il
conflitto**.

In `POST_024_COLUMNS` aggiungi la voce per la tabella nuova (`global_contacts` **non c'è
ancora** nel dizionario, va creata la chiave):

```python
    "global_contacts": ["username_norm"],  # 039
```

- [ ] **Step 2c: Verifica che il censimento basti**

Run: `cd backend && rm -f data/test_bot_ci039.db && WA_TEST_DB_SLOT=ci039 venv/Scripts/python.exe -m pytest tests/test_wa_migration.py -q`
Expected: **tutti verdi**. Un `duplicate column name: username_norm` qui significa che la chiave
non è stata aggiunta o è scritta con un nome diverso da quello del modello.

- [ ] **Step 3: Applica la migrazione**

**Ferma prima bot, worker e backend** (un `idle in transaction` blocca gli `ALTER TABLE`).

Run: `cd backend && venv/Scripts/python.exe -m scripts.migrate`

- [ ] **Step 4: Verifica le COLONNE, non l'uscita del comando**

Un `upgrade` può dire «ok» senza aver emesso DDL (memory `alembic-stesso-numero-da-due-sessioni`). Interroga lo schema:

```sql
SELECT column_name FROM information_schema.columns
WHERE table_name = 'global_contacts' AND column_name = 'username_norm';
```

Expected: una riga. Zero righe = la migrazione non è passata, **non proseguire**.

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/039_username_norm_global_contacts.py backend/app/models/global_contact.py
git commit -m "feat(db): username_norm come ponte fra targa reale e provvisoria (039)"
```

---

### Task 5: La targa provvisoria diventa legittima

**Files:**
- Modify: `backend/app/services/global_contact_service.py`
- Modify: `backend/app/services/reservation.py`
- Modify: `backend/app/services/inbox_browser/gate.py`
- Test: `backend/tests/test_chiave_doppia.py`

**Interfaces:**
- Modifica: `targa_ammessa_in_anagrafica(ig_user_id)` — accetta anche le negative purché il contatto porti un handle valido.
- `upsert_lead` / `_mark_globally_contacted` — match su pk **oppure** `username_norm`.

- [ ] **Step 1: Scrivi i test che falliscono**

Crea `backend/tests/test_chiave_doppia.py`:

```python
"""La targa provvisoria e' una chiave legittima, non di serie B.

Prima di questo lavoro `targa_ammessa_in_anagrafica` rifiutava le negative, e
`reservation.try_reserve` di conseguenza ritornava False: il contatto finiva
`skipped` con motivo "already_contacted_globally" e non riceveva MAI il DM.
Non era una protezione contro il doppio invio: era un invio che non partiva.
"""
from app.services.global_contact_service import targa_ammessa_in_anagrafica
from app.services.inbox_browser.targa import targa_provvisoria


def test_targa_provvisoria_e_ammessa():
    assert targa_ammessa_in_anagrafica(targa_provvisoria("borderline_grow")) is True


def test_pk_reale_e_ammesso():
    assert targa_ammessa_in_anagrafica(12345) is True


def test_zero_e_none_non_sono_targhe():
    assert targa_ammessa_in_anagrafica(0) is False
    assert targa_ammessa_in_anagrafica(None) is False
```

Aggiungi poi il test end-to-end del ponte, con l'harness `client` copiato da `tests/test_enrichment_level_api.py:92-151` (fixture `_temp_db` + `client`, id utente finto `...0006`):

```python
def test_stessa_persona_dai_due_canali_e_una_riga_sola(...):
    """Il ponte: un contatto salvato prima con la targa provvisoria e poi visto
    dal canale API con il pk reale NON deve produrre due righe in anagrafica.
    E' l'incidente dei 32 doppioni su 34 (memory botoutbound-inbox-doppioni-browser-vs-api)."""
```

**Scrivi il corpo di questo test dopo aver letto `upsert_lead`**: la firma richiede `campaign` e `account`, e va costruita con oggetti reali o `SimpleNamespace` coerenti. Il criterio d'accettazione è uno solo: **dopo i due upsert, `SELECT count(*) FROM global_contacts WHERE username_norm='borderline_grow'` deve dare 1, e quella riga deve portare il pk reale.**

- [ ] **Step 2: Esegui e verifica che falliscano**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_chiave_doppia.py -v`

- [ ] **Step 3: Implementa `targa_ammessa_in_anagrafica`**

In `backend/app/services/global_contact_service.py`, sostituisci corpo e docstring:

```python
def targa_ammessa_in_anagrafica(ig_user_id: int | None) -> bool:
    """L'anagrafica accetta sia i pk reali sia le targhe derivate dallo username.

    Storia di questa funzione, perche' il ribaltamento sia leggibile: rifiutava le
    targhe negative (provvisorie) per non far entrare in anagrafica una chiave che
    la stessa persona vista dal canale API non avrebbe riconosciuto. Il costo era
    che `reservation.try_reserve` ritornava False e il contatto finiva `skipped`
    con motivo "already_contacted_globally": non una protezione contro il doppio
    DM, ma un DM che non partiva mai.

    Oggi il ponte fra le due rappresentazioni e' `global_contacts.username_norm`
    (UNIQUE, migration 039): la stessa persona converge su una riga sola
    qualunque canale l'abbia vista, quindi la targa negativa non spacca piu'
    nulla. Resta escluso solo cio' che non e' una targa: None e zero.

    I segnaposto dei profili chiusi non arrivano fin qui: li ferma `handle_valido`
    in ingresso (inbox_browser/targa.py, applicato in scrape_inbox.py).
    """
    return ig_user_id is not None and ig_user_id != 0
```

- [ ] **Step 4: Scrivi `username_norm` negli upsert**

In `upsert_lead` e in `_mark_globally_contacted` (`campaign_orchestrator.py:1509+`):
- calcola `norm = normalizza_username(username)`;
- valorizza `username_norm=norm if handle_valido(username) else None`;
- **la ricerca del contatto esistente cerca per `ig_user_id` OPPURE per `username_norm`** (quando `norm` è valido), non solo per pk;
- se la riga trovata ha una targa provvisoria e ora arriva un pk reale, **promuovi**: aggiorna `ig_user_id` al pk reale.

- [ ] **Step 5: Il lease accetta le provvisorie**

In `backend/app/services/reservation.py::try_reserve`, il presidio su `targa_ammessa_in_anagrafica` ora passa da sé (Step 3). **Verifica che il `return False` residuo scatti solo su None/zero** e aggiorna il messaggio di log, che oggi dice «targa non ammessa in anagrafica».

- [ ] **Step 6: Il gate di configurazione decade**

In `backend/app/services/inbox_browser/gate.py`, **togli la condizione su `enrichment_level`**: il motivo per cui esisteva (la targa provvisoria arriva a GlobalContact e aggira il dedup) non c'è più — il ponte `username_norm` fa convergere le due rappresentazioni.

**Valuta se la seconda condizione (`bio_engine == 'browser'`) resta valida** e aggiorna il docstring del modulo di conseguenza: oggi racconta un vincolo che questo lavoro ha rimosso, e lasciarlo lì manderebbe fuori strada la prossima sessione. Se resta valida, riscrivi il perché; se decade anche quella, il modulo va rimosso e con lui le sue chiamate in `campaigns.py:222` e `:361`.

- [ ] **Step 7: Suite verde**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_chiave_doppia.py tests/test_inbox_browser_gate.py -v`
Expected: PASS. `test_inbox_browser_gate.py` inchioda il vecchio comportamento: i suoi test **vanno cambiati di proposito**, non «aggiustati» — e il commit deve dirlo.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/global_contact_service.py backend/app/services/reservation.py backend/app/services/inbox_browser/gate.py backend/tests/
git commit -m "feat(identita): la targa dallo username e' una chiave legittima, non di serie B"
```

---

### Task 6: Il lease rimanda invece di scartare, e i nomi dicono la verità

**Files:**
- Modify: `backend/app/services/campaign_orchestrator.py:486-492`, `:1468-1507`
- Test: `backend/tests/test_lease_rimanda.py`

**Il difetto:** `contact_reservations` è un lease di **30 minuti** che viene rilasciato subito dopo l'invio. Ma chi perde la corsa viene marcato `skipped` con `skip_reason='already_contacted_globally'` — **definitivo**. Un lock temporaneo produce uno scarto permanente: quel lead non viene più ripreso, mai. E il nome descrive un blocco permanente cross-campagna che **non esiste** (`_legacy_global_contact_placeholder` non è chiamato da nessuno).

- [ ] **Step 1: Scrivi il test che fallisce**

Crea `backend/tests/test_lease_rimanda.py` con un test che verifica: quando `try_reserve` ritorna False, il follower **non** finisce in `skipped` ma resta in uno stato ripescabile (rilasciato, senza lock), e il contatore di sessione registra un rinvio, non uno scarto.

**Costruiscilo sul percorso reale**, monkeypatchando `reservation.try_reserve` per farlo ritornare False; il criterio d'accettazione è `follower.status != FollowerStatus.skipped` e `follower.locked_by_account_id is None`.

- [ ] **Step 2: Esegui e verifica che fallisca**

- [ ] **Step 3: Implementa**

Sostituisci il blocco a `campaign_orchestrator.py:486-492`: rilascia il lock e **lascia il follower nel suo stato**, senza marcarlo `skipped`; logga con parole vere:

```python
                logger.info(
                    f"[Worker] @{follower.username} in lavorazione da un altro worker "
                    "(prenotazione attiva, TTL 30 min) — lo riprendo piu' tardi, non lo scarto"
                )
```

- [ ] **Step 4: Rimuovi il codice morto**

Elimina `_legacy_global_contact_placeholder` (`:1468`) e `_legacy_release_placeholder` (`:1498`). Sono il blocco permanente cross-campagna, **scollegato**: lasciarli lì fa concludere a chi legge che la protezione esista. Verifica prima che nessuno li chiami:

Run: `cd backend && grep -rn "_legacy_global_contact_placeholder\|_legacy_release_placeholder" app/ tests/`
Expected: solo le definizioni (e, dopo la rimozione, niente).

- [ ] **Step 5: Suite verde + verifica del rosso**

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/campaign_orchestrator.py backend/tests/test_lease_rimanda.py
git commit -m "fix(dm): la prenotazione persa rimanda il lead, non lo scarta per sempre"
```

---

### Task 7: Il pulsante, con la regola finale

**Files:**
- Create: `frontend/lib/arricchimento.ts`
- Modify: `frontend/app/campaigns/new/page.tsx:507-534`, `frontend/app/campaigns/[id]/page.tsx:1117-1135`
- Modify: `backend/app/models/campaign.py`, `backend/app/api/campaigns.py`

**La regola finale, una sola condizione:**

> **AI accesa → «Solo DM» spento. AI spenta → tutte e tre le opzioni, su tutte le sorgenti.**

Import e inbox-browser rientrano: dopo le Task 3-5 la targa dallo username è legittima e il pk arriva dal primo DM, quindi «Solo DM» ha senso ovunque.

- [ ] **Step 1: Backend — il predicato e il gate**

Segui **integralmente le Task 1 e 2 del piano `2026-08-22-guardia-ai-senza-bio.md`** (stessa cartella), con **una sola differenza**: `valida_ai_senza_bio` non guarda `source_type`. La firma diventa:

```python
def valida_ai_senza_bio(ai_enabled: bool, enrichment_level: str) -> str | None:
```

e il corpo si riduce a: se l'AI è spenta o il livello non è `none` → `None`; altrimenti il messaggio d'errore. Adatta di conseguenza i test di quel piano: **i due che oggi asseriscono «su import resta permesso» vanno riscritti**, perché la regola è cambiata di proposito.

Messaggio d'errore (non cita più la Fase Bio, che non è più l'unico modo di avere la bio):

```
"Con la personalizzazione AI attiva il livello «Solo DM» non ha dati su cui lavorare: "
"la bio del destinatario arriva solo aprendo il profilo prima di scrivere, e questo "
"livello non lo apre. Alza il livello a «Bio» o «Bio + contatti», oppure spegni la "
"personalizzazione AI e usa i template."
```

- [ ] **Step 2: Frontend — l'helper**

Crea `frontend/lib/arricchimento.ts`:

```ts
// Gemello TypeScript della guardia backend `valida_ai_senza_bio`
// (backend/app/models/campaign.py). Il backend resta l'autorita': risponde 400 comunque.
// Questo serve solo a spegnere il pulsante e dire perche', invece di far scoprire il
// divieto dopo aver compilato il form.
//
// Se cambi la regola qui, cambiala anche di la': sono due copie della stessa decisione.

/** «Solo DM» non apre il profilo, quindi con l'AI accesa il messaggio si genererebbe
 *  senza bio: l'AI ricopierebbe il template spendendo una chiamata. */
export function soloDmVietato(aiEnabled: boolean): boolean {
  return aiEnabled
}

export const MOTIVO_SOLO_DM_VIETATO =
  'Con la personalizzazione AI attiva questo livello non ha dati su cui lavorare: '
  + 'la bio arriva solo aprendo il profilo prima di scrivere, e «Solo DM» non lo apre. '
  + 'Alza il livello a «Bio», oppure spegni la personalizzazione AI e usa i template.'
```

- [ ] **Step 3: Le due pagine**

Applica in `new/page.tsx` e `[id]/page.tsx` le trasformazioni descritte nella **Task 3, Step 2-4 del piano `2026-08-22-guardia-ai-senza-bio.md`**, con `soloDmVietato(form.ai_enabled)` e `soloDmVietato(campaign.ai_enabled ?? false)` al posto delle chiamate a due argomenti. Includi la porta di servizio dello Step 3 di quel piano (accendere l'AI mentre il livello è `none` riporta il livello a `bio`).

- [ ] **Step 4: Typecheck e build**

Run: `cd frontend && npx tsc --noEmit && npm run build`

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/arricchimento.ts frontend/app/campaigns/new/page.tsx "frontend/app/campaigns/[id]/page.tsx" backend/app/models/campaign.py backend/app/api/campaigns.py backend/tests/
git commit -m "feat(ui): Solo DM spento solo quando l'AI non avrebbe la bio"
```

---

### Task 8: Collaudo dal vivo, bonifica e chiusura

**Files:** doc e memoria.

Questo lavoro tocca l'identità dei contatti: la suite verde non basta.

- [ ] **Step 1: Controlli manuali (UI + dati)**

1. Campagna **inbox motore browser** con AI spenta e livello «Solo DM» → si crea senza errore (prima era rifiutata).
2. La stessa campagna manda **almeno un DM** → prima del fix il follower finiva `skipped`.
3. Dopo quell'invio, controlla in DB che quel follower porti un **`ig_user_id` positivo** (Task 3):
   ```sql
   SELECT username, ig_user_id, status, skip_reason FROM followers
   WHERE campaign_id = 'ID' ORDER BY updated_at DESC LIMIT 10;
   ```
4. Campagna **scrape** con AI accesa → «Solo DM» grigio con la spiegazione.
5. Con AI spenta → tutti e tre i livelli cliccabili, su import e su scrape.
6. Aggira la UI e verifica che il backend regga:
   ```bash
   curl -s -X PATCH http://localhost:8000/api/campaigns/ID_CON_AI \
     -H "Content-Type: application/json" -d '{"enrichment_level":"none"}'
   ```
   Expected: `400`.

- [ ] **Step 2: Doppioni pre-esistenti**

La migration 039 ha azzerato `username_norm` sui doppioni invece di fonderli. Contali e riportali a Tommaso — **non fondere righe senza il suo ok**:

```sql
SELECT username, count(*) FROM global_contacts
WHERE username IS NOT NULL GROUP BY lower(trim(username)) HAVING count(*) > 1;
```

Esiste già `backend/scripts/bonifica_doppioni_targa_provvisoria.py` dalla bonifica precedente: **leggilo prima di scriverne un altro.**

- [ ] **Step 3: Contatti da ri-arricchire**

I follower marcati dalla Task 3 vanno visti, non dimenticati:

```sql
SELECT campaign_id, username, ig_user_id FROM followers
WHERE skip_reason IN ('handle_riassegnato', 'targa_gia_presente_su_altra_riga');
```

- [ ] **Step 4: Doc**

Riallinea `docs/architecture/DATABASE.md` (colonna nuova + indice), `docs/architecture/SCALA_E_PARALLELISMO.md` (il dedup cross-campagna **non esiste** ed è una scelta, non una dimenticanza), `INDEX.md` e `PROGRESS.md`.

- [ ] **Step 5: Memoria di progetto**

Sezione datata in `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md` + riga in `MEMORY.md`. Obbligatoria.

- [ ] **Step 6: PR**

Nel corpo: la tabella dei fatti verificati, le cinque decisioni di Tommaso, e l'elenco delle query degli Step 2-3 con i numeri trovati.
