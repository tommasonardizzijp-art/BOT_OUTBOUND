"""Fase Lista alternativa per scrape_mode=dm_threads: raccoglie i contatti dai
DM gia' avviati dell'account. Engine selezionabile (api/browser). Riusa lo stato
listing/listing_break, il session-break via Retry(defer) e il challenge handler.
"""
import asyncio
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select, update

from app.config import settings
from app.models.campaign import CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.account import InstagramAccount, AccountStatus
from app.models.follower import Follower, FollowerStatus
from app.services.bot_state_service import is_halted
from app.services.scraper import is_challenge_exception, isolate_challenged_account
from app.services.inbox_browser.targa import normalizza_username
from app.services.inbox_source import ApiInboxSource
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, ScraperError
from app.utils.instagrapi_client import login as _login
from app.utils.roles import INBOX_ROLES


def inbox_collect(participants, existing_ids) -> list[tuple[int, str]]:
    """Filtra i partecipanti gia' salvati (dedup-frontier) + dedup interno pagina.

    existing_ids = set di ig_user_id gia' presenti come Follower della campagna.
    Conserva l'ordine, prima occorrenza.
    """
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for pk, username in participants:
        if pk in existing_ids or pk in seen:
            continue
        seen.add(pk)
        out.append((pk, username))
    return out


@dataclass
class EsitoPagina:
    """Come si smista una pagina di partecipanti contro quello che c'e' gia' in DB."""
    nuovi: list[tuple[int, str]] = field(default_factory=list)
    # (pk_vero, username_normalizzato): righe gia' in DB con targa provvisoria, a cui
    # l'API puo' finalmente scrivere la targa vera.
    promozioni: list[tuple[int, str]] = field(default_factory=list)
    gia_presenti: int = 0
    # username gia' in DB con una targa REALE diversa: username riassegnato dopo un
    # rename, quindi due persone diverse. Si inserisce e si segnala.
    collisioni_username: list[str] = field(default_factory=list)


def classifica_pagina(participants, existing_ids, targa_per_username) -> EsitoPagina:
    """Smista i partecipanti in nuovi / promozioni / gia' presenti.

    Due reti a maglie diverse, nell'ordine:

    1. `inbox_collect` sul pk: la frontiera storica del percorso API.
    2. lo USERNAME: serve perche' i contatti raccolti dal canale browser hanno una
       targa PROVVISORIA negativa (non conoscono il pk, vedi inbox_browser/targa.py).
       Sul solo pk quelle righe sono invisibili all'API, che le reinserisce tutte
       come "nuove" — 32 doppioni su 34 righe, misurato su prod il 21/08/2026.
       Qui l'API ha in mano il pk vero: promuove la riga esistente invece di
       crearne una seconda.

    `targa_per_username` e' {username normalizzato: ig_user_id} della campagna.
    Funzione pura: non scrive, decide soltanto.
    """
    fresh = inbox_collect(participants, existing_ids)
    gia_presenti = 0
    for p in participants or []:
        try:
            if p[0] in existing_ids:
                gia_presenti += 1
        except (TypeError, IndexError):
            continue

    esito = EsitoPagina(gia_presenti=gia_presenti)
    promossi_in_pagina: set[str] = set()
    for pk, username in fresh:
        u = normalizza_username(username) if isinstance(username, str) else ""
        targa = targa_per_username.get(u) if u else None
        if targa is None or u in promossi_in_pagina:
            # Sconosciuto, oppure lo username l'ha gia' preso un altro pk in questa
            # stessa pagina: la promozione puo' toccare a uno solo, gli altri sono
            # persone diverse e vanno inseriti.
            esito.nuovi.append((pk, username))
        elif targa < 0:
            promossi_in_pagina.add(u)
            esito.promozioni.append((pk, u))
        else:
            esito.collisioni_username.append(u)
            esito.nuovi.append((pk, username))
    return esito


