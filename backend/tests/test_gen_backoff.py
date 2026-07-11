"""Anti-tempesta 429 nella generazione DM.

Due garanzie:
1. `_gen_backoff_seconds` cresce esponenzialmente con tetto.
2. `_get_or_create_message` SOLLEVA AIGenerationTransientError sui fallimenti
   transitori (429/timeout) — cosi' il worker fa backoff invece di riclaimare
   lo stesso follower a delay zero (l'hot-loop che alimentava il 429).
   Sui fallimenti permanenti torna None e marca il follower `failed`.
"""
import asyncio

import pytest

from app.models.follower import FollowerStatus
from app.services import campaign_orchestrator as orch
from app.services.campaign_orchestrator import _gen_backoff_seconds, _get_or_create_message
from app.utils.exceptions import AIGenerationTransientError, OllamaError


# ── _gen_backoff_seconds ────────────────────────────────────────────────────

def test_backoff_raddoppia():
    assert _gen_backoff_seconds(1, 30, 300) == 30
    assert _gen_backoff_seconds(2, 30, 300) == 60
    assert _gen_backoff_seconds(3, 30, 300) == 120
    assert _gen_backoff_seconds(4, 30, 300) == 240


def test_backoff_cap():
    assert _gen_backoff_seconds(10, 30, 300) == 300  # tetto rispettato
    assert _gen_backoff_seconds(5, 30, 300) == 300   # 480 → cappato a 300


def test_backoff_attempt_zero_safe():
    assert _gen_backoff_seconds(0, 30, 300) == 30    # clamp a 1


# ── _get_or_create_message: transient vs permanent ─────────────────────────

class _FakeResult:
    def scalar_one_or_none(self):
        return None  # nessun messaggio pending esistente


class _FakeDB:
    async def execute(self, *a, **k):
        return _FakeResult()

    async def commit(self):
        return None

    async def refresh(self, *a, **k):
        return None

    def add(self, *a, **k):
        return None


class _Follower:
    def __init__(self):
        self.id = "f1"
        self.username = "target"
        self.full_name = "Target"
        self.biography = "bio"
        self.ig_user_id = 123
        self.status = FollowerStatus.bio_scraped
        self.locked_by_account_id = "acc1"
        self.locked_at = "now"


class _Campaign:
    id = "c1"
    message_template_b = None
    base_message_template = "Ciao"
    ai_prompt_context = None


def test_transient_429_solleva_e_lascia_bio_scraped(monkeypatch):
    async def _boom(*a, **k):
        raise OllamaError("Gemini API error: 429 too many requests")
    monkeypatch.setattr(orch, "compose_message", _boom)

    follower = _Follower()
    with pytest.raises(AIGenerationTransientError):
        asyncio.run(_get_or_create_message(follower, _Campaign(), _FakeDB()))

    # rigenerabile: torna bio_scraped e sbloccato
    assert follower.status == FollowerStatus.bio_scraped
    assert follower.locked_by_account_id is None
    assert follower.locked_at is None


def test_permanent_error_torna_none_e_marca_failed(monkeypatch):
    async def _boom(*a, **k):
        raise ValueError("template rotto")  # nessuna keyword transient
    monkeypatch.setattr(orch, "compose_message", _boom)

    follower = _Follower()
    out = asyncio.run(_get_or_create_message(follower, _Campaign(), _FakeDB()))

    assert out is None
    assert follower.status == FollowerStatus.failed
    assert follower.locked_by_account_id is None
