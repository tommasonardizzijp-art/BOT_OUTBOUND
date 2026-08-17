"""Adversarial I -- verifica finale invarianti via SQL diretto
(docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, casi 45-48).

Non "i test passano": query dirette sul DB dopo aver fatto girare una serie
di scenari realistici (run orfane, errori con numeri in chiaro, chat senza
numero, scritture concorrenti), passando dai servizi VERI (apri_run,
chiudi_run, chiudi_se_orfana, salva_scoperta) come farebbe il codice
in produzione -- non righe scritte a mano che aggirano la logica che
dovrebbe garantire l'invariante.

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_i_invarianti_sql.py
"""
import asyncio
import re
from datetime import timedelta

import _bootstrap  # noqa: E402

from app.utils.tempo import adesso_utc  # noqa: E402

_CIFRE_LUNGHE = re.compile(r"\d(?:[\s.\-/]{0,3}\d){5,}")


async def _semina_scenari(maker) -> dict:
    from app.services import wa_discover_runs
    from app.services.wa_discover import classifica, salvataggio
    from tests.factories_wa import make_number, make_tenant

    async with maker() as db:
        tenant = await make_tenant(db)
        n_orfana = await make_number(db, tenant, label="Numero con run orfana")
        n_ok = await make_number(db, tenant, label="Numero con run pulita")
        n_chat = await make_number(db, tenant, label="Numero con chat scoperte")
        await db.commit()

        # 1) Run lasciata oltre soglia, sanata dal servizio vero (non a
        #    mano): deve finire failed/run_orfana, MAI restare running.
        run_orfana = await wa_discover_runs.apri_run(
            db, tenant_id=tenant.id, number_id=n_orfana.id)
        run_orfana.started_at = adesso_utc() - timedelta(minutes=1000)
        await db.commit()

    async with maker() as db:
        await wa_discover_runs.chiudi_se_orfana(db, n_orfana.id)
        await db.commit()

    async with maker() as db:
        # 2) Run chiusa con un errore che (in un motore ipotetico futuro)
        #    contenesse un numero in chiaro -- deve finire mascherato.
        run_errore = await wa_discover_runs.apri_run(
            db, tenant_id=tenant.id, number_id=n_ok.id)
        await db.commit()
        await wa_discover_runs.chiudi_run(
            db, run_errore.id, {},
            errore="pannello non apribile per +39 342 146 0077, elemento assente")
        await db.commit()

        # 3) Chat scoperte realistiche: una con nome vero, una senza numero
        #    (gruppo), una col titolo=numero (mascherata da etichetta_visibile),
        #    e il caso limite gia' documentato nel sorgente (classifica.py,
        #    etichetta_visibile): titolo che E' un numero ma troppo lungo per
        #    l'E.164 -- ne' un nome vero ne' un numero estraibile.
        await salvataggio.salva_scoperta(
            db, tenant.id, n_chat.id,
            classifica.RigaScoperta(titolo="Mario Rossi", numero="+393421111111",
                                    numero_leggibile=True, tipo="individuale"))
        await salvataggio.salva_scoperta(
            db, tenant.id, n_chat.id,
            classifica.RigaScoperta(titolo="Gruppo Famiglia", numero=None,
                                    numero_leggibile=False, tipo="gruppo"))
        await salvataggio.salva_scoperta(
            db, tenant.id, n_chat.id,
            classifica.RigaScoperta(titolo="+39 342 146 0077 99 88 77", numero=None,
                                    numero_leggibile=False, tipo="ignoto"))

    return {"n_orfana": n_orfana.id, "n_ok": n_ok.id, "n_chat": n_chat.id,
           "run_orfana_id": run_orfana.id, "run_errore_id": run_errore.id}


async def _caso45_nessuna_running_oltre_soglia(maker, settings) -> tuple[bool, str]:
    from sqlalchemy import text

    # Naive, questo limite finiva confrontato con `started_at` che e'
    # timestamptz: PostgreSQL lo casta usando il fuso della sessione e la
    # soglia scivolava di 2 ore. Un caso di collaudo che passa perche' guarda
    # la finestra sbagliata e' peggio di un caso che non esiste.
    limite = adesso_utc() - timedelta(minutes=settings.wa_discover_run_orfana_min)
    async with maker() as db:
        righe = (await db.execute(text(
            "SELECT id, number_id FROM wa_discover_runs "
            "WHERE stato='running' AND started_at < :limite"),
            {"limite": limite})).all()
    if righe:
        return False, f"{len(righe)} run 'running' oltre soglia orfana MAI sanate: {righe}"
    return True, "0 righe running oltre la soglia orfana (query diretta SQL)"