def _sample_page_delay(lo: float, hi: float, sigma: float = 0.9) -> float:
    """Lognormale TRONCATA su [lo, hi] (riestrazione), non clampata.

    Perche' non il clamp: `min(hi, max(lo, x))` non scarta la coda, la SCHIACCIA
    sui bound. Con i vecchi 10-40 e sigma 0.9 finiva li' il 45% dei delay (30%
    esattamente su 40.000s, 15% su 10.000s): due picchi netti su due valori fissi
    sono una firma temporale piu' riconoscibile di un delay costante, perche'
    nessun umano ripete lo stesso intervallo al millisecondo trenta volte su cento.
    Riestraendo, la forma lognormale resta liscia dentro tutto il range.

    Mediana = media geometrica sqrt(lo*hi), non (lo+hi)/2: e' il centro naturale
    in scala logaritmica, quindi il troncamento taglia code simmetriche e accetta
    al primo colpo ~2 volte su 3 (con 10-60: mediana 24.5s, ~68% di accettazione).
    Il tetto di tentativi e il fallback sulla mediana coprono il caso degenere
    lo == hi, dove nessuna estrazione cadrebbe mai nel range.
    """
    if hi <= lo:
        return float(lo)
    median = math.sqrt(lo * hi)
    for _ in range(20):
        delay = random.lognormvariate(0, sigma) * median
        if lo <= delay <= hi:
            return delay
    return median


async def _inbox_page_delay() -> None:
    """Pacing umano tra pagine inbox: lognormale troncata (scroll attivo) + pausa
    lunga occasionale ("si ferma a leggere/rispondere"). Distribuzione bimodale:
    la maggior parte delle pagine veloci, raramente uno stop lungo. Piu' credibile
    dell'uniforme piatto perche' un umano non aspetta lo stesso intervallo a ogni
    caricamento.
    """
    if random.random() < settings.inbox_long_pause_probability:
        delay = random.uniform(
            settings.inbox_long_pause_min_seconds, settings.inbox_long_pause_max_seconds
        )
        logger.info(f"[InboxLista] Pausa lunga {delay:.0f}s (legge/risponde)")
    else:
        delay = _sample_page_delay(
            settings.inbox_api_page_delay_min_seconds,
            settings.inbox_api_page_delay_max_seconds,
        )
    await asyncio.sleep(delay)


async def _single_inbox_account(db, campaign_id: str):
    """Ritorna l'unico account inbox attivo per la campagna, o solleva.

    Il listing dell'inbox DM lo fa l'account con capability inbox (una sola per
    campagna). Eventuali account scraping/dm aggiuntivi non leggono l'inbox e
    qui sono esclusi: contano solo gli INBOX_ROLES."""
    rows = (await db.execute(
        select(InstagramAccount)
        .join(CampaignAccount, CampaignAccount.account_id == InstagramAccount.id)
        .where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.is_active == True,  # noqa: E712
            CampaignAccount.role.in_(INBOX_ROLES),
            InstagramAccount.status.in_((AccountStatus.active, AccountStatus.warming_up)),
        )
    )).scalars().all()
    if len(rows) != 1:
        raise ScrapeBudgetError(
            f"Campagna inbox richiede esattamente 1 account inbox attivo (trovati {len(rows)})"
        )
    return rows[0]


async def build_inbox_source(db, campaign):
    """Costruisce la sorgente inbox (SOLO API).

    Lo scraping via browsing del DOM e' stato rimosso: la lista DM su Instagram
    web espone solo il NOME VISUALIZZATO (es. "Tabaccheria Sileoni"), non
    l'@username ne' il pk, e le righe non sono link al thread — quindi dal DOM
    non si ricava nessun identificatore usabile per estrarre i contatti.
    Verificato live (giugno 2026). L'API (direct_v2/inbox) restituisce invece
    pk + username puliti e paginati, e funziona su account sani.

    Ritorna (source, own_pk, account, cleanup); cleanup e' una factory da
    awaitare nel finally.
    """
    account = await _single_inbox_account(db, campaign.id)
    client = await _login(account, db)
    own_pk = int(client.user_id)
    cursor = campaign.scrape_cursor or None  # oldest_cursor API
    source = ApiInboxSource(client, own_pk, cursor=cursor)

    async def _cleanup():
        return None

    return source, own_pk, account, _cleanup


