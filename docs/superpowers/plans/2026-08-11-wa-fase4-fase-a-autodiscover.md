# Fase A auto-discover contatti WhatsApp — piano di implementazione

> **Per chi esegue:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` (consigliata)
> oppure `superpowers:executing-plans`, task per task. Gli step usano checkbox (`- [ ]`).
> REQUIRED anche `sviluppo-modulo` (standard di Tommaso: reviewer per ogni task, QA agent
> dopo ogni funzione, chiusura modulo con 20 test manuali + 30 adversarial).

**Goal:** scansionare in sola lettura la sidebar di WhatsApp Web di un numero già onboardato
ed estrarre nome/numero/tipo-chat di ogni conversazione in una tabella di staging
(`wa_discovered_chats`), senza inviare nulla e senza toccare warmup/cap/breaker/dead-man switch.

**Architettura:** stessa forma del motore inbox Instagram via browser
(`app/services/inbox_browser/`), che ha già pagato gli errori che questo modulo rifarebbe:
**il JS raccoglie, Python decide** — nessuna coordinata cablata, ogni decisione in una funzione
pura testabile senza browser. La riga da aprire si ri-risolve **per contenuto** subito prima del
click (la trappola dell'indice instabile del PoC-4 è la stessa che `apri_riga` ha già risolto).
Il gesto di scorrimento usa le misure vere registrate su questo PC
(`backend/data/scroll_umano.json`), non un modello inventato.

**Tech Stack:** Python 3.13, patchright (fork stealth di Playwright), SQLAlchemy 2 async,
Alembic, pytest + pytest-asyncio, loguru.

## Vincoli globali

- **Sola lettura assoluta.** La Fase A non invia, non digita, non apre chat nuove. Se un task
  tocca warmup/cap/breaker/dead-man switch è fuori scope (spec §5.5).
- **Regola V2** rispettata per costruzione: si leggono chat che esistono già.
- **P12 — mai un numero in chiaro in un campo testuale.** Il 39% delle chat di Primero ha il
  numero COME TITOLO (misurato, PoC-5): quel titolo va in `encrypted_phone` + `phone_hmac`,
  **mai** in `chat_title`. Stessa regola che `WaContact.chat_title` già applica.
- **Migrazione `033`.** La `032` è riservata all'altra sessione (piano inbox-browser velocità).
  La migrazione va su `main` con PR dedicata **prima** di essere applicata al DB condiviso,
  o ogni `start.bat` muore con `Can't locate revision identified by '033'`.
- **Test**: `DATABASE_URL` sqlite forzato da conftest, mai Supabase prod. `WA_TEST_DB_SLOT=<nome>`
  sempre. **Una sola suite pytest alla volta** (DB condiviso → rossi fantasma).
- **Repo condivisa**: prima di ogni commit `git status` e `git branch --show-current`. Mai
  `git add .` cieco: verificare `git diff <file>` e stageare solo i propri file.
- **Non lanciare il browser dal worktree**: i path dei profili sono relativi, partirebbe un
  profilo vuoto = dispositivo nuovo. Per un browser vivo si usa `D:\BOT OUTBOUND\backend`.
- **Codice → branch + PR**, mai commit diretti su `main`.
- I test `test_wa_*` rossi da prima (17 locali, 8 CI) **non sono regressioni**: non "sistemarli".

## Misure su cui questo piano si appoggia (non riaprirle, sono state pagate)

| Misura | Valore | Fonte |
|---|---|---|
| Chat totali Primero | 291 (`aria-rowcount`) | PoC-4 / PoC-5 |
| Costo apertura pannello info | 5,3s medi (3,5–8,7) | PoC-4 |
| Pannello info apribile | 19/20 = 95% | PoC-4 |
| Numero leggibile nel pannello | 100% chat 1:1, 0% gruppi | PoC-4 |
| **Titolo = numero (nessuna apertura necessaria)** | **39% Primero, 14% personale** | PoC-5 |
| Rilevamento gruppo per testo ("N partecipanti") | recall **1/6** — inutilizzabile | PoC-4 |
| Indice di riga dopo aperture in sequenza | mismatch 4-5 su 8 | PoC-4 |
| Etichette Business su Web | esistono (`Impostazioni → Strumenti business`) ma Primero non è Business | PoC-5 |
| Gesto di scorrimento reale (trackpad) | 1472–2517 px/s, picchi 193–342 px/evento, 16,7 ms fra eventi | `registra_scroll_umano.py` |

## Struttura dei file

```
backend/
  alembic/versions/033_wa_discovered_chats.py      NUOVO  migrazione staging
  app/models/wa.py                                 MODIFICA  + WaDiscoveredChat
  app/services/wa_discover/
    __init__.py                                    NUOVO
    classifica.py                                  NUOVO  funzioni pure: gruppo? numero? 
    sincronizzazione.py                            NUOVO  lettura % sync (gate)
    sidebar.py                                     NUOVO  DOM sidebar: scan + scorrimento
    pannello.py                                    NUOVO  DOM pannello info: apri + leggi numero
    salvataggio.py                                 NUOVO  staging idempotente
  app/services/wa_discover_run.py                  NUOVO  orchestratore del giro
  tests/test_wa_discover_classifica.py             NUOVO
  tests/test_wa_discover_sincronizzazione.py       NUOVO
  tests/test_wa_discover_sidebar.py                NUOVO
  tests/test_wa_discover_pannello.py               NUOVO
  tests/test_wa_discover_salvataggio.py            NUOVO
  tests/test_wa_discover_run.py                    NUOVO
```

Perché moduli separati e non un file solo: è la stessa divisione di `inbox_browser/`, e la
ragione è che le funzioni pure (`classifica.py`) devono essere testabili **senza browser**.
Un modulo unico costringerebbe a montare una pagina finta per testare una regola su una stringa.

---

