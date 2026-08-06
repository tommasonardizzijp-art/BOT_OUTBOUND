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
@pytest.mark.skipif(
    sys.platform != "win32",
    reason="junction NTFS + mklink /J esistono solo su Windows; su Linux (CI) "
           "'cmd' non esiste e subprocess.run solleva FileNotFoundError prima "
           "ancora di poter guardare il returncode. Il ramo POSIX equivalente "
           "(symlink) e' coperto da test_9b_symlink_non_viene_attraversato.",
)
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


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="gemello POSIX di test_9: su Windows la creazione di un symlink "
           "richiede privilegi elevati/developer mode, il caso reale li' e' la "
           "junction (test_9). Questo esiste perche' la CI gira su Linux: senza, "
           "il comportamento piu' importante dello script non sarebbe verificato "
           "da NESSUN test nell'unico ambiente che gira a ogni push.",
)
async def test_9b_symlink_non_viene_attraversato(db_session, monkeypatch, tmp_path):
    """Stesso contratto di test_9 sul ramo POSIX: il profilo del numero e' un
    symlink verso una cartella esterna con un canary dentro. Dopo il purge il
    symlink e' sparito ma il target e il suo contenuto esistono ancora --
    prova che la rimozione non ha attraversato il collegamento."""
    ids = await _seed_tenant_completo(db_session, label=f"QAM5-symlink-{uuid.uuid4().hex[:8]}")
    profile_dir = profile_dir_for(ids["number"].id)
    profile_dir.parent.mkdir(parents=True, exist_ok=True)

    esterna = tmp_path / "cartella_esterna_reale"
    esterna.mkdir()
    canary = esterna / "canary.txt"
    canary.write_text("contenuto reale, non deve sparire mai")

    os.symlink(esterna, profile_dir, target_is_directory=True)
    assert profile_dir.is_dir()  # il symlink risolve come directory

    try:
        monkeypatch.setattr(sys, "argv", _argv(ids["tenant"].id, "--yes"))
        await purge.main()

        assert not os.path.lexists(profile_dir), (
            "il symlink del profilo deve essere sparito dopo il purge")
        assert esterna.exists(), (
            "la cartella ESTERNA (target del symlink) deve esistere ancora")
        assert canary.exists() and canary.read_text() == "contenuto reale, non deve sparire mai", (
            "il contenuto dietro il symlink non deve mai essere toccato")
    finally:
        purge.remove_profile_dir_safely(profile_dir)


