"""End-to-end del canale WhatsApp: da `avvia()` a `send_text` chiamato.

Questo file esiste perche' fino a M5.1 **nessun test attraversava la giunzione
worker/sender**, ed e' esattamente li' che vivevano i due difetti che
impedivano ogni invio:

- i test del worker (test_wa_worker.py) esercitano la mini-sessione con un
  `invia_a_contatto` finto, quindi le guardie vere non girano mai;
- i test del sender (test_wa_sender.py) esercitano le guardie con un tempo
  finto -- `browser_avviato_da_s=9999` oppure `wa_resync_quarantine_min=0`, in
  27 casi su 27 -- quindi la quarantena non viene mai attraversata.

Ogni suite e' corretta; la loro unione aveva un buco della forma esatta del
bug. Qui l'UNICO doppio e' il browser (POM e apertura del contesto). Worker,
sender, guardie, cap, template, timing e **configurazione** sono quelli veri.
Il tempo e' virtuale: `sleep` non dorme, fa avanzare un orologio.

Regola da non violare mai in questo file: **orologio finto, config vera.**
Se un giorno un test qui dentro comincia con `monkeypatch.setattr(settings,
"wa_resync_quarantine_min", 0)`, il file ha perso il suo scopo.
"""
from contextlib import asynccontextmanager

import pytest

from app.browser.whatsapp_page import HistoryInfo, OpenResult
from tests.factories_wa import (make_campaign, make_campaign_contact,
                                make_contact, make_number, make_tenant)
from tests.helpers_wa_tempo import orologio_virtuale


class PomFinto:
    """Risponde come una chat sana: cronologia presente, nessun inbound,
    nessuno STOP. Se l'invio non parte, non e' colpa del DOM."""

    def __init__(self):
        self.inviati: list[str] = []
        self.chat_aperte: list[str] = []

    async def open_chat(self, e164):
        self.chat_aperte.append(e164)
        return OpenResult(True, 1200.0, "cronologia:div[data-id]:42")

    async def sync_state(self):
        return "unknown"

    async def load_history(self, minimo=80):
        return HistoryInfo(ok=True, before=42, after=120, rounds=3, exhausted=True)

    async def read_inbound_tail(self, n=40):
        return []

    async def send_text(self, testo):
        self.inviati.append(testo)

    async def read_last_tick(self):
        return "Consegnato"

    async def scan_chat_list(self):
        return []


def _doppi_del_browser(monkeypatch, pom: PomFinto) -> None:
    """Sostituisce SOLO cio' che sta fuori dal processo: il browser e il
    lucchetto Redis. Tutto il resto e' codice vero."""
    from app.workers import wa_worker

    @asynccontextmanager
    async def _contesto_finto(number_id, *, headless, proxy_url):
        class Pagina:
            async def goto(self, url, **kw):
                return None

        class Contesto:
            async def new_page(self):
                return Pagina()

        yield Contesto()

    monkeypatch.setattr(wa_worker, "_open_wa_browser", _contesto_finto)
    monkeypatch.setattr(wa_worker, "WhatsAppWebPage", lambda page: pom)

    @asynccontextmanager
    async def _lock_libero(number_id, **kw):
        yield "token-e2e"

    async def _renew(number_id, token, **kw):
        return True

    monkeypatch.setattr(wa_worker.wa_profile_lock, "held", _lock_libero)
    monkeypatch.setattr(wa_worker.wa_profile_lock, "renew", _renew)


