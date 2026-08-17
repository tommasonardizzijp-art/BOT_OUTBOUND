"""CRUD dei numeri WhatsApp. Scheletro creato in PR-0 e riempito da M2:
la registrazione in main.py avviene UNA volta sola, cosi' i due cantieri
paralleli non toccano mai lo stesso file (contratto §5).

Il punto delicato e' `riattiva` (contratto §2.2): M1 ha chiuso di proposito
la resurrezione automatica di un numero retired/suspended
(wa_session._persist_status), perche' quegli stati li mette un operatore o
la piattaforma e non sono deducibili da una lettura del DOM. Questo file e'
l'UNICO posto che puo' rimettere un numero simile in gioco, e lo fa solo
verso pending_qr -- mai verso active, che resta compito esclusivo di
wa_session.check_session (guarda il browser vero).
"""

from fastapi import APIRouter, Body, Depends, HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import get_db
from app.models.wa import WaNumber, WaNumberStatus
from app.services import wa_discover_gate, wa_discover_runs, wa_profile_lock, wa_session
from app.utils.crypto import decrypt, encrypt
from app.utils.phone_pseudonym import (PhoneNormalizationError, hmac_phone,
                                       mask_phone, normalize_e164)
from app.utils.tempo import adesso_utc
from app.workers.wa_discover_worker import enqueue_wa_discover

router = APIRouter(prefix="/wa/numbers", tags=["wa-numbers"])


def _motivo_pulito(exc: PhoneNormalizationError) -> str:
    """Stesso rischio, stesso fix di wa_ingest._motivo_pulito: il messaggio
    di PhoneNormalizationError contiene il numero in chiaro (forma
    "<diagnosi>: {raw!r}"), e str(exc) lo esporrebbe nel 422 -- trovato in
    review dedicata su questo endpoint."""
    testo = str(exc)
    return testo.split(":")[0].strip() if ":" in testo else type(exc).__name__

# Contratto §4.1: sent_today / sent_date / status NON sono qui, sono di M3 in
# scrittura runtime (tranne l'azzeramento fatto da `riattiva`). Un PATCH che
# li accettasse creerebbe due padroni per la stessa colonna.
# warmup_day invece E' overridabile a mano (decisione prodotto M5): la rampa
# avanza da sola ogni giorno (wa_number_manager.advance_wa_warmup_if_needed,
# chiamato al boot e dal cron) ma resta sovrascrivibile qui. I due
# convivono senza logica speciale -- un override oggi non impedisce ne'
# forza l'avanzamento automatico di domani, che segue il proprio corso
# indipendentemente (guarda warmup_advanced_date, non warmup_day).
#
# Conseguenza operativa da tenere presente, perche' non e' ovvia leggendo
# solo la frase sopra: un operatore che ABBASSA warmup_day per frenare dopo
# un warning viene rialzato dal prossimo avanzamento (boot o cron) del passo
# configurato. La frenata dura meno di un giorno e il rimbalzo va verso
# l'alto: la leva che regge nel tempo e' daily_cap, non questa.
CAMPI_MODIFICABILI = {"label", "proxy_url", "daily_cap", "notes", "warmup_day"}

# I due campi che compongono il tetto di invio (effective_wa_daily_cap fa
# `min(...)` fra loro): un valore non-intero qui non produce un errore al
# PATCH ma esplode DOPO, dentro il worker, con un TypeError sul confronto
# int/str -- e a quel punto il numero non manda piu' niente e la causa e'
# lontana dal punto in cui e' stata scritta. Trovato nel collaudo M5
# eseguendo `PATCH {"daily_cap": "molti"}`: 200 OK, e la funzione di cap
# sollevava "'<' not supported between instances of 'int' and 'str'".
# `daily_cap` era gia' modificabile prima di M5: la validazione mancava
# per entrambi, non solo per il campo nuovo.
_CAMPI_INTERI = {"warmup_day", "daily_cap"}

# Tetto di sanita' per daily_cap. La colonna e' un Integer, cioe' int4 su
# Postgres: un valore oltre i 2^31 passa la validazione Python ma esplode al
# commit con un DataError non catturato -> 500 invece di un 422 leggibile.
# Su SQLite passerebbe in silenzio, quindi la suite non puo' intercettarlo:
# il limite va messo qui a mano (collaudo M5, `warmup_day: 10**30` -> 500).
_MAX_DAILY_CAP = 100_000


