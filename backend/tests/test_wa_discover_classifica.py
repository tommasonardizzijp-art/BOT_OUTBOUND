import pytest

from app.services.wa_discover.classifica import (
    numero_dal_titolo, tipo_da_segnali, titolo_e_numero,
)


# Titoli veri letti dalla sidebar di Primero l'11/08 (PoC-5). Non inventati.
GRUPPI_VERI = [
    "AZIENDA AGRICOLA PRIMERO", "CONSEGNE DOMICILIO", "ORDINI VENDITORI PRIMERO",
    "SPEDIZIONI", "( INFO E BROADCAST)AMICI DELLA GALASSIA", "Primero Sales Team",
]
PERSONE_VERE = ["Fulvio", "Alessio Tutti A Tavola", "Michele Carrozza 🛺", "Mamma"]


@pytest.mark.parametrize("titolo", ["+39 342 146 0077", "+393421460077", "+1 (555) 978-5671"])
def test_riconosce_il_titolo_che_e_un_numero(titolo):
    assert titolo_e_numero(titolo) is True
    assert numero_dal_titolo(titolo) is not None


@pytest.mark.parametrize("titolo", PERSONE_VERE + GRUPPI_VERI)
def test_un_nome_non_e_un_numero(titolo):
    assert titolo_e_numero(titolo) is False
    assert numero_dal_titolo(titolo) is None


def test_titolo_con_cifre_dentro_non_e_un_numero():
    """'4 cessi' e 'I Sopra Savo' sono titoli veri: contengono cifre ma non sono
    numeri di telefono. Un match troppo largo li cifrerebbe come tali."""
    for titolo in ["4 cessi 🚽", "Cuba 2 e bachata 2", "FYS - Comunicazioni ed eventi"]:
        assert titolo_e_numero(titolo) is False


def test_numero_non_leggibile_significa_ignoto_non_gruppo():
    """La proposta dell'handoff era di trattare 'numero non leggibile' come
    proxy di 'gruppo'. Qui si registra ma NON si asserisce come certezza: il
    tri-stato dice 'ignoto', perche' il PoC-4 ha misurato che il pannello
    fallisce nel 5% dei casi anche su chat 1:1 vere. Chiamarle 'gruppo'
    scarterebbe persone contattabili senza lasciare traccia del dubbio."""
    assert tipo_da_segnali(numero_leggibile=False, testo_pannello="", titolo="Fulvio") == "ignoto"


def test_numero_leggibile_significa_individuale():
    assert tipo_da_segnali(numero_leggibile=True, testo_pannello="+39 342 146 0077",
                           titolo="Fulvio") == "individuale"


def test_partecipanti_nel_pannello_significa_gruppo():
    """Segnale ad alta precisione ma bassa recall (1/6, PoC-4): quando c'e' si
    crede, quando manca non si conclude niente."""
    assert tipo_da_segnali(numero_leggibile=False, testo_pannello="12 partecipanti",
                           titolo="SPEDIZIONI") == "gruppo"


# --- etichetta_visibile: il caso che cade fra titolo_e_numero e numero_dal_titolo ---

@pytest.mark.parametrize("titolo", [
    "+39 342 146 0077 99 88 77",     # troppe cifre per E.164
    "123456789012345678",
    "00 12 34 56 78 90 12 34 56 78",
])
def test_titolo_che_sembra_numero_ma_non_normalizza_ha_comunque_un_etichetta(titolo):
    """Misurati l'11/08: titoli che superano titolo_e_numero ma da cui
    numero_dal_titolo non ricava nulla.

    Senza etichetta, quella riga andrebbe a DB con chat_title=None (P12) E
    phone_hmac=None: le due unique di wa_discovered_chats sono cieche sui NULL,
    quindi si duplicherebbe a ogni ri-scansione senza avere nessuna identita'.
    """
    from app.services.wa_discover.classifica import etichetta_visibile

    assert titolo_e_numero(titolo) is True
    assert numero_dal_titolo(titolo) is None

    etichetta = etichetta_visibile(titolo, numero_dal_titolo(titolo))
    assert etichetta
    # Mascherata: le cifre centrali non ci sono piu'.
    assert "\u2022" in etichetta
    assert titolo.replace(" ", "") not in etichetta.replace(" ", "")


def test_etichetta_di_un_nome_vero_resta_il_nome():
    from app.services.wa_discover.classifica import etichetta_visibile

    assert etichetta_visibile("Fulvio CBD", None) == "Fulvio CBD"
    assert etichetta_visibile("  AZIENDA AGRICOLA PRIMERO  ", None) == "AZIENDA AGRICOLA PRIMERO"


def test_etichetta_di_un_numero_vero_e_mascherata_e_stabile():
    """Deterministica fra due scansioni: e' cio' che permette alla unique sul
    titolo di deduplicare anche queste righe."""
    from app.services.wa_discover.classifica import etichetta_visibile

    numero = numero_dal_titolo("+39 342 146 0077")
    prima = etichetta_visibile("+39 342 146 0077", numero)
    dopo = etichetta_visibile("+39 342 146 0077", numero)
    assert prima == dopo
    assert "3421460" not in prima


def test_etichetta_vuota_se_non_ce_niente():
    from app.services.wa_discover.classifica import etichetta_visibile

    assert etichetta_visibile(None, None) is None
    assert etichetta_visibile("   ", None) is None
