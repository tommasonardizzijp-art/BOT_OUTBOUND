"""Ciclo di vita di una scansione auto-discover.

Una run e' l'unica traccia di com'e' andato uno scan. Il motore
(esegui_discover_run) non la conosce: apre e chiude chi lo lancia, cosi' il
motore resta quello gia' collaudato contro il DOM vero.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import desc, select

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
_NUM_RE = re.compile(r"\d(?:[\s.\-/]?\d){5,}")


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
    """Scrive l'esito e chiude la run. Idempotente: una run gia' chiusa non
    viene toccata -- il worker puo' chiamare questa due volte (percorso
    normale + guardia nel finally) e la seconda non deve cancellare la prima.
    """
    run = await db.scalar(select(WaDiscoverRun).where(WaDiscoverRun.id == run_id))
    if run is None or run.stato != "running":
        return

    if errore is not None:
        run.stato = "failed"
        run.motivo = "errore_imprevisto"
        run.errore = _sanifica_errore(errore)[:2000]
    else:
        motivo = esito.get("motivo", "completato")
        run.stato = "done" if motivo in MOTIVI_NON_GUASTO else "failed"
        run.motivo = motivo
        run.salvate = esito.get("salvate", 0)
        run.aggiornate = esito.get("aggiornate", 0)
        run.saltate_gia_note = esito.get("saltate_gia_note", 0)
        run.non_verificate = esito.get("non_verificate", 0)
        run.dichiarato = esito.get("dichiarato")
        run.copertura = calcola_copertura(esito)
        run.sync_letta = esito.get("sync_letta")
        run.sync_stato = esito.get("sync_stato", "ignota")

    run.finished_at = datetime.utcnow()
    await db.flush()


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
