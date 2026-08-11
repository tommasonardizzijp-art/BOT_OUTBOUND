"""Modalita' segnalibro: riprendere da dove si era arrivati.

Il motore, a regime, spende quasi tutta la sessione a riattraversare la parte
alta della lista che ha gia' lavorato. Un umano non lo farebbe: si segnerebbe
la data a cui e' arrivato e da li' ripartirebbe.

Tre scelte di disegno, tutte con una ragione precisa:

1. LA SOGLIA E' UNA DATA, non il riferimento a una chat. Memorizzare "l'ultima
   chat vista" sembra piu' preciso, ma se proprio quella chat ricevesse una
   risposta risalirebbe in cima alla lista e il riferimento sarebbe perso.

2. LA DATA SI LEGGE DALLA RIGA DI LISTA ('5 g', '20 h'), mai aprendo il thread:
   aprire per sapere la data annullerebbe tutto il guadagno.

3. IL CURSORE SCENDE SOLO. Segna quanto in basso si e' arrivati; una riga piu'
   recente incontrata dopo non lo riporta su, altrimenti dopo un reset della
   lista la sessione successiva ripartirebbe da piu' in alto.

Nel dubbio si LEGGE: eta' illeggibile, soglia assente, modalita' spenta — in
tutti questi casi la riga si lavora. Saltare per errore perde contatti in
silenzio, che e' il fallimento peggiore di questo modulo.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.services.inbox_browser.testo import eta_riga_in_ore

LINGUA_PREDEFINITA = "it"


def riga_da_saltare(data_relativa: str | None, soglia_ore: float | None,
                    attiva: bool) -> bool:
    """True se questa riga sta nella zona gia' lavorata e va solo attraversata."""
    if not attiva or soglia_ore is None:
        return False
    eta = eta_riga_in_ore(data_relativa, LINGUA_PREDEFINITA)
    if eta is None:
        return False
    return eta < soglia_ore


# Tetto di buon senso per l'eta' di una riga: 50 anni. Nessuna data relativa
# reale lo avvicina; serve solo a far degradare in sicurezza (cursore
# invariato, stesso trattamento di un'eta' illeggibile) un valore assurdo
# invece di far esplodere `timedelta` con un OverflowError.
_ETA_MASSIMA_ORE = 24 * 365 * 50


def nuovo_cursore(cursore_attuale: datetime | None, eta_ore: float | None,
                  adesso: datetime) -> datetime | None:
    """Il cursore aggiornato dopo aver lavorato una riga di questa eta'."""
    if eta_ore is None or eta_ore > _ETA_MASSIMA_ORE:
        return cursore_attuale
    candidato = adesso - timedelta(hours=eta_ore)
    if cursore_attuale is None:
        return candidato
    return min(cursore_attuale, candidato)


def soglia_in_ore(cursore: datetime | None, adesso: datetime) -> float | None:
    """A quante ore fa corrisponde il cursore, adesso."""
    if cursore is None:
        return None
    ore = (adesso - cursore).total_seconds() / 3600
    return ore if ore > 0 else None
