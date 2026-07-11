# Messaggi template no-AI (default) + toggle AI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** i DM di default vengono renderizzati localmente da template A/B/C con spintax (zero AI, zero token); l'AI resta disponibile per-campagna dietro toggle `ai_enabled`, con contesto + system prompt per-campagna.

**Architecture:** nuova colonna `campaigns.ai_enabled` (default false, esistenti→true) + `message_template_c` + `ai_system_prompt`. Nuovo modulo puro `template_renderer.py` (spintax → nome → normalizzazione). Un'unica entry `compose_message(campaign, follower)` in `ai_personalizer.py` sostituisce la logica duplicata nei 4 call-site (preview batch, batch, orchestrator, regenerate API). Pipeline (approvazione/invio/retry) invariata.

**Tech Stack:** Python 3.13 + SQLAlchemy async + Alembic (Postgres prod, sqlite nei test — vedi `tests/conftest.py` che forza `DATABASE_URL=sqlite` prima degli import `app.*`), pytest-asyncio; Next.js 14 + TypeScript (frontend, nessuna test-infra: verifica = `npm run build`).

**Spec:** `docs/superpowers/specs/2026-07-11-template-messages-no-ai-design.md`

## Global Constraints

- Branch: `feat/template-messages` (repo `D:\BOT OUTBOUND`). Commit frequenti, messaggi in italiano come da storia repo.
- Migration nuova = `023`, `down_revision = "022"`.
- I test girano SEMPRE da `D:\BOT OUTBOUND\backend` con `.\venv\Scripts\python.exe -m pytest`.
- NON toccare: pipeline invio (`dm_sender.py`, `instagram_page.py`), approvazione, recovery.
- `messages.template_variant` è `String(1)`: le varianti sono `'a'`, `'b'`, `'c'`.
- Suite attuale: 512 passed — deve restare verde a ogni task.
- Convenzione repo: a fine lavoro aggiornare `docs/project/PROGRESS.md`, memoria persistente `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md` (sezione datata) — fatto nel Task 7.

---

### Task 1: Migration 023 + colonne modello + schemi API

**Files:**
- Create: `backend/alembic/versions/023_ai_enabled_template_c.py`
- Modify: `backend/app/models/campaign.py` (dopo riga 57, `message_template_b`)
- Modify: `backend/app/schemas/campaign.py` (CampaignCreate, CampaignUpdate, CampaignResponse)
- Test: `backend/tests/test_template_mode_schema.py`

**Interfaces:**
- Produces: `Campaign.ai_enabled: bool` (default Python `False`), `Campaign.message_template_c: str | None`, `Campaign.ai_system_prompt: str | None`; stessi campi su `CampaignCreate` (default `ai_enabled=False`), `CampaignUpdate` (tutti opzionali), `CampaignResponse`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_template_mode_schema.py
"""Colonne ai_enabled / message_template_c / ai_system_prompt su Campaign + schemi."""
import pytest
from app.models.campaign import Campaign
from app.schemas.campaign import CampaignCreate, CampaignUpdate, CampaignResponse


def test_campaign_model_defaults():
    c = Campaign(name="t")
    # default Python: nuove campagne nascono senza AI
    assert c.ai_enabled is False or c.ai_enabled is None  # None pre-flush, False post-default
    assert c.message_template_c is None
    assert c.ai_system_prompt is None


@pytest.mark.asyncio
async def test_campaign_model_persisted_defaults(db_session):
    c = Campaign(name="t")
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.ai_enabled is False
    assert c.message_template_c is None
    assert c.ai_system_prompt is None


def test_create_schema_defaults():
    data = CampaignCreate(name="x", target_username="acme",
                          base_message_template="Ciao {nome}, ti scrivo per...")
    assert data.ai_enabled is False
    assert data.message_template_c is None
    assert data.ai_system_prompt is None


def test_update_schema_accepts_new_fields():
    u = CampaignUpdate(ai_enabled=True, message_template_c="Template C abbastanza lungo",
                       ai_system_prompt="Tono formale.")
    assert u.ai_enabled is True
    assert u.message_template_c.startswith("Template C")
    assert u.ai_system_prompt == "Tono formale."


def test_response_schema_has_fields():
    fields = CampaignResponse.model_fields
    assert "ai_enabled" in fields
    assert "message_template_c" in fields
    assert "ai_system_prompt" in fields
```

Nota: se nel conftest non esiste una fixture `db_session`, cerca il nome reale con `grep -r "def db_session\|async def db\b" backend/tests/conftest.py` e adattalo; se non esiste alcuna fixture di sessione, sostituisci `test_campaign_model_persisted_defaults` con la sola versione sincrona `test_campaign_model_defaults`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_template_mode_schema.py -v`
Expected: FAIL — `AttributeError`/`ValidationError` (campi inesistenti).

- [ ] **Step 3: Add model columns**

In `backend/app/models/campaign.py`, subito dopo `message_template_b` (riga 57):

