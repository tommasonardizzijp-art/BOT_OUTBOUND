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
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models.wa import WaNumber, WaNumberStatus
from app.services import wa_profile_lock, wa_session
from app.utils.crypto import decrypt, encrypt
from app.utils.phone_pseudonym import (PhoneNormalizationError, hmac_phone,
                                       mask_phone, normalize_e164)

router = APIRouter(prefix="/wa/numbers", tags=["wa-numbers"])


def _motivo_pulito(exc: PhoneNormalizationError) -> str:
    """Stesso rischio, stesso fix di wa_ingest._motivo_pulito: il messaggio
    di PhoneNormalizationError contiene il numero in chiaro (forma
    "<diagnosi>: {raw!r}"), e str(exc) lo esporrebbe nel 422 -- trovato in
    review dedicata su questo endpoint."""
    testo = str(exc)
    return testo.split(":")[0].strip() if ":" in testo else type(exc).__name__

# Contratto §4.1: sent_today / sent_date / warmup_day / status NON sono qui,
# sono di M3 in scrittura runtime (tranne l'azzeramento fatto da `riattiva`).
# Un PATCH che li accettasse creerebbe due padroni per la stessa colonna.
CAMPI_MODIFICABILI = {"label", "proxy_url", "daily_cap", "notes"}

# Stati da cui la riattivazione e' ammessa (contratto §2.2): sono gli unici
# che un operatore mette a mano e che un operatore deve poter togliere a mano.
_STATI_RIATTIVABILI = (WaNumberStatus.retired, WaNumberStatus.suspended)

_PROFILO_OCCUPATO = ("il profilo di questo numero e' gia' aperto da un invio, "
                     "un health-check o una scansione risposte: riprova fra "
                     "qualche minuto")


def _serializza(n: WaNumber) -> dict:
    """Il numero torna SEMPRE mascherato (P12): nessun endpoint di questo
    router espone il numero in chiaro, nemmeno la lista admin."""
    return {
        "id": n.id,
        "tenant_id": n.tenant_id,
        "label": n.label,
        "numero": mask_phone(decrypt(n.encrypted_phone)),
        "status": n.status.value,
        "proxy_url": n.proxy_url,
        "daily_cap": n.daily_cap,
        "warmup_day": n.warmup_day,
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

    daily_cap = dati.get("daily_cap")
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
    body (sent_today, sent_date, warmup_day, status, ...) e' ignorata in
    silenzio, non e' un errore -- e' cosi' che un client che manda il record
    intero non riesce comunque a scavalcare M3 (contratto §4.1)."""
    numero = await _numero_o_404(db, number_id)
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
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    nota = f"[{stamp}] riattivato: {motivo.strip()}"
    numero.notes = f"{(numero.notes or '').rstrip()}\n{nota}".strip()
    await db.commit()
    logger.warning(f"[WA] numero {number_id[:8]} riattivato -> pending_qr: {motivo.strip()}")
    return {"status": numero.status.value,
            "prossimo_passo": "avvia il login QR, poi verifica la sessione"}
