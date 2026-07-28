import pytest

from app.models.wa import WaNumberStatus
from app.services.wa_session import stato_da_segnale


@pytest.mark.parametrize("segnale,atteso", [
    ("logged_in",   WaNumberStatus.active),
    ("qr_required", WaNumberStatus.qr_required),
    ("unknown",     WaNumberStatus.disconnected),
])
def test_stato_da_segnale(segnale, atteso):
    assert stato_da_segnale(segnale) == atteso


def test_schermata_ignota_non_diventa_active():
    """'unknown' e' una schermata che non abbiamo riconosciuto: un interstitial,
    un aggiornamento, un ban. Mapparla su active farebbe partire gli invii
    contro una pagina che non e' WhatsApp."""
    assert stato_da_segnale("unknown") != WaNumberStatus.active


def test_profile_dir_e_per_numero():
    from app.services.wa_session import profile_dir_for

    a, b = profile_dir_for("num-a"), profile_dir_for("num-b")
    assert a != b
    assert a.name == "wa_num-a"
