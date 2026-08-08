"""Pre-volo del canale WhatsApp: verifica in un colpo solo che l'ambiente sia
davvero pronto, invece di scoprirlo a meta' collaudo.

    cd backend
    python -m scripts.wa_preflight

Esce 0 se e' tutto a posto, 1 se c'e' qualcosa da sistemare -- cosi' si puo'
incatenare a un avvio o a un cron senza leggerlo a occhio.

**SOLA LETTURA.** Non scrive niente, ne' a DB ne' a Redis: si puo' lanciare
contro la produzione mentre il canale sta girando.

Nasce come prerequisito P3 del collaudo dal vivo, ma resta utile dopo: e' la
lista delle cose che, se sbagliate, fanno fallire gli invii in modo silenzioso.
Le due voci meno ovvie sono le ultime:

- **quarantena vs cap di sessione**: se `WA_RESYNC_QUARANTINE_MIN` superasse il
  cap wall-clock di una mini-sessione, nessuna sessione riuscirebbe mai a
  inviare -- aspetterebbe piu' a lungo di quanto le e' concesso vivere.
- **rampa di volume**: `WA_WARMUP_STEPS` malformata o passo <= 0 congelano la
  rampa senza dirlo. Sono gia' costati un bug in M5 (la rampa saltava da 20 a
  100 al primo riavvio).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath("."))

OK, KO, WARN = "  OK  ", " MANCA", " ATTEN"


def revision_attesa() -> str:
    """La head di Alembic, letta dalle migrazioni sul disco.

    Prima era la stringa "028" scritta a mano, e la 029 (`9be195d`) l'ha resa
    falsa senza che nulla lo segnalasse: il pre-volo bocciava un database
    corretto, uscendo con codice 1 proprio al go-live. Una costante a mano
    qui e' garantita diventare sbagliata alla prossima migrazione — l'unica
    versione che non va in deriva e' quella che va a leggere.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()


def riga(esito: str, titolo: str, dettaglio: str = "") -> None:
    print(f"[{esito}] {titolo}" + (f" — {dettaglio}" if dettaglio else ""))


