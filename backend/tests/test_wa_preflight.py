"""Il pre-volo deve dire la verita' sulla migrazione attesa.

Nato dalla review indipendente dell'08/08: lo script pretendeva la revision
`028` mentre la head reale era gia' `029` (portata su main da `9be195d`).
Uno strumento che esiste per dire "sei pronto" usciva con codice 1 proprio al
go-live, su un database corretto — il modo piu' rapido di far perdere fiducia
in un check.

La causa e' che la revision era scritta a mano nel sorgente: ogni migrazione
nuova la rende falsa, e nessuno se ne accorge finche' non serve. Il test
inchioda l'invariante giusta — "l'attesa del pre-volo e' la head di Alembic",
non "l'attesa e' 029" — cosi' resta vero anche alla 030.
"""
import pytest


def test_revision_attesa_e_la_head_di_alembic():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from scripts.wa_preflight import revision_attesa

    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert revision_attesa() == head


def test_revision_attesa_non_e_hardcoded_a_una_vecchia():
    """Prova del nove del test sopra: se qualcuno rimettesse una costante a
    mano, questo resterebbe verde solo finche' quella costante coincide con
    la head. Qui si verifica che la funzione LEGGA davvero le migrazioni,
    cioe' che non torni un valore inventato o vuoto."""
    from scripts.wa_preflight import revision_attesa

    attesa = revision_attesa()
    assert attesa, "il pre-volo non sa quale revision aspettarsi"
    assert attesa.isdigit() and int(attesa) >= 29, (
        f"revision attesa {attesa!r}: piu' vecchia della 029, che e' su main "
        "da 9be195d")
