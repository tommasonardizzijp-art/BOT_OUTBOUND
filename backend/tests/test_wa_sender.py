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
    "nessuna-cronologia:nessun-messaggio-nel-pannello",
])
def test_guasti_nostri_non_bruciano_il_contatto(signal):
    """Un selettore rotto non deve bruciare una lista (SDD 11): il contatto
    resta queued, e' il NUMERO che si ferma.

    'nessun-messaggio-nel-pannello' e' qui (decisione Tommaso round1,
    escalation whole-branch review item (c)): il POM lo emette SOLO dopo
    aver gia' trovato e cliccato una chat esistente nei risultati di
    ricerca, quindi il segnale dice "nessun messaggio renderizzato in 5s"
    (pannello lento, possibile su cronologie vecchie), non "la chat non
    esiste". Un contatto gia' presente a DB e' evidenza che dovrebbe avere
    storico vero (l'ingest di M2 l'ha validato) -- non si scarta per un
    rendering lento."""
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


# ---------------------------------------------------------------------------
# "Ha risposto" e' una RELAZIONE, non una proprieta' della chat.
#
# Serve un nostro messaggio e qualcosa arrivato dopo. Prima del collaudo
# dell'08/08 la guardia guardava solo il secondo termine: qualunque inbound,
# di qualunque epoca, valeva "ha risposto". Combinato con la regola V2 (si
# scrive SOLO a chi ha gia' una chat aperta) restava scrivibile solo chi ha
# una chat in cui non ha MAI scritto nulla -- praticamente nessuno.
#
# Il ramo non aveva copertura diretta: tutti i test della guardia passavano
# gia_scritto_prima=False, e nessuno verificava la transizione
# "coda non vuota -> ha_risposto". Per questo e' arrivato al primo invio vero.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbound_preesistente_non_e_una_risposta_se_non_abbiamo_mai_scritto(monkeypatch):
    """Il caso trovato dal vivo: chat con una conversazione dentro, ma noi a
    questo contatto non abbiamo mai scritto. Quei messaggi non sono una
    risposta a noi -- sono una conversazione che esisteva prima."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    pom = _PomFinto(["ciao come stai", "ci vediamo domani"])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is True
    assert esito.motivo == "silenzio"


@pytest.mark.asyncio
async def test_inbound_dopo_un_nostro_messaggio_e_una_risposta_e_ferma_tutto(monkeypatch):
    """L'altra meta': se avevamo scritto noi, un inbound E' una risposta e la
    sequenza si ferma. Senza questo test il fix sopra potrebbe disattivare la
    guardia del tutto e i test resterebbero verdi."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    pom = _PomFinto(["ciao, mi interessa"])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=True, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "ha_risposto"
    assert "mi interessa" in esito.prova