def _max_warmup_day() -> int:
    """Oltre l'ultimo gradino configurato, warmup_day non ha piu' significato:
    get_wa_warmup_cap clampa comunque all'ultimo valore, e il numero esce
    dalla query di avanzamento (`warmup_day < len(steps)`) restando congelato
    al cap massimo PER SEMPRE. Accettare 999999 vuol dire quindi offrire
    all'operatore un "sblocca tutto e non gestirlo piu'" che nessuna schermata
    dichiara. Il limite naturale e' il numero di gradini configurati."""
    from app.services.wa_number_manager import _parse_wa_warmup_steps

    return len(_parse_wa_warmup_steps(settings.wa_warmup_steps)) or 1


def _valida_intero(nome: str, valore) -> None:
    # isinstance(x, bool) prima: bool e' sottoclasse di int in Python,
    # True/False passerebbero altrimenti isinstance(x, int) in silenzio.
    if isinstance(valore, bool) or not isinstance(valore, int) or valore < 0:
        raise HTTPException(422, f"{nome} deve essere un intero >= 0")
    massimo = _max_warmup_day() if nome == "warmup_day" else _MAX_DAILY_CAP
    if valore > massimo:
        # "quanti gradini sono configurati", non "il valore dell'ultimo
        # gradino": in un modulo il cui bug principale e' stato confondere
        # l'indice con i messaggi, la differenza va detta per esteso.
        dettaglio = (" (tanti quanti sono i gradini configurati in "
                     "WA_WARMUP_STEPS)" if nome == "warmup_day" else "")
        raise HTTPException(422, f"{nome} non puo' superare {massimo}{dettaglio}")

# Stati da cui la riattivazione e' ammessa (contratto §2.2): sono gli unici
# che un operatore mette a mano e che un operatore deve poter togliere a mano.
_STATI_RIATTIVABILI = (WaNumberStatus.retired, WaNumberStatus.suspended)

_PROFILO_OCCUPATO = ("il profilo di questo numero e' gia' aperto da un invio, "
                     "un health-check o una scansione risposte: riprova fra "
                     "qualche minuto")


