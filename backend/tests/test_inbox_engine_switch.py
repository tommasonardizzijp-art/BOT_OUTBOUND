"""Inbox: cambio engine azzera il cursore intra-engine (resume-by-frontier)."""
from app.api.campaigns import engine_switch_resets_cursor


def test_switch_resets_cursor():
    assert engine_switch_resets_cursor("browser", "api") is True
    assert engine_switch_resets_cursor("api", "browser") is True


def test_same_engine_keeps_cursor():
    assert engine_switch_resets_cursor("browser", "browser") is False
    assert engine_switch_resets_cursor("api", "api") is False


# ── il toggle del segnalibro e' per SESSIONE, non una configurazione ───────
def test_il_body_di_start_accetta_il_flag_del_segnalibro():
    from app.api.campaigns import PhaseStartBody

    body = PhaseStartBody(target=340, salta_lavorate=True)
    assert body.salta_lavorate is True


def test_il_flag_e_spento_se_non_lo_si_chiede():
    """Una sessione normale legge tutto: la modalita' che salta va chiesta
    esplicitamente ogni volta, perche' e' quella che accetta di perdere chi e'
    risalito."""
    from app.api.campaigns import PhaseStartBody

    assert PhaseStartBody().salta_lavorate is False
    assert PhaseStartBody(target=100).salta_lavorate is False