@pytest.mark.asyncio
async def test_e2e_dall_avvio_al_messaggio_inviato(db_session, monkeypatch):
    from app.config import settings
    from app.models.wa import (WaCampaignStatus, WaContactStatus, WaMessage,
                               WaMessageStatus)
    from app.services import wa_campaign_service as svc
    from app.workers import wa_worker
    from sqlalchemy import select

    # --- configurazione VERA: nessun ritocco ai parametri del canale --------
    assert settings.wa_resync_quarantine_min == 15
    assert settings.wa_send_delay_median_s == 90
    monkeypatch.setattr(settings, "wa_send_enabled", True)   # e' il Cancello 0

    orologio = orologio_virtuale(wa_worker, monkeypatch)

    pom = PomFinto()
    _doppi_del_browser(monkeypatch, pom)

    accodate: list[str] = []

    async def _finta_enqueue(campaign_id):
        accodate.append(campaign_id)
        return 1
    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers", _finta_enqueue)

    # Ora locale dentro la finestra attiva (09:30-19:30): il fuso della
    # macchina che esegue i test non deve decidere se il test passa.
    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: 11)

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    campagna, _step = await make_campaign(db_session, tenant, numero)
    contatto = await make_contact(db_session, tenant)
    cc = await make_campaign_contact(db_session, campagna, contatto)
    await db_session.commit()

    # --- 1. avviare la campagna ACCODA il worker (blocco B2) ---------------
    avviata = await svc.avvia(db_session, campagna.id)
    assert avviata.status == WaCampaignStatus.running
    assert accodate == [campagna.id], "avviare una campagna deve accodare il worker"

    # --- 2. la mini-sessione attraversa la quarantena e INVIA (blocco B1) --
    esito = await wa_worker.esegui_mini_sessione(numero.id)

    assert orologio["t"] >= settings.wa_resync_quarantine_min * 60, (
        "la quarantena dev'essere stata attraversata davvero, non saltata")
    assert esito["inviati"] == 1, f"nessun messaggio inviato: {esito}"
    assert pom.inviati, "send_text non e' mai stato chiamato"

    # L'assert che DISCRIMINA il fix, e non va tolto: la chat si apre UNA
    # volta sola. Senza l'attesa a inizio sessione la quarantena viene
    # scoperta un contatto alla volta dalla guardia, quindi la stessa chat si
    # riapre a ogni giro finche' i 15 minuti non sono passati -- una decina di
    # aperture inutili per un messaggio, ognuna delle quali in produzione
    # costa una ricerca vera dentro WhatsApp Web e un contatto claimato e
    # rilasciato. (La prima stesura di questo test si fermava a "e' partito un
    # messaggio" e passava anche col difetto dentro: il messaggio partiva
    # comunque, all'undicesimo tentativo.)
    assert len(pom.chat_aperte) == 1, (
        f"la chat e' stata aperta {len(pom.chat_aperte)} volte: la quarantena "
        "sta venendo scoperta contatto per contatto invece di essere attesa")
    assert esito["saltati"] == 0 and esito["falliti"] == 0, (
        f"nessun tentativo doveva essere sprecato: {esito}")

    # --- 3. il dato a valle e' coerente ------------------------------------
    msg = await db_session.scalar(
        select(WaMessage).where(WaMessage.campaign_id == campagna.id))
    assert msg is not None
    assert msg.status == WaMessageStatus.sent
    assert msg.rendered_text == pom.inviati[0]
    # Il testo e' quello vero: nome sostituito, CTA di opt-out in coda.
    assert "Marco" in pom.inviati[0]
    assert "STOP" in pom.inviati[0]

    await db_session.refresh(numero)
    assert numero.sent_today == 1

    await db_session.refresh(cc)
    assert cc.status == WaContactStatus.completed

    await db_session.refresh(campagna)
    assert campagna.sent == 1

    # --- 4. la campagna si chiude da sola (difetto G2) ---------------------
    chiusa = await wa_worker._chiudi_campagna_se_finita(numero.id)
    await db_session.refresh(campagna)
    assert chiusa == campagna.id
    assert campagna.status == WaCampaignStatus.completed


@pytest.mark.asyncio
async def test_e2e_uno_stop_in_chat_ferma_l_invio(db_session, monkeypatch):
    """Controprova sullo stesso percorso: la catena end-to-end non deve
    limitarsi a "funziona", deve anche rifiutarsi quando serve. Se questo test
    passasse mandando comunque il messaggio, la guardia opt-out sarebbe
    scavalcabile -- che e' il rischio numero uno del canale."""
    from app.config import settings
    from app.models.wa import WaCampaignStatus, WaContact, WaMessage
    from app.services import wa_campaign_service as svc
    from app.workers import wa_worker
    from sqlalchemy import select

    monkeypatch.setattr(settings, "wa_send_enabled", True)
    orologio_virtuale(wa_worker, monkeypatch)

    class PomConStop(PomFinto):
        async def read_inbound_tail(self, n=40):
            return ["Basta, cancellami dalla lista"]

    pom = PomConStop()
    _doppi_del_browser(monkeypatch, pom)

    async def _finta_enqueue(campaign_id):
        return 1
    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers", _finta_enqueue)
    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: 11)

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    campagna, _step = await make_campaign(db_session, tenant, numero)
    contatto = await make_contact(db_session, tenant)
    await make_campaign_contact(db_session, campagna, contatto)
    await db_session.commit()

    await svc.avvia(db_session, campagna.id)
    esito = await wa_worker.esegui_mini_sessione(numero.id)

    assert pom.inviati == [], "un contatto che ha scritto STOP non deve ricevere nulla"
    assert esito["inviati"] == 0
    assert esito["saltati"] == 1

    aggiornato = await db_session.scalar(
        select(WaContact).where(WaContact.id == contatto.id))
    await db_session.refresh(aggiornato)
    assert aggiornato.opted_out is True
    assert aggiornato.do_not_contact is True

    nessun_messaggio = await db_session.scalar(
        select(WaMessage).where(WaMessage.campaign_id == campagna.id))
    assert nessun_messaggio is None

    await db_session.refresh(campagna)
    assert campagna.status == WaCampaignStatus.running
