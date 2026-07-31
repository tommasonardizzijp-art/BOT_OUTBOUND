"""CRUD tenant del canale WhatsApp. Scheletro creato in PR-0 e riempito da
M2: la registrazione in main.py avviene UNA volta sola, cosi' i due
cantieri paralleli non toccano mai lo stesso file (contratto §5).

CRUD semplice, come da piano (Task 5): id, name, status, settings,
created_at."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.database import get_db
from app.models.tenant import Tenant, TenantStatus

router = APIRouter(prefix="/tenants", tags=["tenants"])


def _serializza(t: Tenant) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "status": t.status.value if hasattr(t.status, "value") else t.status,
        "settings": t.settings,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


async def _tenant_o_404(db, tenant_id: str) -> Tenant:
    t = await db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if t is None:
        raise HTTPException(404, "tenant inesistente")
    return t


@router.get("")
async def lista(db=Depends(get_db)) -> dict:
    righe = (await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))).scalars().all()
    return {"tenants": [_serializza(t) for t in righe]}


@router.post("")
async def crea(dati: dict, db=Depends(get_db)) -> dict:
    """Body come dict semplice, non campi scalari con default `Body(None)`:
    quel default e' un oggetto sentinella di FastAPI risolto solo quando la
    richiesta passa dal layer HTTP -- chiamando la funzione a mano senza
    passare la chiave, il sentinella finirebbe scritto a DB cosi' com'e'
    (stesso bug trovato e corretto in wa_numbers.crea)."""
    name = (dati.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "il nome e' obbligatorio")
    t = Tenant(name=name, settings=dati.get("settings"))
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _serializza(t)


@router.get("/{tenant_id}")
async def dettaglio(tenant_id: str, db=Depends(get_db)) -> dict:
    t = await _tenant_o_404(db, tenant_id)
    return _serializza(t)


@router.patch("/{tenant_id}")
async def aggiorna(tenant_id: str, campi: dict, db=Depends(get_db)) -> dict:
    t = await _tenant_o_404(db, tenant_id)
    if "name" in campi and (campi["name"] or "").strip():
        t.name = campi["name"].strip()
    if "settings" in campi:
        t.settings = campi["settings"]
    if "status" in campi:
        try:
            t.status = TenantStatus(campi["status"])
        except ValueError:
            raise HTTPException(422, f"status non valido: {campi['status']!r}")
    await db.commit()
    await db.refresh(t)
    return _serializza(t)
