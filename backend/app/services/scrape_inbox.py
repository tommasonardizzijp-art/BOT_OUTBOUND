"""Fase Lista alternativa per scrape_mode=dm_threads: raccoglie i contatti dai
DM gia' avviati dell'account. Engine selezionabile (api/browser). Riusa lo stato
listing/listing_break, il session-break via Retry(defer) e il challenge handler.
"""
import asyncio
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.config import settings
from app.models.campaign import CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.account import InstagramAccount, AccountStatus
from app.models.follower import Follower, FollowerStatus
from app.services.bot_state_service import is_halted
from app.services.scraper import is_challenge_exception, isolate_challenged_account
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


async def _inbox_page_delay() -> None:
    """Pacing umano tra pagine inbox: lognormale (scroll attivo) + pausa lunga
    occasionale ("si ferma a leggere/rispondere"). Distribuzione bimodale:
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
        lo, hi = settings.inbox_api_page_delay_min_seconds, settings.inbox_api_page_delay_max_seconds
        mid = (lo + hi) / 2
        # lognormvariate(0, 0.9): mediana 1.0 -> mediana delay = mid (6s con 2-10),
        # sigma alto = varianza ampia; il clamp [lo, hi] tiene i bound.
        delay = min(hi, max(lo, random.lognormvariate(0, 0.9) * mid))
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
        source, own_pk, account, cleanup = await build_inbox_source(db, campaign)
        emit_event(campaign_id, "scrape_start", "Fase Lista inbox avviata (API)")

        already = await db.scalar(
            select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
        ) or 0
        existing_ids = set((await db.execute(
            select(Follower.ig_user_id).where(Follower.campaign_id == campaign_id)
        )).scalars().all())
        since_break = 0
        empty_streak = 0   # pagine consecutive con 0 contatti nuovi -> inbox drenato
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
            fresh = inbox_collect(page.participants, existing_ids)
            for pk, username in fresh:
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
            stored = len(fresh)
            already += stored
            since_break += stored
            empty_streak = 0 if stored else empty_streak + 1
            # cursore intra-engine (api: oldest_cursor; browser: marker)
            campaign.scrape_cursor = page.cursor
            campaign.total_followers = already
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            if stored:
                emit_event(campaign_id, "scrape_batch",
                           f"Inbox: {already}" + (f"/{campaign.list_target}" if campaign.list_target else ""))

            if page.exhausted:
                logger.info(f"[InboxLista] Inbox esaurito ({already})")
                campaign.scrape_cursor = None
                break

            # Drenaggio: N pagine consecutive con 0 contatti NUOVI = oltre questo
            # punto l'inbox e' tutta gente gia' in lista. IG puo' tenere has_older
            # sempre True, quindi 'exhausted' da solo non basta e la lista girerebbe
            # a vuoto per sempre in silenzio (il bug segnalato). Ci si ferma e si
            # AVVISA. Cursore azzerato: il prossimo giro riparte dal top e intercetta
            # eventuali DM nuovi arrivati nel frattempo.
            if empty_streak >= settings.inbox_empty_page_stop:
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

            if since_break >= settings.inbox_session_size:
                minutes = random.uniform(settings.inbox_break_min_minutes, settings.inbox_break_max_minutes)
                seconds = int(minutes * 60)
                campaign.scrape_break_prev_status = CampaignStatus.listing.value
                campaign.status = CampaignStatus.listing_break
                campaign.scrape_break_until = datetime.utcnow() + timedelta(seconds=seconds)
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                emit_event(campaign_id, "scrape_break", f"Pausa inbox {int(minutes)} min dopo {already}")
                return seconds

        campaign.status = CampaignStatus.ready
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        if drained:
            emit_event(
                campaign_id, "scrape_complete",
                f"Inbox gia' tutto raccolto: 0 nuovi contatti (rilette {empty_streak} pagine di duplicati). "
                f"{already} in lista — per averne altri servono nuovi DM in entrata o una campagna scrape follower.",
                level="warn",
            )
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
