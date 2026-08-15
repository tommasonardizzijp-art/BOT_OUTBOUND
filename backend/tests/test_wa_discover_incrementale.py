import pytest

from app.services import wa_discover_run
from app.services.wa_discover import pannello


class _PaginaFinta:
    """Pagina che registra se qualcuno ha provato ad aprire un pannello."""

    def __init__(self):
        self.aperture = 0


@pytest.fixture
def conta_aperture(monkeypatch):
    aperture = []

    async def _apri(page, titolo):
        aperture.append(titolo)
        # `salvabile` e' una property calcolata da `esito`, NON un campo del
        # costruttore: passarla esploderebbe con TypeError.
        return pannello.EsitoApertura(esito=pannello.ESITO_VERIFICATA,
                                      numero="+393331112223", testo_pannello="")

    monkeypatch.setattr(wa_discover_run.pannello, "apri_e_leggi", _apri)
    return aperture


@pytest.mark.asyncio
async def test_chat_gia_nota_non_apre_il_pannello(conta_aperture):
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "Mario Rossi", "titolo_e_numero": False},
        titoli_noti={"Mario Rossi"})

    assert decisione.saltata is True
    assert decisione.riga is None
    assert decisione.ha_aperto is False
    assert conta_aperture == []


@pytest.mark.asyncio
async def test_chat_sconosciuta_apre_il_pannello(conta_aperture):
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "Sconosciuto", "titolo_e_numero": False},
        titoli_noti={"Mario Rossi"})

    assert decisione.saltata is False
    assert decisione.riga is not None
    assert conta_aperture == ["Sconosciuto"]


@pytest.mark.asyncio
async def test_chat_col_numero_nel_titolo_si_salta_per_hmac(conta_aperture):
    # Il caso che il primo tentativo sul campo aveva mancato: 194 righe su 241
    # hanno il titolo mascherato a DB, quindi il confronto per titolo non
    # scatta mai. La chiave e' l'hmac.
    from app.utils.phone_pseudonym import hmac_phone

    # normalize_e164 (classifica.numero_dal_titolo, e lo stesso in
    # pannello.py:116) ritorna SEMPRE il numero senza '+' -- e' cosi' che
    # salvataggio.py lo scrive a DB (riga.numero, mai col prefisso). hmac_phone
    # non normalizza: l'hmac atteso va calcolato sulla stessa forma, altrimenti
    # non puo' mai combaciare con quello che il codice vero produce.
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "+39 334 802 8109", "titolo_e_numero": True},
        titoli_noti=set(), hmac_noti={hmac_phone("393348028109")})

    assert decisione.saltata is True
    assert decisione.riga is None
    assert conta_aperture == []


@pytest.mark.asyncio
async def test_numero_nel_titolo_MAI_visto_non_si_salta(conta_aperture):
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "+39 334 802 8109", "titolo_e_numero": True},
        titoli_noti=set(), hmac_noti=set())

    assert decisione.saltata is False
    assert decisione.riga is not None       # risolta dal titolo, senza aprire
    assert conta_aperture == []


@pytest.mark.asyncio
async def test_senza_titoli_noti_si_comporta_come_prima(conta_aperture):
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "Chiunque", "titolo_e_numero": False})

    assert decisione.saltata is False
    assert conta_aperture == ["Chiunque"]


@pytest.mark.asyncio
async def test_il_titolo_che_e_gia_il_numero_resta_gratis_e_non_e_un_salto(conta_aperture):
    # Il ramo esistente non deve diventare un "salto": la riga viene salvata,
    # e contarla come saltata falserebbe la copertura.
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "+39 333 111 2223", "titolo_e_numero": True},
        titoli_noti=set())

    assert decisione.saltata is False
    assert decisione.riga is not None
    assert conta_aperture == []


