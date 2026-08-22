"""Difesa in profondita' sull'anagrafica globale — ribaltata dalla Task 5.

Prima di 2026-08-22-username-chiave-di-prima-classe.md l'anagrafica rifiutava
le targhe provvisorie: se una targa provvisoria fosse arrivata in
GlobalContact, la protezione anti-doppio-DM cross-campagna non avrebbe
riconosciuto la persona (chiave diversa da quella registrata via API) e le
avrebbe potuto mandare un secondo messaggio.

Il costo di quel rifiuto era pero' che il contatto non entrava mai in
anagrafica: `reservation.try_reserve` ritornava False, il follower finiva
`skipped` con motivo "already_contacted_globally", e non riceveva MAI il DM
— non una protezione, un invio che non partiva.

Con `global_contacts.username_norm` (migration 039, UNIQUE) come ponte fra le
due rappresentazioni, la stessa persona converge su una riga sola qualunque
canale l'abbia vista, e la targa negativa non spacca piu' nulla. Questo file
e' stato riscritto DI PROPOSITO per il nuovo comportamento.
"""
from app.services.global_contact_service import targa_ammessa_in_anagrafica


def test_targa_vera_ammessa():
    assert targa_ammessa_in_anagrafica(76561234567) is True


def test_targa_provvisoria_ammessa():
    assert targa_ammessa_in_anagrafica(-8834567123) is True


def test_zero_e_none_rifiutati():
    assert targa_ammessa_in_anagrafica(0) is False
    assert targa_ammessa_in_anagrafica(None) is False