### Task 1: migrazione 033 + modello `WaDiscoveredChat`

Va da sola in una PR propria, mergiata **prima** di tutto il resto (vincolo migrazioni).

**Files:**
- Create: `backend/alembic/versions/033_wa_discovered_chats.py`
- Modify: `backend/app/models/wa.py` (in fondo, dopo `WaContact`)
- Test: `backend/tests/test_wa_discover_modello.py`

**Interfaces:**
- Produce: `WaDiscoveredChat` con campi `id, tenant_id, number_id, chat_title, display_name,
  encrypted_phone, phone_hmac, numero_leggibile, tipo_chat, source_filtro, discovered_at,
  updated_at, status`. I task 6-7 ci scrivono sopra.

- [ ] **Step 1: scrivere il test che fallisce**

```python
# backend/tests/test_wa_discover_modello.py
import pytest
from sqlalchemy import select

from app.models.wa import WaDiscoveredChat


@pytest.mark.asyncio
async def test_chat_senza_numero_e_salvabile(db_session, tenant, wa_number):
    """Una chat di cui non si legge il numero DEVE essere salvabile.

    E' la differenza che ha reso necessaria questa tabella invece di riusare
    WaContact, dove encrypted_phone e phone_hmac sono NOT NULL (spec 5.4).
    """
    riga = WaDiscoveredChat(
        tenant_id=tenant.id, number_id=wa_number.id,
        chat_title="AZIENDA AGRICOLA PRIMERO", display_name="AZIENDA AGRICOLA PRIMERO",
        encrypted_phone=None, phone_hmac=None, numero_leggibile=False,
        tipo_chat="gruppo", status="nuovo",
    )
    db_session.add(riga)
    await db_session.commit()

    trovata = (await db_session.execute(select(WaDiscoveredChat))).scalar_one()
    assert trovata.phone_hmac is None
    assert trovata.numero_leggibile is False


@pytest.mark.asyncio
async def test_stessa_chat_due_volte_non_duplica(db_session, tenant, wa_number):
    """Ri-scansionare lo stesso numero deve essere innocuo (spec 5.3)."""
    from sqlalchemy.exc import IntegrityError
    comune = dict(tenant_id=tenant.id, number_id=wa_number.id,
                  chat_title="Fulvio", display_name="Fulvio",
                  encrypted_phone="x", phone_hmac="abc123",
                  numero_leggibile=True, tipo_chat="individuale", status="nuovo")
    db_session.add(WaDiscoveredChat(**comune))
    await db_session.commit()

    db_session.add(WaDiscoveredChat(**comune))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
```

- [ ] **Step 2: eseguire e vedere il rosso GIUSTO**

Run: `cd backend && WA_TEST_DB_SLOT=discover pytest tests/test_wa_discover_modello.py -v`
Atteso: `ImportError: cannot import name 'WaDiscoveredChat'`.
**Se fallisce per un altro motivo, il test è sbagliato**: correggilo prima di proseguire
(lezione `template_variant NOT NULL`: un rosso da helper incompleto non conta).

- [ ] **Step 3: il modello**

```python
# in coda a backend/app/models/wa.py

class WaDiscoveredChat(Base):
    """Staging delle chat scoperte dalla Fase A (spec 5.4).

    Tabella separata da wa_contacts per una ragione strutturale, non estetica:
    WaContact ha encrypted_phone e phone_hmac NOT NULL, quindi una chat di cui
    non si legge il numero -- che e' il 100% dei gruppi e una parte delle 1:1 --
    non potrebbe proprio esistere li'. Salvarla comunque, marcata, e' una
    decisione presa: il dato resta recuperabile a mano dalla rubrica.

    La roba grezza raccolta dal browser non tocca i contatti veri del bot finche'
    la Fase B non la promuove.
    """
    __tablename__ = "wa_discovered_chats"
    __table_args__ = (
        # Ri-scansionare la stessa lista deve essere innocuo (spec 5.3): la
        # seconda passata aggiorna, non duplica. La chiave e' il TITOLO e non il
        # numero, perche' il numero manca in tutti i gruppi e in parte delle 1:1
        # -- una unique su phone_hmac lascerebbe fuori proprio i casi piu'
        # frequenti.
        UniqueConstraint("number_id", "chat_title",
                         name="uq_wa_discovered_number_title"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"),
                                           nullable=False)
    # Da quale numero WhatsApp e' stata scoperta: due numeri dello stesso tenant
    # hanno rubriche diverse, e la stessa persona in entrambe e' due scoperte.
    number_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_numbers.id"),
                                           nullable=False)
    # Il titolo COSI' COME APPARE, tranne quando e' un numero: in quel caso resta
    # None e il numero va cifrato sotto (P12). E' il 39% delle chat su Primero,
    # non un caso di bordo.
    chat_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    encrypted_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_hmac: Mapped[str | None] = mapped_column(String(64), nullable=True)
    numero_leggibile: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 'individuale' | 'gruppo' | 'ignoto'. Tri-stato e non booleano: il PoC-4 ha
    # misurato recall 1/6 sul rilevamento gruppo, quindi "non lo so" e' uno stato
    # frequente e va detto, non nascosto dietro un False che sembra una risposta.
    tipo_chat: Mapped[str] = mapped_column(String(20), default="ignoto", nullable=False)
    # Quale filtro/etichetta era attivo durante lo scan, se mai ne useremo uno.
    # Oggi sempre None: le Liste non si sincronizzano su Web e Primero non e'
    # Business (PoC-5). Il campo resta perche' costa nulla e il giorno che il
    # filtro tornera' praticabile serve sapere da dove viene ogni riga.
    source_filtro: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 'nuovo' | 'promosso' | 'scartato' (spec 5.4). La Fase B lo muove.
    status: Mapped[str] = mapped_column(String(20), default="nuovo", nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                    default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=datetime.utcnow, nullable=False)
```

