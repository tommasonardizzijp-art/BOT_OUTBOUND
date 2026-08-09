"""Vincolo di configurazione fra i tre motori di una campagna.

Il motore inbox browser assegna una targa PROVVISORIA (il pk non e' ricavabile
dalla pagina). Quella targa e' un ponte: viene sostituita con la targa vera
durante l'arricchimento, che naviga per username e riporta il pk.

Se l'arricchimento non avviene, il ponte non viene mai attraversato e la targa
provvisoria arriva fino a GlobalContact e al dedup anti-doppio-DM: la stessa
persona raccolta via API in un'altra campagna avrebbe una chiave diversa e
potrebbe ricevere DUE messaggi.

Due condizioni, non una:
- enrichment_level != 'none', altrimenti la Fase Bio non parte affatto
  (scrape_bios.py:82, PRIMA del dispatch su bio_engine a :114) — ed e' il default
  sulle campagne nuove (campaign.py:182-184);
- bio_engine == 'browser', perche' l'arricchimento API interroga PER PK
  (profile_lookup.py:49, user_info_v1(pk)) e su una targa provvisoria cercherebbe
  una persona inesistente.
"""
from __future__ import annotations

from app.models.campaign import ENRICHMENT_NONE


def valida_combinazione_motori(
    inbox_engine: str, bio_engine: str, enrichment_level: str
) -> str | None:
    """Ritorna il messaggio d'errore, o None se la combinazione e' valida."""
    if inbox_engine != "browser":
        return None

    problemi: list[str] = []
    if enrichment_level == ENRICHMENT_NONE:
        problemi.append(
            "il livello di arricchimento non puo' essere 'nessuno' (serve 'bio' o 'contatti'): "
            "senza arricchimento i contatti restano con un identificativo provvisorio, "
            "che aggirerebbe la protezione contro il doppio invio alla stessa persona"
        )
    if bio_engine != "browser":
        problemi.append(
            "l'arricchimento deve avvenire via browser: quello via API interroga Instagram "
            "con l'identificativo numerico, che sui contatti raccolti dal browser non esiste ancora"
        )
    if not problemi:
        return None
    return "Campagna con raccolta inbox via browser: " + "; ".join(problemi) + "."