@pytest.mark.asyncio
async def test_10_fallimento_rimozione_un_profilo_non_blocca_gli_altri(
        db_session, monkeypatch):
    """Trovato in code review adversarial (M5 Task 1): il DB e' gia' stato
    cancellato (commit irreversibile) quando parte il loop sui profili
    browser. Se la rimozione del profilo del PRIMO numero solleva (es. file
    bloccato da un processo Chromium ancora vivo), un semplice `for` senza
    try/except interrompe il loop: i profili degli ALTRI numeri non vengono
    nemmeno tentati, e non c'e' modo di ritentarli via CLI perche' i
    wa_numbers che servono a ricostruire il path sono gia' spariti dal DB.
    Il fallimento su un numero non deve impedire il tentativo sugli altri, e
    deve essere segnalato chiaramente (non un crash silenzioso a meta' lista,
    non un traceback grezzo).

    NON usa la fixture `profili_da_pulire`: il suo teardown chiamerebbe
    `remove_profile_dir_safely` mentre il monkeypatch di questo test e'
    ancora attivo (i finalizer dei fixture girano prima dell'undo del
    monkeypatch), risollevando lo stesso PermissionError simulato. La pulizia
    del profilo "rotto" e' fatta a mano in `finally` con la funzione REALE
    salvata prima del patch."""
    tenant = await make_tenant(db_session, f"QAM5-fs-fail-{uuid.uuid4().hex[:8]}")
    numero_rotto = await make_number(db_session, tenant, label="Numero bloccato")
    numero_ok = await make_number(db_session, tenant, label="Numero pulibile")
    await db_session.commit()

    profilo_rotto = profile_dir_for(numero_rotto.id)
    profilo_rotto.mkdir(parents=True, exist_ok=True)
    (profilo_rotto / "canary.txt").write_text("bloccato")

    profilo_ok = profile_dir_for(numero_ok.id)
    profilo_ok.mkdir(parents=True, exist_ok=True)
    (profilo_ok / "canary.txt").write_text("pulibile")

    reale = purge.remove_profile_dir_safely

    def fallisce_sul_primo(path):
        # Confronto sul path ASSOLUTO di entrambi i lati: lo script normalizza
        # il percorso prima di rimuoverlo (browser_profiles_dir e' relativo di
        # default, e un path relativo dipenderebbe dalla CWD di chi lancia).
        # Un `==` fra un path relativo e uno assoluto non matcha mai: il
        # fallimento simulato non scatterebbe e il test passerebbe verificando
        # nulla.
        if os.path.abspath(path) == os.path.abspath(profilo_rotto):
            raise PermissionError(f"simulato: {path} bloccato da un processo")
        return reale(path)

    monkeypatch.setattr(purge, "remove_profile_dir_safely", fallisce_sul_primo)
    monkeypatch.setattr(sys, "argv", _argv(tenant.id, "--yes"))

    try:
        with pytest.raises(SystemExit) as exc:
            await purge.main()

        # Segnalato in modo chiaro (non un traceback grezzo che scoppia fuori
        # dallo script), con un codice di uscita diverso da successo pieno.
        assert exc.value.code != 0

        # Il DB e' comunque gia' stato svuotato (irreversibile, commit
        # avvenuto prima del loop sui profili) -- il fallimento e' SOLO sul
        # filesystem.
        assert await _tenant_fresco(db_session, tenant.id) is None

        # Il profilo del SECONDO numero deve essere stato rimosso comunque:
        # un fallimento sul primo non deve aver fermato il tentativo sul
        # secondo.
        assert not profilo_ok.exists(), (
            "il profilo del numero SANO deve essere rimosso anche se un altro fallisce")

        # Il profilo rotto resta (rimozione fallita) -- verificabile a mano.
        assert profilo_rotto.exists()
    finally:
        reale(profilo_rotto)


def test_11_nessuna_tabella_wa_sfugge_al_purge():
    """GUARDIA ANTI-DRIFT — il test piu' importante per il FUTURO di questo
    file (gli altri guardano il presente).

    Oggi il purge copre tutte e 7 le tabelle wa_*. Il giorno in cui una
    milestone successiva ne aggiunge una ottava, lo script continuerebbe a
    stampare "Purge completato" cancellando solo una parte dei dati del
    cliente: un fallimento GDPR silenzioso, con l'unico segnale visibile che
    e' un messaggio di successo. Nessun altro test se ne accorgerebbe, perche'
    tutti gli altri seminano a mano solo cio' che gia' conoscono.

    Qui invece si parte dallo SCHEMA: ogni tabella registrata in
    Base.metadata che sia wa_* o che abbia una colonna tenant_id deve essere
    nominata in _TABELLE. Se qualcuno aggiunge una tabella e dimentica lo
    script, e' questo test a fallire, con il nome della tabella dimenticata.
    """
    from app.database import Base
    import app.models  # noqa: F401 - registra tutti i modelli in Base

    coperte = set(purge._TABELLE)
    scoperte = {
        nome for nome, tabella in Base.metadata.tables.items()
        if (nome.startswith("wa_") or "tenant_id" in tabella.columns)
        and nome not in coperte and nome != "tenants"
    }
    assert not scoperte, (
        f"tabelle con dati per-tenant NON cancellate dal purge: {sorted(scoperte)}. "
        "Aggiungile a _TABELLE e alla sequenza di delete in main(), rispettando "
        "l'ordine FK (figli prima dei genitori)."
    )


