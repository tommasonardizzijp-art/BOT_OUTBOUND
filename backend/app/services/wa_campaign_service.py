"""Regole di campagna del canale WhatsApp.

Sta in un servizio e non nell'endpoint perche' queste regole valgono anche
per lo script di seed e per i test: una regola che vive dentro un handler
HTTP e' una regola che il resto del sistema puo' aggirare.
"""
from datetime import datetime

from sqlalchemy import exists, func, select, update

from app.models.wa import (WaCampaign, WaCampaignStatus, WaCampaignType, WaNumber,
                           WaNumberStatus, WaSendCondition, WaSequenceStep)
from app.services.wa_template import validate_wa_template


def calcola_optout_enabled(tipo: WaCampaignType) -> bool:
    """V10: marketing -> CTA "scrivi STOP" obbligatoria; follow-up -> no.
    Il server_default=true della migrazione 025 e' la rete di sicurezza; la
    regola vera e' condizionale, e quindi va scritta qui (contratto §2.1)."""
    return tipo == WaCampaignType.marketing


def valida_step(template: str, *, colonne_note: set[str]) -> None:
    """Un template con placeholder che il CSV non copre non si salva: se
    passasse, fallirebbe a tempo di invio, un contatto alla volta, in una
    campagna gia' partita."""
    if not (template or "").strip():
        raise ValueError("Il testo del messaggio non puo' essere vuoto.")
    ignoti = validate_wa_template(template, known_attributes=colonne_note)
    if ignoti:
        raise ValueError(
            "Placeholder non disponibili nella lista contatti: "
            + ", ".join(f"{{{x}}}" for x in ignoti)
            + ". Aggiungi la colonna al CSV oppure togli il segnaposto."
        )


async def crea_campagna(db, dati: dict) -> WaCampaign:
    """Crea la campagna in draft + il suo step 0.

    optout_enabled e' SEMPRE calcolato da calcola_optout_enabled(tipo), mai
    accettato dal chiamante alla creazione (contratto §2.1): il valore
    iniziale e' calcolato, l'override manuale e' un atto successivo
    dell'admin via PATCH, non un parametro di POST. Bug trovato in review
    dedicata: un `optout_enabled` nel payload di creazione bypassava sia il
    calcolo sia il gate CTA sotto, permettendo una campagna marketing senza
    via d'uscita.
    """
    tipo = dati["campaign_type"]
    numero = await db.scalar(select(WaNumber).where(WaNumber.id == dati["wa_number_id"]))
    if numero is None:
        raise ValueError("Numero inesistente.")
    if numero.tenant_id != dati["tenant_id"]:
        raise ValueError("Il numero appartiene a un altro tenant.")

    optout = calcola_optout_enabled(tipo)      # mai dal payload, mai il default a DB
    cta = (dati.get("optout_cta") or "").strip() or None
    if optout and not cta:
        raise ValueError(
            "Una campagna con opt-out attivo deve avere una CTA: non si manda "
            "marketing senza via d'uscita."
        )

    valida_step(dati["template_a"], colonne_note=set(dati.get("colonne_note") or []))

    campagna = WaCampaign(
        tenant_id=dati["tenant_id"], wa_number_id=numero.id, name=dati["name"],
        campaign_type=tipo, status=WaCampaignStatus.draft,
        optout_enabled=bool(optout), optout_cta=cta,
        daily_limit=dati.get("daily_limit"),
        active_hours_start=dati.get("active_hours_start"),
        active_hours_end=dati.get("active_hours_end"),
        created_at=datetime.utcnow(),
    )
    db.add(campagna)
    await db.flush()
    db.add(WaSequenceStep(
        campaign_id=campagna.id, step_index=0, template_a=dati["template_a"],
        template_b=dati.get("template_b"), template_c=dati.get("template_c"),
        template_d=dati.get("template_d"),
        # MVP: un solo step, condizione fissa. Lo SCHEMA e' completo, il
        # motore multi-step si accende post-MVP senza migrazione (SDD Q29).
        send_condition=WaSendCondition.always, wait_days=0,
    ))
    await db.commit()
    await db.refresh(campagna)
    return campagna


async def _ristampa_next_action(db, campaign_id: str, quando: datetime) -> int:
    """Re-pacing (contratto 7.2, SDD Q31): tutte le righe ancora attive
    prendono un appuntamento nuovo. NON tocca le righe terminali, che hanno
    next_action_at NULL per una ragione: uno UPDATE senza il filtro sullo
    status le risveglierebbe silenziosamente."""
    from app.models.wa import WaCampaignContact, WaContactStatus

    res = await db.execute(
        update(WaCampaignContact)
        .where(WaCampaignContact.campaign_id == campaign_id,
               WaCampaignContact.status.in_([WaContactStatus.queued,
                                             WaContactStatus.in_sequence]))
        .values(next_action_at=quando)
    )
    return res.rowcount or 0


