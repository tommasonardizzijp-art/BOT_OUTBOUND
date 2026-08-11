"""Fase B, Task 4: API di lettura/approvazione dello staging auto-discover
(`wa_discovered_chats`).

Guscio HTTP sottile, stesso stile di `wa_contacts.py`/`wa_campaigns.py`: le
regole di dominio (chi e' promuovibile, l'esclusione dei gruppi, l'idempotenza)
stanno tutte in `wa_promote.regole`/`wa_promote.promozione`, qui c'e' solo
risoluzione del `tenant_id`, query e serializzazione.

**Barriera IDOR (corretta in review di Task 2, resa esplicita qui):**
`promozione.promuovi()` richiede un `tenant_id` obbligatorio ed e' il confine
di sicurezza vero -- una riga che esiste ma appartiene a un altro tenant si
scarta con `"non_trovato"`. Ma quella barriera serve a qualcosa solo se QUESTO
endpoint risolve il `tenant_id` corretto da una riga del DB che lo possiede
davvero, mai da un campo `tenant_id` scritto a mano nella richiesta (lo stesso
problema che un client potrebbe sfruttare passando il tenant_id di qualcun
altro se glielo lasciassimo scrivere). Non c'e' ancora un contesto di
richiesta autenticato in questa API (stesso stato di
`wa_campaigns.crea`/`wa_numbers.crea`): la soluzione qui e' risolvere sempre
`WaNumber` per id (`number_id`, sia in query GET che nel body POST) e usare
`numero.tenant_id` -- mai un tenant_id preso direttamente dal client.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.database import get_db
from app.models.wa import WaDiscoveredChat, WaNumber
from app.services.wa_promote import promozione
from app.services.wa_promote.regole import promuovibile
from app.utils.crypto import decrypt
from app.utils.phone_pseudonym import mask_phone

router = APIRouter(prefix="/wa/discovered-chats", tags=["wa-discover"])


async def _numero_o_404(db, number_id: str) -> WaNumber:
    numero = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
    if numero is None:
        raise HTTPException(404, "numero inesistente")
    return numero


def _serializza(riga: WaDiscoveredChat) -> dict:
    """`numero_mascherato` mai il numero intero (P12, stesso vincolo di
    `wa_contacts.lista_contatti`). `promuovibile` riusa `regole.promuovibile`
    -- niente duplicazione della regola fra qui e `promozione.py` (il piano lo
    richiede esplicitamente): un gruppo compare comunque nella lista (visibile
    all'operatore), solo marcato non promuovibile, mai nascosto."""
    numero_mascherato = (mask_phone(decrypt(riga.encrypted_phone))
                         if riga.encrypted_phone else None)
    return {
        "id": riga.id,
        "chat_title": riga.chat_title,
        "display_name": riga.display_name,
        "tipo_chat": riga.tipo_chat,
        "numero_leggibile": riga.numero_leggibile,
        "numero_mascherato": numero_mascherato,
        "status": riga.status,
        "promuovibile": promuovibile(riga).ok,
        "discovered_at": riga.discovered_at.isoformat() if riga.discovered_at else None,
    }


@router.get("")
async def lista(number_id: str, status: str = "nuovo", tipo_chat: str | None = None,
                ha_numero: bool | None = None, limit: int = 200, offset: int = 0,
                db=Depends(get_db)) -> dict:
    numero = await _numero_o_404(db, number_id)

    query = select(WaDiscoveredChat).where(
        WaDiscoveredChat.tenant_id == numero.tenant_id,
        WaDiscoveredChat.number_id == number_id,
    )
    if status:
        query = query.where(WaDiscoveredChat.status == status)
    if tipo_chat:
        query = query.where(WaDiscoveredChat.tipo_chat == tipo_chat)
    if ha_numero is not None:
        colonna = WaDiscoveredChat.phone_hmac
        query = query.where(colonna.is_not(None) if ha_numero else colonna.is_(None))

    query = (query.order_by(WaDiscoveredChat.discovered_at.desc())
             .limit(min(limit, 500)).offset(offset))
    righe = (await db.execute(query)).scalars().all()
    return {"chat": [_serializza(r) for r in righe]}


class PromuoviRequest(BaseModel):
    # Stesso nome del query param di GET "" (number_id), non `wa_number_id`
    # come nella prosa della review di Task 2: coerenza col resto di questo
    # router (GET e POST risolvono il tenant_id nello stesso modo, dalla
    # stessa chiave), decisione di stile non di sicurezza -- la barriera vera
    # e' che il valore passa sempre da `_numero_o_404`, mai letto da solo.
    number_id: str
    ids: list[str]


@router.post("/promote")
async def promote(body: PromuoviRequest, db=Depends(get_db)) -> dict:
    """Nessun controllo di stato campagna qui: la promozione non tocca
    campagne (quello e' `POST /wa/contacts/enroll`, Task 3/4). Risposta:
    `ReportPromozione` serializzato, stesso stile di `wa_contacts.ingest`
    (chiavi del dataclass, `scarti` come lista di dict)."""
    numero = await _numero_o_404(db, body.number_id)
    report = await promozione.promuovi(db, tenant_id=numero.tenant_id, ids=body.ids)
    return {
        "promossi": report.promossi,
        "contatti_creati": report.contatti_creati,
        "contatti_riusati": report.contatti_riusati,
        "gia_dnc": report.gia_dnc,
        "scarti": [{"id": s.id, "motivo": s.motivo} for s in report.scarti],
        "contatti_promossi_ids": report.contatti_promossi_ids,
    }
