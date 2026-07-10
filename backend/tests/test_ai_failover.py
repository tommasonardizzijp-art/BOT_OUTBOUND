"""Failover multi-provider della generazione AI (Gemini → Groq).

Verifica che ConfiguredAIClient provi il provider primario e, SOLO se fallisce,
ripieghi sul fallback configurato. Nessun failover se non configurato.
"""
import asyncio

import pytest

from app.config import settings
from app.services import ai_personalizer as ai
from app.utils.exceptions import OllamaError


def _set(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setattr(settings, k, v)


def _fake_dispatch(monkeypatch, behavior: dict):
    """behavior: provider -> ('ok', text) | ('fail', msg). Registra l'ordine dei provider provati."""
    calls: list[str] = []

    async def _dispatch(provider, api_key, model_override, base_url, system_prompt, user_prompt, max_tokens):
        calls.append(provider)
        kind, payload = behavior[provider]
        if kind == "fail":
            raise OllamaError(payload)
        return payload

    monkeypatch.setattr(ai, "_dispatch", _dispatch)
    return calls


# ── _provider_chain ────────────────────────────────────────────────────────

def test_chain_single_when_no_fallback(monkeypatch):
    _set(monkeypatch, ai_provider="gemini", ai_provider_fallback="")
    chain = ai._provider_chain()
    assert len(chain) == 1
    assert chain[0][0] == "gemini"


def test_chain_adds_fallback(monkeypatch):
    _set(monkeypatch, ai_provider="gemini", ai_api_key="G", ai_model="",
         ai_base_url="", ai_provider_fallback="groq", ai_api_key_fallback="GR",
         ai_model_fallback="", ai_base_url_fallback="")
    chain = ai._provider_chain()
    assert [c[0] for c in chain] == ["gemini", "groq"]
    assert chain[1][1] == "GR"  # api key del fallback


def test_chain_dedup_same_provider(monkeypatch):
    _set(monkeypatch, ai_provider="gemini", ai_provider_fallback="gemini")
    chain = ai._provider_chain()
    assert len(chain) == 1  # fallback == primario → nessun doppione


# ── ConfiguredAIClient.generate ────────────────────────────────────────────

def test_primary_ok_fallback_not_called(monkeypatch):
    _set(monkeypatch, ai_provider="gemini", ai_provider_fallback="groq")
    calls = _fake_dispatch(monkeypatch, {"gemini": ("ok", "MSG-GEMINI"), "groq": ("ok", "MSG-GROQ")})
    out = asyncio.run(ai.ConfiguredAIClient().generate("sys", "usr", 400))
    assert out == "MSG-GEMINI"
    assert calls == ["gemini"]  # groq mai toccato


def test_primary_429_ripiega_su_groq(monkeypatch):
    _set(monkeypatch, ai_provider="gemini", ai_provider_fallback="groq")
    calls = _fake_dispatch(monkeypatch, {"gemini": ("fail", "429 too many requests"), "groq": ("ok", "MSG-GROQ")})
    out = asyncio.run(ai.ConfiguredAIClient().generate("sys", "usr", 400))
    assert out == "MSG-GROQ"
    assert calls == ["gemini", "groq"]  # ordine: primario poi fallback


def test_entrambi_falliscono_solleva(monkeypatch):
    _set(monkeypatch, ai_provider="gemini", ai_provider_fallback="groq")
    calls = _fake_dispatch(monkeypatch, {"gemini": ("fail", "429"), "groq": ("fail", "500 err")})
    with pytest.raises(OllamaError):
        asyncio.run(ai.ConfiguredAIClient().generate("sys", "usr", 400))
    assert calls == ["gemini", "groq"]


def test_no_fallback_solleva_senza_ripiego(monkeypatch):
    _set(monkeypatch, ai_provider="gemini", ai_provider_fallback="")
    calls = _fake_dispatch(monkeypatch, {"gemini": ("fail", "429")})
    with pytest.raises(OllamaError):
        asyncio.run(ai.ConfiguredAIClient().generate("sys", "usr", 400))
    assert calls == ["gemini"]  # nessun secondo tentativo