@pytest.mark.asyncio
async def test_lo_stop_blocca_anche_se_non_abbiamo_mai_scritto(monkeypatch):
    """L'invariante che il fix NON deve intaccare (SDD 7.2): uno STOP non e'
    mai scavalcabile, nemmeno quando arriva da una conversazione che
    precede qualunque nostro invio. Un opt-out vale da qualunque canale
    arrivi e a qualunque epoca -- e' un obbligo, non una preferenza."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    pom = _PomFinto(["non scrivetemi piu", "STOP"])
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "optout"


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


# ---------------------------------------------------------------------------
# QA M3 (Task 15 Step 3) -- adversarial gruppi A, C, D, G, I.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("coda_malformata", [
    [{"foo": "bar"}],
    [None],
    [{"text": None}],
])
async def test_a2_righe_malformate_in_coda_non_sollevano(coda_malformata):
    """Adversarial #2: qualunque riga in coda (anche illeggibile) fa
    scattare fail-closed su 'coda_malformata' -- MAI un crash, MAI un invio,
    e MAI 'ha_risposto': quel motivo marca il contatto 'replied' (terminale)
    e azzera guasti_consecutivi, disarmando l'escalation FM2 per un difetto
    di lettura che e' colpa nostra, non una verita' sul contatto (trovato in
    review, corretto dopo la prima versione di questo test)."""
    pom = _PomFinto(coda_malformata)
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.puo_inviare is False
    assert esito.motivo == "coda_malformata"


@pytest.mark.asyncio
async def test_a2b_coda_malformata_in_invia_a_contatto_non_marca_replied(
        db_session, monkeypatch):
    """Verifica end-to-end (Task 8): il contatto resta 'queued', NON
    diventa 'replied', e l'esito e' 'queued' (guasto nostro -> in
    esegui_mini_sessione incrementa guasti_consecutivi, contratto §3.2)."""
    from app.config import settings
    from app.models.wa import WaContactStatus
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    pom = _PomInvio([{"foo": "bar"}])

    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)

    assert esito.stato == "queued"
    assert esito.motivo == "coda_malformata"
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].status == WaContactStatus.queued


@pytest.mark.asyncio
async def test_a3_scan_chat_list_solleva_non_rompe_invio_gia_riuscito(db_session, monkeypatch):
    """Adversarial #3: un guasto nell'apprendimento del titolo non deve mai
    far fallire un invio gia' avvenuto."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)

    class _PomRotto(_PomInvio):
        async def scan_chat_list(self):
            raise RuntimeError("DOM cambiato")

    pom = _PomRotto([])
    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert esito.stato == "sent"
    await db_session.refresh(ctx["contact"])
    assert ctx["contact"].chat_title is None


def test_a4_ok_true_con_signal_vuota_non_si_fida_del_solo_ok():
    """Adversarial #4: ok=True da solo non basta, serve 'cronologia:' come
    prefisso catalogato."""
    esito = wa_sender.valuta_apertura(OpenResult(True, 1.0, ""))
    assert esito.puo_inviare is False
    assert esito.motivo == "segnale_non_catalogato"
    assert esito.colpa_nostra is True
    assert esito.esito_contatto is None


@pytest.mark.asyncio
async def test_c14_quarantena_e_costo_zero_nessuna_chiamata_al_pom(monkeypatch):
    """Adversarial #14: la quarantena blocca PRIMA di toccare il DOM."""
    from unittest.mock import AsyncMock
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 15)
    pom = AsyncMock()
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=30.0)
    assert esito.motivo == "quarantena_risync"
    pom.load_history.assert_not_called()
    pom.sync_state.assert_not_called()
    pom.read_inbound_tail.assert_not_called()


@pytest.mark.asyncio
async def test_c15_sync_syncing_blocca_prima_del_passo_piu_caro(monkeypatch):
    """Adversarial #15: sync_state='syncing' blocca prima di load_history/
    read_inbound_tail (il passo piu' caro)."""
    from unittest.mock import AsyncMock
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    pom = AsyncMock()
    pom.sync_state.return_value = "syncing"
    esito = await wa_sender.guardia_pre_invio(
        pom, gia_scritto_prima=False, browser_avviato_da_s=9999)
    assert esito.motivo == "sincronizzazione_in_corso"
    pom.load_history.assert_not_called()
    pom.read_inbound_tail.assert_not_called()


@pytest.mark.asyncio
async def test_d18_risposta_normale_nella_seconda_lettura_non_blocca(db_session, monkeypatch):
    """Adversarial #18: la rilettura TOCTOU controlla SOLO cecita' e STOP,
    non risposte generiche -- il messaggio parte comunque (contratto §3.5,
    rischio residuo dichiarato)."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    pom = _PomInvio([], tail_seconda_lettura=["scherzavo, tutto ok"])
    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert pom.inviato is not None
    assert esito.stato == "sent"


@pytest.mark.asyncio
async def test_g28_invia_a_contatto_su_opted_out_non_solleva(db_session, monkeypatch):
    """Adversarial #28: bypass diretto del claim (simula un futuro bug di
    chiamata) -- documenta il gap noto: invia_a_contatto non ri-legge
    opted_out, quella difesa vive SOLO nella query di claim (contratto
    §7.3). PASS = nessuna eccezione non gestita."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    ctx["contact"].opted_out = True
    await db_session.commit()
    pom = _PomInvio([])
    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert esito.stato in ("sent", "queued", "skipped", "opted_out", "failed", "replied")


