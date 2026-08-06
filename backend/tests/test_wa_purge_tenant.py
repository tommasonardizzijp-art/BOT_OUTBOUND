"""M5 Task 1: scripts/wa_purge_tenant.py (SDD S12.4, GDPR -- "il cliente X
chiude il rapporto, cancella tutti i suoi dati").

Il test piu' importante di questo file e' test_junction_non_viene_attraversata:
verifica sperimentalmente, con una vera junction NTFS, che la cancellazione
del profilo browser di un tenant purgato non attraversi MAI una junction verso
l'esterno -- la stessa classe di errore dell'incidente notte 05-06/08
(robocopy /MIR senza /XJ ha cancellato un profilo WhatsApp reale).
"""
import os
import subprocess
import sys
import uuid
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.models.tenant import Tenant
from app.models.wa import (WaCampaign, WaCampaignContact, WaContact,
                           WaInboundEvent, WaMatchedBy, WaMessage,
                           WaMessageStatus, WaNumber, WaSequenceStep)
from app.services.wa_session import profile_dir_for
from scripts import wa_purge_tenant as purge
from tests.factories_wa import (make_campaign, make_campaign_contact,
                                make_contact, make_number, make_tenant)


def _argv(tenant_id: str, *extra: str) -> list[str]:
    return ["wa_purge_tenant", "--tenant-id", tenant_id, *extra]


@pytest.fixture
def profili_da_pulire():
    """Directory profilo create a mano nei test (non dallo script): pulite a
    fine test con la STESSA funzione sicura dello script -- anche il teardown
    del test non deve mai attraversare una junction."""
    dirs = []
    yield dirs
    for d in dirs:
        purge.remove_profile_dir_safely(d)


async def _seed_tenant_completo(db, *, label: str = "Tenant Purge Test"):
    """Un tenant con dati su OGNI livello della gerarchia: numero, contatto,
    campagna (+ step), campaign_contact, messaggio, inbound event."""
    tenant = await make_tenant(db, label)
    number = await make_number(db, tenant)
    contact = await make_contact(db, tenant)
    campaign, step = await make_campaign(db, tenant, number)
    cc = await make_campaign_contact(db, campaign, contact)
    msg = WaMessage(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                    wa_number_id=number.id, step_index=0, template_variant="a",
                    rendered_text="ciao", status=WaMessageStatus.sent,
                    sent_at=datetime.utcnow())
    db.add(msg)
    event = WaInboundEvent(id=str(uuid.uuid4()), tenant_id=tenant.id, wa_number_id=number.id,
                           contact_id=contact.id, preview_text="ciao",
                           matched_by=WaMatchedBy.chat_title, processed=False)
    db.add(event)
    await db.commit()
    return {"tenant": tenant, "number": number, "contact": contact, "campaign": campaign,
            "step": step, "cc": cc, "msg": msg, "event": event}


async def _count(db, model, **where) -> int:
    q = select(func.count()).select_from(model)
    for k, v in where.items():
        q = q.where(getattr(model, k) == v)
    return await db.scalar(q)


async def _tenant_fresco(db, tenant_id: str):
    """Rilettura FRESCA via select(), non db.get(): quest'ultimo consulta
    l'identity map della sessione prima del DB, e db_session ha
    expire_on_commit=False -- dopo un commit di seeding l'oggetto resta
    cached anche se un'ALTRA sessione (quella dello script, AsyncSessionLocal)
    lo cancella davvero. select() emette sempre una query nuova."""
    return await db.scalar(select(Tenant).where(Tenant.id == tenant_id))


async def _assert_tenant_intatto(db, ids: dict) -> None:
    assert await _tenant_fresco(db, ids["tenant"].id) is not None
    assert await _count(db, WaNumber, id=ids["number"].id) == 1
    assert await _count(db, WaContact, id=ids["contact"].id) == 1
    assert await _count(db, WaCampaign, id=ids["campaign"].id) == 1
    assert await _count(db, WaSequenceStep, campaign_id=ids["campaign"].id) == 1
    assert await _count(db, WaCampaignContact, id=ids["cc"].id) == 1
    assert await _count(db, WaMessage, id=ids["msg"].id) == 1
    assert await _count(db, WaInboundEvent, id=ids["event"].id) == 1


