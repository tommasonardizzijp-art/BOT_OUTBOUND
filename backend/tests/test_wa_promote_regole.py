"""Task 1 della Fase B: la decisione pura "promuovibile o no, e perche'".

Nessun I/O, nessun accesso DB -- si istanzia `WaDiscoveredChat` come un
oggetto Python qualunque (mai `.add()`/`.commit()`), esattamente come fa
`test_wa_discover_modello.py` prima di toccare la sessione. Si riusa il
modello vero invece di un dataclass proprio perche' la firma di
`promuovibile` in `regole.py` e' tipizzata su `WaDiscoveredChat` (piano Fase
B, Task 1): un tipo di test diverso dal tipo di produzione romperebbe quella
promessa senza che nessun test se ne accorga.
"""
import uuid

from app.models.wa import WaDiscoveredChat
from app.services.wa_promote.regole import promuovibile


def _riga(*, tipo_chat="individuale", status="nuovo",
          phone_hmac="hmac-test", encrypted_phone="enc-test",
          **overrides) -> WaDiscoveredChat:
    """Una riga di `wa_discovered_chats` mai scritta a DB.

    Default: individuale, nuovo, con numero -- cioe' il caso promuovibile.
    Ogni test sovrascrive solo il campo che gli interessa.
    """
    campi = dict(
        id=str(uuid.uuid4()),
        tenant_id="tenant-test",
        number_id="number-test",
        chat_title="Chat Test",
        display_name="Chat Test",
        encrypted_phone=encrypted_phone,
        phone_hmac=phone_hmac,
        numero_leggibile=phone_hmac is not None,
        tipo_chat=tipo_chat,
        status=status,
    )
    campi.update(overrides)
    return WaDiscoveredChat(**campi)


def test_gruppo_escluso_anche_con_numero():
    """Vincolo globale del piano: mai promuovere un gruppo, anche se per un
    bug/urto il numero di un partecipante e' finito nella riga."""
    riga = _riga(tipo_chat="gruppo", status="nuovo",
                 phone_hmac="x", encrypted_phone="y")
    esito = promuovibile(riga)
    assert esito.ok is False
    assert esito.motivo == "gruppo"


def test_senza_numero_escluso():
    riga = _riga(tipo_chat="individuale", status="nuovo",
                 phone_hmac=None, encrypted_phone=None)
    esito = promuovibile(riga)
    assert esito.ok is False
    assert esito.motivo == "senza_numero"


def test_gia_promosso_escluso_non_si_ripromuove():
    """status non torna mai indietro: una riga gia' promossa non si
    ripropone come promuovibile."""
    riga = _riga(tipo_chat="individuale", status="promosso",
                 phone_hmac="x", encrypted_phone="y")
    esito = promuovibile(riga)
    assert esito.ok is False
    assert esito.motivo == "gia_promosso"


def test_scartato_escluso():
    riga = _riga(tipo_chat="individuale", status="scartato",
                 phone_hmac="x", encrypted_phone="y")
    esito = promuovibile(riga)
    assert esito.ok is False
    assert esito.motivo == "scartato"


def test_ignoto_con_numero_e_promuovibile():
    """Tri-stato: 'non lo so' non e' 'gruppo'. Il backend non blocca
    'ignoto' -- solo 'gruppo' e' escluso per costruzione."""
    riga = _riga(tipo_chat="ignoto", status="nuovo",
                 phone_hmac="x", encrypted_phone="y")
    esito = promuovibile(riga)
    assert esito.ok is True
    assert esito.motivo is None


def test_individuale_con_numero_e_promuovibile():
    riga = _riga(tipo_chat="individuale", status="nuovo",
                 phone_hmac="x", encrypted_phone="y")
    esito = promuovibile(riga)
    assert esito.ok is True
    assert esito.motivo is None


def test_status_diverso_da_nuovo_vince_su_gruppo():
    """Ordine dei controlli: status non-nuovo e' il PRIMO check. Una riga
    gruppo gia' promossa (non dovrebbe mai capitare per l'invariante di
    dominio, ma la funzione deve comunque avere un solo motivo, non due)
    riporta 'gia_promosso', non 'gruppo'."""
    riga = _riga(tipo_chat="gruppo", status="promosso",
                 phone_hmac="x", encrypted_phone="y")
    esito = promuovibile(riga)
    assert esito.ok is False
    assert esito.motivo == "gia_promosso"


def test_senza_numero_ma_gia_scartato_riporta_scartato_non_senza_numero():
    """Stesso principio: status vince sempre come primo check."""
    riga = _riga(tipo_chat="individuale", status="scartato",
                 phone_hmac=None, encrypted_phone=None)
    esito = promuovibile(riga)
    assert esito.ok is False
    assert esito.motivo == "scartato"


def test_solo_encrypted_phone_mancante_e_comunque_senza_numero():
    """Le due colonne vanno scritte insieme (salvataggio.py): se una manca
    la riga e' comunque trattata come 'senza numero', non promuovibile a
    meta'."""
    riga = _riga(tipo_chat="individuale", status="nuovo",
                 phone_hmac="x", encrypted_phone=None)
    esito = promuovibile(riga)
    assert esito.ok is False
    assert esito.motivo == "senza_numero"


def test_solo_phone_hmac_mancante_e_comunque_senza_numero():
    riga = _riga(tipo_chat="individuale", status="nuovo",
                 phone_hmac=None, encrypted_phone="y")
    esito = promuovibile(riga)
    assert esito.ok is False
    assert esito.motivo == "senza_numero"
