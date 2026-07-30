"""Ingest e gestione contatti di una campagna WhatsApp."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaContact, WaContactStatus)
from app.services.wa_csv import CsvParseError
from app.services.wa_ingest import ingerisci_csv
from app.utils.crypto import decrypt
from app.utils.phone_pseudonym import mask_phone

router = APIRouter(prefix="/wa/contacts", tags=["wa-contacts"])

# 10 MB: 5.000 righe con dieci colonne stanno abbondantemente sotto. Serve a
# non tenere in memoria un file che nessuno vuole davvero caricare.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


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
            "in_lavorazione": bool(cc.locked_by),
        }
        for cc, c in righe
    ]}


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
    if cc.locked_by and cc.locked_at and cc.locked_at > (
            datetime.utcnow() - timedelta(minutes=int(settings.wa_lock_timeout_min))):
        raise HTTPException(409, "contatto in lavorazione dal worker: riprova fra poco")
    if cc.status in (WaContactStatus.opted_out, WaContactStatus.completed):
        raise HTTPException(409, f"riga in stato terminale ({cc.status.value}): "
                                 "non si rimuove, resta come storico")
    await db.delete(cc)
    await db.commit()
    return {"rimosso": True}
