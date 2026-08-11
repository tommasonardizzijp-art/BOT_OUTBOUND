# backend/app/services/scrape_inbox_browser.py
"""Fase Lista via browser per scrape_mode=dm_threads, inbox_engine=browser.

Motore SEPARATO da scrape_inbox.py (API), che non viene toccato. Condivide solo
il governo a monte (list_followers: stato campagna, kill-switch, resume) e il
salvataggio a valle.

REGOLA FONDANTE: una riga non riconosciuta si apre SEMPRE. La zona governa solo
il ritmo. Vedi decide_se_aprire.

Lingua dell'interfaccia: hardcoded 'it' per ora. Nessun account ha un campo di
lingua configurabile (verificato: app/models/account.py non ne ha uno) e nessun
task del piano copre l'auto-rilevamento. L'unico account misurato (Task 0) è in
italiano. Su un account con interfaccia inglese il motore classificherebbe male
(vedi test_LINGUA_SBAGLIATA_non_deve_mentire in testo.py: il tri-stato protegge
da 'ha risposto' falso, ma non da un mancato riconoscimento dei segnaposto/date).
Rendere configurabile è lavoro futuro, fuori scope di questo piano.
"""
from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.config import settings
from app.models.campaign import CampaignStatus
from app.models.follower import Follower
from app.services.bot_state_service import is_halted
from app.services.inbox_browser.pagina import (
    apri_riga, decidi_fine_lista, leggi_righe_visibili,
)
from app.services.inbox_browser.riconoscimento import ArchivioNomi, ContatoreZona
from app.services.inbox_browser.ritmo import campiona_pausa, zona_pausa
from app.services.inbox_browser.salvataggio import DatiContatto, salva_contatto
from app.services.inbox_browser.testo import (
    e_segnaposto, estrai_data_thread, estrai_ultimo_messaggio, normalizza_nome,
)
from app.services.scrape_inbox import _single_inbox_account   # sola lettura DB
from app.services.scraper import is_challenge_exception, isolate_challenged_account
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, ScraperError

DURATA_SESSIONE_MIN = 30 * 60
DURATA_SESSIONE_MAX = 55 * 60
LINGUA = "it"
_INBOX_ENDPOINT = re.compile(r"(direct_v2|graphql)", re.I)


def gia_esaminata(chiave: str | None, viste: set[str]) -> bool:
    """True se questa riga e' gia' passata sotto gli occhi in QUESTA sessione.

    Serve a non pagarne la pausa una seconda volta: il ciclo rilegge le righe
    visibili a ogni giro e `human_click` sposta la lista a ogni apertura, quindi
    il lotto successivo ricomincia in mezzo al precedente. Una riga senza chiave
    non e' memorizzabile e viene trattata come nuova: dire il contrario
    zittirebbe tutte le righe anonime dopo la prima.

    A differenza di `ArchivioNomi` (che tratta i nomi duplicati come mai
    riconosciuti, perche' i nomi visualizzati di Instagram non sono univoci),
    questa memoria non gestisce le collisioni: due chat diverse con lo stesso
    nome, nello stesso giro, fanno saltare la seconda. Rischio accettato e
    limitato alla sessione corrente (30-55 min): al giro successivo la memoria
    e' vuota e la riga torna esaminabile.
    """
    if not chiave:
        return False
    return chiave in viste


def decide_se_aprire(nome: str | None, archivio: ArchivioNomi, zona: str) -> bool:
    """REGOLA FONDANTE del modulo.

    Una riga si apre SEMPRE, tranne in due casi: e' gia' riconosciuta, oppure e'
    un segnaposto (profilo cancellato). La ZONA non compare in questa decisione,
    ed e' voluto: nel disegno precedente la zona 'rapida' non apriva niente, e
    questo faceva raccogliere ZERO contatti a regime, perche' in cima alla lista
    ci sono sempre i DM appena inviati (tutti noti).

    Il parametro `zona` resta nella firma solo per rendere esplicito che non
    influenza il risultato: non rimuoverlo pensando sia inutile, e non usarlo.
    """
    if e_segnaposto(nome):
        return False
    return not archivio.e_riconosciuto(nome)


def motore_ancora_nostro(campaign) -> bool:
    """True se la campagna e' ancora impostata sul motore browser.

    NOTA per l'implementer: questa funzione appartiene davvero al Task 14
    (auto-defer su cambio motore a caldo), non a questo task. E' definita qui,
    minimale, perche' il ciclo di QUESTO task la deve gia' chiamare (vedi sotto)
    per non lasciare una sessione da 30-55 minuti a girare a vuoto su un motore
    che l'utente ha spento nel frattempo. Il Task 14 la testera' a fondo e potra'
    estenderla; non rimuoverla né duplicarla quando arriverai a quel task.
    """
    return getattr(campaign, "inbox_engine", "api") == "browser"


