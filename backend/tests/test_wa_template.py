import pytest

from app.services.wa_template import (TemplateRenderError, pick_wa_template,
                                      render_wa_template, validate_wa_template)


class _Step:
    def __init__(self, a, b=None, c=None, d=None):
        self.template_a, self.template_b = a, b
        self.template_c, self.template_d = c, d


def test_pick_legge_i_campi_WA_non_quelli_instagram():
    """template_renderer.pick_template legge base_message_template: su uno
    step WA prenderebbe stringa vuota SENZA sollevare. E' il motivo per cui
    questo modulo esiste (contratto §2.4)."""
    testo, variante = pick_wa_template(_Step("solo A"))
    assert (testo, variante) == ("solo A", "a")


def test_pick_sceglie_fra_le_varianti_compilate():
    varianti = {pick_wa_template(_Step("A", "B", None, "D"))[1] for _ in range(60)}
    assert varianti == {"a", "b", "d"}


def test_render_valorizza_nome_e_attributi():
    out = render_wa_template("Ciao {nome}, ordine {ultimo_ordine}.",
                             display_name="Marco",
                             attributes={"ultimo_ordine": "10/01/2026"})
    assert out == "Ciao Marco, ordine 10/01/2026."


def test_render_senza_nome_non_inventa_un_segnaposto():
    """Su Instagram il fallback e' '@username'. Su WhatsApp non esiste: si
    rende senza nome, non con un simbolo che il destinatario non capisce."""
    out = render_wa_template("Ciao {nome}, promo.", display_name=None, attributes=None)
    assert "@" not in out and "{" not in out


def test_render_solleva_su_placeholder_sconosciuto():
    with pytest.raises(TemplateRenderError):
        render_wa_template("Ciao {azienda}.", display_name="M", attributes={})


def test_render_solleva_su_attributo_vuoto_per_quel_contatto():
    """'il tuo ultimo ordine e' ' e' peggio di un messaggio non inviato."""
    with pytest.raises(TemplateRenderError):
        render_wa_template("Ordine {ultimo_ordine}.", display_name="M",
                           attributes={"ultimo_ordine": "   "})


def test_render_espande_lo_spintax_riusando_il_parser_esistente():
    out = {render_wa_template("{Ciao|Salve} {nome}.", display_name="M", attributes=None)
           for _ in range(40)}
    assert out == {"Ciao M.", "Salve M."}


def test_validate_elenca_i_placeholder_ignoti():
    assert validate_wa_template("Ciao {nome}, {ultimo_ordine} e {citta}.",
                                known_attributes={"ultimo_ordine"}) == ["citta"]
    assert validate_wa_template("{Ciao|Salve} {nome}.", known_attributes=set()) == []
