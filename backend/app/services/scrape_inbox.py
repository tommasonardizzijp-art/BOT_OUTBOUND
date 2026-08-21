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
from sqlalchemy.exc import IntegrityError

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
    # (pk_vero, username_normalizzato, username_grezzo): righe gia' in DB con targa
    # provvisoria, a cui l'API puo' finalmente scrivere la targa vera. Lo username
    # grezzo serve al chiamante se la promozione va a vuoto e deve ripiegare
    # sull'inserimento: si salva quello che ha detto Instagram, non la sua
    # normalizzazione (e' cosi' che nascono le coppie "@mario"/"mario").
    promozioni: list[tuple[int, str, str]] = field(default_factory=list)
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
            esito.promozioni.append((pk, u, username))
        else:
            esito.collisioni_username.append(u)
            esito.nuovi.append((pk, username))
    return esito


# I task degli alert si tengono finche' non finiscono: senza un riferimento forte
# il garbage collector puo' cancellarne uno ancora in volo (stessa ragione, stessa
# soluzione di utils/events.py).
_ALERT_TASKS: set = set()


def _avvisa_telegram(testo: str) -> None:
    """Alert operativo. utils/events.emit manda a Telegram solo scrape_stopped di
    livello error, quindi un avviso che non ferma la campagna resterebbe nel feed
    della UI e nessuno lo vedrebbe: una campagna piantata sembrerebbe completata."""
    try:
        from app.services import notifier
        task = asyncio.create_task(
            notifier.send_telegram(f"[BOT OUTBOUND] {testo}", level="warn")
        )
        _ALERT_TASKS.add(task)
        task.add_done_callback(_ALERT_TASKS.discard)
    except Exception as exc:      # noqa: BLE001 — un alert non deve rompere il giro
        logger.debug(f"[InboxLista] telegram non inviato: {exc}")