async def avvia(db, campaign_id: str) -> WaCampaign:
    """Avvia (start) o riprende (resume, via riprendi()) una campagna.

    Validazioni SDD 8.1, tutte a livello di servizio (non nell'endpoint,
    stessa ragione di calcola_optout_enabled sopra): numero attivo, almeno
    uno step, almeno un contatto, e nessun'altra campagna 'running' sullo
    stesso numero (decisione 23/07, Q2 -- il pacing e' per-job, due
    campagne sullo stesso numero raddoppierebbero il ritmo)."""
    from app.models.wa import WaCampaignContact

    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None:
        raise ValueError("Campagna inesistente.")
    if campagna.status not in (WaCampaignStatus.draft, WaCampaignStatus.paused):
        raise ValueError(f"La campagna e' gia' in stato {campagna.status.value}.")

    numero = await db.scalar(select(WaNumber).where(WaNumber.id == campagna.wa_number_id))
    if numero is None or numero.status != WaNumberStatus.active:
        # Il messaggio distingue il cooldown dagli altri stati non-attivi.
        # Non e' pignoleria: il caso in cui questo errore si legge piu' spesso
        # e' subito dopo un FM2, che mette insieme la campagna in 'error' E il
        # numero in 'cooldown' per quattro ore. Un messaggio generico che dice
        # "serve un nuovo QR" manda l'operatore a riscansionare un QR che non
        # serve, mentre la sessione e' perfettamente viva e basta aspettare
        # (review 07/08 su M5.1).
        if numero is not None and numero.status == WaNumberStatus.cooldown:
            raise ValueError(
                "Il numero e' in cooldown dopo un guasto: la sessione WhatsApp "
                "e' viva, non serve rifare il QR. Aspetta che scada il timer "
                "(4 ore da un blocco per guasti consecutivi) oppure togli a "
                "mano la chiave Redis wa:cooldown:<id> se hai gia' verificato "
                "e risolto la causa.")
        raise ValueError(
            "Il numero non e' attivo: serve una sessione WhatsApp valida (QR) "
            "prima di far partire la campagna.")

    # Max 1 campagna running per numero (Q2, 23/07): con due, il pacing
    # per-job produrrebbe ritmo doppio sullo stesso numero. Il controllo qui
    # sotto (SELECT) e' solo per dare un messaggio d'errore chiaro nel caso
    # comune non concorrente: la garanzia VERA e' nell'UPDATE piu' in basso,
    # che ripete la stessa condizione dentro la transazione. Trovato in
    # review dedicata: due avvia() concorrenti su due campagne dello stesso
    # numero passavano ENTRAMBE con questa sola SELECT (TOCTOU riprodotto
    # con un test di concorrenza vero, non sequenziale).
    altra = await db.scalar(
        select(WaCampaign.id).where(WaCampaign.wa_number_id == numero.id,
                                    WaCampaign.status == WaCampaignStatus.running,
                                    WaCampaign.id != campagna.id))
    if altra:
        raise ValueError("Questo numero ha gia' una campagna in corso: mettila in "
                         "pausa prima di avviarne un'altra.")

    if not await db.scalar(select(func.count(WaSequenceStep.id))
                           .where(WaSequenceStep.campaign_id == campaign_id)):
        raise ValueError("La campagna non ha nessun messaggio.")
    if not await db.scalar(select(func.count(WaCampaignContact.id))
                           .where(WaCampaignContact.campaign_id == campaign_id)):
        raise ValueError("La campagna non ha contatti: carica prima la lista.")

    adesso = datetime.utcnow()
    # UPDATE atomico: la condizione "nessun'altra running sullo stesso
    # numero" e' RIVALUTATA dentro la stessa istruzione che scrive
    # status=running, sotto il lock di riga che l'UPDATE prende. Se due
    # richieste corrono insieme, la seconda a committare vede gia' l'altra
    # running (o viene serializzata dal motore) e rowcount torna 0 -- niente
    # finestra fra "ho controllato" e "ho scritto".
    res = await db.execute(
        update(WaCampaign)
        .where(WaCampaign.id == campaign_id,
               WaCampaign.status.in_([WaCampaignStatus.draft, WaCampaignStatus.paused]),
               ~exists(select(1).where(
                   WaCampaign.wa_number_id == numero.id,
                   WaCampaign.status == WaCampaignStatus.running,
                   WaCampaign.id != campagna.id)))
        .values(status=WaCampaignStatus.running,
                started_at=func.coalesce(WaCampaign.started_at, adesso))
    )
    if res.rowcount == 0:
        await db.rollback()
        raise ValueError("Questo numero ha gia' una campagna in corso: mettila in "
                         "pausa prima di avviarne un'altra.")
    await _ristampa_next_action(db, campaign_id, adesso)
    await db.commit()
    await db.refresh(campagna)
    await _accoda_worker(campaign_id)
    return campagna


