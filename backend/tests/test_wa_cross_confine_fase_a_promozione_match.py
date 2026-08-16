"""§6 dell'AVVIO 12/08: il test che manca fra i tre moduli del canale WhatsApp.

Prima di questo file, ogni lato era testato SOLO contro la propria
convenzione: `test_wa_reply_watcher.py` costruisce il `WaContact` scrivendo a
mano `hmac_phone("393331234567")` ("come scrive la Fase A", ma senza
chiamare la Fase A), e i test di `wa_discover/salvataggio.py` asseriscono la
forma senza '+' dal loro lato, isolatamente. Nessun test attraversava il
confine reale Fase A (`salva_scoperta`) -> Fase B (`promuovi`) ->
reply-watcher (`match_contact`) chiamando le tre funzioni vere in sequenza --
ed e' esattamente li' che il difetto dell'hmac (due forme, mai riconciliate)
e' vissuto indisturbato: la docstring di `promozione.py` affermava che le due
forme coincidevano, falsa, e nessun test la contraddiceva.
"""
import pytest
from sqlalchemy import select

from app.browser.whatsapp_page import ChatRow
from app.models.wa import WaDiscoveredChat, WaMatchedBy
from app.services.wa_discover.classifica import RigaScoperta, TIPO_INDIVIDUALE
from app.services.wa_discover.salvataggio import salva_scoperta
from app.services.wa_promote.promozione import promuovi
from app.services.wa_reply_watcher import match_contact
from tests.factories_wa import make_number, make_tenant


def _row(title, *, title_is_number=False, preview="ciao", unread=1):
    return ChatRow(position=0, title=title, title_is_number=title_is_number,
                   unread_count=unread, preview=preview, last_is_outbound=False,
                   outgoing_state=None, muted=False)


@pytest.mark.asyncio
async def test_fase_a_promuovi_match_contact_stesso_titolo_numero(db_session):
    """Fase A salva una WaDiscoveredChat da un titolo-numero -> promuovi() ->
    match_contact con lo stesso titolo deve tornare WaMatchedBy.phone.

    Le tre funzioni sono quelle VERE (non un WaContact costruito a mano):
    se una delle tre smette di essere compatibile con le altre due sulla
    forma dell'hmac, questo test lo vede."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)

    titolo = "+39 333 1234567"
    riga = RigaScoperta(titolo=titolo, numero="393331234567",
                        numero_leggibile=True, tipo=TIPO_INDIVIDUALE)
    esito = await salva_scoperta(db_session, tenant.id, numero.id, riga)
    assert esito == "creata"

    scoperta = await db_session.scalar(
        select(WaDiscoveredChat).where(WaDiscoveredChat.number_id == numero.id))
    assert scoperta is not None, "la Fase A non ha salvato nulla"

    report = await promuovi(db_session, tenant_id=tenant.id, ids=[scoperta.id])
    assert report.promossi == 1
    assert report.contatti_creati == 1
    assert len(report.contatti_promossi_ids) == 1

    row = _row(titolo, title_is_number=True)
    trovato, via = await match_contact(db_session, tenant.id, row)
    assert trovato is not None, "il contatto promosso dalla Fase A e' invisibile al reply-watcher"
    assert trovato.id == report.contatti_promossi_ids[0]
    assert via == WaMatchedBy.phone


@pytest.mark.asyncio
async def test_fase_a_ritrova_la_riga_gia_scoperta_alla_riscansione(db_session):
    """Stesso confine, ma la seconda scansione: salva_scoperta deve
    aggiornare la riga esistente (stesso number_id + hmac), non duplicarla --
    altrimenti promuovi() vedrebbe due righe 'nuovo' per lo stesso numero."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)

    titolo = "+39 333 7654321"
    riga = RigaScoperta(titolo=titolo, numero="393337654321",
                        numero_leggibile=True, tipo=TIPO_INDIVIDUALE)
    assert await salva_scoperta(db_session, tenant.id, numero.id, riga) == "creata"
    assert await salva_scoperta(db_session, tenant.id, numero.id, riga) == "aggiornata"

    righe = (await db_session.execute(
        select(WaDiscoveredChat).where(WaDiscoveredChat.number_id == numero.id))
    ).scalars().all()
    assert len(righe) == 1, "la ri-scansione ha duplicato la riga invece di aggiornarla"
