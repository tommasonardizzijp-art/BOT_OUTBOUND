"""Ingest e gestione contatti di una campagna WhatsApp."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select, update

from app.config import settings
from app.database import get_db
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaContact, WaContactStatus)
from app.services.wa_csv import CsvParseError
from app.services.wa_ingest import ingerisci_csv
from app.services.wa_promote import arruolamento
from app.services.wa_promote.arruolamento import CampagnaNonModificabile
from app.utils.crypto import decrypt
from app.utils.phone_pseudonym import mask_phone

router = APIRouter(prefix="/wa/contacts", tags=["wa-contacts"])

# 10 MB: 5.000 righe con dieci colonne stanno abbondantemente sotto. Serve a
# non tenere in memoria un file che nessuno vuole davvero caricare.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _lock_fresco(cc: WaCampaignContact) -> bool:
    """Stessa soglia usata da DELETE: un lock piu' vecchio di
    wa_lock_timeout_min e' considerato stale (worker morto a meta'), non
    'in lavorazione'. GET e DELETE devono vedere lo stesso stato, altrimenti
    un operatore vede 'in lavorazione' su una riga che e' gia' rimovibile."""
    return bool(cc.locked_by and cc.locked_at and cc.locked_at > (
        datetime.utcnow() - timedelta(minutes=int(settings.wa_lock_timeout_min))))


@router.post("/ingest")
async def ingest(campaign_id: str = Form(...), file: UploadFile = File(...),
                 db=Depends(get_db)) -> dict:
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None:
        raise HTTPException(404, "campagna inesistente")
    if campagna.status != WaCampaignStatus.draft:
        raise HTTPException(
            409, f"la campagna e' in stato {campagna.status.value}: i contatti si "
                 "caricano quando e' in bozza")

    contenuto = await file.read()
    if len(contenuto) > MAX_UPLOAD_BYTES:
        raise HTTPException(422, "file troppo grande (limite 10 MB)")

    try:
        report = await ingerisci_csv(db, tenant_id=campagna.tenant_id,
                                     campaign_id=campaign_id, contenuto=contenuto)
    except CsvParseError as exc:
        # 422 e non 500: il file e' sbagliato, non il server. Il messaggio di
        # CsvParseError non contiene mai dati di riga (Task 2).
        raise HTTPException(422, str(exc))

    return {
        "creati": report.creati,
        "aggiornati": report.aggiornati,
        "gia_dnc": report.gia_dnc,
        "duplicati_nel_file": report.duplicati_nel_file,
        "scarti": [{"riga": s.riga, "motivo": s.motivo, "valore": s.valore_mascherato}
                   for s in report.scarti],
    }


@router.get("")
async def lista_contatti(campaign_id: str, limit: int = 200, offset: int = 0,
                         db=Depends(get_db)) -> dict:
    """Il numero torna SEMPRE mascherato (P12): la dashboard non ha motivo di
    vedere un numero intero, e un endpoint che lo espone e' un endpoint che
    prima o poi finisce in un log o in uno screenshot."""
    righe = (await db.execute(
        select(WaCampaignContact, WaContact)
        .join(WaContact, WaContact.id == WaCampaignContact.contact_id)
        .where(WaCampaignContact.campaign_id == campaign_id)
        .limit(min(limit, 500)).offset(offset)
    )).all()
    return {"contatti": [
        {
            "id": cc.id,
            "numero": mask_phone(decrypt(c.encrypted_phone)),
            "nome": c.display_name,
            "stato": cc.status.value,
            "tentativi_falliti": cc.failure_count,
            "ultimo_errore": cc.last_error,
            "opted_out": c.opted_out,
            "in_lavorazione": _lock_fresco(cc),
        }
        for cc, c in righe
    ]}


class EnrollRequest(BaseModel):
    campaign_id: str
    contact_ids: list[str]


@router.post("/enroll")
async def enroll(body: EnrollRequest, db=Depends(get_db)) -> dict:
    """Fase B, Task 3/4: aggiunge WaContact gia' esistenti a una campagna
    (arruolamento). Stesso guard 409 di `ingest` se la campagna non e' in
    bozza -- qui `arruolamento.arruola` lo esprime sollevando
    `CampagnaNonModificabile` (anche per campagna inesistente, vedi la
    docstring dell'eccezione), tradotta in HTTPException dal guscio HTTP."""
    try:
        report = await arruolamento.arruola(
            db, campaign_id=body.campaign_id, contact_ids=body.contact_ids)
    except CampagnaNonModificabile as exc:
        raise HTTPException(409, str(exc))
    return {
        "arruolati": report.arruolati,
        "gia_presenti": report.gia_presenti,
        "gia_dnc": report.gia_dnc,
        "scarti": [{"id": s.id, "motivo": s.motivo} for s in report.scarti],
    }


@router.delete("/{campaign_contact_id}")
async def rimuovi_contatto(campaign_contact_id: str, db=Depends(get_db)) -> dict:
    """Q18. Rifiuta se la riga e' sotto lock FRESCO: in quel momento e' in
    mano al worker di M3, e cancellarla sotto i suoi piedi significa un
    invio che scrive su una riga che non esiste piu'. M2 legge i campi di
    lock, non li scrive (invariante I1)."""
    cc = await db.scalar(select(WaCampaignContact)
                         .where(WaCampaignContact.id == campaign_contact_id))
    if cc is None:
        raise HTTPException(404, "riga inesistente")
    if _lock_fresco(cc):
        raise HTTPException(409, "contatto in lavorazione dal worker: riprova fra poco")
    # I quattro stati terminali della state machine (app/models/wa.py,
    # WaContactStatus): replied, completed, opted_out, skipped. failed e'
    # transitorio (retry), non terminale. Trovato in whole-branch review:
    # mancavano replied/skipped -- invisibile finche' M2 da solo non crea
    # mai righe in quegli stati (li scrive M3), ma un contatto che ha
    # risposto o uno scarto diagnostico sarebbe diventato rimovibile.
    if cc.status in (WaContactStatus.opted_out, WaContactStatus.completed,
                     WaContactStatus.replied, WaContactStatus.skipped):
        raise HTTPException(409, f"riga in stato terminale ({cc.status.value}): "
                                 "non si rimuove, resta come storico")
    campaign_id = cc.campaign_id
    contact_id = cc.contact_id
    await db.delete(cc)
    await db.flush()
    # Minimizzazione (Q23, "l'ingest crea SOLO i contatti della campagna,
    # niente anagrafica speculativa"): se questa era l'ULTIMA campagna che
    # referenziava il contatto, l'anagrafica orfana non deve restare a DB
    # per sempre. Trovato in Fase 4 QA (adversarial #46): un WaContact con
    # encrypted_phone/phone_hmac sopravviveva indefinitamente dopo l'unica
    # rimozione che lo riguardava. Un contatto usato da PIU' campagne (dedup
    # cross-campagna, e' lo scopo dichiarato di WaContact) resta intatto.
    altre_campagne = await db.scalar(
        select(func.count(WaCampaignContact.id))
        .where(WaCampaignContact.contact_id == contact_id))
    if not altre_campagne:
        contatto = await db.scalar(select(WaContact).where(WaContact.id == contact_id))
        if contatto is not None:
            await db.delete(contatto)
    # In SQL, mai read-modify-write (contratto §4.2): un secondo ingest o
    # una seconda rimozione concorrenti non devono perdere un decremento.
    # Trovato in review dedicata: total_contacts non veniva mai aggiornato
    # qui, restava per sempre disallineato dal conteggio reale dopo ogni
    # rimozione (visibile nella UI del Task 12, root cause qui).
    await db.execute(
        update(WaCampaign).where(WaCampaign.id == campaign_id)
        .values(total_contacts=WaCampaign.total_contacts - 1))
    await db.commit()
    return {"rimosso": True}
