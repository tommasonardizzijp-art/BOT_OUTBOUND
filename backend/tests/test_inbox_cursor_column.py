"""Le due colonne del segnalibro esistono e sono opzionali.

`inbox_cursor_at` e' la data della riga di lista piu' vecchia gia' lavorata:
la soglia sotto la quale, in modalita' segnalibro, non si scende a leggere.
E' una DATA e non il riferimento a una chat, di proposito: se si memorizzasse
"l'ultima chat vista" e proprio quella ricevesse una risposta, risalirebbe in
cima alla lista e il riferimento sarebbe perso.
"""
from app.models.campaign import Campaign


def test_la_campagna_ha_il_cursore_inbox():
    assert hasattr(Campaign, "inbox_cursor_at")
    assert hasattr(Campaign, "inbox_cursor_updated_at")


def test_il_cursore_e_opzionale():
    """Una campagna che non ha mai girato in modalita' segnalibro non ha
    cursore, e deve poter partire lo stesso."""
    assert Campaign.__table__.c.inbox_cursor_at.nullable is True
    assert Campaign.__table__.c.inbox_cursor_updated_at.nullable is True