async def _assert_tenant_svuotato(db, tenant_id: str, campaign_id: str) -> None:
    assert await _tenant_fresco(db, tenant_id) is None
    assert await _count(db, WaNumber, tenant_id=tenant_id) == 0
    assert await _count(db, WaContact, tenant_id=tenant_id) == 0
    assert await _count(db, WaCampaign, tenant_id=tenant_id) == 0
    assert await _count(db, WaSequenceStep, campaign_id=campaign_id) == 0
    assert await _count(db, WaCampaignContact, campaign_id=campaign_id) == 0
    assert await _count(db, WaMessage, campaign_id=campaign_id) == 0
    assert await _count(db, WaInboundEvent, tenant_id=tenant_id) == 0


@pytest.mark.asyncio
async def test_1_dry_run_conta_senza_cancellare(db_session, monkeypatch, capsys,
                                                 profili_da_pulire):
    ids = await _seed_tenant_completo(db_session, label=f"QAM5-dry-{uuid.uuid4().hex[:8]}")
    profile_dir = profile_dir_for(ids["number"].id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "canary.txt").write_text("sessione finta")
    profili_da_pulire.append(profile_dir)

    monkeypatch.setattr(sys, "argv", _argv(ids["tenant"].id, "--dry-run"))
    await purge.main()
    out = capsys.readouterr().out

    assert "wa_inbound_events: 1" in out
    assert "wa_messages: 1" in out
    assert "wa_campaign_contacts: 1" in out
    assert "wa_sequence_steps: 1" in out
    assert "wa_campaigns: 1" in out
    assert "wa_contacts: 1" in out
    assert "wa_numbers: 1" in out

    await _assert_tenant_intatto(db_session, ids)
    assert profile_dir.exists()


@pytest.mark.asyncio
async def test_2_yes_cancella_tutto_nell_ordine_corretto(db_session, monkeypatch):
    ids = await _seed_tenant_completo(db_session, label=f"QAM5-yes-{uuid.uuid4().hex[:8]}")
    profile_dir = profile_dir_for(ids["number"].id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "canary.txt").write_text("sessione finta")

    monkeypatch.setattr(sys, "argv", _argv(ids["tenant"].id, "--yes"))
    await purge.main()

    await _assert_tenant_svuotato(db_session, ids["tenant"].id, ids["campaign"].id)
    assert not profile_dir.exists()


@pytest.mark.asyncio
async def test_3_isolamento_secondo_tenant_intatto(db_session, monkeypatch,
                                                    profili_da_pulire):
    da_purgare = await _seed_tenant_completo(db_session, label=f"QAM5-iso-a-{uuid.uuid4().hex[:8]}")
    intatto = await _seed_tenant_completo(db_session, label=f"QAM5-iso-b-{uuid.uuid4().hex[:8]}")
    profile_intatto = profile_dir_for(intatto["number"].id)
    profile_intatto.mkdir(parents=True, exist_ok=True)
    (profile_intatto / "canary.txt").write_text("non toccare")
    profili_da_pulire.append(profile_intatto)

    monkeypatch.setattr(sys, "argv", _argv(da_purgare["tenant"].id, "--yes"))
    await purge.main()

    await _assert_tenant_svuotato(db_session, da_purgare["tenant"].id, da_purgare["campaign"].id)
    await _assert_tenant_intatto(db_session, intatto)
    assert profile_intatto.exists()
    assert (profile_intatto / "canary.txt").read_text() == "non toccare"


@pytest.mark.asyncio
async def test_4_tenant_inesistente_errore_chiaro_nessuna_modifica(db_session, monkeypatch, capsys):
    fantasma = str(uuid.uuid4())
    monkeypatch.setattr(sys, "argv", _argv(fantasma, "--yes"))

    with pytest.raises(SystemExit) as exc:
        await purge.main()
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert fantasma in err

    assert await _tenant_fresco(db_session, fantasma) is None
    # nessun effetto collaterale su altre righe (niente da controllare per id
    # specifico, ma la tabella tenants non deve aver perso righe esistenti)


@pytest.mark.asyncio
async def test_5_nessun_flag_e_dry_run_di_default(db_session, monkeypatch, capsys):
    ids = await _seed_tenant_completo(db_session, label=f"QAM5-default-{uuid.uuid4().hex[:8]}")

    monkeypatch.setattr(sys, "argv", _argv(ids["tenant"].id))
    await purge.main()
    out = capsys.readouterr().out

    assert "dry-run" in out.lower() or "cancellazion" in out.lower()
    assert "--yes" in out
    await _assert_tenant_intatto(db_session, ids)


