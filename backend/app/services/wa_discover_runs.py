"""Ciclo di vita di una scansione auto-discover.

Una run e' l'unica traccia di com'e' andato uno scan. Il motore
(esegui_discover_run) non la conosce: apre e chiude chi lo lancia, cosi' il
motore resta quello gia' collaudato contro il DOM vero.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import desc, select, update

from app.config import settings
from app.models.wa import WaDiscoverRun

# I motivi che il motore restituisce e che NON sono un guasto nostro: la run
# si chiude 'done' anche con questi, perche' il sistema ha fatto la cosa
# giusta (non ha scansionato, e ha detto perche').
MOTIVI_NON_GUASTO = {
    # "sync_ignota" NON c'e' piu': dal 15/08 il gate procede su "ignota"
    # invece di fermarsi (puo_scansionare_lettura), quindi il motore non lo
    # produce piu' come esito di rifiuto. Lo stato resta visibile in
    # sync_stato, solo non e' piu' un motivo di run.
    "completato", "raccolta_parziale", "fermato_dopo_stallo",
    "sync_sotto_soglia", "sidebar_coperta", "wa_halted",
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


async def chiudi_se_orfana(db, number_id: str) -> bool:
    """Chiude una run rimasta 'running' oltre ogni tempo credibile.

    True se ne ha chiusa una. Serve perche' l'indice unico parziale rende un
    numero non piu' scansionabile finche' esiste una run aperta: un worker
    ucciso a meta' scan, senza questa, lo blocca per sempre.

    Si chiama dal gate e non da un cron di proposito: chi tenta di
    scansionare e' esattamente chi ha bisogno che la vecchia sia chiusa, e un
    cron in piu' e' un pezzo in piu' che puo' rompersi in silenzio.

    NON e' piu' sola lettura: se trova un'orfana, la chiude con una sessione
    PROPRIA che committa lei stessa (invece di scrivere sulla sessione del
    chiamante e sperare che qualcuno committi dopo). puo_lanciare non
    committa mai di suo -- se dopo aver chiuso l'orfana un'altra guardia
    rifiuta comunque (es. RAM), l'endpoint solleva un 409 senza commit e
    get_db() chiude la sessione con un rollback implicito: la guarigione
    scritta li' sparirebbe, lasciando la run 'running' a DB per un altro
    giro (trovato in review, Task 10). Con una sessione propria la
    guarigione e' durevole a prescindere da cosa decide il chiamante dopo.

    La lettura iniziale resta sulla sessione del chiamante: aprirne una
    propria costerebbe una connessione in piu' a OGNI chiamata, mentre il
    caso normale -- nessuna orfana -- e' quasi sempre.
    """
    run = await run_attiva(db, number_id)
    if run is None:
        return False
    limite = datetime.utcnow() - timedelta(minutes=settings.wa_discover_run_orfana_min)
    if run.started_at > limite:
        return False

    run_id = run.id
    logger.warning(f"[WaDiscover] run {run_id} su {number_id} aperta da oltre "
                   f"{settings.wa_discover_run_orfana_min} minuti: chiusa come orfana "
                   "(il worker che doveva chiuderla non c'e' piu')")

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db_propria:
        await chiudi_run(db_propria, run_id, {},
                         errore=f"run rimasta aperta oltre "
                                f"{settings.wa_discover_run_orfana_min} minuti senza "
                                "che nessuno la chiudesse")
        # chiudi_run scrive motivo='errore_imprevisto' per la via dell'errore:
        # qui il motivo vero e' un altro, e distinguerlo serve a chi legge lo
        # storico. Ri-letta nella sessione propria: 'run' sopra vive nella
        # sessione del chiamante, un'altra connessione.
        chiusa = await db_propria.scalar(
            select(WaDiscoverRun).where(WaDiscoverRun.id == run_id))
        if chiusa is not None:
            chiusa.motivo = "run_orfana"
        await db_propria.commit()

    # La sessione del chiamante ha ancora 'run' nella sua identity map: dopo
    # una chiusura scritta e committata da un'ALTRA sessione, un expire
    # esplicito e' la difesa corretta contro il rischio di identity map gia'
    # trovato nel Task 2 (li' era un vero problema, stessa sessione con un
    # CAS Core). VERIFICATO qui con un test dedicato che togliendo questa
    # riga la suite resta comunque verde (5/5): su SQLite/aiosqlite, in
    # QUESTO schema a due sessioni, la SELECT fresca di run_attiva vede gia'
    # il commit dell'altra sessione senza bisogno dell'expire, perche' il
    # filtro stato='running' e' valutato dal motore SQL sui dati attuali,
    # non dall'identity map. La teniamo comunque: e' a costo zero (nessun
    # I/O), e non e' garantito che regga allo stesso modo su un motore o una
    # configurazione di isolamento diversi (es. Postgres con isolamento piu'
    # stretto di READ COMMITTED).
    db.expire(run)
    return True
