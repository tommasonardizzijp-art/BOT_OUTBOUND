"""Rende visibile su SQLite il difetto naive-su-colonna-aware.

**Il buco che chiude.** La suite gira su SQLite, che non ha il concetto di fuso:
restituisce naive qualunque cosa gli si dia. Quindi un test che scrive
`datetime.utcnow()` in una colonna `DateTime(timezone=True)` resta verde per
sempre, mentre in produzione quello stesso valore atterra due ore indietro
(misurato su Postgres il 17/08: 7200 secondi esatti). E' la cecita' che ha tenuto
nascosto lo skew WhatsApp per settimane -- 4.720 righe da migrare -- e che
lascerebbe passare la prossima occorrenza allo stesso modo.

**Perche' un listener e non un grep.** Cercare le occorrenze nel testo dei test
non funziona: il nome del campo non basta a decidere. `created_at` e' aware su
`WaNumber` e naive su `User`; `locked_at` e' aware su `WaCampaignContact` e naive
su `Follower`. Un rilevatore per nome produce falsi positivi in massa (53 su una
prova, la maggior parte innocenti) e chi lo usa finisce per "correggere" righe
gia' corrette. Qui invece si interroga il **mapper reale** dell'oggetto che sta
per essere scritto: la colonna sa da se' se porta il fuso.

Attivo solo dentro la suite. Il codice di produzione non lo carica.
"""
from datetime import datetime

from sqlalchemy import event
from sqlalchemy.orm import Session


def _colonne_aware(obj) -> list[str]:
    """Attributi dell'oggetto mappati su una colonna che porta il fuso."""
    mapper = getattr(type(obj), "__mapper__", None)
    if mapper is None:
        return []
    nomi = []
    for prop in mapper.column_attrs:
        colonna = prop.columns[0]
        if getattr(colonna.type, "timezone", False):
            nomi.append(prop.key)
    return nomi


def _violazioni(obj) -> list[str]:
    fuori = []
    for nome in _colonne_aware(obj):
        valore = getattr(obj, nome, None)
        if isinstance(valore, datetime) and valore.tzinfo is None:
            fuori.append(f"{type(obj).__name__}.{nome} = {valore!r}")
    return fuori


def installa() -> None:
    """Aggancia il controllo al flush di ogni sessione ORM -- solo se richiesto.

    **Perche' opt-in e non sempre attiva.** Accesa sulla suite intera fa
    emergere **99 rossi** (misurati il 17/08 su 1991 test). Non sono falsi
    allarmi: sullo stesso campione di tre file, 20 rossi con la guardia e **zero**
    senza, quindi cio' che trova esiste davvero. Ma sono 99 correzioni da fare a
    mano, una per una, verificando ogni volta su quale colonna insiste il campo:
    accenderla senza averle fatte lascerebbe la CI rossa, e una CI rossa per
    default e' una CI che si smette di guardare.

    Si accende cosi', un file o una cartella per volta, mentre si bonifica:

        WA_GUARDIA_AWARE=1 pytest tests/test_wa_cron.py

    Quando i 99 saranno chiusi, questa funzione puo' diventare incondizionata --
    ed e' il momento in cui la suite comincera' davvero a difendere l'invariante
    invece di essere cieca ad essa.
    """
    import os

    if os.environ.get("WA_GUARDIA_AWARE") != "1":
        return

    @event.listens_for(Session, "before_flush")
    def _controlla_naive_su_colonna_aware(session, flush_context, instances):
        colpe: list[str] = []
        for obj in list(session.new) + list(session.dirty):
            colpe.extend(_violazioni(obj))
        if colpe:
            raise AssertionError(
                "datetime NAIVE scritto in una colonna timestamptz. Su SQLite non "
                "si vede, in produzione il valore atterra spostato dell'offset del "
                "fuso (-2h in ora legale, misurato su Postgres: 7200s esatti). "
                "Usa app.utils.tempo.adesso_utc().\n  " + "\n  ".join(sorted(set(colpe))))
