"""L'unico posto in cui si dice "adesso", e l'unico in cui si legge un
istante che arriva dal DB.

**Perche' esiste.** `datetime.utcnow()` restituisce un datetime *naive*: sa
che ore sono a Greenwich ma non dichiara di essere UTC. Scritto in una
colonna `DateTime(timezone=True)` (`timestamptz` su PostgreSQL) da un
processo con fuso locale Europe/Rome, il driver non ha modo di sapere che
quel valore era gia' UTC e lo consegna come ora **locale**: PostgreSQL lo
converte a UTC sottraendo l'offset del fuso, cioe' due ore in ora legale.

    naive utcnow inviato : 2026-08-16 13:05:21
    riletto come tstz    : 2026-08-16 11:05:20+00:00     <-- -2h

Il conto e' stato pagato due volte lo stesso giorno (16/08): una campagna in
pieno invio e' sembrata ferma da due ore -- si e' arrivati a un passo dal
riavviare i worker durante invii veri a clienti -- e il confronto fra uno
`started_at` tornato aware e un `utcnow()` naive ha sollevato un `TypeError`
risalito fino all'endpoint come 500 al posto di un 409.

La suite non lo vede perche' gira su SQLite, che per `DateTime(timezone=True)`
restituisce sempre naive: naive-vs-naive non solleva mai. "Verde qui" non e'
evidenza di niente su questo asse -- per questo esiste anche
`tests/test_wa_tempo_guardia.py`, che vieta il ritorno di `utcnow()` nei
moduli WhatsApp invece di sperare che qualcuno se ne ricordi.
"""
from datetime import datetime, timezone


def adesso_utc() -> datetime:
    """L'istante presente, sempre aware. L'unico modo lecito di dire "adesso"."""
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """Rende confrontabile un istante che arriva dal DB, su qualunque backend.

    Serve perche' la stessa colonna torna **aware** da PostgreSQL e **naive**
    da SQLite (dove gira la suite): un confronto scritto per uno dei due
    backend esplode sull'altro con `TypeError: can't compare offset-naive and
    offset-aware datetimes`. Il naive si assume UTC, che e' cio' che
    `adesso_utc()` scrive ovunque.

    `None` passa attraverso: le colonne di questo dominio (`locked_at`,
    `sent_at`, `next_action_at`) sono nullable e il chiamante ha gia' il
    proprio ramo per l'assenza del valore.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
