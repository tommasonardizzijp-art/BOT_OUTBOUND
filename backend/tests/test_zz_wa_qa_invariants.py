"""QA M3 (Task 15 Step 3), gruppo J (36-39): invarianti SQL a fine batch.

Nome del file scelto apposta per essere l'ULTIMO raccolto da pytest fra i
file `test_wa_*`/`test_zz_*` (ordine alfabetico di default): i test 36/37
leggono lo stato PERSISTITO (sessione fresca, non db_session che fa
rollback) dopo che tutto il resto della suite WA ha gia' girato.

38 e 39 NON fanno uno sweep sull'intero DB condiviso: `test_wa_api_kpi.py`
scrive `campaign.sent=20` senza wa_messages veri apposta (testa la
matematica del KPI, non l'invariante di scrittura) e `test_wa_ingest.py`
marca `opted_out=True` direttamente senza passare da `persist_wa_optout`
(testa il filtro ingest). Sono dati fabbricati legittimi per un altro
scopo, non una violazione reale -- uno sweep globale li becca come falsi
positivi. 38/39 costruiscono invece un proprio scenario end-to-end (stesso
codice di produzione, non dati fabbricati) e verificano l'invariante SOLO
su quello: e' la lettura corretta di "fine del batch di test di QUESTO
FILE" (qa-m3-adversarial.md), non "fine di tutta la suite storica".
"""
import pytest
from sqlalchemy import func, select

from app.database import AsyncSessionLocal


@pytest.mark.asyncio
async def test_36_nessun_lock_piu_vecchio_del_timeout():
    from datetime import datetime, timedelta
    from app.config import settings
    from app.models.wa import WaCampaignContact, WaContactStatus

    cutoff = datetime.utcnow() - timedelta(minutes=int(settings.wa_lock_timeout_min))
    async with AsyncSessionLocal() as db:
        righe = (await db.execute(
            select(WaCampaignContact.id).where(
                WaCampaignContact.status.in_([WaContactStatus.queued,
                                              WaContactStatus.in_sequence]),
                WaCampaignContact.locked_by.is_not(None),
                WaCampaignContact.locked_at < cutoff,
            )
        )).scalars().all()
    assert righe == [], f"lock piu' vecchi del timeout: {righe}"


@pytest.mark.asyncio
async def test_37_nessun_messaggio_fermo_in_sending():
    from app.models.wa import WaMessage, WaMessageStatus

    async with AsyncSessionLocal() as db:
        righe = (await db.execute(
            select(WaMessage.id).where(WaMessage.status == WaMessageStatus.sending)
        )).scalars().all()
    assert righe == [], f"messaggi fermi in sending: {righe}"


@pytest.mark.asyncio
async def test_38_contatore_sent_coerente_col_conteggio_reale(db_session, monkeypatch):
    """Scenario proprio, end-to-end vero (stesso codice del Task 8), non uno
    sweep sui dati fabbricati di test_wa_api_kpi.py."""
    from app.config import settings
    from app.models.wa import WaCampaign, WaMessage, WaMessageStatus
    from app.services import wa_sender
    from tests.test_wa_sender import _PomInvio, _scenario_invio

    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    pom = _PomInvio([])
    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert esito.stato == "sent"

    sent = await db_session.scalar(
        select(WaCampaign.sent).where(WaCampaign.id == ctx["campaign"].id))
    reale = await db_session.scalar(
        select(func.count(WaMessage.id)).where(
            WaMessage.campaign_id == ctx["campaign"].id,
            WaMessage.status == WaMessageStatus.sent))
    assert sent == reale == 1


@pytest.mark.asyncio
async def test_39_nessun_contatto_optato_con_righe_non_terminali(db_session):
    """Stesso principio: scenario proprio via persist_wa_optout vero (Task
    4), non uno sweep sui dati fabbricati di test_wa_ingest.py."""
    from app.models.wa import WaCampaignContact, WaContactStatus
    from app.services import wa_optout
    from tests.test_wa_worker import _scenario_claim

    ctx = await _scenario_claim(db_session)
    await wa_optout.persist_wa_optout(db_session, ctx["contact"].id, prova="STOP")

    terminali = (WaContactStatus.opted_out, WaContactStatus.completed,
                WaContactStatus.skipped, WaContactStatus.replied)
    residue = (await db_session.execute(
        select(WaCampaignContact.id).where(
            WaCampaignContact.contact_id == ctx["contact"].id,
            WaCampaignContact.status.notin_(terminali))
    )).scalars().all()
    assert residue == []