@pytest.mark.asyncio
async def test_le_righe_note_finiscono_in_saltate_gia_note_non_in_non_verificate(
        db_session, monkeypatch):
    from tests.factories_wa import make_discovered_chat, make_number, make_tenant

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await make_discovered_chat(db_session, tenant, number, chat_title="Nota")
    await db_session.commit()

    righe = [{"titolo": "Nota", "titolo_e_numero": False},
             {"titolo": "Nuova", "titolo_e_numero": True}]
    await _monta_scan_finto(monkeypatch, righe)

    esito = await wa_discover_run._esegui_scan(
        _PaginaFinta(), db=db_session, tenant_id=tenant.id, number_id=number.id)

    assert esito["saltate_gia_note"] == 1
    assert esito["non_verificate"] == 0
    assert esito["salvate"] == 1


async def _monta_scan_finto(monkeypatch, righe):
    """Sostituisce sidebar, gate e pause: qui si misura il conteggio, non il DOM."""
    async def _scan(page):
        return righe

    async def _totale(page):
        return len(righe)

    async def _scorri(page):
        class _Stato:
            al_fondo = True
        return _Stato()

    async def _sync(page):
        # Task 7: il gate e' tri-stato (LetturaSync), _esegui_scan non
        # chiama piu' leggi_percentuale. "letta" sopra soglia lascia
        # passare, stesso comportamento del vecchio _percentuale=100.
        from app.services.wa_discover.sincronizzazione import LetturaSync
        return LetturaSync(stato="letta", percentuale=100)

    async def _lista_ok(page):
        return True

    async def _niente(*a, **kw):
        return None

    monkeypatch.setattr(wa_discover_run.sidebar, "scan_sidebar", _scan)
    monkeypatch.setattr(wa_discover_run.sidebar, "totale_dichiarato", _totale)
    monkeypatch.setattr(wa_discover_run.sidebar, "scorri_sidebar", _scorri)
    monkeypatch.setattr(wa_discover_run, "leggi_sincronizzazione", _sync)
    monkeypatch.setattr(wa_discover_run, "lista_utilizzabile", _lista_ok)
    monkeypatch.setattr(wa_discover_run.asyncio, "sleep", _niente)


@pytest.mark.asyncio
async def test_selettore_impostazioni_rotto_NON_ferma_lo_scan(db_session, monkeypatch):
    """La regressione che questo test esiste per impedire.

    Il tri-stato del gate di sincronizzazione (Task 7) inizialmente rifiutava
    di scansionare quando lo stato era 'ignota'. Sembrava prudenza. Ma
    _SEL_IMPOSTAZIONI non matcha su questo WhatsApp Web -- verificato dal
    vivo il 15/08 su due sessioni distinte -- quindi la lettura e' SEMPRE
    'ignota', e quel rifiuto avrebbe spento il discover per intero: zero chat
    raccolte, su ogni numero, per sempre. Da guardia finta a sistema fermo.

    Nessuno dei test del tri-stato lo vedeva: giravano su una pagina finta
    dove il selettore funziona. Questo invece monta il caso reale -- gate che
    torna 'ignota' -- e pretende che lo scan faccia comunque il suo lavoro.
    """
    from tests.factories_wa import make_number, make_tenant

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    righe = [{"titolo": "+39 333 111 2223", "titolo_e_numero": True},
             {"titolo": "+39 333 111 2224", "titolo_e_numero": True}]
    await _monta_scan_finto(monkeypatch, righe)

    async def _sync_ignota(page):
        from app.services.wa_discover.sincronizzazione import LetturaSync
        return LetturaSync(stato="ignota", percentuale=None)

    monkeypatch.setattr(wa_discover_run, "leggi_sincronizzazione", _sync_ignota)

    esito = await wa_discover_run._esegui_scan(
        _PaginaFinta(), db=db_session, tenant_id=tenant.id, number_id=number.id)

    # Lo scan e' PARTITO e ha raccolto: e' l'invariante che conta.
    assert esito["salvate"] == 2, esito
    assert esito["motivo"] != "sync_ignota", esito
    # E lo stato ignoto resta scritto: si procede, ma non in silenzio.
    assert esito["sync_stato"] == "ignota", esito