async def _caso46_nessun_numero_in_chiaro(maker) -> tuple[bool, str]:
    from sqlalchemy import text

    async with maker() as db:
        righe = (await db.execute(text(
            "SELECT id, errore FROM wa_discover_runs WHERE errore IS NOT NULL"))).all()
    sospette = [(rid, err) for rid, err in righe if _CIFRE_LUNGHE.search(err)]
    if sospette:
        return False, f"numero(i) in chiaro trovati in wa_discover_runs.errore: {sospette}"
    return True, f"{len(righe)} righe con errore non-NULL ispezionate, nessuna sequenza di 6+ cifre in chiaro"


async def _caso47_nessuna_scrittura_invio_durante_scan(maker, seme) -> tuple[bool, str]:
    from sqlalchemy import text

    from tests.factories_wa import make_campaign, make_campaign_contact, make_contact

    # Seme non banale: una riga wa_messages VERA esiste per questo numero
    # (altrimenti la query passerebbe solo perche' la tabella e' vuota, non
    # perche' il filtro sulla finestra funziona). queued_at e' FUORI dalla
    # finestra del discover run (seminato ore prima nel test).
    async with maker() as db:
        from sqlalchemy import select

        from app.models.wa import WaNumber
        numero = await db.scalar(select(WaNumber).where(WaNumber.id == seme["n_ok"]))
        tenant_id = numero.tenant_id
        from app.models.tenant import Tenant
        tenant = await db.get(Tenant, tenant_id)
        camp, _ = await make_campaign(db, tenant, numero)
        contatto = await make_contact(db, tenant)
        cc = await make_campaign_contact(db, camp, contatto)
        await db.commit()
        from app.models.wa import WaMessage
        msg = WaMessage(campaign_id=camp.id, contact_id=contatto.id,
                        wa_number_id=numero.id, step_index=0, template_variant="a",
                        rendered_text="ciao", queued_at=adesso_utc() - timedelta(days=1))
        db.add(msg)
        await db.commit()

        run = (await db.execute(text(
            "SELECT started_at, finished_at FROM wa_discover_runs WHERE id=:id"),
            {"id": seme["run_errore_id"]})).first()
        # wa_messages: tabella dell'invio reale. La Fase A discover e' sola
        # lettura -- non deve MAI esserci una riga di invio scritta durante
        # la finestra di una run di discover sullo STESSO number_id.
        righe = (await db.execute(text(
            "SELECT COUNT(*) FROM wa_messages WHERE wa_number_id = :nid "
            "AND queued_at BETWEEN :a AND :b"),
            # I due estremi del BETWEEN devono essere dello stesso tipo:
            # `run.started_at` arriva aware dal DB, e un `utcnow()` naive come
            # estremo destro spostava la finestra di 2 ore -- proprio la
            # finestra che questo controllo deve misurare.
            {"nid": seme["n_ok"], "a": run.started_at,
             "b": run.finished_at or adesso_utc()})).scalar()
        totale_messaggi = (await db.execute(text(
            "SELECT COUNT(*) FROM wa_messages WHERE wa_number_id = :nid"),
            {"nid": seme["n_ok"]})).scalar()
    if righe:
        return False, f"{righe} righe wa_messages scritte durante la finestra di un discover run"
    return True, (f"{totale_messaggi} riga/e wa_messages esiste/esistono per questo "
                 f"numero (fuori finestra, seminata di proposito) -- 0 dentro la "
                 f"finestra della run di discover (query diretta SQL, filtrata per "
                 f"number_id)")


async def _caso48_niente_duplicati_sulle_unique(maker, seme) -> tuple[bool, str]:
    from sqlalchemy import text

    problemi = []
    async with maker() as db:
        dup_running = (await db.execute(text(
            "SELECT number_id, COUNT(*) c FROM wa_discover_runs "
            "WHERE stato='running' GROUP BY number_id HAVING c > 1"))).all()
        if dup_running:
            problemi.append(f"numeri con >1 run running: {dup_running}")

        dup_titolo = (await db.execute(text(
            "SELECT number_id, chat_title, COUNT(*) c FROM wa_discovered_chats "
            "WHERE chat_title IS NOT NULL GROUP BY number_id, chat_title "
            "HAVING c > 1"))).all()
        if dup_titolo:
            problemi.append(f"duplicati su (number_id, chat_title): {dup_titolo}")

        dup_hmac = (await db.execute(text(
            "SELECT number_id, phone_hmac, COUNT(*) c FROM wa_discovered_chats "
            "WHERE phone_hmac IS NOT NULL GROUP BY number_id, phone_hmac "
            "HAVING c > 1"))).all()
        if dup_hmac:
            problemi.append(f"duplicati su (number_id, phone_hmac): {dup_hmac}")

        # Extra (nominato esplicitamente dal team-lead, non nella lista 45-48
        # originale ma nella stessa famiglia): nessuna riga wa_discovered_chats
        # senza NESSUNA identita' (ne' titolo ne' numero).
        senza_identita = (await db.execute(text(
            "SELECT id FROM wa_discovered_chats "
            "WHERE chat_title IS NULL AND phone_hmac IS NULL"))).all()
        if senza_identita:
            problemi.append(f"{len(senza_identita)} righe wa_discovered_chats SENZA "
                            f"identita' (ne' titolo ne' hmac): {senza_identita}")

    if problemi:
        return False, "\n".join(problemi)
    return True, ("0 numeri con >1 run running, 0 duplicati su (number_id,chat_title), "
                 "0 duplicati su (number_id,phone_hmac), 0 righe wa_discovered_chats "
                 "senza identita' -- nei dati seminati dal giro adversarial")


