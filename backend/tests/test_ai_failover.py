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


# ── budget dei token di ragionamento (gpt-oss) ─────────────────────────────
# Perche' esistono: il fallback Groq e' rimasto rotto in silenzio per giorni con
# un modello dismesso, e il suo sostituto ragiona dentro `max_tokens`. Un DM
# troncato non solleva: `_validate_message` lo scarta e restituisce il template,
# quindi il guasto NON si vede nei log degli invii. Va inchiodato qui.

def test_reasoning_spento_solo_su_gpt_oss():
    assert ai._reasoning_va_spento("openai/gpt-oss-120b")
    assert ai._reasoning_va_spento("openai/gpt-oss-20b")
    # Un provider OpenAI-compatible che non conosce il parametro risponderebbe 400.
    assert not ai._reasoning_va_spento("qwen/qwen3.6-27b")
    assert not ai._reasoning_va_spento("llama-3.1-8b-instant")
    assert not ai._reasoning_va_spento("")


def _cattura_payload(monkeypatch):
    """Intercetta la POST e restituisce il payload che sarebbe partito davvero."""
    visti: list[dict] = []

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "un messaggio lungo abbastanza"}}]}

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None, **kw):
            visti.append(json)
            return _Resp()

    monkeypatch.setattr(ai.httpx, "AsyncClient", _Client)
    return visti


def test_payload_gpt_oss_porta_reasoning_effort(monkeypatch):
    visti = _cattura_payload(monkeypatch)
    asyncio.run(ai._generate_openai_compatible("sys", "usr", 400, "K", "openai/gpt-oss-120b"))
    assert visti[0]["reasoning_effort"] == "low"


def test_payload_altro_modello_non_porta_reasoning_effort(monkeypatch):
    visti = _cattura_payload(monkeypatch)
    asyncio.run(ai._generate_openai_compatible("sys", "usr", 400, "K", "qwen/qwen3.6-27b"))
    assert "reasoning_effort" not in visti[0]


def test_default_groq_non_e_il_modello_dismesso():
    # 404 model_not_found il 22/08/2026: il catalogo Groq non ha piu' Llama di chat.
    assert ai._resolve_model("groq", "") == "openai/gpt-oss-120b"
    assert "llama" not in ai._GROQ_DEFAULT_MODEL.lower()
