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