- [ ] **Step 4: la migrazione**

```python
# backend/alembic/versions/033_wa_discovered_chats.py
"""Staging delle chat scoperte dalla Fase A auto-discover WhatsApp.

Tabella nuova, nessun ALTER su tabelle esistenti: additiva e reversibile.
La 032 e' riservata al piano inbox-browser velocita' (altra sessione).

Revision ID: 033
Revises: 032
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wa_discovered_chats",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("number_id", sa.String(length=36), sa.ForeignKey("wa_numbers.id"), nullable=False),
        sa.Column("chat_title", sa.String(length=200), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("encrypted_phone", sa.Text(), nullable=True),
        sa.Column("phone_hmac", sa.String(length=64), nullable=True),
        sa.Column("numero_leggibile", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tipo_chat", sa.String(length=20), nullable=False, server_default="ignoto"),
        sa.Column("source_filtro", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="nuovo"),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("number_id", "chat_title", name="uq_wa_discovered_number_title"),
    )
    op.create_index("ix_wa_discovered_number_status", "wa_discovered_chats",
                    ["number_id", "status"])
    op.create_index("ix_wa_discovered_phone_hmac", "wa_discovered_chats", ["phone_hmac"])


def downgrade() -> None:
    op.drop_index("ix_wa_discovered_phone_hmac", table_name="wa_discovered_chats")
    op.drop_index("ix_wa_discovered_number_status", table_name="wa_discovered_chats")
    op.drop_table("wa_discovered_chats")
```

⚠️ **Prima di scrivere `down_revision = "032"`**: verifica che la 032 esista davvero su `main`.
Se l'altra sessione non l'ha ancora mergiata, `down_revision` è `"031"` e la tua diventa la 032.
Non fidarti di questo numero: `ls backend/alembic/versions/` all'inizio del task.

- [ ] **Step 5: verde**

Run: `cd backend && WA_TEST_DB_SLOT=discover pytest tests/test_wa_discover_modello.py -v`
Atteso: 2 passed.

- [ ] **Step 6: commit + PR SOLO della migrazione**

```bash
git add backend/alembic/versions/033_wa_discovered_chats.py backend/app/models/wa.py backend/tests/test_wa_discover_modello.py
git commit -m "feat(wa-discover): tabella di staging per le chat scoperte dalla Fase A"
```

Poi PR e merge su `main` **prima** del Task 2. È il vincolo delle migrazioni.

---

### Task 2: `classifica.py` — le decisioni pure

Il cuore testabile senza browser: dato ciò che il DOM ha raccolto, decidere cos'è.

**Files:**
- Create: `backend/app/services/wa_discover/__init__.py` (vuoto), `backend/app/services/wa_discover/classifica.py`
- Test: `backend/tests/test_wa_discover_classifica.py`

**Interfaces:**
- Produce: `titolo_e_numero(titolo) -> bool`, `numero_dal_titolo(titolo) -> str | None`,
  `tipo_da_segnali(numero_leggibile, testo_pannello, titolo) -> str`,
  `RigaScoperta` (dataclass: `titolo, numero, numero_leggibile, tipo`).
  I task 4, 5 e 6 consumano queste.

- [ ] **Step 1: il test che fallisce, coi dati VERI del PoC-5**

```python
# backend/tests/test_wa_discover_classifica.py
import pytest

from app.services.wa_discover.classifica import (
    numero_dal_titolo, tipo_da_segnali, titolo_e_numero,
)


# Titoli veri letti dalla sidebar di Primero l'11/08 (PoC-5). Non inventati.
GRUPPI_VERI = [
    "AZIENDA AGRICOLA PRIMERO", "CONSEGNE DOMICILIO", "ORDINI VENDITORI PRIMERO",
    "SPEDIZIONI", "( INFO E BROADCAST)AMICI DELLA GALASSIA", "Primero Sales Team",
]
PERSONE_VERE = ["Fulvio", "Alessio Tutti A Tavola", "Michele Carrozza 🛺", "Mamma"]


@pytest.mark.parametrize("titolo", ["+39 342 146 0077", "+393421460077", "+1 (555) 978-5671"])
def test_riconosce_il_titolo_che_e_un_numero(titolo):
    assert titolo_e_numero(titolo) is True
    assert numero_dal_titolo(titolo) is not None


@pytest.mark.parametrize("titolo", PERSONE_VERE + GRUPPI_VERI)
def test_un_nome_non_e_un_numero(titolo):
    assert titolo_e_numero(titolo) is False
    assert numero_dal_titolo(titolo) is None


def test_titolo_con_cifre_dentro_non_e_un_numero():
    """'4 cessi' e 'I Sopra Savo' sono titoli veri: contengono cifre ma non sono
    numeri di telefono. Un match troppo largo li cifrerebbe come tali."""
    for titolo in ["4 cessi 🚽", "Cuba 2 e bachata 2", "FYS - Comunicazioni ed eventi"]:
        assert titolo_e_numero(titolo) is False


def test_numero_non_leggibile_significa_ignoto_non_gruppo():
    """La proposta dell'handoff era di trattare 'numero non leggibile' come
    proxy di 'gruppo'. Qui si registra ma NON si asserisce come certezza: il
    tri-stato dice 'ignoto', perche' il PoC-4 ha misurato che il pannello
    fallisce nel 5% dei casi anche su chat 1:1 vere. Chiamarle 'gruppo'
    scarterebbe persone contattabili senza lasciare traccia del dubbio."""
    assert tipo_da_segnali(numero_leggibile=False, testo_pannello="", titolo="Fulvio") == "ignoto"


def test_numero_leggibile_significa_individuale():
    assert tipo_da_segnali(numero_leggibile=True, testo_pannello="+39 342 146 0077",
                           titolo="Fulvio") == "individuale"


def test_partecipanti_nel_pannello_significa_gruppo():
    """Segnale ad alta precisione ma bassa recall (1/6, PoC-4): quando c'e' si
    crede, quando manca non si conclude niente."""
    assert tipo_da_segnali(numero_leggibile=False, testo_pannello="12 partecipanti",
                           titolo="SPEDIZIONI") == "gruppo"
```