async def _stato_campagna(db, campaign_id: str):
    """Fotografia delle righe della campagna: (quante, targhe, targa_per_username,
    id_per_username).

    Si rilegge a OGNI pagina, non una volta per sessione: una sessione dura fino a
    quindici pagine con pause fra l'una e l'altra, e in quei minuti il canale
    browser (che lavora la stessa campagna) puo' aver creato altre righe a targa
    provvisoria. Con una fotografia vecchia quelle righe sono invisibili e l'API le
    reinserisce — cioe' esattamente il doppione che questo codice deve impedire.

    Si tiene anche l'id della riga: la promozione deve colpire QUELLA riga, non
    ripescarla per username (in DB uno username puo' stare con la chiocciola o in
    maiuscolo: una WHERE sullo username grezzo non lo troverebbe, la promozione
    andrebbe a vuoto e il contatto — classificato come promozione, quindi mai
    inserito — sparirebbe in silenzio).
    """
    targa_per_username: dict[str, int] = {}
    id_per_username: dict[str, str] = {}
    existing_ids: set[int] = set()
    righe = (await db.execute(
        select(Follower.id, Follower.username, Follower.ig_user_id).where(
            Follower.campaign_id == campaign_id
        )
    )).all()
    for rid, uname, targa in righe:
        existing_ids.add(targa)
        u = normalizza_username(uname) if isinstance(uname, str) else ""
        if not u:
            continue
        targa_per_username[u] = targa
        id_per_username[u] = rid
    return len(righe), existing_ids, targa_per_username, id_per_username


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
        modo_cima = campaign.inbox_bottom_reached
        # Da dove riparte la sorgente: in discesa dalla frontiera salvata, in cima
        # sempre dall'alto. `scrape_cursor` non serve piu' a questo (significa
        # "Fase Lista interrotta a meta'" e lo legge campaign_control), ma le
        # campagne partite prima della 036 hanno li' l'unica frontiera che esiste:
        # si travasa una volta e non se ne parla piu'.
        if not modo_cima and not campaign.inbox_deep_cursor and campaign.scrape_cursor:
            campaign.inbox_deep_cursor = campaign.scrape_cursor
        campaign.scrape_cursor = campaign.inbox_deep_cursor if not modo_cima else None
        await db.commit()

        source, own_pk, account, cleanup = await build_inbox_source(db, campaign)
        emit_event(
            campaign_id, "scrape_start",
            "Fase Lista inbox avviata (API, giro di cima: solo DM nuovi)" if modo_cima
            else "Fase Lista inbox avviata (API, discesa verso i thread piu' vecchi)",
        )

        already, existing_ids, targa_per_username, id_per_username = await _stato_campagna(
            db, campaign_id
        )
        since_break = 0
        pagine_da_pausa = 0   # pagine lette dall'ultima pausa: e' il budget di sessione
        empty_streak = 0   # pagine consecutive senza contatti nuovi (drain-stop, modo cima)
        vuote_streak = 0   # pagine consecutive senza NESSUN partecipante (fine discesa)
        nuovi_tot = 0
        promossi_tot = 0
        cursore_precedente = campaign.inbox_deep_cursor
        drained = False
        fine_discesa = False

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
            # Fotografia fresca a ogni pagina: vedi _stato_campagna.
            already, existing_ids, targa_per_username, id_per_username = await _stato_campagna(
                db, campaign_id
            )
            esito = classifica_pagina(page.participants, existing_ids, targa_per_username)

            # PRIMA le promozioni, POI gli inserimenti. L'ordine conta: se nella
            # stessa pagina lo stesso username arriva con due pk diversi, uno finisce
            # in promozioni e l'altro in nuovi; inserendo per primo si toglierebbe di
            # mezzo la riga da promuovere e si finirebbe con TRE righe per una persona
            # sola, cioe' il doppione che questo codice deve chiudere.
            recuperi: list[tuple[int, str]] = []   # promozioni a vuoto: si inseriscono
            promossi = 0
            scartati = 0    # collisioni di targa: una riga per quel pk c'e' gia'
            for pk, u, username_grezzo in esito.promozioni:
                # Nessuna guardia "il pk e' gia' in lista" qui: esito.promozioni
                # nasce da inbox_collect, che quei pk li ha gia' scartati, quindi
                # sarebbe un ramo che non si esegue mai. La corsa vera e' contro una
                # riga scritta da un altro motore DOPO la rilettura, che nessuna
                # fotografia puo' vedere: la regge il savepoint qui sotto.
                rid = id_per_username.get(u)
                if rid is None:
                    recuperi.append((pk, username_grezzo))
                    continue
                righe_toccate = 0
                try:
                    async with db.begin_nested():
                        res = await db.execute(
                            update(Follower)
                            .where(Follower.id == rid, Follower.ig_user_id < 0)
                            .values(ig_user_id=pk, updated_at=datetime.utcnow())
                        )
                        righe_toccate = res.rowcount
                except IntegrityError:
                    # UNIQUE(campaign_id, ig_user_id): quel pk l'ha appena preso
                    # un'altra riga (Fase Bio browser, inbox browser). Il savepoint
                    # annulla SOLO questo contatto: senza, l'eccezione arriverebbe al
                    # commit e si perderebbe la pagina intera — e la frontiera
                    # salterebbe oltre, rendendo quei thread irraggiungibili.
                    logger.warning(
                        f"[InboxLista] @{u}: il pk {pk} e' gia' su un'altra riga "
                        "della campagna — promozione saltata."
                    )
                    scartati += 1
                    continue
                if righe_toccate == 0:
                    # La riga non c'e' piu' o non e' piu' provvisoria (promossa nel
                    # frattempo dalla Fase Bio, o cancellata). L'UPDATE non solleva
                    # niente: senza questo ramo il contatto risulterebbe promosso e
                    # il pk non finirebbe da nessuna parte.
                    logger.warning(
                        f"[InboxLista] @{u}: la riga da promuovere non e' piu' "
                        "provvisoria — inserisco il contatto invece di perderlo."
                    )
                    recuperi.append((pk, username_grezzo))
                    continue
                promossi += 1
                existing_ids.add(pk)
                targa_per_username[u] = pk
                logger.info(f"[InboxLista] @{u}: targa provvisoria promossa a pk reale {pk}")

            da_inserire = list(esito.nuovi) + recuperi
            stored = 0
            for pk, username in da_inserire:
                try:
                    async with db.begin_nested():
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
                        await db.flush()
                except IntegrityError:
                    # Stessa corsa dell'UPDATE sopra, dall'altro lato: qualcuno ha
                    # gia' scritto una riga con quel pk. Salta il contatto, non la
                    # pagina.
                    logger.warning(
                        f"[InboxLista] @{username}: pk {pk} gia' presente in campagna "
                        "— inserimento saltato."
                    )
                    scartati += 1
                    continue
                stored += 1
                existing_ids.add(pk)
                u = normalizza_username(username) if isinstance(username, str) else ""
                if u:
                    targa_per_username[u] = pk
            for u in esito.collisioni_username:
                logger.warning(
                    f"[InboxLista] @{u} esiste gia' con una targa REALE diversa: "
                    "username riassegnato dopo un rename, la nuova riga e' un'altra persona."
                )
            nuovi_tot += stored
            promossi_tot += promossi
            already += stored
            since_break += stored
            # Il drain-stop guarda al LAVORO fatto, non ai soli inserimenti: otto
            # pagine di sole promozioni sono il primo giro dopo questo fix, non un
            # inbox drenato.
            empty_streak = 0 if (stored or promossi) else empty_streak + 1
            vuote_streak = 0 if (page.threads_letti or page.participants) else vuote_streak + 1
            # In discesa la frontiera va nel suo campo; `scrape_cursor` serve a
            # campaign_control per sapere che il giro e' a meta', e viene azzerato
            # quando il giro chiude (vedi in fondo).
            if not modo_cima:
                # Il contatore delle pagine di discesa si incrementa QUI, insieme al
                # commit della pagina: scrivendolo dopo, il `db.refresh(campaign)` in
                # testa al giro successivo lo butterebbe via (refresh non fa
                # autoflush) e sopravviverebbe solo l'ultimo incremento di ogni
                # sessione — un tetto di 500 pagine diventerebbe un tetto di 500
                # sessioni, cioe' mesi. E solo in discesa: le passate di cima non
                # scendono, non devono consumare quel budget.
                campaign.inbox_deep_pages = (campaign.inbox_deep_pages or 0) + 1
            if not modo_cima and page.cursor:
                # Solo un cursore VERO fa avanzare la frontiera: una risposta senza
                # oldest_cursor (payload troncato, blip) la azzererebbe, e il giro
                # dopo ripartirebbe dalla cima ri-attraversando tutto l'inbox.
                campaign.inbox_deep_cursor = page.cursor
                campaign.scrape_cursor = page.cursor
            campaign.total_followers = already
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            logger.info(
                f"[InboxLista] pagina: {page.threads_letti} thread, {stored} nuovi, "
                f"{promossi} promossi, {esito.gia_presenti} gia' in lista, "
                f"{scartati} saltati per collisione (totale {already})"
            )
            if scartati:
                emit_event(
                    campaign_id, "scrape_warning",
                    f"{scartati} contatti saltati: un altro motore aveva gia' scritto "
                    "quelle stesse persone.",
                    level="warn",
                )
            if stored or promossi:
                emit_event(
                    campaign_id, "scrape_batch",
                    f"Inbox: {already}" + (f"/{campaign.list_target}" if campaign.list_target else "")
                    + (f" (+{promossi} gia' presi dal browser, ora con pk reale)" if promossi else ""),
                )

            if page.bottom_confirmed:
                # Il fondo VERO: IG ha detto has_older=False. Solo qui si alza
                # l'interruttore permanente.
                logger.info(f"[InboxLista] Fondo dell'inbox raggiunto ({already})")
                campaign.inbox_bottom_reached = True
                campaign.inbox_deep_cursor = None
                campaign.inbox_deep_pages = 0
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                break
            if page.exhausted:
                # Pagina finale senza fondo dichiarato (manca il cursore): ci si
                # ferma, ma NON si dichiara finito l'inbox — e non si spaccia per
                # una discesa completata, altrimenti e' indistinguibile da una
                # finita bene.
                logger.warning(f"[InboxLista] Nessun cursore per proseguire — mi fermo ({already})")
                fine_discesa = True
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
                _avvisa_telegram(
                    f"Fase Lista inbox ferma: il cursore di Instagram non avanza piu' "
                    f"({already} contatti in lista). La discesa non prosegue da sola."
                )
                break
            cursore_precedente = page.cursor

            # N pagine consecutive senza NESSUN thread: il giro si ferma. Il segnale
            # e' "pagine vuote", non "pagine senza contatti nuovi" — in discesa
            # attraversare gente gia' raccolta e' normale e non deve fermare niente.
            #
            # Fermarsi si', dichiarare il fondo NO: pagine vuote sono anche il
            # sintomo di un soft-block o di un payload degradato, e il fondo alza un
            # interruttore permanente. La frontiera resta dov'e': il giro dopo
            # riprende da li' invece di ricominciare dalla cima.
            if vuote_streak >= settings.inbox_empty_page_stop:
                logger.warning(
                    f"[InboxLista] {vuote_streak} pagine consecutive vuote — "
                    f"mi fermo senza dichiarare il fondo ({already})"
                )
                fine_discesa = True
                break

            # Tetto della DISCESA, non della sessione: il budget pagine qui sotto
            # chiude una sessione e ne fa ripartire un'altra, quindi da solo non
            # garantisce che la discesa finisca. Se IG continua a servire pagine
            # piene con cursori sempre diversi (inbox che cicla), senza questo si
            # scenderebbe per sempre, sessione dopo sessione.
            if not modo_cima and campaign.inbox_deep_pages >= settings.inbox_deep_max_pages:
                logger.warning(
                    f"[InboxLista] {campaign.inbox_deep_pages} pagine di discesa senza "
                    f"raggiungere il fondo — mi fermo ({already})"
                )
                emit_event(
                    campaign_id, "scrape_warning",
                    f"Discesa fermata dopo {campaign.inbox_deep_pages} pagine senza mai "
                    "raggiungere il fondo dell'inbox. La frontiera e' salva: si riprende "
                    "da li' rilanciando la lista.",
                    level="warn",
                )
                _avvisa_telegram(
                    f"Fase Lista inbox: {campaign.inbox_deep_pages} pagine di discesa "
                    "senza mai raggiungere il fondo. Giro fermato, frontiera conservata."
                )
                await db.commit()
                fine_discesa = True
                break

            if modo_cima and empty_streak >= settings.inbox_empty_page_stop:
                logger.info(
                    f"[InboxLista] {empty_streak} pagine consecutive senza lavoro "
                    f"— inbox gia' tutto raccolto ({already})"
                )
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
        # Il giro e' chiuso: `scrape_cursor` deve tornare a NULL. campaign_control lo
        # legge come "Fase Lista interrotta a meta'" e con quel campo valorizzato una
        # ripresa tornerebbe SEMPRE in lista, senza passare mai alla Fase Bio. La
        # frontiera della discesa resta in inbox_deep_cursor, che serve proprio a
        # sopravvivere alla fine del giro.
        campaign.scrape_cursor = None
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        coda = (
            f" · {promossi_tot} gia' presi dal browser, ora con pk reale" if promossi_tot else ""
        )
        if drained:
            emit_event(
                campaign_id, "scrape_complete",
                f"Inbox gia' tutto raccolto: 0 contatti nuovi{coda} "
                f"(rilette {empty_streak} pagine di duplicati). {already} in lista — per averne "
                "altri servono nuovi DM in entrata o una campagna scrape follower.",
                level="warn",
            )
        elif fine_discesa:
            emit_event(
                campaign_id, "scrape_complete",
                f"Discesa interrotta prima del fondo: {nuovi_tot} contatti nuovi{coda} — "
                f"{already} in lista. La posizione e' salva, rilancia la lista per "
                "riprendere da li'.",
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