@pytest.mark.asyncio
async def test_i33_nessuna_sequenza_di_9_cifre_nel_log(db_session, monkeypatch, caplog):
    """Adversarial #33: zero occorrenze di una sequenza di 9+ cifre in
    tutto il log del run, generalizzato oltre il numero di test fisso."""
    import re
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session, e164="+393331239876")
    pom = _PomInvio([])
    # INFO, non DEBUG: a DEBUG l'echo grezzo del driver sqlite logga i bind
    # param delle query, incluso il TESTO CIFRATO del telefono -- una
    # sequenza di cifre nel ciphertext e' rumore statistico, non una fuga di
    # PII (il numero in chiaro non e' mai nei log applicativi, verificato
    # sotto). L'invariante riguarda il logging applicativo (P12), non l'echo
    # SQL che in produzione non e' comunque attivo.
    with caplog.at_level("INFO"):
        await wa_sender.invia_a_contatto(
            db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
            contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert re.search(r"\d{9,}", caplog.text) is None


@pytest.mark.asyncio
async def test_i34_rendered_text_non_contiene_mai_il_numero(db_session, monkeypatch):
    """Adversarial #34."""
    from sqlalchemy import select
    from app.config import settings
    from app.models.wa import WaMessage
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    e164 = "+393339998877"
    ctx = await _scenario_invio(db_session, e164=e164)
    pom = _PomInvio([])
    await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    msg = await db_session.scalar(
        select(WaMessage).where(WaMessage.contact_id == ctx["contact"].id))
    assert e164 not in msg.rendered_text
    assert e164.lstrip("+") not in msg.rendered_text


@pytest.mark.asyncio
async def test_i35_chat_title_mai_salvato_come_numero(db_session, monkeypatch):
    """Adversarial #35: title_is_number=True -> chat_title resta NULL."""
    from app.browser.whatsapp_page import ChatRow
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)

    class _PomNumero(_PomInvio):
        async def scan_chat_list(self):
            return [ChatRow(position=0, title="+391234567890", title_is_number=True,
                            unread_count=0, preview="", last_is_outbound=False)]

    pom = _PomNumero([])
    await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    await db_session.refresh(ctx["contact"])
    assert ctx["contact"].chat_title is None


@pytest.mark.asyncio
async def test_impara_chat_title_normalizza_sempre_in_nfc(db_session, monkeypatch):
    """Backlog M4: il DOM di WhatsApp puo' restituire un nome accentato in
    forma NFD (lettera base + accento combinante separato); il matching del
    reply-watcher confronta con '=='. Se si salva il testo cosi' com'e', un
    contatto scritto oggi non verra' mai agganciato a una risposta futura
    (bug silenzioso, non un crash)."""
    import unicodedata
    from app.browser.whatsapp_page import ChatRow
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)

    titolo_nfd = unicodedata.normalize("NFD", "Città Bella")

    class _PomAccentato(_PomInvio):
        async def scan_chat_list(self):
            return [ChatRow(position=0, title=titolo_nfd, title_is_number=False,
                            unread_count=0, preview="", last_is_outbound=False,
                            outgoing_state=None, muted=False)]

    pom = _PomAccentato([])
    await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    await db_session.refresh(ctx["contact"])

    assert ctx["contact"].chat_title == unicodedata.normalize("NFC", "Città Bella")
    assert ctx["contact"].chat_title == unicodedata.normalize("NFC", ctx["contact"].chat_title)