- [ ] **Step 2: rosso**

Run: `cd backend && WA_TEST_DB_SLOT=discover pytest tests/test_wa_discover_classifica.py -v`
Atteso: `ModuleNotFoundError: No module named 'app.services.wa_discover'`.

- [ ] **Step 3: implementazione**

```python
# backend/app/services/wa_discover/classifica.py
"""Le decisioni della Fase A, tutte pure e testabili senza browser.

REGOLA DI DISEGNO ereditata dal motore inbox Instagram: il JS raccoglie, Python
decide. Qui vive il 'decide'. Cablare queste regole dentro una query JS le
renderebbe verificabili solo con un browser vivo e una sessione WhatsApp reale --
cioe' mai, in pratica.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.utils.phone_pseudonym import PhoneNormalizationError, normalize_e164

# Un titolo che e' un numero: solo cifre e separatori tipografici, almeno 8
# cifre. Il confine stretto e' voluto -- '4 cessi' e 'Cuba 2 e bachata 2' sono
# titoli veri, e un match largo li cifrerebbe come numeri di telefono.
_SOLO_NUMERO = re.compile(r"^[+\d][\d\s\-.()/\u202a-\u202e\u2066-\u2069]*$")
_ALMENO_8_CIFRE = re.compile(r"(?:\D*\d){8}")
# Il segnale ad alta precisione per i gruppi (recall bassa: 1/6, PoC-4).
_PARTECIPANTI = re.compile(r"\d+\s*(partecipant|membr|iscritt)", re.IGNORECASE)

TIPO_INDIVIDUALE = "individuale"
TIPO_GRUPPO = "gruppo"
TIPO_IGNOTO = "ignoto"


@dataclass
class RigaScoperta:
    titolo: str | None          # None quando il titolo E' il numero (P12)
    numero: str | None          # E.164, gia' normalizzato
    numero_leggibile: bool
    tipo: str                   # TIPO_*


def titolo_e_numero(titolo: str | None) -> bool:
    """Il titolo della chat e' il numero stesso (contatto non in rubrica).

    Misurato: 39% delle chat di Primero, 14% del numero personale (PoC-5). Non
    e' un caso di bordo, e' un caso frequente -- e sono proprio le chat per cui
    NON serve aprire il pannello info, cioe' quelle che fanno risparmiare i 5,3s.
    """
    t = (titolo or "").strip()
    if not t or not _SOLO_NUMERO.match(t):
        return False
    return bool(_ALMENO_8_CIFRE.match(t))


def numero_dal_titolo(titolo: str | None) -> str | None:
    """Il numero E.164 se il titolo e' un numero, altrimenti None.

    Non solleva: un titolo che sembra un numero ma non normalizza (prefisso
    esotico, lunghezza fuori range) non e' un guasto -- e' una riga che verra'
    trattata come 'numero non letto', esattamente come un pannello illeggibile.
    """
    if not titolo_e_numero(titolo):
        return None
    try:
        return normalize_e164(titolo)
    except PhoneNormalizationError:
        return None


def tipo_da_segnali(*, numero_leggibile: bool, testo_pannello: str | None,
                    titolo: str | None) -> str:
    """'individuale' | 'gruppo' | 'ignoto'.

    Tri-stato e non booleano, per una ragione misurata: l'euristica testuale sui
    gruppi ha recall 1/6 (PoC-4), e il pannello info fallisce nel 5% dei casi
    anche su chat 1:1 vere. Con un booleano quel 5% diventerebbe 'gruppo' --
    persone contattabili scartate senza lasciare traccia del dubbio. 'ignoto' e'
    una risposta onesta e resta filtrabile nella UI della Fase B.
    """
    if _PARTECIPANTI.search(testo_pannello or ""):
        return TIPO_GRUPPO
    if numero_leggibile:
        return TIPO_INDIVIDUALE
    return TIPO_IGNOTO
```

- [ ] **Step 4: verde**

Run: `cd backend && WA_TEST_DB_SLOT=discover pytest tests/test_wa_discover_classifica.py -v`
Atteso: tutti passati. Verifica che `normalize_e164` esista con quel nome in
`app/utils/phone_pseudonym.py` (grep prima di importarla).

- [ ] **Step 5: commit**

```bash
git add backend/app/services/wa_discover/ backend/tests/test_wa_discover_classifica.py
git commit -m "feat(wa-discover): classificazione pura di titolo, numero e tipo chat"
```

---

### Task 3: `sincronizzazione.py` — il gate che impedisce lo scan cieco

**Perché esiste.** Se la Fase A scansiona mentre WhatsApp Web sta ancora tirando giù la
cronologia, raccoglie una frazione delle chat e dichiara "lista esaurita". Nessun errore,
nessun allarme: è la stessa forma del "collaudo 90/90 verde con zero messaggi inviati".
`aria-rowcount` stesso può essere parziale.

**Files:**
- Create: `backend/app/services/wa_discover/sincronizzazione.py`
- Test: `backend/tests/test_wa_discover_sincronizzazione.py`

**Interfaces:**
- Produce: `percentuale_da_testi(testi: list[str]) -> int | None`,
  `puo_scansionare(percentuale, soglia) -> tuple[bool, str]`. Il task 7 le chiama.

- [ ] **Step 1: test**