async def _accoda_worker(campaign_id: str) -> None:
    """Mette in coda il job di invio del numero della campagna.

    Sta QUI e non nell'endpoint per la stessa ragione delle validazioni sopra
    (docstring del modulo): una regola che vive dentro un handler HTTP e' una
    regola che il resto del sistema puo' aggirare. E infatti la aggirava --
    fino a M5.1 `enqueue_wa_workers` aveva due soli chiamanti, entrambi in
    wa_ops.py (`kick` e `ops/resume`), e nessuno dei due era il verbo che usa
    la UI. Una campagna avviata dalla pagina restava 'running' senza nessun
    worker, in silenzio, per sempre: nessun invio, nessun errore, e lo stato a
    schermo diceva "In corso" (review 07/08, blocco B2).

    Un errore NON annulla l'avvio: lo stato e' gia' committato, e il cron
    wa_campaign_supervisor riaccoda le campagne running rimaste senza job.
    Sollevare qui lascerebbe la campagna avviata a DB e un'eccezione all'utente
    -- il peggio dei due mondi.

    Import locale: wa_worker apre un pool Redis e questo servizio e' importato
    anche da script che non hanno ARQ sotto.
    """
    from loguru import logger

    from app.utils import events
    from app.workers.wa_worker import enqueue_wa_workers

    try:
        n = await enqueue_wa_workers(campaign_id)
    except Exception as exc:
        logger.error(f"[WA] campagna {campaign_id}: avviata, ma accodamento del "
                     f"worker fallito ({type(exc).__name__}) -- il supervisore "
                     "riprovera'")
        events.emit(campaign_id, "wa.campaign.enqueue_failed",
                    f"worker non accodato: {type(exc).__name__}", level="error")
        return
    events.emit(campaign_id, "wa.campaign.started", f"worker accodati: {n}")


async def pausa(db, campaign_id: str) -> WaCampaign:
    """running -> paused. Non tocca next_action_at: la ri-stampa avviene
    solo al resume (riprendi), non qui -- mettere in pausa non deve alterare
    le righe, solo fermare il consumo (contratto 7.2)."""
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None or campagna.status != WaCampaignStatus.running:
        raise ValueError("Si mette in pausa solo una campagna in corso.")
    campagna.status = WaCampaignStatus.paused
    await db.commit()
    # NB: i job gia' accodati di M3 vedranno lo stato al prossimo controllo
    # (la mini-sessione ricontrolla a ogni messaggio, non solo all'avvio).
    await db.refresh(campagna)
    return campagna


async def riprendi(db, campaign_id: str) -> WaCampaign:
    """paused -> running, passando dalle stesse validazioni di avvia() (il
    numero potrebbe essere stato sospeso durante la pausa, o un'altra
    campagna potrebbe essere partita sullo stesso numero nel frattempo) e
    dalla ri-stampa di next_action_at su tutte le righe non terminali."""
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None or campagna.status != WaCampaignStatus.paused:
        raise ValueError("Si riprende solo una campagna in pausa.")
    return await avvia(db, campaign_id)


async def ferma(db, campaign_id: str) -> WaCampaign:
    """Stop definitivo. Non cancella niente: i contatti e i KPI restano, e
    la campagna diventa storico.

    Confine con M3 (contratto 4.1): running -> completed e running -> error
    sono SOLO di M3. Questo servizio scrive solo lo stato 'stopped', mai
    'completed' ne' 'error'.
    """
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None:
        raise ValueError("Campagna inesistente.")
    if campagna.status in (WaCampaignStatus.completed, WaCampaignStatus.stopped):
        raise ValueError(f"La campagna e' gia' {campagna.status.value}.")
    campagna.status = WaCampaignStatus.stopped
    # completed_at NON si tocca qui: contratto §4.1, lo scrive solo M3
    # "insieme a completed" -- una riga 'stopped' con completed_at
    # valorizzato sarebbe fuorviante per chi legge dopo (KPI, M3).
    await db.commit()
    await db.refresh(campagna)
    return campagna