# ---------------------------------------------------------------------------
# Fix 2 (review finale whole-branch, Critical C2): "failed" da
# invia_a_contatto azzerava guasti_consecutivi (FM2 mai armato). send_text
# fallito (selettore/DOM cambiato) e ValueError di configurazione campagna
# (optout_cta vuota) sono "colpa nostra": ora tornano 'queued', non 'failed'.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_text_fallito_non_brucia_il_contatto_arma_fm2(db_session, monkeypatch):
    """send_text che solleva = composer non trovato = DOM cambiato =
    ESATTAMENTE FM2, colpa nostra. Prima tornava 'failed' e
    _incrementa_fallimento marcava DNC dopo 3 giri per un selettore rotto,
    e wa_worker azzerava guasti_consecutivi su 'failed' -- l'escalation non
    scattava mai."""
    from app.config import settings
    from app.models.wa import WaContactStatus
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)

    class _PomRompeInvio(_PomInvio):
        async def send_text(self, text):
            raise RuntimeError("composer non trovato")

    pom = _PomRompeInvio([])
    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)

    assert esito.stato == "queued"
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].failure_count == 0
    assert ctx["cc"].status == WaContactStatus.queued
    await db_session.refresh(ctx["contact"])
    assert ctx["contact"].do_not_contact is False


@pytest.mark.asyncio
async def test_config_optout_cta_vuota_non_brucia_il_contatto(db_session, monkeypatch):
    """ValueError di prepara_testo per optout_cta vuota e' un errore di
    CONFIGURAZIONE della campagna, identico per ogni contatto -- colpa
    nostra, non deve armare il DNC di questo (o nessun) contatto."""
    from app.config import settings
    from app.models.wa import WaContactStatus
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    ctx["campaign"].optout_cta = "   "
    await db_session.commit()
    pom = _PomInvio([])

    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)

    assert esito.stato == "queued"
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].failure_count == 0
    assert ctx["cc"].status == WaContactStatus.queued
    await db_session.refresh(ctx["contact"])
    assert ctx["contact"].do_not_contact is False