def _serializza(n: WaNumber) -> dict:
    """Il numero torna SEMPRE mascherato (P12): nessun endpoint di questo
    router espone il numero in chiaro, nemmeno la lista admin.

    `warmup_cap` e `warmup_advanced_date` sono derivati, non colonne
    modificabili: servono a rendere leggibile la rampa. `warmup_day` da solo
    e' un indice senza significato per chi guarda la pagina -- "3" non dice
    quanti messaggi sono, e la colonna "Cap/giorno" accanto sembra decidere
    ma spesso non decide, perche' il tetto vero e' il minimo fra tre valori
    (wa_number_manager.effective_wa_daily_cap). `warmup_advanced_date` dice
    se e quando la rampa e' salita l'ultima volta: senza, dalla UI e'
    impossibile capire PERCHE' un numero non sta avanzando.
    """
    from app.services.wa_number_manager import get_wa_warmup_cap

    return {
        "id": n.id,
        "tenant_id": n.tenant_id,
        "label": n.label,
        "numero": mask_phone(decrypt(n.encrypted_phone)),
        "status": n.status.value,
        "proxy_url": n.proxy_url,
        "daily_cap": n.daily_cap,
        "warmup_day": n.warmup_day,
        # None quando la rampa e' fuori gioco (warmup_day <= 0): in quel caso
        # NON c'e' un tetto di warmup, e mostrare un numero suggerirebbe il
        # contrario.
        "warmup_cap": get_wa_warmup_cap(n.warmup_day) if (n.warmup_day or 0) > 0 else None,
        "warmup_advanced_date": n.warmup_advanced_date,
        "sent_today": n.sent_today,
        "sent_date": n.sent_date,
        "session_checked_at": n.session_checked_at.isoformat() if n.session_checked_at else None,
        "notes": n.notes,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


async def _numero_o_404(db, number_id: str) -> WaNumber:
    numero = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
    if numero is None:
        raise HTTPException(404, "numero inesistente")
    return numero


@router.get("")
async def lista(tenant_id: str | None = None, db=Depends(get_db)) -> dict:
    query = select(WaNumber).order_by(WaNumber.created_at.desc())
    if tenant_id:
        query = query.where(WaNumber.tenant_id == tenant_id)
    righe = (await db.execute(query)).scalars().all()
    return {"numeri": [_serializza(n) for n in righe]}


@router.post("")
async def crea(dati: dict, db=Depends(get_db)) -> dict:
    """Body come un dict semplice (stesso stile di `aggiorna`), non campi
    scalari con default `Body(None)`: un default `Body(...)` e' un oggetto
    sentinella di FastAPI, risolto solo quando la richiesta passa dal layer
    HTTP. Chiamando la funzione a mano (test, script) senza passare quella
    chiave, quel sentinella finirebbe scritto a DB cosi' com'e' -- bug reale
    trovato scrivendo un test diretto per questo endpoint."""
    tenant_id = (dati.get("tenant_id") or "").strip()
    label = (dati.get("label") or "").strip()
    numero_raw = dati.get("numero") or ""
    if not tenant_id or not label or not numero_raw:
        raise HTTPException(422, "tenant_id, label e numero sono obbligatori")

    try:
        e164 = normalize_e164(numero_raw)
    except PhoneNormalizationError as exc:
        # MAI str(exc): contiene il numero in chiaro (contratto §2.3).
        raise HTTPException(422, _motivo_pulito(exc))

    esiste = await db.scalar(select(WaNumber).where(WaNumber.phone_hmac == hmac_phone(e164)))
    if esiste is not None:
        raise HTTPException(409, "esiste gia' un numero WA con questo numero")

    # Stessa validazione del PATCH, e qui conta di PIU': la pagina Numeri non
    # ha una form di creazione (waApi.numeri.create non e' chiamato da nessuna
    # UI), quindi un numero nasce solo da qui -- via API o script. Un
    # daily_cap sporco scritto alla nascita non fallisce adesso: fallisce piu'
    # tardi dentro il worker, in effective_wa_daily_cap, e il numero smette di
    # mandare senza che l'errore indichi da dove viene. Il collaudo M5 aveva
    # fuzzato solo il PATCH: questa meta' era rimasta scoperta.
    daily_cap = dati.get("daily_cap")
    if daily_cap is not None:
        _valida_intero("daily_cap", daily_cap)
    n = WaNumber(
        tenant_id=tenant_id, label=label,
        phone_hmac=hmac_phone(e164), encrypted_phone=encrypt(e164),
        proxy_url=dati.get("proxy_url"),
        daily_cap=daily_cap if daily_cap is not None else settings.wa_daily_cap_default,
    )
    db.add(n)
    await db.commit()
    await db.refresh(n)
    return _serializza(n)


@router.get("/{number_id}")
async def dettaglio(number_id: str, db=Depends(get_db)) -> dict:
    numero = await _numero_o_404(db, number_id)
    return _serializza(numero)


@router.patch("/{number_id}")
async def aggiorna(number_id: str, campi: dict, db=Depends(get_db)) -> dict:
    """Solo CAMPI_MODIFICABILI vengono applicati: qualunque altra chiave nel
    body (sent_today, sent_date, status, ...) e' ignorata in silenzio, non e'
    un errore -- e' cosi' che un client che manda il record intero non
    riesce comunque a scavalcare M3 (contratto §4.1). warmup_day E' fra i
    campi modificabili (override manuale M5).

    I campi che compongono il cap di invio (warmup_day, daily_cap) sono
    validati PRIMA di essere applicati: un valore sporco li' dentro non
    fallisce qui, fallisce dopo dentro il worker
    (wa_number_manager.effective_wa_daily_cap) con un TypeError lontano
    dalla causa, e nel frattempo il numero smette di mandare."""
    numero = await _numero_o_404(db, number_id)
    for chiave in _CAMPI_INTERI & campi.keys():
        _valida_intero(chiave, campi[chiave])
    for chiave in CAMPI_MODIFICABILI & campi.keys():
        setattr(numero, chiave, campi[chiave])
    await db.commit()
    await db.refresh(numero)
    return _serializza(numero)


@router.delete("/{number_id}")
async def elimina(number_id: str, db=Depends(get_db)) -> dict:
    numero = await _numero_o_404(db, number_id)
    await db.delete(numero)
    await db.commit()
    return {"eliminato": True}


@router.post("/{number_id}/login")
async def login(number_id: str, db=Depends(get_db)) -> dict:
    """Apre un browser VISIBILE (wa_session.assisted_login, headless=False):
    va lanciato solo quando qualcuno e' davanti allo schermo per inquadrare
    il QR. Solleva 404 prima di aprire nulla se il numero non esiste.

    Sotto lucchetto profilo come ogni altro consumatore (invio, health-check,
    reply-scan): senza, bastava un click su "ri-associa" mentre l'health-check
    teneva il profilo per avere due Chromium sullo stesso profilo. Il TTL e'
    quello di default, che copre i 180s di polling del QR con margine."""
    await _numero_o_404(db, number_id)
    try:
        async with wa_profile_lock.held(number_id):
            stato = await wa_session.assisted_login(number_id)
    except wa_profile_lock.WaProfileBusy:
        raise HTTPException(409, _PROFILO_OCCUPATO)
    except wa_session.WaBrowserLoopUnsupported as exc:
        raise HTTPException(503, str(exc))
    return {"status": stato.value}


@router.post("/{number_id}/check")
async def check(number_id: str, db=Depends(get_db)) -> dict:
    """Health-check headless (wa_session.check_session): legge il DOM vero,
    non deduce nulla da uno stato in memoria di un run precedente. Sotto
    lucchetto profilo per lo stesso motivo di `login`."""
    await _numero_o_404(db, number_id)
    try:
        async with wa_profile_lock.held(number_id):
            stato = await wa_session.check_session(number_id)
    except wa_profile_lock.WaProfileBusy:
        raise HTTPException(409, _PROFILO_OCCUPATO)
    # 503 e non 500: la richiesta e' legittima, e' il processo che non puo'
    # servirla finche' gira con l'event loop sbagliato. Il messaggio dice
    # cosa fare -- il 500 generico del middleware non diceva nemmeno cosa
    # fosse andato storto (vedi WaBrowserLoopUnsupported).
    except wa_session.WaBrowserLoopUnsupported as exc:
        raise HTTPException(503, str(exc))
    return {"status": stato.value}


@router.post("/{number_id}/riattiva")
async def riattiva(number_id: str, motivo: str = Body(..., embed=True),
                    db=Depends(get_db)) -> dict:
    """retired|suspended -> pending_qr (contratto §2.2).

    Mai -> active: la sessione potrebbe non esserci piu', e chi lo dice e'
    il browser (wa_session.check_session), non questo endpoint.
    """
    numero = await _numero_o_404(db, number_id)
    if numero.status not in _STATI_RIATTIVABILI:
        raise HTTPException(
            409, f"il numero e' in stato {numero.status.value}: la riattivazione "
                 "esiste solo per 'retired' e 'suspended'")
    if not (motivo or "").strip():
        raise HTTPException(422, "il motivo e' obbligatorio: uno stato messo a mano "
                                  "si toglie a mano, lasciando traccia")

    numero.status = WaNumberStatus.pending_qr
    numero.sent_today = 0
    numero.sent_date = None
    numero.warmup_day = 1        # riparte dalla rampa, non dal cap raggiunto
    stamp = adesso_utc().strftime("%Y-%m-%d %H:%M")
    nota = f"[{stamp}] riattivato: {motivo.strip()}"
    numero.notes = f"{(numero.notes or '').rstrip()}\n{nota}".strip()
    await db.commit()
    logger.warning(f"[WA] numero {number_id[:8]} riattivato -> pending_qr: {motivo.strip()}")
    return {"status": numero.status.value,
            "prossimo_passo": "avvia il login QR, poi verifica la sessione"}


def _serializza_run(run) -> dict:
    return {
        "id": run.id,
        "stato": run.stato,
        "avviato_da": run.avviato_da,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "salvate": run.salvate,
        "aggiornate": run.aggiornate,
        "saltate_gia_note": run.saltate_gia_note,
        "non_verificate": run.non_verificate,
        "dichiarato": run.dichiarato,
        "copertura": run.copertura,
        "motivo": run.motivo,
        "sync_stato": run.sync_stato,
        "errore": run.errore,
    }


@router.post("/{number_id}/discover")
async def avvia_discover(number_id: str, db=Depends(get_db)) -> dict:
    """Lancia una scansione auto-discover sul numero.

    Rifiuta invece di accodare: nessuno stato differito, nessun browser che si
    apre da solo mezz'ora dopo quando nessuno guarda. Il codice del rifiuto va
    in `detail` insieme alla frase da mostrare -- "Errore 409" non dice a
    nessuno cosa fare dopo.
    """
    numero = await _numero_o_404(db, number_id)

    rifiuto = await wa_discover_gate.puo_lanciare(db, numero)
    if rifiuto is not None:
        raise HTTPException(409, {"codice": rifiuto,
                                  "messaggio": wa_discover_gate.MESSAGGI[rifiuto]})

    try:
        run = await wa_discover_runs.apri_run(db, tenant_id=numero.tenant_id,
                                              number_id=number_id)
        await db.commit()
    except IntegrityError:
        # Due POST quasi simultanei sullo stesso numero: il gate ha visto
        # 'nessuna run attiva' per entrambi (finestra fra la lettura del
        # gate e l'INSERT), e l'indice unico parziale del Task 1 fa vincere
        # UNA sola apri_run -- corretto, nessuna riga doppia. Ma senza
        # questo except la seconda solleva IntegrityError DENTRO
        # l'endpoint e risulterebbe un 500 generico invece del 409
        # scan_gia_in_corso che il gate stesso avrebbe dato con un
        # millisecondo di ritardo. Il rollback e' necessario: senza,
        # la sessione resta sporca e un PendingRollbackError risalirebbe
        # al posto nostro alla prossima query.
        await db.rollback()
        raise HTTPException(409, {"codice": "scan_gia_in_corso",
                                  "messaggio": wa_discover_gate.MESSAGGI["scan_gia_in_corso"]})

    try:
        accodato = await enqueue_wa_discover(number_id, run.id)
        errore_accodamento = "accodamento ARQ rifiutato"
    except Exception as exc:  # noqa: BLE001 -- vedi sotto
        # enqueue_job di ARQ torna None (quindi enqueue_wa_discover torna
        # False) SOLO se il _job_id collide -- il nostro ha un UUID fresco
        # a ogni chiamata, non puo' mai succedere: quel ramo e' quasi morto.
        # Lo scenario vero (Redis giu') fa SOLLEVARE arq.create_pool, non
        # tornare un sentinella: senza questo except prendeva la strada non
        # gestita (500, run appesa 'running' per sempre).
        logger.error(f"[WaDiscover] {number_id}: enqueue_wa_discover ha "
                     f"sollevato invece di tornare False ({type(exc).__name__}: {exc})")
        accodato = False
        errore_accodamento = f"{type(exc).__name__}: {exc}"

    if not accodato:
        # ARQ ha scartato l'accodamento (torna False) o ha sollevato
        # (gestito sopra): in entrambi i casi la run non verra' mai chiusa
        # da nessuno, e l'indice unico parziale renderebbe il numero non
        # piu' scansionabile. Si chiude subito.
        await wa_discover_runs.chiudi_run(db, run.id, {}, errore=errore_accodamento)
        await db.commit()
        raise HTTPException(409, {
            "codice": "accodamento_fallito",
            "messaggio": ("La coda dei job ha rifiutato la scansione. "
                          "Verifica che il worker ARQ sia in esecuzione."),
        })

    return {"run_id": run.id, "queued": True}


@router.get("/{number_id}/discover")
async def stato_discover(number_id: str, db=Depends(get_db)) -> dict:
    await _numero_o_404(db, number_id)
    ultima = await wa_discover_runs.ultima_run(db, number_id)
    righe = await wa_discover_runs.storico(
        db, number_id, limit=settings.wa_discover_storico_limit)
    return {
        "ultima": _serializza_run(ultima) if ultima else None,
        "storico": [_serializza_run(r) for r in righe],
        "in_corso": ultima is not None and ultima.stato == "running",
    }