```python
# backend/tests/test_wa_discover_sincronizzazione.py
from app.services.wa_discover.sincronizzazione import (
    percentuale_da_testi, puo_scansionare,
)


def test_legge_la_percentuale_dal_pannello():
    assert percentuale_da_testi(["Sincronizzazione messaggi", "47%"]) == 47
    assert percentuale_da_testi(["Sincronizzazione in corso... 8 %"]) == 8


def test_nessuna_percentuale_significa_non_lo_so():
    """None non e' zero. Un pannello che non espone la percentuale (o l'ha gia'
    tolta perche' ha finito) non deve diventare '0%' e bloccare tutto per
    sempre: la decisione su cosa fare con l'incertezza sta in puo_scansionare."""
    assert percentuale_da_testi(["Impostazioni", "Account", "Privacy"]) is None
    assert percentuale_da_testi([]) is None


def test_percentuale_impossibile_viene_scartata():
    """Un '2026%' o un '150%' viene da un match sbagliato, non da WhatsApp."""
    assert percentuale_da_testi(["IT01879020517A2026%"]) is None
    assert percentuale_da_testi(["150%"]) is None


def test_sopra_soglia_si_parte():
    ok, motivo = puo_scansionare(72, soglia=60)
    assert ok is True
    assert "72" in motivo


def test_sotto_soglia_non_si_parte():
    ok, motivo = puo_scansionare(31, soglia=60)
    assert ok is False
    assert "31" in motivo and "60" in motivo


def test_percentuale_ignota_si_parte_ma_lo_si_dice():
    """Decisione presa: l'incertezza non blocca. La percentuale sparisce anche
    quando la sincronizzazione E' FINITA, e trattare 'non lo so' come 'fermo'
    renderebbe la Fase A inavviabile proprio nel caso normale. Ma il motivo
    deve dirlo, perche' una raccolta parziale va diagnosticata da qui."""
    ok, motivo = puo_scansionare(None, soglia=60)
    assert ok is True
    assert "non" in motivo.lower()
```

- [ ] **Step 2: rosso** — `ModuleNotFoundError`.

- [ ] **Step 3: implementazione**

```python
# backend/app/services/wa_discover/sincronizzazione.py
"""Il gate della Fase A: quanto e' sincronizzata la sessione WhatsApp Web.

Idea di Tommaso (11/08), e non e' un'alternativa alla quarantena esistente: e'
la MISURA DIRETTA di cio' che la quarantena stima a occhio. Oggi
_attendi_quarantena_risync (wa_worker.py) aspetta WA_RESYNC_QUARANTINE_MIN
minuti a browser aperto e fermo, per ogni mini-sessione, e la motivazione scritta
nel codice e' proprio la sincronizzazione ("finche' non ha finito la guardia
opt-out leggerebbe il vuoto invece del silenzio"). Un timer cieco sbaglia in
entrambe le direzioni.

ATTENZIONE ALLO SCOPE. Questo modulo governa lo SCAN, non l'invio. Per lo scan
una soglia bassa costa una raccolta parziale (recuperabile: si riscansiona). Per
l'invio costerebbe un messaggio a chi ha risposto STOP senza che noi lo avessimo
ancora sincronizzato.

L'ORDINE DI SINCRONIZZAZIONE E' NOTO, e questo restringe molto quel rischio.
Testo letterale del pannello, misurato l'11/08 col numero personale:

    "Sincronizzazione dei messaggi precedenti in corso"
    "Completata al 61%"   ->   "Completata all'87%" novanta secondi dopo

"Messaggi PRECEDENTI": WhatsApp scarica all'indietro, i recenti ci sono gia'.
Quindi un opt-out recente e' sincronizzato molto prima del 100%, e la soglia
bassa proposta da Tommaso e' difendibile anche per l'invio. Riserva residua, piu'
stretta ma reale: se il numero e' rimasto DISCONNESSO per giorni, gli opt-out di
quel periodo sono "precedenti" anche loro. Chi tocchera' la quarantena d'invio
deve tenerne conto -- p.es. soglia piu' alta quando session_checked_at e' vecchio.
"""
from __future__ import annotations

import re

# Una percentuale plausibile: 0-100. Il filtro sul range non e' pedanteria --
# 'IT01879020517A2026%' e' un nome di file vero visto nella sidebar, e senza
# limite superiore diventerebbe una sincronizzazione al 2026%.
_PERCENTUALE = re.compile(r"(?<!\d)(\d{1,3})\s*%")

SOGLIA_DEFAULT = 60


def percentuale_da_testi(testi: list[str] | None) -> int | None:
    """La percentuale di sincronizzazione, o None se il pannello non la espone.

    None NON e' zero: la percentuale sparisce anche quando la sincronizzazione
    e' finita. Confondere le due cose bloccherebbe la Fase A nel caso normale.
    """
    for t in testi or []:
        for grezzo in _PERCENTUALE.findall(t or ""):
            valore = int(grezzo)
            if 0 <= valore <= 100:
                return valore
    return None


def puo_scansionare(percentuale: int | None, soglia: int = SOGLIA_DEFAULT) -> tuple[bool, str]:
    """(si_parte, motivo). Il motivo finisce nei log e negli eventi: e' il primo
    posto dove si guarda quando una raccolta risulta piu' corta del previsto."""
    if percentuale is None:
        return True, ("percentuale di sincronizzazione non esposta dal pannello: "
                      "si procede, ma se la raccolta risulta corta e' il primo indiziato")
    if percentuale >= soglia:
        return True, f"sincronizzazione al {percentuale}% (soglia {soglia}%)"
    return False, (f"sincronizzazione al {percentuale}%, sotto la soglia del {soglia}%: "
                   "scansionare ora raccoglierebbe una parte delle chat e la "
                   "dichiarerebbe completa")
```

- [ ] **Step 4: verde.** Run: `pytest tests/test_wa_discover_sincronizzazione.py -v`

- [ ] **Step 5: commit**

