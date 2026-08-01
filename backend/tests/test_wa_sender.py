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


@pytest.mark.asyncio
async def test_scroll_mai_tentato_blocca(monkeypatch):
    """HistoryInfo.ok=False vuol dire che il box del pannello non e' stato
    trovato e NESSUNO scroll e' stato tentato: 'after' e' solo cio' che
    c'era gia' nel DOM, non uno storico validato. Stesso principio di
    cecita'!=silenzio, applicato al caricamento della cronologia."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    pom = _PomFinto([], history_ok=False)
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "storico_non_caricato"


from types import SimpleNamespace


def _step(**over):
    base = dict(step_index=0, template_a="Ciao {nome}, promo attiva.",
                template_b=None, template_c=None, template_d=None)
    base.update(over)
    return SimpleNamespace(**base)


def _campaign(**over):
    base = dict(optout_enabled=True, optout_cta="Scrivi STOP per non ricevere piu' messaggi.")
    base.update(over)
    return SimpleNamespace(**base)


def _contact(**over):
    base = dict(display_name="Marco", attributes=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_cta_appesa_solo_allo_step_zero():
    testo, variante = wa_sender.prepara_testo(_step(step_index=0), _contact(), _campaign())
    assert testo.endswith("Scrivi STOP per non ricevere piu' messaggi.")
    assert variante == "a"

    testo2, _ = wa_sender.prepara_testo(_step(step_index=1), _contact(), _campaign())
    assert "STOP" not in testo2


def test_cta_non_appesa_se_optout_disabilitato():
    testo, _ = wa_sender.prepara_testo(
        _step(), _contact(), _campaign(optout_enabled=False))
    assert "STOP" not in testo


def test_cta_vuota_con_optout_attivo_solleva():
    """Una campagna marketing senza via d'uscita non deve partire. M2 lo
    blocca a 422 in creazione; qui e' la seconda rete, perche' i dati a DB
    possono essere stati scritti prima di quella validazione."""
    with pytest.raises(ValueError):
        wa_sender.prepara_testo(_step(), _contact(),
                                _campaign(optout_cta="   "))


def test_placeholder_mancante_solleva_e_non_manda_messaggio_monco():
    from app.services.wa_template import TemplateRenderError
    step = _step(template_a="Ciao {nome}, il tuo ultimo ordine e' del {ultimo_ordine}.")
    with pytest.raises(TemplateRenderError):
        wa_sender.prepara_testo(step, _contact(attributes={}), _campaign())


def test_placeholder_presente_viene_valorizzato():
    step = _step(template_a="Ciao {nome}, ultimo ordine {ultimo_ordine}.")
    testo, _ = wa_sender.prepara_testo(
        step, _contact(attributes={"ultimo_ordine": "10/01/2026"}), _campaign())
    assert "10/01/2026" in testo and "Marco" in testo


async def _scenario_invio(db_session, e164: str = "+393331112223"):
    """Tenant + numero + contatto + campagna running + step 0, tutto a DB.
    Deliberatamente locale a questo file: i test devono restare eseguibili
    anche se factories_wa.py (M2/PR-0) cambia forma."""
    import uuid
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus, WaNumber,
                               WaSendCondition, WaSequenceStep)
    from app.utils.crypto import encrypt
    from app.utils.phone_pseudonym import hmac_phone

    tenant = Tenant(id=str(uuid.uuid4()), name="T", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"n-{uuid.uuid4()}", encrypted_phone=encrypt("+390000000000"))
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=hmac_phone(e164), encrypted_phone=encrypt(e164),
                        display_name="Marco")
    db_session.add_all([number, contact])
    await db_session.flush()
    campaign = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id,
                          wa_number_id=number.id, name="c",
                          campaign_type=WaCampaignType.marketing,
                          status=WaCampaignStatus.running, optout_enabled=True,
                          optout_cta="Scrivi STOP per non ricevere piu' messaggi.")
    db_session.add(campaign)
    await db_session.flush()
    step = WaSequenceStep(id=str(uuid.uuid4()), campaign_id=campaign.id, step_index=0,
                          template_a="Ciao {nome}, promo attiva.",
                          send_condition=WaSendCondition.always, wait_days=0)
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id,
                           contact_id=contact.id, status=WaContactStatus.queued,
                           current_step=-1)
    db_session.add_all([step, cc])
    await db_session.commit()
    return {"tenant": tenant, "number": number, "contact": contact,
            "campaign": campaign, "step": step, "cc": cc}


_NON_PASSATO = object()  # sentinella: distingue "non passato" (usa tail) da
                          # "passato esplicitamente None" (simula cecita').


class _PomInvio(_PomFinto):
    """Estende il doppio con il composer: registra cosa e' stato digitato e
    permette di far comparire uno STOP TRA la guardia e l'invio."""
    def __init__(self, tail, *, tail_seconda_lettura=_NON_PASSATO, **kw):
        super().__init__(tail, **kw)
        self._tail_seconda = tail if tail_seconda_lettura is _NON_PASSATO else tail_seconda_lettura
        self._letture = 0
        self.inviato = None
        self.tick = "Consegnato"

    async def read_inbound_tail(self, n: int = 40):
        self._letture += 1
        return self._tail if self._letture == 1 else self._tail_seconda

    async def open_chat(self, e164: str):
        from app.browser.whatsapp_page import OpenResult
        return OpenResult(True, 100.0, "cronologia:div[data-id]:30")

    async def send_text(self, text: str):
        self.inviato = text

    async def read_last_tick(self):
        return self.tick


@pytest.mark.asyncio
async def test_stop_arrivato_nella_finestra_toctou_annulla_l_invio(db_session, monkeypatch):
    """La guardia non aveva visto nulla; nei 20 secondi successivi arriva
    STOP. La seconda lettura lo intercetta e il messaggio NON parte."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    pom = _PomInvio([], tail_seconda_lettura=["STOP"])

    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)

    assert pom.inviato is None
    assert esito.stato == "opted_out"


@pytest.mark.asyncio
async def test_cecita_nella_seconda_lettura_annulla_l_invio(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    pom = _PomInvio([], tail_seconda_lettura=None)
    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert pom.inviato is None
    assert esito.stato == "queued"      # colpa nostra: il contatto non si brucia


@pytest.mark.asyncio
async def test_invio_riuscito_scrive_messaggio_stato_e_contatori(db_session, monkeypatch):
    from app.config import settings
    from sqlalchemy import select
    from app.models.wa import WaMessage, WaMessageStatus, WaContactStatus
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    pom = _PomInvio([])

    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)

    assert esito.stato == "sent"
    assert pom.inviato is not None and "STOP" in pom.inviato   # CTA step 0
    msg = await db_session.scalar(select(WaMessage).where(
        WaMessage.contact_id == ctx["contact"].id))
    assert msg.status == WaMessageStatus.sent
    assert msg.delivery_check is not None
    assert msg.rendered_text == pom.inviato
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].status == WaContactStatus.completed   # MVP: 1 solo step
    assert ctx["cc"].current_step == 0
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].sent == 1
    await db_session.refresh(ctx["number"])
    assert ctx["number"].sent_today == 1


@pytest.mark.asyncio
async def test_il_numero_in_chiaro_non_finisce_mai_nei_log(db_session, monkeypatch, caplog):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session, e164="+393421460077")
    pom = _PomInvio([])
    with caplog.at_level("DEBUG"):
        await wa_sender.invia_a_contatto(
            db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
            contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert "+393421460077" not in caplog.text
    assert "3421460077" not in caplog.text
