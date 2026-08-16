"""La guardia dello script di migrazione: e' l'unica cosa fra un errore di
sequenza e 4400 righe di produzione spostate due volte.

Lo script non e' idempotente, e nel percorso `--fino-a` nemmeno auto-protetto.
Questi test tengono i casi che hanno gia' prodotto un falso allarme in review.
"""
import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "migra_skew_timestamp",
    Path(__file__).resolve().parents[1] / "scripts" / "migra_skew_timestamp.py")
migra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migra)


def _col(*, n=100, scarto_ore=2.0, futuri=0, sola_lettura=False):
    return {"tabella": "wa_messages", "colonna": "sent_at", "n": n,
            "min": None, "max": None,
            "scarto": timedelta(hours=scarto_ore), "futuri": futuri,
            "sola_lettura": sola_lettura}


def test_una_colonna_ancora_storta_non_viene_segnalata():
    """Scarto ~2h = quello che ci aspettiamo prima della migrazione."""
    gia_corrette, ambigue = migra._guardia([_col(scarto_ore=2.05)])
    assert gia_corrette == []
    assert ambigue == []


def test_una_colonna_gia_corretta_ferma_tutto():
    """Scarto di minuti = il codice aware sta gia' scrivendo: dataset misto."""
    gia_corrette, _ = migra._guardia([_col(scarto_ore=0.05)])
    assert len(gia_corrette) == 1


def test_le_colonne_con_istanti_futuri_non_sono_scambiate_per_corrette():
    """La regressione che la review ha trovato.

    `next_action_at` contiene per progetto istanti FUTURI: il backoff scrive
    adesso+6h. Il suo scarto da now() e' quindi NEGATIVO, e una guardia che
    chiede solo "scarto piccolo" lo scambia per "colonna gia' corretta". Il
    falso allarme non e' innocuo: spinge l'operatore verso `--fino-a`, cioe'
    l'unico percorso in cui la protezione e' spenta e un secondo lancio
    sposterebbe le stesse righe di altre due ore.
    """
    riga = _col(scarto_ore=-4.0, futuri=37)
    gia_corrette, ambigue = migra._guardia([riga])
    assert gia_corrette == [], "una colonna con valori futuri non e' una prova"
    assert len(ambigue) == 1, "ma va segnalata, non ignorata in silenzio"


def test_una_colonna_vuota_non_dice_nulla():
    gia_corrette, ambigue = migra._guardia([_col(n=0, scarto_ore=0.0)])
    assert gia_corrette == [] and ambigue == []


def test_le_tabelle_in_sola_lettura_restano_fuori():
    """wa_discover_runs si mostra nella fotografia ma non si tocca e non vota."""
    gia_corrette, ambigue = migra._guardia(
        [_col(scarto_ore=0.01, sola_lettura=True)])
    assert gia_corrette == [] and ambigue == []


def test_wa_discover_runs_non_e_fra_le_tabelle_scritte():
    """Esclusa dagli UPDATE, ma presente in fotografia: l'operatore deve poter
    verificare da solo che la premessa dell'esclusione ('0 righe') sia ancora
    vera quando esegue, invece di fidarsi di un commento."""
    assert "wa_discover_runs" not in migra.COLONNE
    assert "wa_discover_runs" in migra.SOLA_LETTURA


@pytest.mark.parametrize("testo", ["2026-08-16T14:30:00", "16/08/2026", "adesso"])
def test_un_confine_orario_ambiguo_viene_rifiutato(testo):
    """Un istante senza fuso verrebbe risolto nel TimeZone della sessione:
    esattamente la classe di errore che questo script esiste per rimuovere."""
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        migra._istante_aware(testo)


def test_un_confine_orario_col_fuso_passa():
    val = migra._istante_aware("2026-08-16T14:30:00+00:00")
    assert val.tzinfo is not None
