import pytest

from app.services.wa_discover.sincronizzazione import (
    percentuale_da_testi, puo_scansionare,
)


def test_legge_la_percentuale_dal_pannello():
    assert percentuale_da_testi(["Sincronizzazione messaggi", "47%"]) == 47
    assert percentuale_da_testi(["Sincronizzazione in corso... 8 %"]) == 8


def test_nessuna_percentuale_significa_non_lo_so():
    """None non e' zero. Un pannello che non espone la percentuale (o l'ha gia'
    tolta perche' ha finito) non deve diventare '0%' e bloccare tutto per
    sempre: la decisione su cosa fare con l'incertezza sta in puo_scansionare."""
    assert percentuale_da_testi(["Impostazioni", "Account", "Privacy"]) is None
    assert percentuale_da_testi([]) is None


def test_percentuale_impossibile_viene_scartata():
    """Un '2026%' o un '150%' viene da un match sbagliato, non da WhatsApp."""
    assert percentuale_da_testi(["IT01879020517A2026%"]) is None
    assert percentuale_da_testi(["150%"]) is None


def test_sopra_soglia_si_parte():
    ok, motivo = puo_scansionare(72, soglia=60)
    assert ok is True
    assert "72" in motivo


def test_sotto_soglia_non_si_parte():
    ok, motivo = puo_scansionare(31, soglia=60)
    assert ok is False
    assert "31" in motivo and "60" in motivo


def test_percentuale_ignota_si_parte_ma_lo_si_dice():
    """Decisione presa: l'incertezza non blocca. La percentuale sparisce anche
    quando la sincronizzazione E' FINITA, e trattare 'non lo so' come 'fermo'
    renderebbe la Fase A inavviabile proprio nel caso normale. Ma il motivo
    deve dirlo, perche' una raccolta parziale va diagnosticata da qui."""
    ok, motivo = puo_scansionare(None, soglia=60)
    assert ok is True
    assert "non" in motivo.lower()


# --- La percentuale va ANCORATA al contesto di sincronizzazione ---

def test_una_percentuale_in_un_messaggio_di_chat_non_e_la_sincronizzazione():
    """Il caso che rende il gate pericoloso, non un caso di scuola.

    I testi arrivano dal DOM, e il DOM di WhatsApp Web contiene le anteprime dei
    messaggi. Un cliente che ha in chat 'sconto 50%' farebbe leggere al gate una
    sincronizzazione al 50%: sotto soglia, quindi la Fase A non partirebbe mai --
    e il motivo cambierebbe da cliente a cliente, in silenzio.
    """
    testi = ["Chat", "Impostazioni", "Fulvio: sconto 50% sul prossimo ordine",
             "Mamma: e' finita al 100% ieri"]
    assert percentuale_da_testi(testi) is None


def test_la_percentuale_si_legge_quando_il_pannello_parla_di_sincronizzazione():
    """I due nodi veri, misurati l'11/08: sono separati, e la percentuale sta
    nel secondo."""
    testi = ["Sincronizzazione dei messaggi precedenti in corso", "Completata al 61%"]
    assert percentuale_da_testi(testi) == 61


def test_funziona_anche_in_inglese():
    """Il censimento dell'inbox Instagram ha trovato interfaccia inglese su un
    account trattato come italiano: la lingua non si assume."""
    assert percentuale_da_testi(["Syncing older messages", "23% complete"]) == 23


def test_fra_piu_percentuali_vince_quella_del_contesto_giusto():
    """Un messaggio con una percentuale non deve vincere sulla riga vera."""
    testi = ["Fulvio: sconto 50%", "Sincronizzazione dei messaggi precedenti in corso",
             "Completata al 87%"]
    assert percentuale_da_testi(testi) == 87


# --- La chiusura del pannello deve essere VERIFICATA, non sperata ---

class _PaginaImpostazioni:
    """Simula il pannello Impostazioni di WhatsApp Web.

    `escape_necessari` dice quanti Escape servono davvero per chiuderlo: sul DOM
    reale (misurato l'11/08) UNO non basta -- il pannello resta aperto, e a volte
    si finisce in una sottopagina (drawer-title-privacy). Finche' resta aperto,
    la sidebar mostra 5 righe su 65 e nessun click apre una chat: un giro intero
    di scansione va perso.
    """

    class _Keyboard:
        def __init__(self, pagina):
            self._p = pagina

        async def press(self, tasto):
            if tasto == "Escape" and self._p.aperto:
                self._p.escape_ricevuti += 1
                if self._p.escape_ricevuti >= self._p.escape_necessari:
                    self._p.aperto = False

    class _Locator:
        def __init__(self, pagina):
            self._p = pagina

        @property
        def first(self):
            return self

        async def count(self):
            return 1

        async def click(self, **kw):
            self._p.aperto = True

    def __init__(self, *, testi, escape_necessari=1):
        self.testi = testi
        self.aperto = False
        self.escape_ricevuti = 0
        self.escape_necessari = escape_necessari
        self.keyboard = self._Keyboard(self)

    def locator(self, selettore):
        return self._Locator(self)

    async def evaluate(self, script, *args):
        if "pane-side" in script:
            return True
        return self.testi if self.aperto else []

    async def reload(self, **kwargs):
        """Il reload riporta la UI allo stato iniziale: e' la garanzia su cui si
        appoggia _richiudi_pannello quando gli Escape non bastano."""
        self.aperto = False

    async def wait_for_timeout(self, ms):
        return None


@pytest.mark.asyncio
async def test_il_pannello_viene_chiuso_anche_se_un_escape_non_basta():
    """Il difetto che ha fatto fallire il collaudo dell'11/08.

    leggi_percentuale apriva Impostazioni e lo chiudeva con UN Escape cieco.
    Sul DOM reale quell'Escape non chiudeva niente: il pannello restava aperto
    per tutto il giro, la sidebar mostrava 5 righe su 65 e nessuna chat si
    apriva. Il gate scritto per proteggere lo scan era la cosa che lo rompeva.
    """
    from app.services.wa_discover.sincronizzazione import leggi_percentuale

    pagina = _PaginaImpostazioni(
        testi=["Sincronizzazione dei messaggi precedenti in corso", "Completata al 61%"],
        escape_necessari=3,
    )
    assert await leggi_percentuale(pagina) == 61
    assert pagina.aperto is False, (
        "il pannello e' rimasto aperto: il resto del giro di scansione sarebbe perso"
    )


@pytest.mark.asyncio
async def test_pannello_che_non_si_chiude_proprio_non_blocca_la_lettura():
    """Se il pannello non si chiude nemmeno insistendo, la percentuale letta si
    restituisce lo stesso -- ma il chiamante deve poterlo sapere: e' compito del
    log, non di un'eccezione che farebbe perdere anche il dato."""
    from app.services.wa_discover.sincronizzazione import leggi_percentuale

    pagina = _PaginaImpostazioni(
        testi=["Sincronizzazione in corso", "Completata al 42%"],
        escape_necessari=99,
    )
    assert await leggi_percentuale(pagina) == 42