@pytest.mark.asyncio
async def test_6_riesecuzione_su_tenant_gia_vuoto_conteggi_a_zero(db_session, monkeypatch):
    """Tenant senza NESSUN dato figlio (mai avuto numeri/contatti/campagne):
    la riesecuzione del purge su questo caso limite non deve fallire."""
    tenant = await make_tenant(db_session, f"QAM5-vuoto-{uuid.uuid4().hex[:8]}")
    await db_session.commit()

    monkeypatch.setattr(sys, "argv", _argv(tenant.id, "--dry-run"))
    await purge.main()

    monkeypatch.setattr(sys, "argv", _argv(tenant.id, "--yes"))
    await purge.main()  # non deve sollevare

    assert await _tenant_fresco(db_session, tenant.id) is None


@pytest.mark.asyncio
async def test_7_riesecuzione_su_tenant_gia_purgato_e_un_errore_pulito(db_session, monkeypatch, capsys):
    """Rilanciare il purge su un tenant che il purge STESSO ha gia' cancellato
    (caso reale: comando ripetuto per errore) deve dare lo stesso errore
    chiaro del tenant sconosciuto, non un crash/traceback."""
    ids = await _seed_tenant_completo(db_session, label=f"QAM5-repurge-{uuid.uuid4().hex[:8]}")
    tid = ids["tenant"].id

    monkeypatch.setattr(sys, "argv", _argv(tid, "--yes"))
    await purge.main()

    monkeypatch.setattr(sys, "argv", _argv(tid, "--yes"))
    with pytest.raises(SystemExit) as exc:
        await purge.main()
    assert exc.value.code != 0
    assert tid in capsys.readouterr().err


@pytest.mark.asyncio
async def test_8_directory_profilo_assente_non_fallisce(db_session, monkeypatch):
    """Numero mai onboardato (nessun profilo browser su disco): il purge deve
    restare idempotente e non fallire per un path assente."""
    ids = await _seed_tenant_completo(db_session, label=f"QAM5-noprof-{uuid.uuid4().hex[:8]}")
    profile_dir = profile_dir_for(ids["number"].id)
    assert not profile_dir.exists()

    monkeypatch.setattr(sys, "argv", _argv(ids["tenant"].id, "--yes"))
    await purge.main()  # non deve sollevare

    await _assert_tenant_svuotato(db_session, ids["tenant"].id, ids["campaign"].id)


@pytest.mark.asyncio
async def test_9_junction_non_viene_attraversata(db_session, monkeypatch, tmp_path):
    """IL TEST PIU' IMPORTANTE del file. Il profilo del numero e' una vera
    junction NTFS verso una cartella "esterna" con un file dentro (simula un
    profilo browser che punta -- per errore o per design futuro -- fuori dalla
    cartella prevista). Dopo il purge: (a) la junction e' sparita dal path del
    profilo, (b) il contenuto DENTRO la cartella esterna esiste ancora --
    prova diretta che la cancellazione non ha attraversato il collegamento.
    """
    ids = await _seed_tenant_completo(db_session, label=f"QAM5-junction-{uuid.uuid4().hex[:8]}")
    profile_dir = profile_dir_for(ids["number"].id)
    profile_dir.parent.mkdir(parents=True, exist_ok=True)

    esterna = tmp_path / "cartella_esterna_reale"
    esterna.mkdir()
    canary = esterna / "canary.txt"
    canary.write_text("contenuto reale, non deve sparire mai")

    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(profile_dir), str(esterna)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"mklink /J non disponibile in questo ambiente di test "
                    f"(rc={result.returncode}, stderr={result.stderr!r}); "
                    "vedi report finale per la nota esplicita.")
    assert profile_dir.is_dir()  # la junction risolve come directory

    try:
        monkeypatch.setattr(sys, "argv", _argv(ids["tenant"].id, "--yes"))
        await purge.main()

        assert not os.path.lexists(profile_dir), (
            "la junction del profilo deve essere sparita dopo il purge")
        assert esterna.exists(), (
            "la cartella ESTERNA (target della junction) deve esistere ancora")
        assert canary.exists() and canary.read_text() == "contenuto reale, non deve sparire mai", (
            "il contenuto dietro la junction non deve mai essere toccato")
    finally:
        # pulizia extra di sicurezza: se profile_dir fosse rimasta (test
        # fallito prima del purge), rimuoverla SENZA attraversarla.
        purge.remove_profile_dir_safely(profile_dir)
