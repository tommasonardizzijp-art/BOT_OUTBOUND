"""Renderer no-AI: spintax, placeholder nome, pick_template A/B/C."""
import random
import pytest
from app.services.template_renderer import (
    resolve_spintax, render_template, pick_template, TemplateRenderError,
)


class FakeCampaign:
    def __init__(self, a="Template base abbastanza lungo", b=None, c=None, d=None):
        self.base_message_template = a
        self.message_template_b = b
        self.message_template_c = c
        self.message_template_d = d


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

def test_spintax_malformed_with_pipe_stays_literal():
    # graffa aperta con pipe ma mai chiusa: letterale, nessuna eccezione
    assert resolve_spintax("Ciao {nome|amico") == "Ciao {nome|amico"


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

def test_render_blank_template_raises():
    with pytest.raises(TemplateRenderError):
        render_template("   ", full_name="X", username="x")

def test_render_unclosed_brace_with_pipe_stays_literal_no_exception():
    # Fix 1b: graffa spintax mai chiusa ({a|b amico) non matcha SPINTAX_RE ne'
    # RESIDUAL_PLACEHOLDER_RE (nessuna graffa di chiusura) -> resta letterale,
    # nessuna eccezione (solo un warning loggato, comportamento invariato).
    out = render_template("Ciao {a|b amico", full_name="X", username="x")
    assert out == "Ciao {a|b amico"


# ── pick_template ──────────────────────────────────────────────────

def test_pick_only_a():
    text, variant = pick_template(FakeCampaign())
    assert variant == "a"
    assert text.startswith("Template base")

def test_pick_a_b_c_all_come_out():
    camp = FakeCampaign(b="Secondo template B lungo", c="Terzo template C lungo")
    variants = {pick_template(camp, rng=random.Random(i))[1] for i in range(60)}
    assert variants == {"a", "b", "c"}

def test_pick_a_b_c_d_all_come_out():
    camp = FakeCampaign(b="Secondo template B lungo", c="Terzo template C lungo",
                        d="Quarto template D lungo")
    variants = {pick_template(camp, rng=random.Random(i))[1] for i in range(80)}
    assert variants == {"a", "b", "c", "d"}

def test_pick_d_without_legacy_attr():
    # Campagna/oggetto senza l'attributo message_template_d (es. mock vecchi):
    # pick_template non deve esplodere, solo ignorare la variante d.
    class LegacyCampaign:
        base_message_template = "Template base abbastanza lungo"
        message_template_b = None
        message_template_c = None
    variants = {pick_template(LegacyCampaign(), rng=random.Random(i))[1] for i in range(20)}
    assert variants == {"a"}

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
