"""Unica verita' su quali follower sono lavorabili.

Il difetto che questi test prevengono: con enrichment_level='none' i follower
restano in 'pending'. Se la query "resta lavoro?" non li conta, la campagna si
segna 'completed' immediatamente e non parte nessun DM.
"""
from types import SimpleNamespace

import pytest

from app.models.follower import FollowerStatus
from app.services.follower_workability import (
    is_sendable, remaining_work_statuses, sendable_statuses,
)


def _campagna(livello: str):
    return SimpleNamespace(enrichment_level=livello)


def test_con_arricchimento_solo_chi_ha_la_bio_e_mandabile():
    for livello in ("bio", "contacts"):
        stati = sendable_statuses(_campagna(livello))
        assert FollowerStatus.bio_scraped in stati
        assert FollowerStatus.message_generated in stati
        assert FollowerStatus.pending not in stati


def test_senza_arricchimento_anche_pending_e_mandabile():
    stati = sendable_statuses(_campagna("none"))
    assert FollowerStatus.pending in stati
    assert FollowerStatus.bio_scraped in stati
    assert FollowerStatus.message_generated in stati


@pytest.mark.parametrize("stato_escluso", [
    FollowerStatus.sent, FollowerStatus.failed,
    FollowerStatus.skipped, FollowerStatus.replied,
    # pending_approval NON e' terminale (il messaggio e' pronto, aspetta solo
    # un umano) ma non e' comunque mai mandabile ORA: e' la regressione che
    # questo task esiste per impedire, uniformare le due liste e' il bug.
    FollowerStatus.pending_approval,
])
def test_alcuni_stati_non_sono_mai_mandabili(stato_escluso):
    for livello in ("none", "bio", "contacts"):
        assert not is_sendable(_campagna(livello), stato_escluso)
        assert stato_escluso not in sendable_statuses(_campagna(livello))


def test_pending_mandabile_solo_a_livello_none():
    assert is_sendable(_campagna("none"), FollowerStatus.pending)
    assert not is_sendable(_campagna("bio"), FollowerStatus.pending)
    assert not is_sendable(_campagna("contacts"), FollowerStatus.pending)


def test_il_lavoro_residuo_include_lapprovazione_e_i_pending_a_livello_none():
    # "resta lavoro?" e' una domanda diversa da "posso mandare a questo?":
    # include chi e' in coda di approvazione.
    residuo_bio = remaining_work_statuses(_campagna("bio"))
    assert FollowerStatus.pending_approval in residuo_bio
    assert FollowerStatus.pending not in residuo_bio

    residuo_none = remaining_work_statuses(_campagna("none"))
    assert FollowerStatus.pending_approval in residuo_none
    assert FollowerStatus.pending in residuo_none


@pytest.mark.parametrize("stato_terminale", [
    FollowerStatus.sent, FollowerStatus.failed,
    FollowerStatus.skipped, FollowerStatus.replied,
])
def test_gli_stati_terminali_non_sono_mai_lavoro_residuo(stato_terminale):
    for livello in ("none", "bio", "contacts"):
        assert stato_terminale not in remaining_work_statuses(_campagna(livello))


def test_campagna_senza_il_campo_si_comporta_come_prima():
    # Robustezza: un oggetto Campaign vecchio (o un mock nei test) senza
    # enrichment_level non deve far esplodere niente ne' aprire la porta.
    vecchia = SimpleNamespace()
    assert not is_sendable(vecchia, FollowerStatus.pending)