```bash
git add backend/app/services/wa_discover/sincronizzazione.py backend/tests/test_wa_discover_sincronizzazione.py
git commit -m "feat(wa-discover): gate di sincronizzazione prima dello scan"
```

---

### Task 4: `sidebar.py` — leggere le righe e scorrere come una mano vera

**Files:**
- Create: `backend/app/services/wa_discover/sidebar.py`
- Test: `backend/tests/test_wa_discover_sidebar.py`

**Interfaces:**
- Consuma: `classifica.RigaScoperta`, `classifica.titolo_e_numero`.
- Produce: `piano_scroll_misurato(px) -> list[tuple[int, float]]`,
  `righe_dalla_sidebar(grezze) -> list[dict]`, `async scan_sidebar(page) -> list[dict]`,
  `async scorri_sidebar(page) -> StatoScorrimento`, `StatoScorrimento(altezza, al_fondo, rowcount)`.

**Il punto delicato: il gesto di scorrimento.** Il motore inbox Instagram usa
`SCATTO_MAX_PX = 60`, ma le misure vere su questo PC (`backend/data/scroll_umano.json`,
1660 eventi) dicono **picchi 193–342 px** e **16,7 ms** fra eventi. Il modello attuale è
sottostimato di 3-5×: non va ereditato, vanno ereditate le misure.

- [ ] **Step 1: test**

```python
# backend/tests/test_wa_discover_sidebar.py
import statistics

from app.services.wa_discover.sidebar import (
    piano_scroll_misurato, righe_dalla_sidebar,
)


def test_il_gesto_rispetta_i_picchi_misurati():
    """Le misure vere (registra_scroll_umano.py, 1660 eventi, trackpad): picchi
    193-342 px per evento. Un gesto che non supera mai i 60 px e' il modello
    inventato del motore Instagram, ed e' 3-5 volte piu' lento del vero."""
    piano = piano_scroll_misurato(3000)
    picco = max(abs(px) for px, _ in piano)
    assert 100 < picco <= 342, f"picco {picco} fuori dal range misurato"


def test_il_gesto_copre_la_distanza_chiesta():
    piano = piano_scroll_misurato(3000)
    percorso = sum(px for px, _ in piano if px > 0)
    assert 2500 <= percorso <= 3500


def test_le_pause_sono_a_ritmo_di_frame():
    """16,7 ms mediani fra eventi: e' il ritmo che detta il browser, non una
    scelta. Pause da decine di ms sarebbero una firma."""
    piano = piano_scroll_misurato(3000)
    mediana = statistics.median(p for _, p in piano)
    assert 0.008 <= mediana <= 0.040


def test_due_gesti_non_sono_identici():
    a = piano_scroll_misurato(3000)
    b = piano_scroll_misurato(3000)
    assert a != b


def test_scarta_le_righe_senza_titolo():
    """La lista virtualizzata tiene nel DOM placeholder senza testo."""
    grezze = [{"titolo": "", "top": 200}, {"titolo": "Fulvio", "top": 260}]
    assert [r["titolo"] for r in righe_dalla_sidebar(grezze, altezza_viewport=800)] == ["Fulvio"]


def test_scarta_le_righe_sotto_la_piega():
    """Misurato sul motore Instagram: righe a top=1473 con finestra alta 660
    passavano il filtro e arrivavano fino all'apertura, che le buttava con
    'nome mancante' -- rumore che nascondeva i fallimenti veri."""
    grezze = [{"titolo": "Fulvio", "top": 1473}, {"titolo": "Mamma", "top": 300}]
    assert [r["titolo"] for r in righe_dalla_sidebar(grezze, altezza_viewport=660)] == ["Mamma"]
```

- [ ] **Step 2: rosso.**

- [ ] **Step 3: implementazione**

```python
# backend/app/services/wa_discover/sidebar.py
"""La sidebar di WhatsApp Web: leggerne le righe e scorrerla come una mano.

I NUMERI DI QUESTO MODULO SONO MISURATI, NON MODELLATI. Vengono da
scripts/registra_scroll_umano.py (1660 eventi wheel registrati sull'hardware di
Tommaso l'11/08): verdetto TRACKPAD, 16,7 ms mediani fra eventi, picchi 193-342
px, gesti da 5.700 a 24.000 px. Il motore inbox Instagram usa ancora un modello
scritto a mano (SCATTO_MAX_PX=60) che quelle misure hanno smentito: non
ereditarlo, ereditare le misure.
"""
from __future__ import annotations

import math
import random

# Dalle misure vere: i picchi osservati stanno fra 193 e 342 px per evento.
PICCO_MIN_PX = 150
PICCO_MAX_PX = 342
# 16,7 ms e' la mediana misurata: e' il ritmo di frame del browser, non una
# scelta di design. Il caso qui non c'entra quasi niente.
PAUSA_MIN_S = 0.010
PAUSA_MAX_S = 0.030
# Sotto una schermata: sopra il buffer renderizzato si perdono righe in silenzio
# (lista virtualizzata, stessa trappola del motore Instagram).
FRAZIONE_PASSO_MIN = 0.6
FRAZIONE_PASSO_MAX = 0.8
PROB_RIMBALZO = 0.12


def piano_scroll_misurato(px_totali: int) -> list[tuple[int, float]]:
    """Il gesto come sequenza di (pixel, pausa). Profilo a campana.

    La campana (piano -> veloce -> piano) e' cio' che i gesti registrati
    mostrano: una sequenza di scatti tutti uguali e' un motore, non una mano.
    Il picco della campana si estrae nel range MISURATO, invece di essere
    tagliato a un massimo inventato.
    """
    if px_totali <= 0:
        return []

    picco = random.uniform(PICCO_MIN_PX, PICCO_MAX_PX)
    # Quanti eventi servono: l'area sotto una campana di ampiezza `picco` con
    # `n` eventi vale circa picco * n * 2/pi.
    quanti = max(6, round(px_totali / (picco * 2 / math.pi)))

    pesi = [math.sin(math.pi * (i + 0.5) / quanti) for i in range(quanti)]
    totale = sum(pesi) or 1.0

    piano: list[tuple[int, float]] = []
    for peso in pesi:
        delta = px_totali * peso / totale * random.uniform(0.85, 1.15)
        piano.append((max(1, min(PICCO_MAX_PX, round(delta))),
                      random.uniform(PAUSA_MIN_S, PAUSA_MAX_S)))

    if random.random() < PROB_RIMBALZO and len(piano) > 3:
        dove = random.randint(len(piano) // 2, len(piano) - 1)
        piano.insert(dove, (-random.randint(8, 40),
                            random.uniform(PAUSA_MIN_S, PAUSA_MAX_S)))
    return piano


def righe_dalla_sidebar(grezze: list[dict], altezza_viewport: int) -> list[dict]:
    """Solo le righe che sono davvero chat visibili. Funzione pura."""
    fuori = []
    for r in grezze or []:
        if not (r.get("titolo") or "").strip():
            continue
        if float(r.get("top", 0)) >= altezza_viewport:
            continue
        fuori.append(r)
    return fuori
```