async def main() -> int:
    from sqlalchemy import func, select, text

    from app.config import settings
    from app.database import AsyncSessionLocal
    from app.models.wa import (WaCampaign, WaCampaignStatus, WaContact, WaNumber,
                               WaNumberStatus)
    from app.services import bot_state_service, wa_number_manager
    from app.services.wa_session import profile_dir_for

    problemi = 0
    print("=" * 72)
    print("PRE-VOLO CANALE WHATSAPP")
    print("=" * 72)

    async with AsyncSessionLocal() as db:
        # --- 1. migrazione -------------------------------------------------
        # Prima query del giro: se il DB non e' quello giusto (o non e' mai
        # stato migrato) si scopre qui, e un traceback di SQLAlchemy non dice a
        # chi lancia lo script QUALE delle due cose e' successa. Il caso tipico
        # e' lanciarlo da un worktree, che ha un .env con uno sqlite di test.
        try:
            rev = (await db.execute(
                text("SELECT version_num FROM alembic_version"))).scalar()
        except Exception as exc:
            riga(KO, "connessione al database",
                 f"{type(exc).__name__}. Il pre-volo va lanciato dalla directory "
                 "da cui gira il backend (backend/), non da un worktree: li' il "
                 ".env punta a uno sqlite di test senza le tabelle wa_*.")
            print("=" * 72)
            print("PRE-VOLO NON SUPERATO: database non raggiungibile o non migrato.")
            print("=" * 72)
            return 1
        attesa = revision_attesa()
        if rev == attesa:
            riga(OK, "migrazione", f"revision {rev}")
        else:
            riga(KO, "migrazione", f"revision {rev}, attesa {attesa}")
            problemi += 1

        colonna = (await db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='wa_numbers' AND column_name='warmup_advanced_date'"
        ))).scalar()
        if colonna:
            riga(OK, "colonna wa_numbers.warmup_advanced_date", "presente")
        else:
            riga(KO, "colonna wa_numbers.warmup_advanced_date",
                 "ASSENTE: uvicorn non partira' (il lifespan la legge)")
            problemi += 1

        # --- 2. master switch e kill-switch ---------------------------------
        if settings.wa_send_enabled:
            riga(WARN, "WA_SEND_ENABLED", "TRUE — il canale puo' inviare")
        else:
            riga(OK, "WA_SEND_ENABLED", "false (cancello 0 chiuso, si accende a mano)")

        halted = await bot_state_service.is_wa_halted(db)
        riga(KO if halted else OK, "kill-switch di canale",
             "wa_halted=True, nessun invio partira'" if halted else "wa_halted=False")
        if halted:
            problemi += 1

        # --- 3. numeri ------------------------------------------------------
        numeri = (await db.execute(select(WaNumber))).scalars().all()
        if not numeri:
            riga(KO, "numeri registrati", "nessuno: va creato e onboardato (P4)")
            problemi += 1
        for n in numeri:
            cap_warmup = (wa_number_manager.get_wa_warmup_cap(n.warmup_day)
                          if (n.warmup_day or 0) > 0 else None)
            inviati = wa_number_manager.wa_sent_today(n)
            riga(OK if n.status == WaNumberStatus.active else WARN,
                 f"numero {n.label!r}",
                 f"stato={n.status.value} warmup_day={n.warmup_day} "
                 f"cap_warmup={cap_warmup} daily_cap={n.daily_cap} "
                 f"inviati_oggi={inviati} avanzato_il={n.warmup_advanced_date}")

            profilo = profile_dir_for(n.id)
            esiste = profilo.exists() and any(profilo.iterdir())
            riga(OK if esiste else KO, f"  profilo browser di {n.label!r}",
                 str(profilo) + ("" if esiste else "  VUOTO O ASSENTE: serve il QR"))
            if not esiste:
                problemi += 1

            if not n.proxy_url:
                riga(WARN, f"  proxy di {n.label!r}",
                     "assente: il traffico esce dall'IP locale (rischio T3)")

        # --- 4. cap globale --------------------------------------------------
        oggi = wa_number_manager._utc_today_str()
        globale = await db.scalar(
            select(func.coalesce(func.sum(WaNumber.sent_today), 0))
            .where(WaNumber.sent_date == oggi)) or 0
        riga(OK if int(globale) < settings.wa_global_daily_cap else KO,
             "cap globale di macchina",
             f"{globale}/{settings.wa_global_daily_cap} inviati oggi")

        # --- 5. campagne ------------------------------------------------------
        running = (await db.execute(
            select(WaCampaign).where(WaCampaign.status == WaCampaignStatus.running)
        )).scalars().all()
        riga(OK, "campagne in corso", f"{len(running)}")
        for c in running:
            riga(OK, f"  campagna {c.name!r}",
                 f"contatti={c.total_contacts} inviati={c.sent} "
                 f"risposte={c.replied} optout={c.opted_out} falliti={c.failed}")

        optati = await db.scalar(
            select(func.count(WaContact.id)).where(WaContact.opted_out.is_(True))) or 0
        riga(OK, "contatti in opt-out", f"{optati} (non riceveranno nulla)")

    # --- 6. Redis -----------------------------------------------------------
    try:
        import arq

        from app.services.work_enqueue import arq_redis_settings
        redis = await arq.create_pool(arq_redis_settings())
        try:
            await redis.ping()
            riga(OK, "Redis", "raggiungibile")
        finally:
            await redis.aclose()
    except Exception as exc:
        riga(KO, "Redis", f"IRRAGGIUNGIBILE ({type(exc).__name__}): "
                          "niente code, niente lucchetti profilo, niente cooldown")
        problemi += 1

    # --- 7. coerenza della configurazione di timing --------------------------
    from app.workers import wa_worker

    quarantena_s = settings.wa_resync_quarantine_min * 60
    limite_s = wa_worker._limite_sessione_s()
    if quarantena_s >= limite_s:
        riga(KO, "quarantena vs cap di sessione",
             f"{settings.wa_resync_quarantine_min} min >= {round(limite_s/60)} min: "
             "nessuna sessione potrebbe inviare")
        problemi += 1
    else:
        riga(OK, "quarantena vs cap di sessione",
             f"attesa {settings.wa_resync_quarantine_min} min, "
             f"cap sessione {round(limite_s/60)} min")

    gradini = wa_number_manager._parse_wa_warmup_steps(settings.wa_warmup_steps)
    passo = settings.wa_warmup_advance_steps_per_day
    riga(OK if gradini and passo > 0 else KO, "rampa di volume",
         f"gradini={gradini} passo={passo}/giorno")
    if not gradini or passo <= 0:
        problemi += 1

    print("=" * 72)
    if problemi:
        print(f"PRE-VOLO NON SUPERATO: {problemi} punti da sistemare.")
    else:
        print("PRE-VOLO SUPERATO: l'ambiente e' pronto.")
    print("=" * 72)
    return 1 if problemi else 0


if __name__ == "__main__":
    # La riga era nuda: importare il modulo lo ESEGUIVA e lo faceva uscire dal
    # processo. Senza questa guardia nessun test puo' nemmeno importarlo.
    sys.exit(asyncio.run(main()))
