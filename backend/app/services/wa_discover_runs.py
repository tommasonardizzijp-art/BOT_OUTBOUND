"""Ciclo di vita di una scansione auto-discover.

Una run e' l'unica traccia di com'e' andato uno scan. Il motore
(esegui_discover_run) non la conosce: apre e chiude chi lo lancia, cosi' il
motore resta quello gia' collaudato contro il DOM vero.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import desc, select, update

from app.models.wa import WaDiscoverRun

# I motivi che il motore restituisce e che NON sono un guasto nostro: la run
# si chiude 'done' anche con questi, perche' il sistema ha fatto la cosa
# giusta (non ha scansionato, e ha detto perche').
MOTIVI_NON_GUASTO = {
    "completato", "raccolta_parziale", "fermato_dopo_stallo",
    "sync_sotto_soglia", "sync_ignota", "sidebar_coperta", "wa_halted",
    "numero_non_attivo", "profilo_occupato", "sessione_non_loggata",
}

# P12: nessun numero di telefono in chiaro in colonne testuali. Oggi
# esegui_discover_run cattura tutto in un blanket except e non ri-solleva mai
# (wa_discover_run.py), quindi 'errore' arriva qui vuoto in pratica -- ma
# 'titolo_atteso' dentro wa_discover/pannello.py e' spesso il numero grezzo
# quando manca un nome salvato, e chiudi_run e' l'ultimo cancello prima del
# DB: non ci si puo' affidare al fatto che nessun raise futuro lo porti
# dentro un messaggio d'eccezione. Stesso pattern di scripts/poc_wa/wa_lib.py
# (mask_pii): sequenze di 6+ cifre, separate o no, sono quasi certamente un
# numero e non servono a diagnosticare il guasto.
#
# [\s.\-/]{0,3}, non [\s.\-/]? (un solo separatore): con due separatori
# consecutivi -- doppio spazio, o il doppio no-break space visto in un
# titolo WhatsApp vero ('Sofia\xa0\xa0D'ari') -- la versione a un solo
# carattere lascia scoperto il pezzo di cifre prima del secondo separatore
# (verificato: "333\xa0\xa01234567" -> "333\xa0\xa0<num>", "333" in chiaro).
# {0,3} copre i casi osservati senza normalizzare gli spazi Unicode prima:
# un carattere in piu' nella stessa regex, niente passaggio aggiuntivo.
_NUM_RE = re.compile(r"\d(?:[\s.\-/]{0,3}\d){5,}")


def _sanifica_errore(errore: str) -> str:
    return _NUM_RE.sub("<num>", errore)


def calcola_copertura(esito: dict) -> int | None:
    """Percentuale di lista coperta, 0-100, o None se non calcolabile.

    I salti dell'incrementale contano come raccolto: quelle chat le abbiamo
    gia', non ripagarle e' il punto. Senza includerli, ogni riscansione
    riuscita sembrerebbe una raccolta al 2%.
    """
    dichiarato = esito.get("dichiarato")
    if not dichiarato or dichiarato <= 0:
        return None
    coperte = (esito.get("salvate", 0) + esito.get("aggiornate", 0)
               + esito.get("saltate_gia_note", 0))
    return min(100, round(coperte * 100 / dichiarato))


async def apri_run(db, *, tenant_id: str, number_id: str,
                   avviato_da: str = "manuale") -> WaDiscoverRun:
    run = WaDiscoverRun(tenant_id=tenant_id, number_id=number_id,
                        avviato_da=avviato_da, stato="running", motivo="in_corso")
    db.add(run)
    await db.flush()
    return run


async def chiudi_run(db, run_id: str, esito: dict, *, errore: str | None = None) -> None:
    """Scrive l'esito e chiude la run. Compare-and-swap SQL, non
    SELECT-poi-scrivi: un solo UPDATE ... WHERE id=:id AND stato='running'.

    Il worker (Task 4, a fine scan) e l'endpoint (Task 5, quando
    l'accodamento ARQ fallisce) possono chiamare questa funzione sulla
    STESSA run quasi in contemporanea. Con una SELECT seguita da uno UPDATE
    in Python (versione precedente) la guardia 'if run.stato != running' non
    e' atomica fra sessioni diverse: due chiamate indipendenti la superano
    entrambe e scrivono, in ordine imprevedibile, producendo un ibrido --
    riprodotto 3/3 in review con due chiudi_run in asyncio.gather su due
    sessioni indipendenti (una chiudeva con successo, l'altra con errore: la
    riga finale mescolava stato='failed' coi contatori della run riuscita).
    Il WHERE nell'UPDATE e' la guardia, valutata dal DB in una singola
    operazione atomica: se un'altra sessione ha gia' chiuso la run, la
    UPDATE non tocca nessuna riga e non si scrive nulla -- stessa idempotenza
    di prima, garantita dal DB e non da un controllo Python fra due query
    separate.
    """
    ora = datetime.utcnow()
    if errore is not None:
        valori = {
            "stato": "failed",
            "motivo": "errore_imprevisto",
            "errore": _sanifica_errore(errore)[:2000],
            "finished_at": ora,
        }
    else:
        motivo = esito.get("motivo", "completato")
        valori = {
            "stato": "done" if motivo in MOTIVI_NON_GUASTO else "failed",
            "motivo": motivo,
            "salvate": esito.get("salvate", 0),
            "aggiornate": esito.get("aggiornate", 0),
            "saltate_gia_note": esito.get("saltate_gia_note", 0),
            "non_verificate": esito.get("non_verificate", 0),
            "dichiarato": esito.get("dichiarato"),
            "copertura": calcola_copertura(esito),
            "sync_letta": esito.get("sync_letta"),
            "sync_stato": esito.get("sync_stato", "ignota"),
            "finished_at": ora,
        }

    await db.execute(
        update(WaDiscoverRun)
        .where(WaDiscoverRun.id == run_id, WaDiscoverRun.stato == "running")
        .values(**valori))


async def run_attiva(db, number_id: str) -> WaDiscoverRun | None:
    return await db.scalar(select(WaDiscoverRun).where(
        WaDiscoverRun.number_id == number_id, WaDiscoverRun.stato == "running"))


async def ultima_run(db, number_id: str) -> WaDiscoverRun | None:
    return await db.scalar(
        select(WaDiscoverRun).where(WaDiscoverRun.number_id == number_id)
        .order_by(desc(WaDiscoverRun.started_at)).limit(1))


async def storico(db, number_id: str, *, limit: int = 10) -> list[WaDiscoverRun]:
    righe = await db.execute(
        select(WaDiscoverRun).where(WaDiscoverRun.number_id == number_id)
        .order_by(desc(WaDiscoverRun.started_at)).limit(limit))
    return list(righe.scalars().all())