async def _caso_extra_salva_scoperta_senza_filtro_a_monte(maker) -> tuple[bool, str]:
    """NON e' uno dei 45-48: e' un finding emerso leggendo salvataggio.py
    (Fase B) per capire cosa garantisce l'invariante 'nessuna riga senza
    identita''. La garanzia oggi vive nel CHIAMANTE (wa_discover_run.py,
    linea ~280: `if not titolo: continue` PRIMA di costruire la
    RigaScoperta), non dentro salva_scoperta/etichetta_visibile stessa.
    Verificato chiamando salva_scoperta DIRETTAMENTE (bypassando quel
    filtro, come farebbe un futuro secondo chiamante, es. uno script di
    recupero) con titolo=None e numero=None."""
    from app.services.wa_discover import classifica, salvataggio
    from tests.factories_wa import make_number, make_tenant

    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant)
        await db.commit()
        try:
            await salvataggio.salva_scoperta(
                db, tenant.id, number.id,
                classifica.RigaScoperta(titolo=None, numero=None,
                                        numero_leggibile=False, tipo="ignoto"))
        except Exception as exc:  # noqa: BLE001
            return True, (f"salva_scoperta(titolo=None, numero=None) ha sollevato "
                          f"({type(exc).__name__}) invece di scrivere una riga senza "
                          "identita' -- MEGLIO di quanto temuto dalla lettura del "
                          "sorgente, nessun'azione necessaria.")

        from sqlalchemy import select

        from app.models.wa import WaDiscoveredChat
        riga = await db.scalar(select(WaDiscoveredChat).where(
            WaDiscoveredChat.number_id == number.id))
        if riga is not None and riga.chat_title is None and riga.phone_hmac is None:
            return False, (
                "FINDING (non uno dei 45-48, emerso dalla lettura del sorgente): "
                "salva_scoperta(titolo=None, numero=None) SCRIVE una riga "
                "wa_discovered_chats con chat_title=None E phone_hmac=None -- "
                "senza identita' per nessuna delle due UniqueConstraint. Nel flusso "
                "di scan reale questo non accade perche' il CHIAMANTE "
                "(wa_discover_run.py, il loop di scan) scarta le righe con titolo "
                "vuoto PRIMA di costruire la RigaScoperta -- ma salva_scoperta "
                "stessa non si difende: un futuro secondo chiamante (es. uno script "
                "di recupero mirato) che non replicasse quel filtro produrrebbe "
                "esattamente la riga 'orfana' che il docstring del modulo dice di "
                "voler escludere strutturalmente.")
        return True, "riga con identita' presente (il filtro a monte ha comunque tenuto)"


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    seme = await _semina_scenari(maker)

    esiti = {}
    esiti["I.45"] = await _caso45_nessuna_running_oltre_soglia(maker, settings)
    esiti["I.46"] = await _caso46_nessun_numero_in_chiaro(maker)
    esiti["I.47"] = await _caso47_nessuna_scrittura_invio_durante_scan(maker, seme)
    esiti["I.48"] = await _caso48_niente_duplicati_sulle_unique(maker, seme)
    esiti["I extra (salva_scoperta senza filtro a monte)"] = \
        await _caso_extra_salva_scoperta_senza_filtro_a_monte(maker)

    await eng.dispose()

    tutti_ok = True
    for nome, (ok, dettaglio) in esiti.items():
        print(f"\n=== {'PASS' if ok else 'FAIL'} -- {nome} ===\n{dettaglio}")
        tutti_ok = tutti_ok and ok

    if not tutti_ok:
        raise SystemExit(1)


asyncio.run(main())