async def run_inbox_list(campaign_id: str, db, campaign) -> int | None:
    """Loop Fase Lista inbox. Eseguito dentro la sessione DB di list_followers.

    Ritorna i secondi di defer al raggiungimento del session-break (il worker
    solleva Retry(defer=...)); None se completata/interrotta.
    """
    from app.utils.events import emit as emit_event

    source = None
    cleanup = None
    account = None
    try:
        # Due modalita', decise dal fondo dell'inbox (vedi migration 036):
        #  - DISCESA (bottom_reached False): si scende verso i thread piu' vecchi
        #    partendo dal cursore salvato. Le pagine di soli contatti gia' noti sono
        #    normali e NON fermano la discesa: fermarsi li' renderebbe irraggiungibili
        #    proprio i thread vecchi per cui si usa l'API.
        #  - CIMA (bottom_reached True): il fondo e' gia' stato toccato, quindi ogni
        #    giro riparte dalla cima solo per intercettare i DM nuovi e si ferma
        #    appena vede inbox_empty_page_stop pagine consecutive senza nuovi.
        modo_cima = bool(getattr(campaign, "inbox_bottom_reached", False))
        if modo_cima and campaign.scrape_cursor:
            # Il cursore profondo non serve piu' e non deve far credere a
            # campaign_control che ci sia una lista interrotta a meta'.
            campaign.scrape_cursor = None
            await db.commit()

        source, own_pk, account, cleanup = await build_inbox_source(db, campaign)
        emit_event(
            campaign_id, "scrape_start",
            "Fase Lista inbox avviata (API, giro di cima: solo DM nuovi)" if modo_cima
            else "Fase Lista inbox avviata (API, discesa verso i thread piu' vecchi)",
        )

        already = await db.scalar(
            select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
        ) or 0
        existing_ids = set((await db.execute(
            select(Follower.ig_user_id).where(Follower.campaign_id == campaign_id)
        )).scalars().all())
        # Seconda rete, sullo USERNAME: i contatti presi dal canale browser hanno una
        # targa provvisoria negativa, invisibile alla rete sul pk. Vedi classifica_pagina.
        # Si tiene anche l'id della riga: la promozione deve colpire QUELLA riga, non
        # ripescarla per username (in DB alcuni username sono salvati con la chiocciola
        # o in maiuscolo, e una WHERE sullo username grezzo non li troverebbe — la
        # promozione andrebbe a vuoto e il contatto sparirebbe in silenzio, perche'
        # classificato come promozione non viene nemmeno inserito).
        targa_per_username: dict[str, int] = {}
        id_per_username: dict[str, str] = {}
        for rid, uname, targa in (await db.execute(
            select(Follower.id, Follower.username, Follower.ig_user_id).where(
                Follower.campaign_id == campaign_id
            )
        )).all():
            u = normalizza_username(uname) if isinstance(uname, str) else ""
            if not u:
                continue
            targa_per_username[u] = targa
            id_per_username[u] = rid
        since_break = 0
        pagine_da_pausa = 0   # pagine lette dall'ultima pausa: e' il budget di sessione
        empty_streak = 0   # pagine consecutive con 0 contatti nuovi -> inbox drenato
        nuovi_tot = 0
        promossi_tot = 0
        cursore_precedente = campaign.scrape_cursor
        drained = False

        while True:
            if await is_halted(db):
                raise BotHaltedError("kill-switch")
            await db.refresh(campaign)
            if campaign.status not in (CampaignStatus.listing, CampaignStatus.listing_break):
                logger.info(f"[InboxLista] Stato '{campaign.status.value}' — interrotto a {already}")
                return None
            if campaign.list_target and already >= campaign.list_target:
                logger.info(f"[InboxLista] Target {campaign.list_target} raggiunto ({already})")
                break

            page = await source.next_page()
            pagine_da_pausa += 1
            esito = classifica_pagina(page.participants, existing_ids, targa_per_username)
            for pk, username in esito.nuovi:
                db.add(Follower(
                    campaign_id=campaign_id,
                    ig_user_id=pk,
                    username=username,
                    full_name=None,
                    is_private=False,
                    is_verified=False,
                    profile_pic_url=None,
                    status=FollowerStatus.pending,
                ))
                existing_ids.add(pk)
                u = normalizza_username(username) if isinstance(username, str) else ""
                if u:
                    targa_per_username[u] = pk
                    id_per_username.pop(u, None)   # la riga nuova non ha ancora un id qui
            recuperi = 0    # promozioni finite a vuoto e salvate come riga nuova
            for pk, u in esito.promozioni:
                # UPDATE mirato sulla riga a targa provvisoria: e' la stessa persona,
                # presa dal browser senza pk. Non si tocca nient'altro della riga
                # (full_name, last_message_*, stato: sono dati che l'API non ha).
                # La guardia `ig_user_id < 0` regge la corsa con un altro worker che
                # avesse gia' promosso la stessa riga: la seconda UPDATE non passa.
                rid = id_per_username.get(u)
                if rid is None:
                    # Non dovrebbe succedere (la promozione nasce da questa mappa), ma
                    # se succedesse si perderebbe il contatto: meglio inserirlo.
                    logger.warning(f"[InboxLista] @{u}: riga da promuovere sparita — inserisco")
                    db.add(Follower(
                        campaign_id=campaign_id, ig_user_id=pk, username=u,
                        full_name=None, is_private=False, is_verified=False,
                        profile_pic_url=None, status=FollowerStatus.pending,
                    ))
                    recuperi += 1
                else:
                    await db.execute(
                        update(Follower)
                        .where(Follower.id == rid, Follower.ig_user_id < 0)
                        .values(ig_user_id=pk, updated_at=datetime.utcnow())
                    )
                    logger.info(f"[InboxLista] @{u}: targa provvisoria promossa a pk reale {pk}")
                existing_ids.add(pk)
                targa_per_username[u] = pk
            for u in esito.collisioni_username:
                logger.warning(
                    f"[InboxLista] @{u} esiste gia' con una targa REALE diversa: "
                    "username riassegnato dopo un rename, la nuova riga e' un'altra persona."
                )
            stored = len(esito.nuovi) + recuperi
            promossi = len(esito.promozioni) - recuperi
            nuovi_tot += stored
            promossi_tot += promossi
            already += stored
            since_break += stored
            empty_streak = 0 if stored else empty_streak + 1
            # cursore intra-engine: si salva SOLO in discesa. In modalita' cima e'
            # una passata corta che riparte sempre dall'alto, e un cursore salvato
            # li' farebbe credere a campaign_control che la lista sia a meta'.
            if not modo_cima:
                campaign.scrape_cursor = page.cursor
            campaign.total_followers = already
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            if stored or promossi:
                emit_event(
                    campaign_id, "scrape_batch",
                    f"Inbox: {already}" + (f"/{campaign.list_target}" if campaign.list_target else "")
                    + (f" (+{promossi} gia' presi dal browser, ora con pk reale)" if promossi else ""),
                )

            if page.exhausted:
                logger.info(f"[InboxLista] Fondo dell'inbox raggiunto ({already})")
                campaign.scrape_cursor = None
                campaign.inbox_bottom_reached = True
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                break

            # Cursore che non avanza: IG risponde ma la finestra non si sposta.
            # Continuare significherebbe richiedere la stessa pagina all'infinito.
            if page.cursor is not None and page.cursor == cursore_precedente:
                logger.warning(f"[InboxLista] Cursore fermo su {page.cursor!r} — interrompo ({already})")
                emit_event(
                    campaign_id, "scrape_warning",
                    "Fase Lista inbox interrotta: il cursore di Instagram non avanza piu'.",
                    level="warn",
                )
                break
            cursore_precedente = page.cursor

            # Drenaggio: N pagine consecutive con 0 contatti NUOVI = oltre questo
            # punto l'inbox e' tutta gente gia' in lista. IG puo' tenere has_older
            # sempre True, quindi 'exhausted' da solo non basta e la lista girerebbe
            # a vuoto per sempre in silenzio (il bug segnalato). Ci si ferma e si
            # AVVISA. Cursore azzerato: il prossimo giro riparte dal top e intercetta
            # eventuali DM nuovi arrivati nel frattempo.
            #
            # SOLO in modalita' cima. In discesa una pagina senza nuovi e' la norma
            # (sono thread gia' raccolti dal browser, o gia' presi in un giro
            # precedente): fermarsi li' lascerebbe per sempre irraggiunto il fondo.
            # In discesa il giro e' limitato dal budget pagine qui sotto.
            if modo_cima and empty_streak >= settings.inbox_empty_page_stop:
                logger.info(
                    f"[InboxLista] {empty_streak} pagine consecutive senza nuovi "
                    f"— inbox gia' tutto raccolto ({already})"
                )
                campaign.scrape_cursor = None
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                drained = True
                break

            # pacing umano tra pagine (lognormale + pausa lunga occasionale)
            await _inbox_page_delay()

            # Budget di sessione: due tetti, quello che scatta prima vince.
            # - contatti nuovi: il tetto storico, valido finche' l'inbox e' vergine.
            # - PAGINE lette: l'unico che conta in discesa, dove le pagine possono
            #   non portare nessun contatto nuovo per decine di giri. Senza questo,
            #   una discesa lunga sarebbe una raffica ininterrotta di richieste.
            if (
                since_break >= settings.inbox_session_size
                or pagine_da_pausa >= settings.inbox_session_pages
            ):
                minutes = random.uniform(settings.inbox_break_min_minutes, settings.inbox_break_max_minutes)
                seconds = int(minutes * 60)
                campaign.scrape_break_prev_status = CampaignStatus.listing.value
                campaign.status = CampaignStatus.listing_break
                campaign.scrape_break_until = datetime.utcnow() + timedelta(seconds=seconds)
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                emit_event(campaign_id, "scrape_break", f"Pausa inbox {int(minutes)} min dopo {already}")
                return seconds

        # Il contatore dice quante RIGHE ci sono davvero, non quante ne ha contate il
        # loop: una promozione non crea una riga, e la verita' sta nel DB.
        already = await db.scalar(
            select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
        ) or 0
        campaign.total_followers = already
        campaign.status = CampaignStatus.ready
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        coda = (
            f" · {promossi_tot} gia' presi dal browser, ora con pk reale" if promossi_tot else ""
        )
        if drained:
            emit_event(
                campaign_id, "scrape_complete",
                f"Inbox gia' tutto raccolto: 0 nuovi contatti (rilette {empty_streak} pagine di duplicati). "
                f"{already} in lista — per averne altri servono nuovi DM in entrata o una campagna scrape follower.",
                level="warn",
            )
        else:
            emit_event(
                campaign_id, "scrape_complete",
                f"Fase Lista inbox completata: {nuovi_tot} contatti nuovi{coda} — {already} in lista",
            )
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
        emit_event(campaign_id, "scrape_stopped", f"Fase Lista inbox non avviata: {e}", level="error")
        return None
    except Exception as e:
        if is_challenge_exception(e) and account is not None:
            await isolate_challenged_account(db, campaign, account, e)
        else:
            logger.exception(f"[InboxLista] Errore campaign {campaign_id}: {e}")
            campaign.status = CampaignStatus.error
            campaign.updated_at = datetime.utcnow()
            await db.commit()
        return None
    finally:
        if cleanup is not None:
            try:
                await cleanup()
            except Exception as exc:
                logger.warning(f"[InboxLista] cleanup fallito: {exc}")
