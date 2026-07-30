import pytest

from app.api import wa_numbers
from app.models.wa import WaNumberStatus
from tests.factories_wa import make_number, make_tenant


@pytest.mark.asyncio
async def test_riattivazione_porta_a_pending_qr_non_ad_active(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.retired)
    n.sent_today, n.sent_date, n.warmup_day = 57, "2026-07-01", 7
    await db_session.commit()

    await wa_numbers.riattiva(n.id, motivo="numero rientrato dal cliente", db=db_session)
    await db_session.refresh(n)
    assert n.status == WaNumberStatus.pending_qr
    assert n.sent_today == 0 and n.sent_date is None and n.warmup_day == 1
    assert "numero rientrato dal cliente" in (n.notes or "")


@pytest.mark.asyncio
async def test_riattivazione_senza_motivo_rifiutata(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.suspended)
    await db_session.commit()
    with pytest.raises(Exception):
        await wa_numbers.riattiva(n.id, motivo="   ", db=db_session)


@pytest.mark.asyncio
async def test_riattivazione_su_numero_attivo_e_un_errore_non_un_no_op(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.active)
    await db_session.commit()
    with pytest.raises(Exception):
        await wa_numbers.riattiva(n.id, motivo="tanto per", db=db_session)


@pytest.mark.asyncio
async def test_il_numero_non_e_mai_esposto_in_chiaro(db_session):
    tenant = await make_tenant(db_session)
    await make_number(db_session, tenant, e164="+393421460077")
    await db_session.commit()
    elenco = await wa_numbers.lista(db=db_session)
    testo = str(elenco)
    assert "3421460077" not in testo
    assert "•" in testo


@pytest.mark.asyncio
async def test_patch_non_puo_scrivere_i_contatori_di_runtime(db_session):
    """Contratto §4.1: sent_today/sent_date/warmup_day sono di M3 in
    scrittura (tranne l'azzeramento in riattivazione). Un PATCH che li
    accetta e' una violazione del contratto, non una comodita'."""
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.sent_today = 5
    await db_session.commit()
    await wa_numbers.aggiorna(n.id, {"label": "nuovo nome", "sent_today": 0},
                              db=db_session)
    await db_session.refresh(n)
    assert n.label == "nuovo nome"
    assert n.sent_today == 5      # ignorato, non applicato


@pytest.mark.asyncio
async def test_crea_con_numero_malformato_non_stampa_il_numero_in_chiaro(db_session):
    """Trovato in review: crea() faceva raise HTTPException(422, str(exc)),
    e PhoneNormalizationError porta il numero grezzo nel proprio messaggio
    (stesso rischio di wa_ingest, contratto §2.3)."""
    tenant = await make_tenant(db_session)
    with pytest.raises(Exception) as exc:
        await wa_numbers.crea(
            {"tenant_id": tenant.id, "label": "N", "numero": "ABC123NONVALIDO456"},
            db=db_session)
    assert "ABC123NONVALIDO456" not in str(exc.value)
