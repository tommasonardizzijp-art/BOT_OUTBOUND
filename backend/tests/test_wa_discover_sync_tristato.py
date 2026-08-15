import pytest

from app.services.wa_discover import sincronizzazione


class _Locator:
    def __init__(self, quanti: int):
        self._quanti = quanti
        self.first = self

    async def count(self):
        return self._quanti

    async def click(self, timeout=None):
        return None


class _Pagina:
    def __init__(self, *, impostazioni_presenti: bool, testi: list[str]):
        self._loc = _Locator(1 if impostazioni_presenti else 0)
        self._testi = testi

    def locator(self, _sel):
        return self._loc

    async def wait_for_timeout(self, _ms):
        return None

    async def evaluate(self, _js):
        return self._testi

    async def keyboard_press(self, _tasto):
        return None


@pytest.fixture(autouse=True)
def niente_richiusura(monkeypatch):
    async def _ok(page):
        return True

    monkeypatch.setattr(sincronizzazione, "_richiudi_pannello", _ok)


@pytest.mark.asyncio
async def test_percentuale_presente_stato_letta():
    pagina = _Pagina(impostazioni_presenti=True,
                     testi=["Sincronizzazione dei messaggi piu' recenti 42%"])
    lettura = await sincronizzazione.leggi_sincronizzazione(pagina)
    assert lettura.stato == "letta"
    assert lettura.percentuale == 42


@pytest.mark.asyncio
async def test_impostazioni_aperto_senza_percentuale_stato_assente():
    # Sincronizzazione finita: WhatsApp non mostra piu' nessuna percentuale.
    # Questo e' il caso in cui SI DEVE procedere.
    pagina = _Pagina(impostazioni_presenti=True, testi=["Profilo", "Chat", "Notifiche"])
    lettura = await sincronizzazione.leggi_sincronizzazione(pagina)
    assert lettura.stato == "assente"
    assert lettura.percentuale is None


@pytest.mark.asyncio
async def test_impostazioni_non_trovato_stato_ignota():
    # Il caso del 14/08: il pulsante non c'e' nel DOM. Non significa
    # "sincronizzato", significa "non lo sappiamo".
    pagina = _Pagina(impostazioni_presenti=False, testi=[])
    lettura = await sincronizzazione.leggi_sincronizzazione(pagina)
    assert lettura.stato == "ignota"
    assert lettura.percentuale is None


def test_puo_scansionare_procede_su_assente():
    ok, _ = sincronizzazione.puo_scansionare_lettura(
        sincronizzazione.LetturaSync(stato="assente", percentuale=None), soglia=60)
    assert ok is True


def test_puo_scansionare_si_ferma_su_ignota():
    ok, motivo = sincronizzazione.puo_scansionare_lettura(
        sincronizzazione.LetturaSync(stato="ignota", percentuale=None), soglia=60)
    assert ok is False
    assert "ignota" in motivo or "non" in motivo


def test_puo_scansionare_si_ferma_sotto_soglia():
    ok, _ = sincronizzazione.puo_scansionare_lettura(
        sincronizzazione.LetturaSync(stato="letta", percentuale=42), soglia=60)
    assert ok is False


def test_puo_scansionare_procede_sopra_soglia():
    ok, _ = sincronizzazione.puo_scansionare_lettura(
        sincronizzazione.LetturaSync(stato="letta", percentuale=95), soglia=60)
    assert ok is True
