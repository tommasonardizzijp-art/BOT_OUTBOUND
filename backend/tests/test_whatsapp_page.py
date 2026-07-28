"""Test del POM WhatsApp Web (M1, Task 4). Nessun browser: tutto contro
oggetti finti o funzioni pure. L'E2E sul browser vero e' fuori scope di
questo task (fermato allo Step 7 del brief)."""
import pytest

from app.browser.whatsapp_page import classify_direction


@pytest.mark.parametrize("segnali,atteso", [
    # (aria_tu, tail_icon, data_id) -> "out" | "in"
    ((True,  "tail-out", "A5" + "x" * 30), "out"),   # tutti concordi: nostro
    ((False, "tail-in",  "3A" + "x" * 18), "in"),    # tutti concordi: loro
    ((False, None,       "A5" + "x" * 30), "out"),   # solo il data_id: basta
    ((False, None,       None),            "in"),    # nessun segnale -> inbound
    ((True,  "tail-in",  "A5" + "x" * 30), "in"),    # DISCORDANTI -> inbound
    ((False, "tail-in",  "A5" + "x" * 30), "in"),    # DISCORDANTI -> inbound
])
def test_direzione_in_dubbio_vale_inbound(segnali, atteso):
    """Asimmetria deliberata: un messaggio e' 'nostro' solo se ALMENO UN segnale
    dice OUT e NESSUNO dice IN.

    I due errori non costano uguale. Trattare un nostro messaggio come inbound
    = si legge qualcosa in piu' e al peggio non si invia. Trattare un loro
    messaggio come nostro = la guardia salta lo STOP e si scrive a chi aveva
    chiesto di smettere. Il secondo e' irreversibile.
    """
    aria_tu, tail_icon, data_id = segnali
    assert classify_direction(aria_tu=aria_tu, tail_icon=tail_icon, data_id=data_id) == atteso


@pytest.mark.asyncio
async def test_tail_none_quando_il_dom_non_aggancia_nulla(monkeypatch):
    """None (cecita') non e' [] (silenzio). Se un selettore si rompe e il POM
    tornasse [], il chiamante concluderebbe 'nessuno STOP' e invierebbe SEMPRE,
    sembrando funzionare. E' esattamente il bug che M0 ha evitato con la
    sentinella."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageSenzaBolle:
        async def evaluate(self, _script, *_a):
            return None

    pom = WhatsAppWebPage(PageSenzaBolle())
    assert await pom.read_inbound_tail() is None


@pytest.mark.asyncio
async def test_tail_vuota_e_diversa_da_tail_assente():
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageSenzaInbound:
        async def evaluate(self, _script, *_a):
            return []

    assert await WhatsAppWebPage(PageSenzaInbound()).read_inbound_tail() == []


@pytest.mark.asyncio
async def test_sync_state_unknown_finche_il_selettore_non_e_catalogato():
    """A9: WhatsApp Web non sincronizza tutte le chat subito. Su una chat non
    ancora sincronizzata la guardia non legge un silenzio, legge il VUOTO.

    Il selettore dell'indicatore non e' ancora catalogato: catturarlo richiede
    un re-scan del QR, che azzererebbe PoC-1. Quindi in M1 sync_state() esiste
    con la sua interfaccia e torna 'unknown', ed e' la POLITICA (M3) a decidere
    cosa fare di 'unknown'. Quello che NON si fa e' far finta che 'unknown'
    sia 'synced'."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageQualunque:
        async def evaluate(self, _s, *_a):
            return None

        def locator(self, _sel):
            raise AssertionError("nessun selettore catalogato: non si inventa")

    assert await WhatsAppWebPage(PageQualunque()).sync_state() == "unknown"
