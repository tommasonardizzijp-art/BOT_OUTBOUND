"""CRUD campagne WhatsApp. Scheletro creato in PR-0 e riempito da M2:
la registrazione in main.py avviene UNA volta sola, cosi' i due cantieri
paralleli non toccano mai lo stesso file (contratto §5).

Le regole di dominio (optout_enabled condizionale, validazione del template)
stanno in wa_campaign_service: qui c'e' solo il guscio HTTP -- traduzione
del ValueError del servizio in 422, guardie di stato macchina (draft-only)
e serializzazione.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.database import get_db
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaCampaignType, WaContact, WaSequenceStep)
from app.services import wa_campaign_service as svc

router = APIRouter(prefix="/wa/campaigns", tags=["wa-campaigns"])

# Contratto §4.1-equivalente per le campagne: status, i contatori
# denormalizzati (sent/replied/opted_out/failed/total_contacts) e le date di
# ciclo vita (started_at/completed_at) sono scritti dal motore di invio (M3)
# o dagli endpoint di ciclo vita (Task 7). Un PATCH che li accettasse
# creerebbe due padroni per la stessa colonna.
CAMPI_MODIFICABILI = {"name", "daily_limit", "optout_cta", "active_hours_start",
                      "active_hours_end", "optout_enabled"}


def _serializza(c: WaCampaign) -> dict:
    return {
        "id": c.id,
        "tenant_id": c.tenant_id,
        "wa_number_id": c.wa_number_id,
        "name": c.name,
        "campaign_type": c.campaign_type.value,
        "status": c.status.value,
        "daily_limit": c.daily_limit,
        "optout_enabled": c.optout_enabled,
        "optout_cta": c.optout_cta,
        "active_hours_start": c.active_hours_start,
        "active_hours_end": c.active_hours_end,
        "total_contacts": c.total_contacts,
        "sent": c.sent,
        "replied": c.replied,
        "opted_out": c.opted_out,
        "failed": c.failed,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


def _serializza_step(s: WaSequenceStep) -> dict:
    return {
        "step_index": s.step_index,
        "template_a": s.template_a,
        "template_b": s.template_b,
        "template_c": s.template_c,
        "template_d": s.template_d,
    }


async def _campagna_o_404(db, campaign_id: str) -> WaCampaign:
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None:
        raise HTTPException(404, "campagna inesistente")
    return campagna


async def _step0_o_404(db, campaign_id: str) -> WaSequenceStep:
    step = await db.scalar(select(WaSequenceStep)
                           .where(WaSequenceStep.campaign_id == campaign_id,
                                  WaSequenceStep.step_index == 0))
    if step is None:
        raise HTTPException(404, "step 0 inesistente")
    return step


async def _colonne_note(db, campaign_id: str) -> set[str]:
    """Le colonne libere effettivamente presenti nei contatti gia' caricati
    per questa campagna: e' contro QUESTE che si rivalida il template dello
    step 0 (Task 6), non contro un elenco dichiarato a mano."""
    righe = (await db.execute(
        select(WaContact.attributes)
        .join(WaCampaignContact, WaCampaignContact.contact_id == WaContact.id)
        .where(WaCampaignContact.campaign_id == campaign_id)
    )).scalars().all()
    colonne: set[str] = set()
    for attrs in righe:
        if attrs:
            colonne.update(attrs.keys())
    return colonne


@router.get("")
async def lista(tenant_id: str | None = None, status: str | None = None,
                db=Depends(get_db)) -> dict:
    query = select(WaCampaign).order_by(WaCampaign.created_at.desc())
    if tenant_id:
        query = query.where(WaCampaign.tenant_id == tenant_id)
    if status:
        query = query.where(WaCampaign.status == status)
    righe = (await db.execute(query)).scalars().all()
    return {"campagne": [_serializza(c) for c in righe]}


@router.post("")
async def crea(dati: dict, db=Depends(get_db)) -> dict:
    """Body dict semplice, stesso stile degli altri router WA (vedi
    wa_numbers.crea): niente default `Body(...)` che risolverebbe solo
    passando dal layer HTTP."""
    tenant_id = (dati.get("tenant_id") or "").strip()
    wa_number_id = (dati.get("wa_number_id") or "").strip()
    name = (dati.get("name") or "").strip()
    template_a = dati.get("template_a") or ""
    tipo_raw = dati.get("campaign_type")
    if not tenant_id or not wa_number_id or not name or not template_a.strip() or not tipo_raw:
        raise HTTPException(
            422, "tenant_id, wa_number_id, name, campaign_type e template_a sono obbligatori")
    try:
        tipo = WaCampaignType(tipo_raw)
    except ValueError:
        raise HTTPException(422, f"campaign_type non valido: {tipo_raw!r}")

    payload = dict(dati)
    payload.update(tenant_id=tenant_id, wa_number_id=wa_number_id, name=name,
                   template_a=template_a, campaign_type=tipo)
    try:
        campagna = await svc.crea_campagna(db, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _serializza(campagna)


@router.get("/{campaign_id}")
async def dettaglio(campaign_id: str, db=Depends(get_db)) -> dict:
    campagna = await _campagna_o_404(db, campaign_id)
    step = await _step0_o_404(db, campaign_id)
    out = _serializza(campagna)
    out["step_0"] = _serializza_step(step)
    return out


@router.patch("/{campaign_id}")
async def aggiorna(campaign_id: str, campi: dict, db=Depends(get_db)) -> dict:
    """Solo CAMPI_MODIFICABILI vengono applicati, e solo in draft: a
    campagna partita si modifica passando prima da pausa (Task 7). Se il
    risultato lascerebbe optout_enabled=True con una CTA vuota, 422 -- si
    valida sui valori FINALI, prima di scrivere niente sull'oggetto."""
    campagna = await _campagna_o_404(db, campaign_id)
    if campagna.status != WaCampaignStatus.draft:
        raise HTTPException(
            409, f"la campagna e' in stato {campagna.status.value}: si modifica solo "
                 "in bozza (metti in pausa una campagna avviata prima di modificarla)")

    aggiornamenti = {k: campi[k] for k in CAMPI_MODIFICABILI & campi.keys()}
    optout_enabled = aggiornamenti.get("optout_enabled", campagna.optout_enabled)
    optout_cta = aggiornamenti.get("optout_cta", campagna.optout_cta)
    if optout_enabled and not (optout_cta or "").strip():
        raise HTTPException(
            422, "una campagna con opt-out attivo deve avere una CTA: non si manda "
                 "marketing senza via d'uscita")

    for chiave, valore in aggiornamenti.items():
        setattr(campagna, chiave, valore)
    await db.commit()
    await db.refresh(campagna)
    return _serializza(campagna)