async def run_inbox_browser_list(campaign_id: str, db, campaign) -> int | None:
    """Loop Fase Lista inbox via browser. Stesso contratto di run_inbox_list:
    secondi di defer al session-break (il worker solleva Retry(defer=...)),
    None se completata/interrotta."""
    from app.browser.context_manager import BrowserSession
    from app.utils.events import emit as emit_event

    account = None
    session = None
    falliti_inbox: list[str] = []
    already = await db.scalar(
        select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
    ) or 0
    since_break = 0
    nuovi_in_sessione = 0
    viste_in_sessione: set[str] = set()
    session_started = datetime.utcnow()
    session_budget_s = random.uniform(DURATA_SESSIONE_MIN, DURATA_SESSIONE_MAX)

    async def _deve_fermarsi() -> str | None:
        """Kill-switch, stato campagna, motore ancora nostro: 'halted' | 'status'
        | 'motore' | None. Va richiamato SIA in cima al lotto SIA dentro il for
        di ogni riga (I2): con `campiona_pausa` che arriva fino a 2-5 minuti per
        riga e un lotto di 30 righe, controllare solo in cima al while farebbe
        impiegare diversi minuti al kill-switch prima di fermare davvero il
        browser — la spec chiede stop immediato."""
        if await is_halted(db):
            return "halted"
        await db.refresh(campaign)
        if campaign.status not in (CampaignStatus.listing, CampaignStatus.listing_break):
            logger.info(f"[InboxBrowser] Stato '{campaign.status.value}' — interrotto a {already}")
            return "status"
        if not motore_ancora_nostro(campaign):
            logger.info("[InboxBrowser] motore cambiato durante la sessione — esco pulito")
            emit_event(campaign_id, "scrape_stopped",
                       "Motore inbox cambiato durante la raccolta: sessione interrotta", level="warn")
            return "motore"
        return None

    try:
        # _single_inbox_account puo' sollevare ScrapeBudgetError (0 o 2+ account
        # inbox attivi): DEVE stare dentro il try, altrimenti quell'eccezione
        # sfugge a tutti gli except sotto e la campagna resta in uno stato
        # inconsistente senza nessun evento emesso (visto in run_inbox_list,
        # che per lo stesso motivo include build_inbox_source nel try).
        account = await _single_inbox_account(db, campaign.id)
        session = BrowserSession(account.id)
        await session.open()
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        def _on_requestfailed(req):
            try:
                if _INBOX_ENDPOINT.search(req.url):
                    falliti_inbox.append(req.url)
            except Exception:
                pass

        page.on("requestfailed", _on_requestfailed)

        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="commit", timeout=60000)
        await page.wait_for_timeout(3000)
        if "/accounts/login" in page.url:
            raise ScraperError("Sessione web non loggata su Instagram — nessun tentativo di login automatico")

        emit_event(campaign_id, "scrape_start", "Fase Lista inbox avviata (browser)")

        nomi_esistenti = (await db.execute(
            select(Follower.full_name).where(Follower.campaign_id == campaign_id)
        )).scalars().all()
        archivio = ArchivioNomi(list(nomi_esistenti))
        contatore = ContatoreZona()

        drained = False

        while True:
            esito_stop = await _deve_fermarsi()
            if esito_stop == "halted":
                raise BotHaltedError("kill-switch")
            if esito_stop is not None:
                return None
            if campaign.list_target and already >= campaign.list_target:
                logger.info(f"[InboxBrowser] Target {campaign.list_target} raggiunto ({already})")
                break

            righe = await leggi_righe_visibili(page, LINGUA)
            for riga in righe:
                esito_stop = await _deve_fermarsi()
                if esito_stop == "halted":
                    raise BotHaltedError("kill-switch")
                if esito_stop is not None:
                    return None
                if riga.non_letta:
                    # Preserva il badge dei non letti (decisione di Tommaso): si
                    # salta SENZA aprire e senza contare nel riconoscimento, la
                    # riga resta da raccogliere quando sarà stata letta a mano.
                    # Non entra nella memoria di sessione: se nel frattempo
                    # viene letta a mano va rivalutata, e il salto qui non paga
                    # comunque nessuna pausa da risparmiare.
                    continue

                chiave = normalizza_nome(riga.nome)
                if gia_esaminata(chiave, viste_in_sessione):
                    # Gia' guardata in questa sessione: niente pausa, niente
                    # decisione, niente contatore di zona. Ripagarla e' il 65%
                    # delle pause di scorrimento misurate l'11/08.
                    continue
                if chiave:
                    viste_in_sessione.add(chiave)

                riconosciuta_prima = archivio.e_riconosciuto(riga.nome)
                ha_aperto = decide_se_aprire(riga.nome, archivio, contatore.zona)
                if ha_aperto:
                    username = await apri_riga(page, riga.indice, riga.nome, LINGUA, account.username)
                    if username:
                        testo_pagina = await page.evaluate("() => document.body.innerText")
                        dati = DatiContatto(
                            username=username,
                            nome=riga.nome,
                            last_message_at=estrai_data_thread(testo_pagina, LINGUA),
                            last_message_from=(
                                "us" if riga.ultimo_nostro is True
                                else "them" if riga.ultimo_nostro is False
                                else None
                            ),
                            last_message_text=estrai_ultimo_messaggio(testo_pagina, LINGUA),
                        )
                        esito = await salva_contatto(db, campaign_id, dati)
                        archivio.aggiungi(riga.nome)
                        if esito == "creato":
                            already += 1
                            nuovi_in_sessione += 1
                            since_break += 1
                            campaign.total_followers = already
                            campaign.updated_at = datetime.utcnow()
                            await db.commit()
                            emit_event(campaign_id, "scrape_batch",
                                       f"Inbox: {already}" + (f"/{campaign.list_target}" if campaign.list_target else ""))
                    # username None (verifica post-click fallita, o profilo sparito):
                    # nessuna scrittura, si riprova alla prossima ripresa — mai a metà.

                contatore.registra(riconosciuta_prima)
                # La pausa dipende da cosa si e' FATTO, non da dove ci si trova:
                # una riga solo scorsa non ha toccato Instagram, e pagarla al
                # ritmo delle aperture costava il 91% del tempo di sessione
                # (misurato l'11/08). Vedi ritmo.zona_pausa.
                await asyncio.sleep(campiona_pausa(zona_pausa(contatore.zona, ha_aperto)))

                durata_s = (datetime.utcnow() - session_started).total_seconds()
                if durata_s >= session_budget_s:
                    minutes = random.uniform(settings.inbox_break_min_minutes, settings.inbox_break_max_minutes)
                    seconds = int(minutes * 60)
                    campaign.scrape_break_prev_status = CampaignStatus.listing.value
                    campaign.status = CampaignStatus.listing_break
                    campaign.scrape_break_until = datetime.utcnow() + timedelta(seconds=seconds)
                    campaign.updated_at = datetime.utcnow()
                    await db.commit()
                    emit_event(campaign_id, "scrape_break", f"Pausa inbox {int(minutes)} min dopo {already}")
                    return seconds

            # Esaurite le righe visibili in questo giro: decidi_fine_lista fa lei
            # scroll + attese a pazienza crescente + la decisione finale (Task 8,
            # gia' corretta in review per contare i falliti nella FINESTRA di
            # attesa, non cumulativi di sessione — non reimplementare qui).
            decisione = await decidi_fine_lista(page, falliti_inbox)
            if decisione == "continua":
                continue
            if decisione == "piantato":
                raise ScraperError(
                    "Lista inbox piantata: richieste fallite verso gli endpoint inbox durante l'attesa"
                )
            # decisione == "fine"
            drained = nuovi_in_sessione == 0
            break

        campaign.status = CampaignStatus.ready
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        if drained:
            emit_event(campaign_id, "scrape_complete",
                       f"Nessun contatto nuovo in questa sessione ({already} totali in lista) — "
                       "rilanciare non serve finché non arrivano nuovi DM in entrata.", level="warn")
        else:
            emit_event(campaign_id, "scrape_complete", f"Fase Lista inbox completata: {already} contatti in lista")
        return None

    except BotHaltedError:
        campaign.status = CampaignStatus.paused
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — inbox interrotta", level="warn")
        return None
    except (ScrapeBudgetError, ScraperError) as e:
        campaign.status = CampaignStatus.error
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        emit_event(campaign_id, "scrape_stopped", f"Fase Lista inbox (browser) non avviata: {e}", level="error")
        return None
    except Exception as e:
        if is_challenge_exception(e) and account is not None:
            await isolate_challenged_account(db, campaign, account, e)
        else:
            logger.exception(f"[InboxBrowser] Errore campaign {campaign_id}: {e}")
            campaign.status = CampaignStatus.error
            campaign.updated_at = datetime.utcnow()
            await db.commit()
        return None
    finally:
        if session is not None:
            await session.close()
