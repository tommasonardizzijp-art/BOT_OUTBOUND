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


async def _single_inbox_account(db, campaign_id: str):
    """Ritorna l'unico account assegnato attivo per la campagna inbox, o solleva."""
    rows = (await db.execute(
        select(InstagramAccount)
        .join(CampaignAccount, CampaignAccount.account_id == InstagramAccount.id)
        .where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.is_active == True,  # noqa: E712
            CampaignAccount.role.in_(("scraping", "both")),
            InstagramAccount.status.in_((AccountStatus.active, AccountStatus.warming_up)),
        )
    )).scalars().all()
    if len(rows) != 1:
        raise ScrapeBudgetError(
            f"Campagna inbox richiede esattamente 1 account attivo (trovati {len(rows)})"
        )
    return rows[0]


async def build_inbox_source(db, campaign):
    """Costruisce la sorgente inbox per l'engine scelto.

    Ritorna (source, own_pk, account, cleanup) dove cleanup e' una coroutine
    factory da awaitare nel finally (chiude browser / rilascia sessione).
    """
    account = await _single_inbox_account(db, campaign.id)
    engine = getattr(campaign, "inbox_engine", "browser") or "browser"

    if engine == "api":
        client = await _login(account, db)
        own_pk = int(client.user_id)
        # cursore valido solo per engine api (oldest_cursor)
        cursor = campaign.scrape_cursor or None
        source = ApiInboxSource(client, own_pk, cursor=cursor)

        async def _cleanup():
            return None

        return source, own_pk, account, _cleanup

    # engine == "browser"
    from app.services.inbox_browser_source import build_browser_inbox_source
    src, own_pk_b, cleanup = await build_browser_inbox_source(db, campaign, account)
    return src, own_pk_b, account, cleanup


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
        emit_event(campaign_id, "scrape_start",
                   f"Fase Lista inbox avviata (engine {campaign.inbox_engine})")

        already = await db.scalar(
            select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
        ) or 0
        existing_ids = set((await db.execute(
            select(Follower.ig_user_id).where(Follower.campaign_id == campaign_id)
        )).scalars().all())
        since_break = 0

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

            # pacing API tra pagine (il browser gestisce il proprio pacing interno)
            if getattr(campaign, "inbox_engine", "browser") == "api":
                lo, hi = settings.inbox_api_page_delay_min_seconds, settings.inbox_api_page_delay_max_seconds
                await asyncio.sleep(random.uniform(lo, hi))

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

        engine = getattr(campaign, "inbox_engine", "browser") or "browser"
        if engine == "browser" and already == 0:
            # Engine browser non ancora operativo (selettori DOM + risoluzione
            # username->pk in verifica live): una run che esaurisce l'inbox senza
            # raccogliere nulla NON e' un successo. Segnalala come errore azionabile
            # invece di fingere "completata", cosi' l'operatore non crede che
            # l'account non abbia DM. Rimuovere questo guard quando il browser
            # engine sara' verificato live e capace di raccogliere contatti.
            campaign.status = CampaignStatus.error
            campaign.scrape_outcome = "browser_not_wired"
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(
                campaign_id,
                "scrape_stopped",
                "Engine browser non operativo (selettori/risoluzione pk in verifica live): 0 contatti raccolti. Usa l'engine API.",
                level="error",
            )
            return None
        campaign.status = CampaignStatus.ready
        campaign.updated_at = datetime.utcnow()
        await db.commit()
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