@pytest.mark.asyncio
async def test_render_error_per_contatto_resta_failed(db_session, monkeypatch):
    """Contro-prova: un TemplateRenderError (placeholder mancante per QUESTO
    contatto) resta 'failed' per-contatto -- non e' un problema di
    configurazione della campagna, e' il dato di questo contatto. Non deve
    cambiare col Fix 2."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    ctx["step"].template_a = "Ciao {nome}, ultimo ordine {ultimo_ordine}."
    ctx["contact"].attributes = {}
    await db_session.commit()
    pom = _PomInvio([])

    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)

    assert esito.stato == "failed"
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].failure_count == 1


@pytest.mark.asyncio
async def test_l48_null_byte_scritto_e_riletto_da_sqlite_senza_rompere(db_session, monkeypatch):
    """Adversarial #48: round-trip DB vero (SQLite), non solo rendering in
    memoria -- il messaggio scritto con un null byte nell'attributo si
    rilegge senza rompere."""
    from sqlalchemy import select
    from app.config import settings
    from app.models.wa import WaMessage
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    ctx = await _scenario_invio(db_session)
    ctx["contact"].attributes = {"nota": "test\x00fine"}
    ctx["step"].template_a = "Ciao {nome}, nota: {nota}"
    await db_session.commit()

    pom = _PomInvio([])
    esito = await wa_sender.invia_a_contatto(
        db_session, pom, campaign=ctx["campaign"], step=ctx["step"], cc=ctx["cc"],
        contact=ctx["contact"], number=ctx["number"], browser_avviato_da_s=9999)
    assert esito.stato == "sent"

    msg = await db_session.scalar(
        select(WaMessage).where(WaMessage.contact_id == ctx["contact"].id))
    assert "test" in msg.rendered_text and "fine" in msg.rendered_text


# ---------------------------------------------------------------------------
# Decisione 2 (Tommaso, round1 post-review): il discriminatore §3.3 non e'
# implementato stanotte -- resta un contatore semplice. In compenso, quando
# quel contatore fa scattare la DNC 'invalid_number' via il percorso
# ambiguo "ricerca_senza_risultati", un alert Telegram avvisa un umano cosi'
# puo' controllare nel tempo se e' un bug reale o un vero non-raggiungibile.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_soglia_ricerca_senza_risultati_avvisa_telegram(db_session, monkeypatch):
    from app.config import settings
    from app.models.wa import WaContact, WaDncReason
    from app.services import notifier

    ctx = await _scenario_invio(db_session)
    ctx["cc"].failure_count = int(settings.wa_max_failures_per_contact) - 1
    await db_session.commit()

    chiamate = []
    async def _fake_send(message, level="info", **kw):
        chiamate.append((message, level))
    monkeypatch.setattr(notifier, "send_telegram", _fake_send)

    await wa_sender._incrementa_fallimento(db_session, ctx["cc"], "ricerca_senza_risultati")

    await db_session.refresh(ctx["contact"])
    assert ctx["contact"].dnc_reason == WaDncReason.invalid_number
    assert len(chiamate) == 1
    message, level = chiamate[0]
    assert message.startswith("WhatsApp: ")
    assert ctx["contact"].id[:8] in message      # id opaco, non il numero
    e164 = "+393331112223"
    assert e164 not in message and e164.lstrip("+") not in message


@pytest.mark.asyncio
async def test_soglia_altri_motivi_non_avvisano_telegram(db_session, monkeypatch):
    """L'alert e' specifico del percorso ambiguo 'ricerca_senza_risultati':
    altre cause di DNC (es. render fallito ripetuto) non devono generare
    rumore Telegram, non sono la stessa incertezza."""
    from app.config import settings
    from app.services import notifier

    ctx = await _scenario_invio(db_session)
    ctx["cc"].failure_count = int(settings.wa_max_failures_per_contact) - 1
    await db_session.commit()

    chiamate = []
    async def _fake_send(message, level="info", **kw):
        chiamate.append((message, level))
    monkeypatch.setattr(notifier, "send_telegram", _fake_send)

    await wa_sender._incrementa_fallimento(db_session, ctx["cc"], "render:ValueError")

    assert chiamate == []


@pytest.mark.asyncio
async def test_ricerca_senza_risultati_sotto_soglia_non_avvisa(db_session, monkeypatch):
    """Il contatore sale ma non arriva ancora alla soglia DNC: nessun
    alert, non c'e' ancora nessuna decisione ambigua presa sul contatto."""
    from app.config import settings
    from app.services import notifier

    ctx = await _scenario_invio(db_session)
    ctx["cc"].failure_count = 0
    await db_session.commit()
    assert int(settings.wa_max_failures_per_contact) > 1   # precondizione del test

    chiamate = []
    async def _fake_send(message, level="info", **kw):
        chiamate.append((message, level))
    monkeypatch.setattr(notifier, "send_telegram", _fake_send)

    await wa_sender._incrementa_fallimento(db_session, ctx["cc"], "ricerca_senza_risultati")

    assert chiamate == []


@pytest.mark.asyncio
async def test_ha_risposto_incrementa_contatore_replied_e_marca_step(db_session):
    """Backlog M4: la guardia M3 marcava 'replied' ma non toccava
    wa_campaigns.replied ne' replied_at_step/last_replied_at -- a differenza
    del reply-watcher (wa_reply_watcher.process_chat_row) che per lo stesso
    evento fa tutte e tre le cose. Le due strade devono avere lo stesso
    effetto sul contatto/campagna."""
    from app.services.wa_sender import EsitoGuardia

    ctx = await _scenario_invio(db_session)
    ctx["cc"].current_step = 2
    await db_session.commit()

    guardia = EsitoGuardia(False, "ha_risposto", prova="ciao, mi interessa")
    esito = await wa_sender._esito_guardia_negativa(
        db_session, ctx["cc"], ctx["contact"], ctx["campaign"], guardia, "+39***")

    assert esito.stato == "replied"

    await db_session.refresh(ctx["cc"])
    await db_session.refresh(ctx["contact"])
    await db_session.refresh(ctx["campaign"])

    assert ctx["cc"].replied_at_step == 2
    assert ctx["contact"].last_replied_at is not None
    assert ctx["campaign"].replied == 1