```python
    # Template mode: se False (default nuove campagne) i DM sono renderizzati
    # localmente dai template A/B/C + spintax, SENZA chiamate AI. Migration 023
    # setta True sulle campagne esistenti (comportamento invariato).
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Terzo template opzionale (variante 'c'), simmetrico a message_template_b.
    message_template_c: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Istruzioni AI per-campagna: override del prompt di sistema globale (.env).
    # NULL/vuoto = usa il globale.
    ai_system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Create migration 023**

```python
# backend/alembic/versions/023_ai_enabled_template_c.py
"""Template mode: ai_enabled + message_template_c + ai_system_prompt su campaigns.

ai_enabled nasce con server_default TRUE cosi' le campagne esistenti mantengono
il comportamento attuale (AI); subito dopo il default passa a FALSE per le nuove.

Revision ID: 023
Revises: 022
Create Date: 2026-07-11
"""
from alembic import op
import sqlalchemy as sa

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.alter_column("campaigns", "ai_enabled", server_default=sa.text("false"))
    op.add_column("campaigns", sa.Column("message_template_c", sa.Text(), nullable=True))
    op.add_column("campaigns", sa.Column("ai_system_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "ai_system_prompt")
    op.drop_column("campaigns", "message_template_c")
    op.drop_column("campaigns", "ai_enabled")
```

- [ ] **Step 5: Add schema fields**

In `backend/app/schemas/campaign.py`:

`CampaignCreate` — dopo `message_template_b` (riga 15):
```python
    # Template mode: False (default) = rendering locale A/B/C+spintax, no AI.
    ai_enabled: bool = False
    message_template_c: str | None = Field(default=None, min_length=10)
    # Override per-campagna del prompt di sistema AI (vuoto = globale .env)
    ai_system_prompt: str | None = None
```

`CampaignUpdate` — dopo `message_template_b` (riga 57):
```python
    ai_enabled: bool | None = None
    # Come message_template_b: None esplicito = rimuovi il template
    message_template_c: str | None = Field(default=None, min_length=10)
    ai_system_prompt: str | None = None
```

`CampaignResponse` — dopo `message_template_b` (riga 82):
```python
    ai_enabled: bool = True
    message_template_c: str | None = None
    ai_system_prompt: str | None = None
```
(`ai_enabled: bool = True` nel Response è solo il default pydantic se l'attributo mancasse; `from_attributes` lo legge sempre dal modello.)

- [ ] **Step 6: Run tests**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_template_mode_schema.py -v`
Expected: PASS.

Run full suite: `.\venv\Scripts\python.exe -m pytest tests -q`
Expected: 512 + nuovi, tutti PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/023_ai_enabled_template_c.py backend/app/models/campaign.py backend/app/schemas/campaign.py backend/tests/test_template_mode_schema.py
git commit -m "feat(template-mode): migration 023 + colonne ai_enabled/template_c/ai_system_prompt"
```

NB: la migration su Supabase (`alembic upgrade head`) la applica Tommaso/l'operatore al deploy — NON applicarla da questo task (regola memory: mai migration su DB prod senza conferma).

---

### Task 2: `template_renderer.py` — spintax, nome, pick_template

**Files:**
- Create: `backend/app/services/template_renderer.py`
- Test: `backend/tests/test_template_renderer.py`

**Interfaces:**
- Produces:
  - `resolve_spintax(text: str, rng: random.Random | None = None) -> str`
  - `render_template(template: str, full_name: str | None, username: str, rng: random.Random | None = None) -> str` — solleva `TemplateRenderError` su placeholder sconosciuti residui
  - `pick_template(campaign, rng: random.Random | None = None) -> tuple[str, str]` — ritorna `(testo_template, variante)` con variante in `'a'|'b'|'c'`
  - `class TemplateRenderError(Exception)`
  - `NAME_PLACEHOLDER_RE` (regex compilata, riusata dal Task 3)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_template_renderer.py
"""Renderer no-AI: spintax, placeholder nome, pick_template A/B/C."""
import random
import pytest
from app.services.template_renderer import (
    resolve_spintax, render_template, pick_template, TemplateRenderError,
)


class FakeCampaign:
    def __init__(self, a="Template base abbastanza lungo", b=None, c=None):
        self.base_message_template = a
        self.message_template_b = b
        self.message_template_c = c


# ── resolve_spintax ────────────────────────────────────────────────

def test_spintax_single_group():
    out = resolve_spintax("{Ciao|Hey} Marco", rng=random.Random(1))
    assert out in ("Ciao Marco", "Hey Marco")

def test_spintax_multiple_groups_all_resolved():
    out = resolve_spintax("{Ciao|Hey} {nome}, {volevo|mi andava di} scriverti")
    assert "|" not in out
    assert "{nome}" in out  # gruppo senza pipe NON è spintax: resta intatto

def test_spintax_no_pipe_untouched():
    assert resolve_spintax("Testo con {nome} e basta") == "Testo con {nome} e basta"

def test_spintax_covers_all_options():
    seen = {resolve_spintax("{a|b|c}", rng=random.Random(i)) for i in range(60)}
    assert seen == {"a", "b", "c"}

def test_spintax_empty_option_allowed():
    # {ciao|} = variante vuota legittima (a volte la parola non c'è)
    out = resolve_spintax("Bella{ciao|}", rng=random.Random(3))
    assert out in ("Bellaciao", "Bella")

def test_spintax_malformed_brace_stays_literal():
    # graffa mai chiusa: testo letterale, nessuna eccezione
    assert resolve_spintax("Ciao {nome, come va") == "Ciao {nome, come va"


# ── render_template ────────────────────────────────────────────────

def test_render_fills_name_with_full_name():
    out = render_template("Ciao {nome}!", full_name="Marco Rossi", username="marco.r")
    assert out == "Ciao Marco Rossi!"

def test_render_fills_name_fallback_username():
    out = render_template("Ciao {nome}!", full_name=None, username="marco.r")
    assert out == "Ciao @marco.r!"

def test_render_all_name_variants():
    out = render_template("{nome} [Nome] {Name} [name]", full_name="Anna", username="a")
    assert out == "Anna Anna Anna Anna"

def test_render_spintax_then_name():
    out = render_template("{Ciao|Hey} {nome}", full_name="Luca", username="l",
                          rng=random.Random(5))
    assert out in ("Ciao Luca", "Hey Luca")

def test_render_unknown_placeholder_raises():
    with pytest.raises(TemplateRenderError):
        render_template("Ciao {azienda}!", full_name="X", username="x")

def test_render_unknown_square_placeholder_raises():
    with pytest.raises(TemplateRenderError):
        render_template("Ciao [Azienda]!", full_name="X", username="x")

def test_render_normalizes_newlines():
    out = render_template("Riga1\r\nRiga2\n\n\n\nRiga3", full_name="X", username="x")
    assert out == "Riga1\nRiga2\n\nRiga3"


# ── pick_template ──────────────────────────────────────────────────

def test_pick_only_a():
    text, variant = pick_template(FakeCampaign())
    assert variant == "a"
    assert text.startswith("Template base")

def test_pick_a_b_c_all_come_out():
    camp = FakeCampaign(b="Secondo template B lungo", c="Terzo template C lungo")
    variants = {pick_template(camp, rng=random.Random(i))[1] for i in range(60)}
    assert variants == {"a", "b", "c"}

def test_pick_skips_blank_templates():
    camp = FakeCampaign(b="   ", c=None)  # B solo spazi = non compilato
    variants = {pick_template(camp, rng=random.Random(i))[1] for i in range(30)}
    assert variants == {"a"}

def test_pick_variant_matches_text():
    camp = FakeCampaign(b="Secondo template B lungo")
    for i in range(20):
        text, variant = pick_template(camp, rng=random.Random(i))
        if variant == "b":
            assert text == "Secondo template B lungo"
        else:
            assert text == camp.base_message_template
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_template_renderer.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.template_renderer`.

- [ ] **Step 3: Implement the module**

```python
# backend/app/services/template_renderer.py
"""Rendering locale dei template DM (modalità no-AI).

Pipeline: spintax -> placeholder nome -> normalizzazione whitespace.
Nessuna dipendenza da ai_personalizer (è ai_personalizer che importa da qui).
"""
import random
import re

# Gruppo spintax = graffe con almeno un '|' dentro: {Ciao|Hey|Salve}.
# {nome} non ha pipe -> non matcha -> resta per il fill del nome.
SPINTAX_RE = re.compile(r"\{([^{}|]*(?:\|[^{}|]*)+)\}")

# Placeholder nome accettati (stessa semantica storica di _fallback_message).
NAME_PLACEHOLDER_RE = re.compile(
    r"\{nome\}|\[nome\]|\{name\}|\[name\]", re.IGNORECASE
)

# Residuo sospetto: qualunque {x}/[x] corto rimasto dopo spintax+nome.
RESIDUAL_PLACEHOLDER_RE = re.compile(r"[{\[][^{}\[\]]{0,40}[}\]]")


class TemplateRenderError(Exception):
    """Template non renderizzabile in sicurezza (placeholder sconosciuti)."""


def resolve_spintax(text: str, rng: random.Random | None = None) -> str:
    """Espande ogni gruppo {a|b|c} scegliendo una variante a caso.
    Un solo livello (niente gruppi annidati). Graffe malformate = letterali."""
    r = rng or random
    def _pick(m: re.Match) -> str:
        return r.choice(m.group(1).split("|"))
    return SPINTAX_RE.sub(_pick, text)


def _fill_name(text: str, full_name: str | None, username: str) -> str:
    name = (full_name or "").strip() or f"@{username}"
    return NAME_PLACEHOLDER_RE.sub(name, text)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(ln.rstrip() for ln in text.split("\n"))
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render_template(
    template: str,
    full_name: str | None,
    username: str,
    rng: random.Random | None = None,
) -> str:
    """Spintax -> nome -> normalizzazione. Solleva TemplateRenderError se
    restano placeholder sconosciuti (es. {azienda}): meglio fallire UN
    messaggio che mandare un DM col placeholder letterale."""
    out = resolve_spintax(template, rng=rng)
    out = _fill_name(out, full_name, username)
    residual = RESIDUAL_PLACEHOLDER_RE.search(out)
    if residual:
        raise TemplateRenderError(
            f"Placeholder sconosciuto nel template: {residual.group(0)!r}"
        )
    return _normalize(out)


def pick_template(campaign, rng: random.Random | None = None) -> tuple[str, str]:
    """Sceglie a caso (pesi uguali) tra i template compilati della campagna.
    Ritorna (testo, variante) con variante in 'a'|'b'|'c'.
    Unifica i vecchi meccanismi (50/50 random e alternanza generated%2)."""
    r = rng or random
    candidates: list[tuple[str, str]] = [(campaign.base_message_template or "", "a")]
    if (campaign.message_template_b or "").strip():
        candidates.append((campaign.message_template_b, "b"))
    if (getattr(campaign, "message_template_c", None) or "").strip():
        candidates.append((campaign.message_template_c, "c"))
    return r.choice(candidates)
```

Nota su `SPINTAX_RE` e graffa malformata: `{nome, come va` non ha graffa chiusa → il regex non matcha → letterale (test dedicato). Il warning log per spintax malformato non è distinguibile a runtime senza parser: la validazione visiva sta nel frontend (Task 6, preview).

- [ ] **Step 4: Run tests**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_template_renderer.py -v`
Expected: PASS (tutti).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/template_renderer.py backend/tests/test_template_renderer.py
git commit -m "feat(template-mode): renderer locale spintax+nome+pick A/B/C con test"
```

---

### Task 3: `compose_message()` + override prompt per-campagna

**Files:**
- Modify: `backend/app/services/ai_personalizer.py`
- Test: `backend/tests/test_compose_message.py`

**Interfaces:**
- Consumes: `render_template`, `pick_template`, `TemplateRenderError` dal Task 2.
- Produces:
  - `async compose_message(campaign, follower) -> tuple[str, str]` — `(testo, variante)`; UNICA entry per tutti i call-site (Task 4). In modalità no-AI non tocca il client AI; in modalità AI propaga le eccezioni esistenti di `generate_message` (retry/transient invariati).
  - `generate_message(..., system_prompt_override: str | None = None)` — nuovo kwarg opzionale, backward-compatible.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_compose_message.py
"""compose_message: branch no-AI senza chiamate AI; branch AI con prompt override."""
import pytest
from unittest.mock import AsyncMock, patch

from app.services.ai_personalizer import compose_message


class FakeCampaign:
    base_message_template = "Ciao {nome}, ti scrivo per il progetto"
    message_template_b = None
    message_template_c = None
    ai_enabled = False
    ai_prompt_context = None
    ai_system_prompt = None


class FakeFollower:
    username = "marco.r"
    full_name = "Marco Rossi"
    biography = "Barista a Roma"


@pytest.mark.asyncio
async def test_no_ai_mode_never_touches_ai_client():
    camp, fol = FakeCampaign(), FakeFollower()
    with patch("app.services.ai_personalizer.get_ai_client") as boom:
        boom.side_effect = AssertionError("AI client chiamato in modalità no-AI!")
        text, variant = await compose_message(camp, fol)
    assert variant == "a"
    assert text == "Ciao Marco Rossi, ti scrivo per il progetto"


@pytest.mark.asyncio
async def test_no_ai_mode_resolves_spintax():
    camp, fol = FakeCampaign(), FakeFollower()
    camp = FakeCampaign()
    camp.base_message_template = "{Ciao|Hey} {nome}, due righe veloci"
    text, _ = await compose_message(camp, fol)
    assert text in ("Ciao Marco Rossi, due righe veloci",
                    "Hey Marco Rossi, due righe veloci")


@pytest.mark.asyncio
async def test_ai_mode_calls_generate_with_override():
    camp, fol = FakeCampaign(), FakeFollower()
    camp.ai_enabled = True
    camp.ai_system_prompt = "Tono piratesco."
    with patch("app.services.ai_personalizer.generate_message",
               new_callable=AsyncMock, return_value="msg generato") as gen:
        text, variant = await compose_message(camp, fol)
    assert text == "msg generato"
    assert variant == "a"
    gen.assert_awaited_once()
    assert gen.call_args.kwargs["system_prompt_override"] == "Tono piratesco."


@pytest.mark.asyncio
async def test_ai_mode_spintax_resolved_before_ai():
    camp, fol = FakeCampaign(), FakeFollower()
    camp.ai_enabled = True
    camp.base_message_template = "{Ciao|Hey} {nome}, collaborazione?"
    with patch("app.services.ai_personalizer.generate_message",
               new_callable=AsyncMock, return_value="ok") as gen:
        await compose_message(camp, fol)
    sent = gen.call_args.kwargs["base_template"]
    assert sent.startswith(("Ciao ", "Hey "))
    assert "{nome}" in sent  # il nome lo gestisce l'AI/prompt, non il renderer


def test_get_system_prompt_override():
    from app.services.ai_personalizer import _get_system_prompt
    assert _get_system_prompt("Custom X") == "Custom X"
    assert _get_system_prompt("   ") != "   "      # vuoto/spazi -> globale
    assert _get_system_prompt(None) == _get_system_prompt()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_compose_message.py -v`
Expected: FAIL — `ImportError: compose_message`.

- [ ] **Step 3: Implement**

In `backend/app/services/ai_personalizer.py`:

3a. Modifica `_get_system_prompt` (riga 103):
```python
def _get_system_prompt(override: str | None = None) -> str:
    if override and override.strip():
        return override.strip()
    return settings.ai_system_prompt.strip() if settings.ai_system_prompt.strip() else DEFAULT_SYSTEM_PROMPT
```

3b. Modifica la firma di `generate_message` (riga 257) aggiungendo il kwarg e usandolo:
```python
async def generate_message(
    base_template: str,
    follower_username: str,
    follower_full_name: str | None,
    follower_bio: str | None,
    ai_context: str | None = None,
    system_prompt_override: str | None = None,
) -> str:
```
e dentro: `system_prompt = _get_system_prompt(system_prompt_override)`.

3c. Aggiungi in fondo al file (dopo `generate_messages_batch`):
```python
async def compose_message(campaign, follower) -> tuple[str, str]:
    """UNICA entry per comporre il testo DM di un follower.

    Sceglie il template (A/B/C, pesi uguali), risolve SEMPRE lo spintax,
    poi: ai_enabled=False -> rendering locale (zero AI, zero token);
    ai_enabled=True -> generate_message col system prompt per-campagna.
    Ritorna (testo, variante). Propaga le eccezioni di generate_message
    (i call-site mantengono la loro gestione transient/retry) e
    TemplateRenderError per placeholder sconosciuti.
    """
    from app.services.template_renderer import (
        pick_template, render_template, resolve_spintax,
    )

    template, variant = pick_template(campaign)

    if not getattr(campaign, "ai_enabled", True):
        text = render_template(
            template,
            full_name=follower.full_name,
            username=follower.username,
        )
        return text, variant

    text = await generate_message(
        base_template=resolve_spintax(template),
        follower_username=follower.username,
        follower_full_name=follower.full_name,
        follower_bio=follower.biography,
        ai_context=campaign.ai_prompt_context,
        system_prompt_override=getattr(campaign, "ai_system_prompt", None),
    )
    return text, variant
```

- [ ] **Step 4: Run tests**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_compose_message.py tests/test_template_renderer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_personalizer.py backend/tests/test_compose_message.py
git commit -m "feat(template-mode): compose_message unica entry + override prompt per-campagna"
```

---

### Task 4: sostituire i 4 call-site con `compose_message`

**Files:**
- Modify: `backend/app/services/ai_personalizer.py` — `generate_preview_batch` (righe 424-439), `generate_messages_batch` (righe 499-514)
- Modify: `backend/app/services/campaign_orchestrator.py` — `_get_or_create_message` (righe 1311-1345)
- Modify: `backend/app/api/followers.py` — `regenerate_message` (righe 94-130)
- Test: `backend/tests/test_template_mode_batch.py`

**Interfaces:**
- Consumes: `compose_message(campaign, follower) -> tuple[str, str]` dal Task 3.
- Produces: nessuna nuova interfaccia — i 4 punti registrano `generated_text` e `template_variant` dal ritorno di `compose_message`. La gestione errori esistente di ogni call-site resta INVARIATA (try/except attorno alla chiamata).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_template_mode_batch.py
"""Batch di generazione in modalità no-AI: nessuna chiamata AI, variante registrata."""
import pytest
from unittest.mock import patch

from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message
from sqlalchemy import select


@pytest.mark.asyncio
async def test_batch_no_ai_generates_without_ai_client(db_session):
    camp = Campaign(
        name="tpl", status=CampaignStatus.ready,
        base_message_template="{Ciao|Hey} {nome}, ti va una collaborazione?",
        message_template_c="Buongiorno {nome}! Due righe veloci sul progetto",
        ai_enabled=False, messaging_enabled=True,
    )
    db_session.add(camp)
    await db_session.flush()
    for i in range(8):
        db_session.add(Follower(
            campaign_id=camp.id, username=f"user{i}", full_name=f"Utente {i}",
            biography="bio", status=FollowerStatus.bio_scraped,
        ))
    await db_session.commit()

    from app.services import ai_personalizer
    with patch.object(ai_personalizer, "get_ai_client") as boom:
        boom.side_effect = AssertionError("AI client chiamato in modalità no-AI!")
        count = await ai_personalizer.generate_messages_batch(camp.id)

    assert count == 8
    msgs = (await db_session.execute(
        select(Message).where(Message.campaign_id == camp.id)
    )).scalars().all()
    assert len(msgs) == 8
    assert all(m.template_variant in ("a", "c") for m in msgs)
    assert all("{" not in m.generated_text for m in msgs)
    assert any("Utente" in m.generated_text for m in msgs)
```

Nota fixture: usa la stessa fixture di sessione DB del Task 1 (nome reale dal conftest). `generate_messages_batch` apre `AsyncSessionLocal` internamente: nei test il conftest forza sqlite condiviso, quindi i dati creati con la fixture sono visibili — se nel repo i test esistenti per i batch usano un altro pattern (es. seed via `AsyncSessionLocal` diretto), copia quel pattern (`grep -l "generate_messages_batch\|AsyncSessionLocal" backend/tests/*.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_template_mode_batch.py -v`
Expected: FAIL — il batch attuale chiama `generate_message` → il mock esplode (`AssertionError`) e i follower finiscono `failed`, `count == 0`.

- [ ] **Step 3: Swap dei call-site**

3a. `generate_preview_batch` — sostituisci le righe 426-439 (blocco `if campaign.message_template_b ... text = await generate_message(...)`) con:
```python
                text, variant = await compose_message(campaign, follower)
```
(elimina anche `import random` a inizio funzione se resta inutilizzato).

3b. `generate_messages_batch` — sostituisci le righe 501-514 con:
```python
                    text, variant = await compose_message(campaign, follower)
```

3c. `campaign_orchestrator._get_or_create_message` — sostituisci le righe 1324-1338 (pick A/B + `generate_message`) con:
```python
        text, variant = await compose_message(follower=follower, campaign=campaign)
```
e aggiorna l'import in testa alla funzione/file: `from app.services.ai_personalizer import compose_message` (verifica l'import esistente di `generate_message` con `grep -n "from app.services.ai_personalizer import" backend/app/services/campaign_orchestrator.py` e aggiungi `compose_message` lì). La gestione `AIGenerationTransientError`/transient (righe 1353-1368) resta identica.

3d. `api/followers.py regenerate_message` — sostituisci le righe 94-130 (import random, pick A/B, `generate_message`) con:
```python
    from app.services.ai_personalizer import compose_message
    ...
    try:
        text, variant = await compose_message(campaign, follower)
    except Exception as e:
        follower.status = FollowerStatus.bio_scraped
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Generazione fallita: {str(e)[:120]}")
```
(la delete dei messaggi esistenti e il resto della funzione restano invariati; `variant` va nel campo `template_variant` del nuovo `Message` come oggi).

In `ai_personalizer.py`, poiché compose_message è definita nello stesso modulo, i punti 3a/3b la chiamano direttamente.

- [ ] **Step 4: Run tests**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_template_mode_batch.py -v`
Expected: PASS.

Run full suite: `.\venv\Scripts\python.exe -m pytest tests -q`
Expected: tutto verde. Se `test_gen_backoff.py` (unico test esistente che tocca la generazione) mocka `generate_message` nell'orchestrator, ora va aggiornato a mockare `compose_message` — sistemalo mantenendo l'intento del test (backoff sui transient), non indebolirlo.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_personalizer.py backend/app/services/campaign_orchestrator.py backend/app/api/followers.py backend/tests/test_template_mode_batch.py
git commit -m "refactor(template-mode): compose_message sostituisce la logica duplicata nei 4 call-site"
```

---

### Task 5: API — create/update campi nuovi, editabili in ogni stato

**Files:**
- Modify: `backend/app/api/campaigns.py` — endpoint create (~riga 199) e `update_campaign` (righe 228-310)
- Test: `backend/tests/test_template_mode_api.py`

**Interfaces:**
- Consumes: schemi dal Task 1.
- Produces: `POST /campaigns` accetta `ai_enabled`/`message_template_c`/`ai_system_prompt`; `PATCH/PUT /campaigns/{id}` li aggiorna in QUALSIASI stato (anche `running`), insieme a `base_message_template`/`message_template_b`/`ai_prompt_context`. `bio_engine` resta col suo guard esistente.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_template_mode_api.py
"""API: nuovi campi in create; campi messaggi editabili anche a campagna running."""
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.models.campaign import Campaign, CampaignStatus


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.asyncio
async def test_create_defaults_no_ai(client):
    r = await client.post("/api/campaigns", json={
        "name": "tpl-api", "target_username": "acme",
        "base_message_template": "Ciao {nome}, ti scrivo per il progetto",
    })
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["ai_enabled"] is False
    assert body["message_template_c"] is None


@pytest.mark.asyncio
async def test_create_with_ai_and_template_c(client):
    r = await client.post("/api/campaigns", json={
        "name": "tpl-api2", "target_username": "acme",
        "base_message_template": "Ciao {nome}, ti scrivo per il progetto",
        "message_template_c": "Terzo template abbastanza lungo",
        "ai_enabled": True,
        "ai_system_prompt": "Tono formale, max 3 frasi.",
    })
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["ai_enabled"] is True
    assert body["message_template_c"].startswith("Terzo")
    assert body["ai_system_prompt"].startswith("Tono")


@pytest.mark.asyncio
async def test_update_message_fields_while_running(client, db_session):
    camp = Campaign(name="run", status=CampaignStatus.running,
                    base_message_template="Vecchio template abbastanza lungo",
                    ai_enabled=True)
    db_session.add(camp)
    await db_session.commit()

    r = await client.put(f"/api/campaigns/{camp.id}", json={
        "ai_enabled": False,
        "base_message_template": "Nuovo template abbastanza lungo davvero",
        "message_template_c": "Template C abbastanza lungo pure lui",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ai_enabled"] is False
    assert body["base_message_template"].startswith("Nuovo")
    assert body["message_template_c"].startswith("Template C")


@pytest.mark.asyncio
async def test_bio_engine_still_blocked_while_running(client, db_session):
    camp = Campaign(name="run2", status=CampaignStatus.running,
                    base_message_template="Template abbastanza lungo",
                    bio_engine="api")
    db_session.add(camp)
    await db_session.commit()
    r = await client.put(f"/api/campaigns/{camp.id}", json={"bio_engine": "browser"})
    assert r.status_code == 400
```

Nota: verifica metodo (PUT vs PATCH) e prefix reale con `grep -n "router = APIRouter\|@router.put\|@router.patch" backend/app/api/campaigns.py` e adatta URL/metodo. Idem per la fixture client se il conftest ne offre già una.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_template_mode_api.py -v`
Expected: FAIL — `ai_enabled` assente dalla response / 400 sull'update running.

- [ ] **Step 3: Implement**

3a. Endpoint create (~riga 199, accanto a `bio_engine=data.bio_engine`):
```python
        ai_enabled=data.ai_enabled,
        message_template_c=data.message_template_c,
        ai_system_prompt=data.ai_system_prompt,
```

3b. `update_campaign` — estendi `always_editable` (riga 233):
```python
    always_editable = {
        "daily_limit",
        "scrape_session_size",
        "scrape_break_minutes_min",
        "scrape_break_minutes_max",
        "bio_fetch_delay_min",
        "bio_fetch_delay_max",
        "scrape_daily_limit",
        "inbox_engine",
        "bio_engine",
        # Campi messaggi/AI: letti freschi a ogni generazione, sicuri da
        # cambiare anche a campagna running — i messaggi già generati restano,
        # i prossimi seguono la nuova modalità (decisione 11/07).
        "base_message_template",
        "message_template_b",
        "message_template_c",
        "ai_prompt_context",
        "ai_enabled",
        "ai_system_prompt",
    }
```

3c. Setter nuovi campi (dopo il blocco `message_template_b`, riga 286):
```python
    if "message_template_c" in data.model_fields_set:
        campaign.message_template_c = data.message_template_c
    if data.ai_enabled is not None:
        campaign.ai_enabled = data.ai_enabled
    if "ai_system_prompt" in data.model_fields_set:
        campaign.ai_system_prompt = (data.ai_system_prompt or "").strip() or None
```

3d. Il blocco `completed_message_fields` (riga 255) diventa ridondante per i campi ora in `always_editable`: aggiorna il set togliendo i campi promossi (`base_message_template`, `message_template_b`, `ai_prompt_context`) e lasciando `messaging_enabled`, `require_approval`, `approval_sample_size` — il comportamento per quei tre resta identico a oggi.

- [ ] **Step 4: Run tests**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests/test_template_mode_api.py -v`
Expected: PASS.

Run full suite: `.\venv\Scripts\python.exe -m pytest tests -q` → tutto verde (attenzione ai test esistenti dell'update endpoint: se qualcuno asseriva il 400 su template-update a campagna running, va aggiornato alla nuova policy — è un cambio VOLUTO).

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/campaigns.py backend/tests/test_template_mode_api.py
git commit -m "feat(template-mode): API create/update con ai_enabled/template_c/prompt, campi messaggi editabili in ogni stato"
```

---

### Task 6: Frontend — types, spintax lib, form nuova campagna

**Files:**
- Modify: `frontend/lib/types.ts` (righe ~39-46 Campaign, ~109-119 create/update)
- Create: `frontend/lib/spintax.ts`
- Modify: `frontend/app/campaigns/new/page.tsx`

**Interfaces:**
- Consumes: API dal Task 5.
- Produces: `resolveSpintax(text: string): string` e `renderPreview(template: string, sampleName?: string): string` in `lib/spintax.ts` (riusati dal Task 7).

- [ ] **Step 1: types.ts**

Nel tipo `Campaign` (dopo `message_template_b`, riga 46):
```typescript
  message_template_c: string | null
  ai_enabled: boolean
  ai_system_prompt: string | null
```
Nei tipi create/update (dopo riga 111):
```typescript
  message_template_c?: string | null
  ai_enabled?: boolean
  ai_system_prompt?: string | null
```

- [ ] **Step 2: lib/spintax.ts**

```typescript
// Rispecchia backend/app/services/template_renderer.py (solo per anteprima UI).
const SPINTAX_RE = /\{([^{}|]*(?:\|[^{}|]*)+)\}/g

export function resolveSpintax(text: string): string {
  return text.replace(SPINTAX_RE, (_, group: string) => {
    const options = group.split('|')
    return options[Math.floor(Math.random() * options.length)]
  })
}

const NAME_RE = /\{nome\}|\[nome\]|\{name\}|\[name\]/gi

export function renderPreview(template: string, sampleName = 'Marco'): string {
  return resolveSpintax(template).replace(NAME_RE, sampleName)
}

/** Placeholder sconosciuti rimasti dopo spintax+nome (es. {azienda}): il backend
 *  li rifiuta al rendering — segnalali nel form. */
export function findUnknownPlaceholders(template: string): string[] {
  const cleaned = template.replace(SPINTAX_RE, 'x').replace(NAME_RE, 'x')
  return cleaned.match(/[{[][^{}[\]]{0,40}[}\]]/g) ?? []
}
```

- [ ] **Step 3: form nuova campagna** (`frontend/app/campaigns/new/page.tsx`)

3a. State (riga 26-47): aggiungi al form `message_template_c: ''`, `ai_enabled: false`, `ai_system_prompt: ''`; aggiungi `const [showTemplateC, setShowTemplateC] = useState(false)` accanto a `showTemplateB`.

3b. Payload submit (righe 85-87), accanto ai campi esistenti:
```typescript
        message_template_c: messagingEnabled && showTemplateC && form.message_template_c.trim() ? form.message_template_c : null,
        ai_enabled: messagingEnabled ? form.ai_enabled : false,
        ai_system_prompt: messagingEnabled && form.ai_enabled && form.ai_system_prompt.trim() ? form.ai_system_prompt : undefined,
```

3c. UI, nella sezione template (dopo il blocco Template B, righe 306-330):
- Bottone "+ Aggiungi template C" identico al pattern B (`showTemplateC`), textarea `form.message_template_c`.
- Sotto il textarea del template A, hint statico:
```tsx
              <p className="text-xs text-gray-500">
                {'{nome}'} = nome del destinatario · {'{Ciao|Hey|Salve}'} = il bot sceglie una variante a caso per ogni DM
              </p>
```
- Bottone anteprima + render (stato `const [previews, setPreviews] = useState<string[]>([])`):
```tsx
              <button type="button" className="text-xs text-blue-400 hover:text-blue-300"
                onClick={() => setPreviews([1, 2, 3].map(() => renderPreview(form.base_message_template)))}>
                ⚡ Anteprima varianti
              </button>
              {previews.length > 0 && (
                <div className="space-y-1">
                  {previews.map((p, i) => (
                    <p key={i} className="text-xs text-gray-400 bg-gray-800 rounded p-2 whitespace-pre-wrap">{p}</p>
                  ))}
                </div>
              )}
```
- Validazione submit: se `findUnknownPlaceholders(form.base_message_template).length > 0` (e idem B/C quando compilati) → errore campo "Placeholder sconosciuto: {...} — usa solo {nome} o gruppi {a|b}".

3d. Toggle AI, DOPO la sezione template e PRIMA del campo "Contesto AI" esistente (riga ~334): il campo contesto va reso condizionale al toggle.
```tsx
            <div className="flex items-center justify-between rounded-lg border border-gray-700 p-3">
              <div>
                <p className="text-sm text-gray-200 font-medium">Personalizza con AI</p>
                <p className="text-xs text-gray-500">
                  OFF (default): il template parte così com'è, con le varianti {'{a|b}'} — zero quota AI.
                  ON: l'AI riscrive il messaggio sulla bio del destinatario.
                </p>
              </div>
              <Switch checked={form.ai_enabled}
                onCheckedChange={v => setForm(f => ({ ...f, ai_enabled: v }))} />
            </div>
            {form.ai_enabled && (
              <>
                {/* campo Contesto AI esistente, spostato qui dentro */}
                {/* nuovo campo: */}
                <div className="space-y-2">
                  <label className="text-sm text-gray-300 font-medium">Istruzioni AI (opzionale)</label>
                  <Textarea rows={3} value={form.ai_system_prompt}
                    onChange={e => setForm(f => ({ ...f, ai_system_prompt: e.target.value }))}
                    placeholder="Sovrascrive le istruzioni globali solo per questa campagna. Es: tono informale, max 3 frasi, niente emoji."
                    className="bg-gray-800 border-gray-700 text-white resize-none" />
                </div>
              </>
            )}
```
Se il componente `Switch` di shadcn non è già importato nel file, verifica che esista in `frontend/components/ui/` (`ls frontend/components/ui/`); se manca, usa il pattern toggle già presente nel file per `messagingEnabled` (riga 279) invece di introdurre un componente nuovo.

- [ ] **Step 4: Build**

Run: `cd "D:\BOT OUTBOUND\frontend"; npm run build`
Expected: build OK, zero errori TypeScript.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/types.ts frontend/lib/spintax.ts frontend/app/campaigns/new/page.tsx
git commit -m "feat(template-mode): form campagna — toggle AI, template C, hint+anteprima spintax"
```

---

### Task 7: Frontend pagina campagna + docs/memoria

**Files:**
- Modify: `frontend/app/campaigns/[id]/page.tsx` (dialog edit template: state righe 240-245, form settings righe 259-309, handleSaveTemplate riga 542, UI riga ~1357)
- Modify: `docs/project/PROGRESS.md`, `D:\BOT OUTBOUND\CLAUDE.md` (sezione messaggi/AI se esiste), memoria `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md`

**Interfaces:**
- Consumes: `renderPreview`, `findUnknownPlaceholders` da `lib/spintax.ts` (Task 6); API update (Task 5).

- [ ] **Step 1: estendi il dialog di edit template**

1a. State (righe 240-245): aggiungi
```typescript
  const [editTemplateCValue, setEditTemplateCValue] = useState('')
  const [editAiEnabled, setEditAiEnabled] = useState(false)
  const [editAiSystemPrompt, setEditAiSystemPrompt] = useState('')
```

1b. All'apertura del dialog (riga ~1366, dove popola `editTemplateValue`/`editTemplateBValue`/`editContextValue`):
```typescript
  setEditTemplateCValue(campaign.message_template_c ?? '')
  setEditAiEnabled(campaign.ai_enabled)
  setEditAiSystemPrompt(campaign.ai_system_prompt ?? '')
```

1c. `handleSaveTemplate` (riga 542) — aggiungi al payload:
```typescript
        message_template_c: editMessagingEnabled ? (editTemplateCValue.trim() || null) : null,
        ai_enabled: editMessagingEnabled ? editAiEnabled : false,
        ai_system_prompt: editMessagingEnabled && editAiEnabled ? (editAiSystemPrompt.trim() || null) : null,
```
e prima del save la stessa validazione placeholder del Task 6 (`findUnknownPlaceholders` su A/B/C compilati → messaggio d'errore, no save).

1d. UI del dialog: textarea Template C sotto Template B (stesso pattern), toggle "Personalizza con AI" + campo "Istruzioni AI" condizionale (stesso blocco del Task 6, stati `editAiEnabled`/`editAiSystemPrompt`), hint spintax + bottone "⚡ Anteprima varianti" con `renderPreview(editTemplateValue)`.

1e. Badge modalità nella card "Template messaggio" (riga ~1357): accanto al titolo mostra
```tsx
  <span className={`text-xs px-2 py-0.5 rounded ${campaign.ai_enabled ? 'bg-purple-900 text-purple-300' : 'bg-gray-800 text-gray-400'}`}>
    {campaign.ai_enabled ? '🤖 AI attiva' : '📋 Template'}
  </span>
```

- [ ] **Step 2: Build**

Run: `cd "D:\BOT OUTBOUND\frontend"; npm run build`
Expected: build OK.

- [ ] **Step 3: Full backend suite un'ultima volta**

Run: `cd "D:\BOT OUTBOUND\backend"; .\venv\Scripts\python.exe -m pytest tests -q`
Expected: tutto verde.

- [ ] **Step 4: Docs + memoria (regola repo, non opzionale)**

- `docs/project/PROGRESS.md`: sezione datata 2026-07-11 con la feature (2-4 righe).
- `D:\BOT OUTBOUND\CLAUDE.md`: nella sezione flusso messaggi/AI aggiorna: default = template renderer locale (spintax A/B/C), AI opt-in per-campagna via `ai_enabled`, prompt per-campagna `ai_system_prompt`.
- `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md`: sezione datata (cosa, file toccati, comportamento atteso, nota migration 023 da applicare su Supabase al deploy).

- [ ] **Step 5: Commit finale**

```bash
git add frontend/app/campaigns/[id]/page.tsx docs/project/PROGRESS.md CLAUDE.md
git commit -m "feat(template-mode): pagina campagna — edit template C/toggle AI/prompt + badge modalità; docs"
```

---

## Verifica end-to-end (manuale, post-merge — a carico operatore)

1. `alembic upgrade head` su Supabase (Tommaso conferma prima).
2. Campagna esistente → deve mostrarsi "🤖 AI attiva" (migration → true).
3. Nuova campagna → default "📋 Template"; creare con 2 template + spintax; approvazione campione → i messaggi devono uscire già pronti, istantanei, senza log AI.
4. Su campagna running: switch AI→template dal dialog → i DM successivi usano il renderer (log worker: nessuna chiamata provider).
