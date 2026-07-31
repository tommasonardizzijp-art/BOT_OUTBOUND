"""Test del servizio di creazione campagne WA (Task 6).

Punto delicato (contratto M2-M3 §2.1): optout_enabled ha server_default=true
a DB (migrazione 025). La regola vera e' condizionale (True se marketing),
e deve essere assegnata ESPLICITAMENTE da questo servizio. Il test dedicato
verifica la riga a DB con una query diretta, non solo l'oggetto Python
appena creato: e' proprio il tipo di bug che un test superficiale non becca,
perche' il default a DB e' gia' True.
"""
import pytest

from app.models.wa import WaCampaignType
from app.services import wa_campaign_service as svc
from tests.factories_wa import make_number, make_tenant


def test_optout_e_attivo_per_marketing_e_spento_per_followup():
    assert svc.calcola_optout_enabled(WaCampaignType.marketing) is True
    assert svc.calcola_optout_enabled(WaCampaignType.followup) is False


@pytest.mark.asyncio
async def test_campagna_followup_ha_optout_false_A_DB_non_solo_nella_risposta(db_session):
    """Il server_default e' True: se il servizio non assegna esplicitamente,
    la riga a DB esce sbagliata anche con una risposta API giusta."""
    from sqlalchemy import select
    from app.models.wa import WaCampaign
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    camp = await svc.crea_campagna(db_session, {
        "tenant_id": tenant.id, "wa_number_id": number.id, "name": "follow",
        "campaign_type": WaCampaignType.followup, "template_a": "Ciao {nome}.",
    })

    # Query diretta sulla riga a DB, non l'oggetto Python in memoria: e' la
    # prova esplicita richiesta dal task, perche' il server_default=true
    # farebbe passare un test che controllasse solo `camp.optout_enabled`
    # se quell'oggetto restasse per caso quello giusto prima del commit.
    riga = await db_session.scalar(select(WaCampaign).where(WaCampaign.id == camp.id))
    assert riga.optout_enabled is False
    assert riga.optout_cta is None


@pytest.mark.asyncio
async def test_campagna_marketing_ha_optout_true_a_db(db_session):
    """Simmetrico del test precedente: marketing deve risultare True a DB,
    non solo per assenza di errori."""
    from sqlalchemy import select
    from app.models.wa import WaCampaign
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    camp = await svc.crea_campagna(db_session, {
        "tenant_id": tenant.id, "wa_number_id": number.id, "name": "promo",
        "campaign_type": WaCampaignType.marketing, "template_a": "Ciao {nome}.",
        "optout_cta": "Scrivi STOP per non ricevere piu' messaggi.",
    })
    riga = await db_session.scalar(select(WaCampaign).where(WaCampaign.id == camp.id))
    assert riga.optout_enabled is True
    assert riga.optout_cta == "Scrivi STOP per non ricevere piu' messaggi."


@pytest.mark.asyncio
async def test_marketing_senza_cta_rifiutata(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.crea_campagna(db_session, {
            "tenant_id": tenant.id, "wa_number_id": number.id, "name": "promo",
            "campaign_type": WaCampaignType.marketing, "template_a": "Ciao {nome}.",
            "optout_cta": "  ",
        })


def test_step_con_placeholder_ignoto_non_si_salva():
    with pytest.raises(ValueError) as exc:
        svc.valida_step("Ciao {nome}, ordine {ultimo_ordine}.", colonne_note=set())
    assert "ultimo_ordine" in str(exc.value)


def test_step_con_placeholder_coperto_dal_csv_si_salva():
    svc.valida_step("Ciao {nome}, ordine {ultimo_ordine}.",
                    colonne_note={"ultimo_ordine"})


def test_step_vuoto_non_si_salva():
    with pytest.raises(ValueError):
        svc.valida_step("   ", colonne_note=set())


@pytest.mark.asyncio
async def test_campagna_su_numero_di_un_altro_tenant_rifiutata(db_session):
    """Scoping: un numero appartiene a un tenant. Incrociarli e' il bug che
    manda i messaggi di un cliente dal numero di un altro."""
    tenant_a = await make_tenant(db_session, name="A")
    tenant_b = await make_tenant(db_session, name="B")
    number_b = await make_number(db_session, tenant_b)
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.crea_campagna(db_session, {
            "tenant_id": tenant_a.id, "wa_number_id": number_b.id, "name": "x",
            "campaign_type": WaCampaignType.followup, "template_a": "Ciao.",
        })


@pytest.mark.asyncio
async def test_optout_enabled_nel_payload_di_creazione_non_bypassa_il_gate_cta(db_session):
    """Trovato in review: un client poteva passare optout_enabled=False in
    creazione per una campagna marketing, bypassando sia il calcolo sia il
    gate CTA -- risultato: marketing senza via d'uscita, esattamente il
    danno che il contratto §2.1 esiste per impedire. optout_enabled ora e'
    SEMPRE calcolato in crea_campagna, il payload del chiamante e' ignorato."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.crea_campagna(db_session, {
            "tenant_id": tenant.id, "wa_number_id": number.id, "name": "exploit",
            "campaign_type": WaCampaignType.marketing, "template_a": "Ciao {nome}.",
            "optout_enabled": False,   # tentativo di bypass: deve essere ignorato
        })