@router.put("/{campaign_id}/steps/0")
async def aggiorna_step(campaign_id: str, dati: dict, db=Depends(get_db)) -> dict:
    """Rivalida il template contro le colonne EFFETTIVAMENTE presenti nei
    contatti gia' caricati (non contro un elenco dichiarato dal client): uno
    step con placeholder ignoti non si salva (Task 6)."""
    campagna = await _campagna_o_404(db, campaign_id)
    if campagna.status != WaCampaignStatus.draft:
        raise HTTPException(
            409, f"la campagna e' in stato {campagna.status.value}: il testo si "
                 "modifica solo in bozza")
    step = await _step0_o_404(db, campaign_id)

    template_a = dati.get("template_a") or ""
    colonne_note = await _colonne_note(db, campaign_id)
    try:
        svc.valida_step(template_a, colonne_note=colonne_note)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    step.template_a = template_a
    step.template_b = dati.get("template_b")
    step.template_c = dati.get("template_c")
    step.template_d = dati.get("template_d")
    await db.commit()
    await db.refresh(step)
    return _serializza_step(step)


# Ciclo di vita (Task 7). Le regole vere (macchina a stati SDD 8.1, max 1
# campagna 'running' per numero, re-pacing di next_action_at) stanno TUTTE
# nel servizio: qui solo la traduzione ValueError -> 422, stesso pattern di
# crea()/aggiorna() sopra. Nessun body: lo stato di partenza e' sempre e
# solo quello a DB, un client non ha nessun campo da passare per aggirare
# le validazioni (a differenza del bug optout_enabled trovato in crea()).
@router.post("/{campaign_id}/start")
async def start(campaign_id: str, db=Depends(get_db)) -> dict:
    try:
        campagna = await svc.avvia(db, campaign_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _serializza(campagna)


@router.post("/{campaign_id}/pause")
async def pause(campaign_id: str, db=Depends(get_db)) -> dict:
    try:
        campagna = await svc.pausa(db, campaign_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _serializza(campagna)


@router.post("/{campaign_id}/resume")
async def resume(campaign_id: str, db=Depends(get_db)) -> dict:
    try:
        campagna = await svc.riprendi(db, campaign_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _serializza(campagna)


@router.post("/{campaign_id}/stop")
async def stop(campaign_id: str, db=Depends(get_db)) -> dict:
    try:
        campagna = await svc.ferma(db, campaign_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _serializza(campagna)


# KPI (Task 8). I contatori sono TUTTI denormalizzati e scritti altrove
# (sent/failed/opted_out da M3, replied da M4, contratto 4.1): qui c'e' solo
# lettura e aritmetica. L'unico contatore che M2 stesso scrive e'
# total_contacts, dall'ingest (Task 3).
#
# I tassi si calcolano sugli INVIATI, non sui caricati (altrimenti una
# campagna appena partita, con 5 invii su 5.000 contatti, sembra un
# disastro anche se va benissimo). Con inviati=0 il tasso e' 0.0 per
# definizione: un contatto puo' risultare opted_out/failed prima ancora di
# aver ricevuto un messaggio di QUESTA campagna (es. DNC importato, o
# opt-out arrivato da un altro canale) -- il conteggio assoluto resta
# visibile, il tasso non mente con una divisione per zero o un 'infinito
# per cento'.
SOGLIA_ALLARME_OPTOUT_PCT = 5.0   # SDD 10.3, da confermare con dati veri (Q65)


@router.get("/{campaign_id}/kpi")
async def kpi(campaign_id: str, db=Depends(get_db)) -> dict:
    campagna = await _campagna_o_404(db, campaign_id)

    inviati = campagna.sent or 0

    def _pct(n: int) -> float:
        """0.0 senza inviati (niente da dividere). Sopra gli inviati si
        clampa a 100: n > inviati non dovrebbe succedere (opted_out/failed
        sono un sottoinsieme dei messaggi inviati), ma sono contatori scritti
        da un altro modulo (M3, non ancora costruito) senza un vincolo a DB
        che lo garantisca -- un bug li' non deve tradursi in un tasso tipo
        '100000%' mostrato all'operatore. E' un dato fuorviante quanto una
        divisione per zero, solo meno ovvio da notare."""
        if not inviati:
            return 0.0
        return round(min(100.0, 100.0 * n / inviati), 1)

    return {
        "stato": campagna.status.value,
        "caricati": campagna.total_contacts or 0,
        "inviati": inviati,
        "da_inviare": max(0, (campagna.total_contacts or 0) - inviati),
        "risposti": campagna.replied or 0,
        "optout": campagna.opted_out or 0,
        "falliti": campagna.failed or 0,
        "tasso_risposta": _pct(campagna.replied or 0),
        "tasso_optout": _pct(campagna.opted_out or 0),
        "allarme_optout": _pct(campagna.opted_out or 0) > SOGLIA_ALLARME_OPTOUT_PCT,
        # Onesta' del dato: le risposte arrivate dopo la fine della campagna
        # possono sfuggire allo scan, quindi il tasso e' una stima per
        # DIFETTO (SDD 15.1). La UI lo dice, non lo nasconde.
        "nota": "Il tasso di risposta e' una stima per difetto: le risposte "
                "arrivate a campagna chiusa possono non essere conteggiate.",
    }
