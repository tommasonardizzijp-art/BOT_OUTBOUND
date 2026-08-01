import pytest

from app.browser.whatsapp_page import OpenResult
from app.services import wa_sender


def _ok(signal: str) -> OpenResult:
    return OpenResult(True, 1234.0, signal)


def test_invia_solo_con_cronologia_agganciata():
    esito = wa_sender.valuta_apertura(_ok("cronologia:div[data-id]:37"))
    assert esito.puo_inviare is True
    assert esito.esito_contatto is None


def test_ok_true_ma_zero_messaggi_non_invia():
    """ok=True dice solo 'composer comparso'. Zero bolle agganciate = chat
    vuota o DOM che mente: in entrambi i casi non si scrive."""
    esito = wa_sender.valuta_apertura(_ok("cronologia:div[data-id]:0"))
    assert esito.puo_inviare is False


def test_conteggio_non_parsabile_non_invia():
    """Un segnale che non si sa leggere e' un segnale che dice no."""
    esito = wa_sender.valuta_apertura(_ok("cronologia:div[data-id]:molti"))
    assert esito.puo_inviare is False
    assert esito.colpa_nostra is True


@pytest.mark.parametrize("signal,atteso", [
    ("nessuna-cronologia:nessun-messaggio-nel-pannello", "skipped"),
    ("nessuna-cronologia:sezione-chat-vuota:nessuna-conversazione-esistente", "skipped"),
    ("nessuna-cronologia:nessuna-sezione-chat:solo-gruppi-o-contatti-senza-conversazione", "skipped"),
])
def test_chat_inesistente_e_colpa_del_contatto_non_nostra(signal, atteso):
    esito = wa_sender.valuta_apertura(OpenResult(False, 1.0, signal))
    assert esito.puo_inviare is False
    assert esito.esito_contatto == atteso
    assert esito.motivo == "no_existing_chat"
    assert esito.colpa_nostra is False


@pytest.mark.parametrize("signal", [
    "nessuna-cronologia:casella-ricerca-non-trovata",
    "nessuna-cronologia:ricerca-non-svuotata",
    "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio",
])
def test_guasti_nostri_non_bruciano_il_contatto(signal):
    """Un selettore rotto non deve bruciare una lista (SDD 11): il contatto
    resta queued, e' il NUMERO che si ferma."""
    esito = wa_sender.valuta_apertura(OpenResult(False, 1.0, signal))
    assert esito.puo_inviare is False
    assert esito.esito_contatto is None      # nessuna transizione di stato
    assert esito.colpa_nostra is True


def test_nessun_risultato_di_ricerca_e_ambiguo_e_non_decide_da_solo():
    """Puo' essere un numero non su WhatsApp o una ricerca rotta: chi
    chiama decide con il contesto della sessione (contratto §3.3)."""
    esito = wa_sender.valuta_apertura(
        OpenResult(False, 1.0, "nessuna-cronologia:nessun-risultato-di-ricerca"))
    assert esito.puo_inviare is False
    assert esito.esito_contatto is None
    assert esito.motivo == "ricerca_senza_risultati"
    assert esito.colpa_nostra is False


def test_segnale_sconosciuto_e_trattato_come_colpa_nostra():
    """Un segnale che il POM non produce oggi (versione futura, bug) non
    deve mai finire nel ramo 'skipped': si ferma il numero, non si brucia
    il contatto."""
    esito = wa_sender.valuta_apertura(OpenResult(False, 1.0, "boh:qualcosa-di-nuovo"))
    assert esito.puo_inviare is False
    assert esito.esito_contatto is None
    assert esito.colpa_nostra is True


class _PomFinto:
    """Doppio del POM: nessun browser. Ogni test costruisce lo scenario
    dichiarando cosa 'vede' il DOM."""
    def __init__(self, tail, *, history_ok=True, count=30, sync="unknown"):
        self._tail = tail
        self._history_ok = history_ok
        self._count = count
        self._sync = sync
        self.load_history_chiamata = False

    async def load_history(self, minimo: int = 80):
        from app.browser.whatsapp_page import HistoryInfo
        self.load_history_chiamata = True
        return HistoryInfo(ok=self._history_ok, before=0, after=self._count,
                           rounds=1, exhausted=True)

    async def read_inbound_tail(self, n: int = 40):
        return self._tail

    async def sync_state(self):
        return self._sync


@pytest.mark.asyncio
async def test_guardia_blocca_su_stop_in_coda():
    pom = _PomFinto(["ciao", "STOP", "ah no scusa"])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "optout"
    assert "STOP" in esito.prova


@pytest.mark.asyncio
async def test_guardia_blocca_su_stop_seguito_da_altri_messaggi():
    """Uno STOP seguito da altro NON diventa invisibile: la coda si legge
    tutta, non ci si ferma al primo messaggio."""
    pom = _PomFinto(["STOP", "cmq grazie", "buona giornata"])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False


@pytest.mark.asyncio
async def test_guardia_blocca_su_cecita_del_dom():
    """None = nessuna bolla agganciata. NON e' 'nessuno STOP'."""
    pom = _PomFinto(None)
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "coda_non_agganciata"


@pytest.mark.asyncio
async def test_guardia_passa_su_silenzio_vero():
    """[] = bolle presenti, nessun inbound: questo si', si invia."""
    pom = _PomFinto([])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is True


@pytest.mark.asyncio
async def test_guardia_carica_sempre_la_cronologia_prima_di_leggere():
    pom = _PomFinto([])
    await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert pom.load_history_chiamata is True


@pytest.mark.asyncio
async def test_quarantena_post_riconnessione_blocca(monkeypatch):
    """Nei primi minuti dopo l'avvio del browser la sincronizzazione e'
    ancora in corso e la guardia leggerebbe il vuoto (A9/FM16)."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 15)
    pom = _PomFinto([])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=60)
    assert esito.puo_inviare is False
    assert esito.motivo == "quarantena_risync"


@pytest.mark.asyncio
async def test_incoerenza_db_dom_blocca(monkeypatch):
    """Il DB dice che a questo contatto avevamo gia' scritto, il DOM mostra
    zero messaggi: il DOM sta mentendo (chat non sincronizzata)."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    pom = _PomFinto([], count=0)
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=True, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "incoerenza_db_dom"


@pytest.mark.asyncio
async def test_sync_state_synced_non_e_richiesto_ma_syncing_blocca(monkeypatch):
    """Oggi sync_state torna sempre 'unknown' (selettore non catalogato):
    'unknown' non blocca da solo. Ma se un giorno tornera' 'syncing', quello
    deve bloccare senza altre modifiche."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    pom = _PomFinto([], sync="syncing")
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "sincronizzazione_in_corso"