Le parti che toccano `page` (`scan_sidebar`, `scorri_sidebar`) vanno scritte nello stesso
file seguendo `inbox_browser/pagina.py:scorri` — **con una differenza obbligatoria**: su
WhatsApp il contenitore della lista è `#pane-side`, che esiste ed è stabile (misurato in
PoC-5: `left=65, right=474`), quindi non serve la ricerca euristica del contenitore che su
Instagram era necessaria. Leggere anche `aria-rowcount` dal `[role='grid']`: è il totale
dichiarato, e confrontarlo col raccolto è come si scopre una raccolta parziale.

- [ ] **Step 4: verde.** - [ ] **Step 5: commit.**

---

### Task 5: `pannello.py` — aprire la chat e leggere il numero, senza sbagliare persona

**La trappola da non riscoprire** (PoC-4, misurata: 4-5 mismatch su 8): aprire una chat la
segna letta, WhatsApp riordina la lista per ultima attività, e gli indici presi a inizio scan
puntano alla chat sbagliata. Il motore Instagram l'ha già risolta e la soluzione va copiata:
**ri-risolvere la riga per contenuto (titolo) immediatamente prima del click**, più
**verifica post-click** che ciò che si è aperto sia ciò che si voleva.

**Files:**
- Create: `backend/app/services/wa_discover/pannello.py`
- Test: `backend/tests/test_wa_discover_pannello.py`

**Interfaces:**
- Produce: `titolo_combacia(atteso, trovato) -> bool`,
  `numero_dal_pannello(testo) -> str | None`,
  `async apri_e_leggi(page, titolo_atteso) -> tuple[str | None, str]` → `(numero_e164, testo_pannello)`.

- [ ] **Step 1: test**

```python
# backend/tests/test_wa_discover_pannello.py
from app.services.wa_discover.pannello import numero_dal_pannello, titolo_combacia


def test_titolo_mancante_non_combacia_mai():
    """Meglio rinunciare a una riga che salvare un numero attribuito alla
    persona sbagliata. Stessa regola di inbox_browser.nome_combacia."""
    assert titolo_combacia(None, "Fulvio") is False
    assert titolo_combacia("Fulvio", None) is False
    assert titolo_combacia("", "") is False


def test_titolo_combacia_a_meno_di_spazi_e_maiuscole():
    assert titolo_combacia("  Fulvio CBD ", "fulvio cbd") is True


def test_titolo_diverso_non_combacia():
    assert titolo_combacia("Fulvio", "Fulvio CBD") is False


def test_legge_il_numero_dal_testo_del_pannello():
    testo = "Fulvio CBD\n+39 342 146 0077\nInfo contatto\nFile multimediali"
    assert numero_dal_pannello(testo) == "393421460077"


def test_pannello_di_gruppo_non_ha_un_numero():
    """Misurato PoC-4: 0% di numeri leggibili sui gruppi. Non e' un buco, e'
    corretto -- un gruppo non ha un numero singolo."""
    assert numero_dal_pannello("SPEDIZIONI\n12 partecipanti\nAggiungi partecipante") is None


def test_non_scambia_una_data_o_un_orario_per_un_numero():
    assert numero_dal_pannello("Fulvio\nUltimo accesso 11/08/2026 alle 14:35") is None
```

- [ ] **Step 2: rosso.** - [ ] **Step 3: implementazione** seguendo `apri_riga`
  (`inbox_browser/pagina.py:414-533`) per la struttura: risoluzione per contenuto, click,
  attese a pazienza crescente sull'header, verifica, e **nessuna scrittura se la verifica
  fallisce** (la riga si ritenta al giro dopo, mai a metà).
  Selettori dal PoC-4, già verificati: header `#main header` /
  `header[data-testid='conversation-header']`, pannello `[data-testid='drawer-right']`
  (95% di successo).
- [ ] **Step 4: verde.** - [ ] **Step 5: commit.**

---

### Task 6: `salvataggio.py` — staging idempotente

Copia la forma di `inbox_browser/salvataggio.py`: lookup esplicita, fusione che **integra e
non sovrascrive**, gestione di `IntegrityError` come ripiego sulla fusione (due processi
concorrenti sulla stessa chat).

**Files:**
- Create: `backend/app/services/wa_discover/salvataggio.py`
- Test: `backend/tests/test_wa_discover_salvataggio.py`

**Interfaces:**
- Consuma: `classifica.RigaScoperta`, modello `WaDiscoveredChat`.
- Produce: `async salva_scoperta(db, tenant_id, number_id, riga) -> str` (`'creata'|'aggiornata'`).

**Il test che conta più di tutti:**

