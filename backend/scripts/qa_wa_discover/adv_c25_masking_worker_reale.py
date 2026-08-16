"""Adversarial C.25 -- numero in chiaro nel testo di un'eccezione del
motore, verificato END-TO-END dal worker vero (non solo a livello di
chiudi_run isolato).

docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, caso 25:
"errore di una run chiusa contenente un numero di telefono in chiaro nel
testo dell'eccezione originale (es. un motore che un giorno solleva con un
numero nel messaggio) -> verificato che la sanificazione lo maschera prima
di finire a DB (test gia' coperto a livello unit, qui riverificare
end-to-end passando dal worker vero)."

I test unit esistenti (test_wa_discover_runs_servizio.py::
test_chiudi_run_con_errore_maschera_un_numero_in_chiaro) chiamano
chiudi_run() direttamente con la stringa d'errore gia' pronta. Qui invece
si passa da wa_discover_worker.wa_discover_task() vero, con un
AsyncSessionLocal VERO (non una sessione condivisa col test come fa
_sessione_finta in test_wa_discover_worker.py), esattamente come lo
chiamerebbe ARQ in produzione.

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_c25_masking_worker_reale.py
"""
import asyncio
import re
from unittest.mock import patch

import _bootstrap  # noqa: E402

NUMERO_IN_CHIARO = "+39 342 146 0077"
# Stessa regex del sanificatore (wa_discover_runs._NUM_RE): sequenze di 6+
# cifre, separate o no. Qui la usiamo per la verifica indipendente, non
# importata dal modulo sotto esame -- altrimenti un bug nella regex stessa
# passerebbe il test per costruzione.
_CIFRE_LUNGHE = re.compile(r"\d(?:[\s.\-/]{0,3}\d){5,}")


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.models.wa import WaDiscoverRun
    from app.services import wa_discover_runs
    from app.utils.db_dialect import to_async_database_url
    from app.workers import wa_discover_worker
    from tests.factories_wa import make_number, make_tenant

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant)
        run = await wa_discover_runs.apri_run(db, tenant_id=tenant.id, number_id=number.id)
        await db.commit()
        run_id = run.id
        number_id = number.id

    async def motore_che_sputa_il_numero(number_id, **kw):
        # Scenario del caso 25: un motore che un giorno solleva con un
        # numero VERO nel messaggio (es. incollato da un titolo non ancora
        # mascherato in un punto del codice che non dovrebbe farlo).
        raise RuntimeError(f"pannello non apribile per {NUMERO_IN_CHIARO}, "
                           "elemento non trovato")

    with patch.object(wa_discover_worker, "esegui_discover_run", motore_che_sputa_il_numero):
        # AsyncSessionLocal VERO del modulo, non una sessione condivisa col
        # test: e' esattamente il percorso che ARQ userebbe in produzione.
        await wa_discover_worker.wa_discover_task({}, number_id, run_id)

    problemi = []
    async with maker() as db:
        chiusa = await db.scalar(select(WaDiscoverRun).where(WaDiscoverRun.id == run_id))
        if chiusa is None:
            problemi.append("la run e' sparita")
        else:
            if chiusa.stato != "failed":
                problemi.append(f"stato atteso 'failed', trovato {chiusa.stato!r}")
            if chiusa.errore is None:
                problemi.append("errore e' None, non e' stato scritto niente")
            elif _CIFRE_LUNGHE.search(chiusa.errore):
                problemi.append(
                    f"NUMERO IN CHIARO trovato nella colonna errore (P12 violato): "
                    f"{chiusa.errore!r}")
            elif "<num>" not in chiusa.errore:
                problemi.append(
                    f"nessuna sequenza di cifre in chiaro, ma manca anche il "
                    f"placeholder <num> atteso dalla sanificazione: {chiusa.errore!r}")
    await eng.dispose()

    if problemi:
        _bootstrap.esito("C.25 masking end-to-end dal worker vero", False, "\n".join(problemi))
    else:
        _bootstrap.esito(
            "C.25 masking end-to-end dal worker vero", True,
            "eccezione REALE con un numero in chiaro sollevata dal motore, run "
            "chiusa dal worker vero (AsyncSessionLocal, non una sessione di test "
            "condivisa) -- colonna errore mascherata, nessuna sequenza di cifre "
            "in chiaro a DB.")


asyncio.run(main())
