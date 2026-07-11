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