```python
@pytest.mark.asyncio
async def test_il_numero_non_finisce_mai_in_chiaro(db_session, tenant, wa_number):
    """P12. Il 39% delle chat di Primero ha il numero COME TITOLO: se finisse in
    chat_title, avremmo una tabella piena di numeri in chiaro."""
    riga = RigaScoperta(titolo=None, numero="393421460077",
                        numero_leggibile=True, tipo="individuale")
    await salva_scoperta(db_session, tenant.id, wa_number.id, riga)

    salvata = (await db_session.execute(select(WaDiscoveredChat))).scalar_one()
    assert salvata.chat_title is None
    assert salvata.phone_hmac == hmac_phone("393421460077")
    assert "3421460077" not in (salvata.chat_title or "")
    assert "3421460077" not in (salvata.display_name or "")


@pytest.mark.asyncio
async def test_riscansione_aggiorna_e_non_duplica(db_session, tenant, wa_number):
    """Spec 5.3: ri-scansionare la stessa lista deve essere innocuo."""
    riga = RigaScoperta(titolo="Fulvio", numero=None, numero_leggibile=False, tipo="ignoto")
    assert await salva_scoperta(db_session, tenant.id, wa_number.id, riga) == "creata"

    migliore = RigaScoperta(titolo="Fulvio", numero="393421460077",
                            numero_leggibile=True, tipo="individuale")
    assert await salva_scoperta(db_session, tenant.id, wa_number.id, migliore) == "aggiornata"

    tutte = (await db_session.execute(select(WaDiscoveredChat))).scalars().all()
    assert len(tutte) == 1
    assert tutte[0].numero_leggibile is True     # il dato migliore vince


@pytest.mark.asyncio
async def test_una_riga_promossa_non_torna_indietro(db_session, tenant, wa_number):
    """Stessa logica di stato_vincente in inbox_browser: uno stato piu' avanzato
    non retrocede. Una chat gia' promossa a WaContact che tornasse 'nuovo'
    verrebbe promossa due volte."""
```

- [ ] Step 1-5 come sopra (test → rosso → implementazione → verde → commit).

---

### Task 7: `wa_discover_run.py` — l'orchestratore

Struttura ricalcata su `scrape_inbox_browser.run_inbox_browser_list`, con le differenze
imposte dal dominio:

- **niente campagna**: la Fase A lavora su un `number_id`, non su una campagna;
- **kill-switch WA**: `bot_state_service.is_wa_halted()` (non `is_halted`, che è Instagram —
  sono due colonne diverse, trappola già documentata nello spec §2.2);
- **lock del profilo obbligatorio**: `wa_profile_lock` prima di aprire il browser, rinnovato
  durante il giro (il cron `wa_session_healthcheck` apre lo stesso profilo);
- **gate di sincronizzazione** (Task 3) prima di iniziare, e **nessuna** quarantena d'invio:
  la Fase A non manda niente;
- **ritmo**: riusare `inbox_browser.ritmo` (lognormale troncata per riestrazione, mai
  clampata) con la distinzione azione/scorrimento — una riga risolta dal solo titolo non ha
  toccato WhatsApp e non merita la pausa di un'apertura;
- **fine lista**: `aria-rowcount` confrontato col raccolto. Se il raccolto è molto sotto il
  dichiarato, l'evento finale deve dirlo — è così che si scopre una raccolta parziale invece
  di festeggiare un "completata".

**Files:** Create `backend/app/services/wa_discover_run.py`, Test
`backend/tests/test_wa_discover_run.py` (con una pagina finta, come
`tests/test_scrape_inbox_browser.py`).

- [ ] Step 1-5 come sopra.

---

## Chiusura del modulo (obbligatoria, skill `sviluppo-modulo`)

1. **20 test manuali** in `.superpowers/sdd/qa-wa-discover-tests.md`, eseguiti dal QA agent.
2. **30 test adversarial** in `.superpowers/sdd/qa-wa-discover-adversarial.md`. Categorie che
   qui non possono mancare:
   - titoli ostili: emoji (`PRIMERO 🤵👨‍🌾`, vero), marcatori bidi (`\u202a`, già visti nel
     catalogo), 200+ caratteri, titolo che normalizza uguale a un altro;
   - numeri ostili: prefissi esotici, `+1 (555) 978-5671` (vero), numeri non normalizzabili;
   - **concorrenza vera** (`asyncio.gather`, non sequenziale) su `salva_scoperta` con la
     stessa chat: deve fondere, non esplodere né duplicare;
   - macchina a stati: promozione doppia, scoperta di una chat già promossa;
   - **invariante finale via SQL**: nessuna riga con un numero in chiaro in `chat_title` o
     `display_name`, nessuna riga con `numero_leggibile=True` e `phone_hmac IS NULL`.
3. **Fix loop fino al 100%.** "Quasi tutti" = modulo non chiuso.
4. **Review dell'intero branch** (`superpowers:requesting-code-review`).

## Fuori da questo piano, con motivo

| Cosa | Perché fuori |
|---|---|
| **Fase B** (promozione a `WaContact` + campagna) | Piano proprio. La Fase A da sola produce software completo e verificabile: scan + staging. |
| Filtro per etichetta/lista | Le Liste non si sincronizzano su Web e Primero non è Business (PoC-5). Il campo `source_filtro` resta pronto. |
| Sostituire la quarantena d'invio con la soglia di sync | Ora è **fattibile** (l'ordine è noto: "messaggi precedenti", quindi all'indietro) e farebbe risparmiare ~15 min di browser fermo per ogni mini-sessione. Ma tocca `wa_worker`/`wa_sender`, cioè il percorso d'invio: merita la sua PR e i suoi test, non un'aggiunta di contrabbando a un piano di sola lettura. |
| UI della Fase A | La Fase B avrà bisogno di una lista approvabile: si disegna lì, non qui. |