@pytest.mark.asyncio
async def test_12_una_delete_che_fallisce_non_lascia_il_tenant_a_meta(
        db_session, monkeypatch):
    """ROLLBACK — la proprieta' che lo script dichiara a parole (docstring di
    main: "se qualcosa fallisce a meta', il commit non parte e nessuna riga
    resta cancellata a meta'") non era verificata da nessun test.

    Si fa esplodere l'ULTIMA delete prima del commit (quella del tenant):
    a quel punto le delete precedenti sono gia' state eseguite nella
    transazione. Se l'atomicita' regge, alla fine il tenant deve essere
    ancora li' CON tutti i suoi dati; se non reggesse, resterebbe un tenant
    orfano coi figli spariti -- lo stato peggiore possibile, perche' sembra
    integro dall'alto e non lo e'.
    """
    ids = await _seed_tenant_completo(db_session, label=f"QAM5-rollback-{uuid.uuid4().hex[:8]}")
    tenant_id = ids["tenant"].id

    vera_delete = purge.delete
    esplosa = {"fatto": False}

    def delete_che_esplode(target):
        if target is Tenant and not esplosa["fatto"]:
            esplosa["fatto"] = True
            raise RuntimeError("guasto simulato a meta' cancellazione")
        return vera_delete(target)

    monkeypatch.setattr(purge, "delete", delete_che_esplode)
    monkeypatch.setattr(sys, "argv", _argv(tenant_id, "--yes"))

    with pytest.raises(RuntimeError):
        await purge.main()

    assert esplosa["fatto"], "il guasto simulato non e' scattato: test inutile"

    # Nulla deve essere sopravvissuto al rollback: ne' il tenant, ne' i figli
    # gia' passati dalla delete prima dello scoppio.
    assert await _tenant_fresco(db_session, tenant_id) is not None, (
        "il tenant deve essere ancora presente: il commit non e' mai avvenuto")
    assert await _count(db_session, WaNumber, tenant_id=tenant_id) == 1
    assert await _count(db_session, WaContact, tenant_id=tenant_id) == 1
    assert await _count(db_session, WaCampaign, tenant_id=tenant_id) == 1
    assert await _count(db_session, WaInboundEvent, tenant_id=tenant_id) == 1


@pytest.mark.asyncio
async def test_13_junction_ANNIDATA_dentro_il_profilo_non_viene_attraversata(
        db_session, monkeypatch, tmp_path):
    """La forma ESATTA dell'incidente del 05-06/08: non un profilo che E' un
    collegamento (test_9), ma una junction NASCOSTA DENTRO un albero vero --
    che e' quello che `robocopy /MIR` senza `/XJ` ha seguito, cancellando un
    profilo WhatsApp reale fuori dalla cartella prevista.

    La docstring di remove_profile_dir_safely dichiara questo caso
    "verificato sperimentalmente" ma nessun test lo copriva.
    """
    if sys.platform == "win32":
        crea_link = lambda src, dst: subprocess.run(  # noqa: E731
            ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
            capture_output=True, text=True).returncode == 0
    else:
        def crea_link(src, dst):
            os.symlink(src, dst, target_is_directory=True)
            return True

    ids = await _seed_tenant_completo(db_session, label=f"QAM5-annidata-{uuid.uuid4().hex[:8]}")
    profile_dir = profile_dir_for(ids["number"].id)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "vero.txt").write_text("file interno, puo' sparire")

    esterna = tmp_path / "cartella_esterna_reale"
    esterna.mkdir()
    canary = esterna / "canary.txt"
    canary.write_text("contenuto reale, non deve sparire mai")

    if not crea_link(esterna, profile_dir / "Default" / "collegamento"):
        pytest.skip("creazione del collegamento non permessa in questo ambiente")

    try:
        monkeypatch.setattr(sys, "argv", _argv(ids["tenant"].id, "--yes"))
        await purge.main()

        assert not os.path.lexists(profile_dir), (
            "l'albero vero del profilo deve essere stato cancellato")
        assert esterna.exists() and canary.exists(), (
            "la cancellazione ha ATTRAVERSATO la junction annidata: e' "
            "esattamente l'incidente del 05-06/08")
        assert canary.read_text() == "contenuto reale, non deve sparire mai"
    finally:
        purge.remove_profile_dir_safely(profile_dir)
