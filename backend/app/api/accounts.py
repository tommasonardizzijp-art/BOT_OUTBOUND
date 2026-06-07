import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from app.database import get_db
from app.models.account import InstagramAccount, AccountStatus
from app.models.activity_log import ActivityLog
from app.schemas.account import AccountCreate, AccountUpdate, AccountResponse, ChallengeVerify
from app.utils.crypto import encrypt

router = APIRouter(prefix="/accounts", tags=["accounts"])


async def _active_campaign_names_for_account(account_id: str, db: AsyncSession) -> list[str]:
    from app.models.campaign import Campaign, CampaignStatus
    from app.models.campaign_account import CampaignAccount

    result = await db.execute(
        select(Campaign.name)
        .join(CampaignAccount, CampaignAccount.campaign_id == Campaign.id)
        .where(
            CampaignAccount.account_id == account_id,
            CampaignAccount.is_active == True,
            Campaign.status.in_(
                (
                    CampaignStatus.running,
                    CampaignStatus.scraping,
                    CampaignStatus.scraping_and_running,
                    CampaignStatus.scraping_break,
                )
            ),
        )
        .order_by(Campaign.name)
    )
    return [row[0] for row in result.all()]


def _raise_if_account_is_in_active_campaigns(campaign_names: list[str]) -> None:
    if not campaign_names:
        return

    names = ", ".join(f'"{name}"' for name in campaign_names[:3])
    suffix = " e altre" if len(campaign_names) > 3 else ""
    raise HTTPException(
        status_code=409,
        detail=(
            f"Account usato da campagne attive: {names}{suffix}. "
            "Metti in pausa quelle campagne prima di disabilitare o eliminare l'account."
        ),
    )


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InstagramAccount).order_by(InstagramAccount.created_at.desc()))
    return result.scalars().all()


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(data: AccountCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(
        select(InstagramAccount).where(InstagramAccount.username == data.username)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Account with this username already exists")

    account = InstagramAccount(
        username=data.username,
        encrypted_password=encrypt(data.password),
        proxy=data.proxy,
        daily_message_limit=data.daily_message_limit,
        notes=data.notes,
        status=AccountStatus.active,
    )
    db.add(account)

    log = ActivityLog(account_id=account.id, action="account_created", details=json.dumps({"username": data.username}))
    db.add(log)

    await db.commit()
    await db.refresh(account)
    return account


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: str, db: AsyncSession = Depends(get_db)):
    account = await _get_or_404(account_id, db)
    return account


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(account_id: str, data: AccountUpdate, db: AsyncSession = Depends(get_db)):
    account = await _get_or_404(account_id, db)

    # Proxy: distinguish "field absent" (keep current) from "explicit clear" (set None).
    # Empty string from frontend form treated as clear.
    if "proxy" in data.model_fields_set:
        account.proxy = data.proxy if (data.proxy and data.proxy.strip()) else None
    if data.daily_message_limit is not None:
        account.daily_message_limit = data.daily_message_limit
    if "notes" in data.model_fields_set:
        account.notes = data.notes if (data.notes and data.notes.strip()) else None
    if data.status is not None:
        if data.status == AccountStatus.disabled and account.status != AccountStatus.disabled:
            _raise_if_account_is_in_active_campaigns(
                await _active_campaign_names_for_account(account.id, db)
            )
        account.status = data.status
        if data.status == AccountStatus.active:
            account.cooldown_until = None

    account.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(account)
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: str, db: AsyncSession = Depends(get_db)):
    account = await _get_or_404(account_id, db)
    _raise_if_account_is_in_active_campaigns(
        await _active_campaign_names_for_account(account.id, db)
    )
    await db.delete(account)
    await db.commit()
    # BUG-NEW-15: clean up persistent Chromium profile to reclaim disk space
    import shutil
    from app.config import settings
    import os
    profile_dir = os.path.join(settings.browser_profiles_dir, account_id)
    if os.path.exists(profile_dir):
        shutil.rmtree(profile_dir, ignore_errors=True)
        from loguru import logger
        logger.info(f"Deleted browser profile for account {account_id} at {profile_dir}")


@router.get("/{account_id}/metrics")
async def get_account_metrics(account_id: str, db: AsyncSession = Depends(get_db)):
    """Return performance and health metrics for an account. M9."""
    from app.models.message import Message, MessageStatus

    account = await _get_or_404(account_id, db)
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    today_sent = await db.scalar(
        select(func.count(Message.id)).where(
            Message.account_id == account_id,
            Message.status == MessageStatus.sent,
            Message.sent_at >= today_start,
        )
    ) or 0

    total_sent = await db.scalar(
        select(func.count(Message.id)).where(
            Message.account_id == account_id,
            Message.status == MessageStatus.sent,
        )
    ) or 0

    total_failed = await db.scalar(
        select(func.count(Message.id)).where(
            Message.account_id == account_id,
            Message.status == MessageStatus.failed,
        )
    ) or 0

    ban_events = await db.scalar(
        select(func.count(ActivityLog.id)).where(
            ActivityLog.account_id == account_id,
            ActivityLog.action == "account_banned",
        )
    ) or 0

    challenge_events = await db.scalar(
        select(func.count(ActivityLog.id)).where(
            ActivityLog.account_id == account_id,
            ActivityLog.action == "login_challenge",
        )
    ) or 0

    # Count skipped followers in campaigns where this account was ever assigned
    # (best approximation — skipped followers don't track which account processed them)
    from app.models.campaign_account import CampaignAccount
    from app.models.follower import Follower, FollowerStatus
    ca_result = await db.execute(
        select(CampaignAccount.campaign_id).where(CampaignAccount.account_id == account_id)
    )
    campaign_ids = [row[0] for row in ca_result.all()]
    total_skipped = 0
    if campaign_ids:
        total_skipped = await db.scalar(
            select(func.count(Follower.id)).where(
                Follower.campaign_id.in_(campaign_ids),
                Follower.status == FollowerStatus.skipped,
            )
        ) or 0

    total_attempts = total_sent + total_failed + total_skipped
    success_rate = round((total_sent / total_attempts) * 100, 1) if total_attempts > 0 else 0.0

    return {
        "today_sent": today_sent,
        "today_limit": account.daily_message_limit,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "success_rate": success_rate,
        "ban_events": ban_events,
        "challenge_events": challenge_events,
        "warmup_day": account.warmup_day,
        "daily_message_count": account.daily_message_count,
    }


@router.get("/{account_id}/dm-count")
async def get_dm_count(account_id: str, db: AsyncSession = Depends(get_db)):
    """M8 lite: return DM inbox unread + pending request counts via instagrapi session.
    Uses _login() from scraper for fresh session (same logic as reply checker)."""
    import asyncio
    from loguru import logger

    account = await _get_or_404(account_id, db)

    if not account.session_data:
        raise HTTPException(status_code=400, detail="Sessione non disponibile. Effettua prima il login.")

    try:
        from app.services.scraper import _login

        # Use _login for session restore + refresh (same as reply checker)
        client = await _login(account, db)

        # Fetch inbox threads (page 1 only — lightweight)
        threads = await asyncio.to_thread(client.direct_threads, amount=20)
        unread_count = sum(getattr(t, "unread_count", 0) or 0 for t in threads)

        # Pending DM requests count
        try:
            pending_count = await asyncio.to_thread(client.direct_pending_count)
        except Exception:
            pending_count = 0

        return {
            "unread_count": unread_count,
            "pending_count": pending_count,
            "checked_at": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.warning(f"DM count failed for account {account_id}: {e}")
        raise HTTPException(status_code=503, detail=f"Impossibile recuperare conteggio DM: {str(e)[:120]}")


@router.post("/{account_id}/force-cancel-cooldown", response_model=AccountResponse)
async def force_cancel_cooldown(account_id: str, db: AsyncSession = Depends(get_db)):
    """Force-cancel an account's cooldown, setting it back to active immediately."""
    account = await _get_or_404(account_id, db)

    if account.status != AccountStatus.cooldown:
        raise HTTPException(status_code=400, detail="Account is not in cooldown")

    prev_cooldown = account.cooldown_until.isoformat() if account.cooldown_until else None
    account.status = AccountStatus.active
    account.cooldown_until = None
    account.updated_at = datetime.utcnow()

    log = ActivityLog(
        account_id=account.id,
        action="cooldown_force_cancelled",
        details=json.dumps({"previous_cooldown_until": prev_cooldown}),
    )
    db.add(log)
    await db.commit()
    await db.refresh(account)
    return account


@router.post("/{account_id}/test-connection")
async def test_connection(account_id: str, db: AsyncSession = Depends(get_db)):
    """Probe the account's real egress IP/ISP through its proxy (or WiFi if none).

    Same egress path the bot uses for this account: request via the account's
    proxy reveals the public IP Instagram would see. Lets the operator confirm
    proxied accounts exit on a different (mobile) IP than the PC's WiFi.
    """
    import asyncio
    from app.utils.proxy_probe import probe_egress

    account = await _get_or_404(account_id, db)
    result = await asyncio.to_thread(probe_egress, account.proxy)
    result["account_id"] = account.id
    result["username"] = account.username
    return result


@router.post("/{account_id}/verify-challenge", response_model=AccountResponse)
async def verify_challenge(account_id: str, data: ChallengeVerify, db: AsyncSession = Depends(get_db)):
    account = await _get_or_404(account_id, db)
    if account.status != AccountStatus.challenge_required:
        raise HTTPException(status_code=400, detail="Account is not in challenge_required status")

    # The actual challenge submission is handled by the scraper service.
    # Here we just store the code in session_data for the worker to pick up.
    session_info = json.loads(account.session_data or "{}")
    session_info["pending_challenge_code"] = data.code
    account.session_data = json.dumps(session_info)
    account.updated_at = datetime.utcnow()

    log = ActivityLog(account_id=account.id, action="challenge_code_submitted")
    db.add(log)
    await db.commit()
    await db.refresh(account)
    return account


@router.post("/{account_id}/login", response_model=AccountResponse)
async def login_account(account_id: str, db: AsyncSession = Depends(get_db)):
    """Manually attempt Instagram login via instagrapi API.
    WARNING: This uses automated login which may trigger IP bans.
    Prefer /manual-login (browser-based) for safer login."""
    import asyncio
    from app.utils.crypto import decrypt

    account = await _get_or_404(account_id, db)

    try:
        from instagrapi import Client
        from instagrapi.exceptions import ChallengeRequired, BadPassword, LoginRequired

        client = Client()
        if account.proxy:
            client.set_proxy(account.proxy)

        # Try session restore first
        if account.session_data:
            try:
                session = json.loads(account.session_data)
                client.set_settings(session)
                client.login(account.username, decrypt(account.encrypted_password), relogin=False)

                # Verify session is valid by making a test call
                await asyncio.to_thread(client.account_info)

                account.session_data = json.dumps(client.get_settings())
                account.last_login_at = datetime.utcnow()
                account.status = AccountStatus.active
                await db.commit()
                await db.refresh(account)

                log = ActivityLog(account_id=account.id, action="login_success",
                                  details=json.dumps({"method": "session_restore"}))
                db.add(log)
                await db.commit()
                return account
            except Exception:
                pass  # Session expired, try full login

        # Full login
        password = decrypt(account.encrypted_password)
        try:
            await asyncio.to_thread(client.login, account.username, password)
        finally:
            del password

        account.session_data = json.dumps(client.get_settings())
        account.last_login_at = datetime.utcnow()
        account.status = AccountStatus.active
        account.updated_at = datetime.utcnow()

        log = ActivityLog(account_id=account.id, action="login_success",
                          details=json.dumps({"method": "full_login"}))
        db.add(log)
        await db.commit()
        await db.refresh(account)
        return account

    except ChallengeRequired:
        account.status = AccountStatus.challenge_required
        account.updated_at = datetime.utcnow()
        log = ActivityLog(account_id=account.id, action="login_challenge")
        db.add(log)
        await db.commit()
        await db.refresh(account)
        raise HTTPException(status_code=403, detail="Instagram richiede verifica. Controlla email/SMS dell'account.")

    except BadPassword:
        raise HTTPException(status_code=401, detail="Password errata o IP bloccato da Instagram. Prova da un'altra rete.")

    except Exception as e:
        error_msg = str(e)
        if "blacklist" in error_msg.lower() or "ip" in error_msg.lower():
            raise HTTPException(status_code=403, detail="IP bloccato da Instagram. Cambia rete (es. hotspot mobile).")
        raise HTTPException(status_code=500, detail=f"Login fallito: {error_msg}")


@router.post("/{account_id}/manual-login", response_model=AccountResponse)
async def manual_login_account(account_id: str, db: AsyncSession = Depends(get_db)):
    """Open a real browser for manual Instagram login.
    The user logs in themselves — no automated API calls, no IP ban risk.
    The bot captures cookies and converts them to an instagrapi-compatible session."""
    import asyncio
    import traceback
    from loguru import logger
    from app.services.manual_login import manual_browser_login_sync

    account = await _get_or_404(account_id, db)

    try:
        # Run browser in a separate thread with its own event loop
        # to avoid conflicts with uvicorn's asyncio loop
        settings_dict = await asyncio.to_thread(
            manual_browser_login_sync, account.id, account.username
        )

        account.session_data = json.dumps(settings_dict)
        account.last_login_at = datetime.utcnow()
        account.status = AccountStatus.active
        account.updated_at = datetime.utcnow()

        log = ActivityLog(
            account_id=account.id,
            action="manual_login_success",
            details=json.dumps({"method": "browser", "cookies_count": len(settings_dict.get("cookies", {}))}),
        )
        db.add(log)
        await db.commit()
        await db.refresh(account)
        return account

    except TimeoutError as e:
        raise HTTPException(status_code=408, detail=str(e))
    except RuntimeError as e:
        logger.error(f"Manual login RuntimeError: {e}\n{traceback.format_exc()}")
        detail = str(e) or "Errore sconosciuto durante il login browser. Controlla i log del backend."
        log = ActivityLog(
            account_id=account.id,
            action="manual_login_failed",
            details=json.dumps({"error": detail[:200]}),
        )
        db.add(log)
        await db.commit()
        raise HTTPException(status_code=422, detail=detail)
    except Exception as e:
        logger.error(f"Manual login error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        detail = f"Login manuale fallito: {type(e).__name__}: {e}"
        log = ActivityLog(
            account_id=account.id,
            action="manual_login_failed",
            details=json.dumps({"error": detail[:200]}),
        )
        db.add(log)
        await db.commit()
        raise HTTPException(status_code=500, detail=detail)


@router.post("/{account_id}/browse-session")
async def browse_session(account_id: str, db: AsyncSession = Depends(get_db), max_minutes: int = 60):
    """Open browser for manual organic activity (warm-up dormant accounts).
    Uses same profile + proxy + fingerprint as DM sender — IG sees consistent device.
    Browser stays open until user closes it (or max_minutes timeout).
    No automation — pure manual browsing."""
    import asyncio
    import traceback
    from loguru import logger
    from app.services.manual_login import manual_browse_session_sync

    account = await _get_or_404(account_id, db)

    try:
        result = await asyncio.to_thread(
            manual_browse_session_sync, account.id, account.username, max_minutes
        )
        log = ActivityLog(
            account_id=account.id,
            action="manual_browse_session",
            details=json.dumps(result),
        )
        db.add(log)
        await db.commit()
        return result
    except Exception as e:
        logger.error(f"Browse session error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        detail = f"Sessione browse fallita: {type(e).__name__}: {e}"
        log = ActivityLog(
            account_id=account.id,
            action="manual_browse_failed",
            details=json.dumps({"error": detail[:200]}),
        )
        db.add(log)
        await db.commit()
        raise HTTPException(status_code=500, detail=detail)


@router.get("/{account_id}/check-session")
async def check_session(account_id: str, db: AsyncSession = Depends(get_db)):
    """Fast session validity check via instagrapi — no browser required (~2-3s).
    Returns {"valid": bool, "username": str}."""
    import asyncio
    from loguru import logger

    account = await _get_or_404(account_id, db)

    if not account.session_data:
        return {"valid": False, "username": account.username}

    try:
        from app.services.scraper import _login

        def _check():
            import json
            from instagrapi import Client
            settings = json.loads(account.session_data)
            client = Client()
            client.set_settings(settings)
            if account.proxy:
                client.set_proxy(account.proxy)
            # Web GQL verify (not mobile account_info) — avoids UFAC after
            # fresh manual login. See manual_login.py module docstring.
            client.user_info_by_username_gql(account.username)
            return True

        valid = await asyncio.to_thread(_check)
        return {"valid": valid, "username": account.username}
    except Exception as e:
        logger.debug(f"Session check failed for {account.username}: {type(e).__name__}")
        return {"valid": False, "username": account.username}


@router.post("/{account_id}/reset-session", response_model=AccountResponse)
async def reset_session(account_id: str, db: AsyncSession = Depends(get_db)):
    """Wipe browser profile + instagrapi session for this account.

    Use after UFAC challenge, ban recovery, proxy change, or 2+ failed logins.
    Account appears as a brand-new device to Instagram on next login.
    BLOCKED if account is currently used by a running/paused campaign — would
    destroy a live session mid-flight.

    Effects:
    - Deletes `browser_profiles/<account_id>/` (cookies, localStorage, cache,
      trust tokens, GPU shader, Service Worker)
    - Sets `session_data = NULL` (instagrapi mobile UUIDs regenerated on next login)
    - Resets status `challenge_required`/`banned` → `active` (re-login needed)
    - Logs `session_reset` activity
    """
    import shutil
    import os
    from app.config import settings as app_settings
    from app.models.campaign_account import CampaignAccount
    from app.models.campaign import Campaign, CampaignStatus

    account = await _get_or_404(account_id, db)

    # Block if account is in any campaign currently using a live session
    active_statuses = (
        CampaignStatus.scraping,
        CampaignStatus.scraping_break,
        CampaignStatus.scraping_and_running,
        CampaignStatus.running,
        CampaignStatus.paused,
    )
    in_use = await db.execute(
        select(Campaign.id, Campaign.name, Campaign.status)
        .join(CampaignAccount, CampaignAccount.campaign_id == Campaign.id)
        .where(
            CampaignAccount.account_id == account_id,
            CampaignAccount.is_active == True,  # noqa: E712
            Campaign.status.in_(active_statuses),
        )
    )
    blocking = in_use.first()
    if blocking:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Account in uso dalla campagna '{blocking.name}' (stato: {blocking.status.value}). "
                "Ferma la campagna o disattiva l'account su quella campagna prima del reset."
            ),
        )

    # Wipe browser profile directory
    profile_dir = os.path.join(app_settings.browser_profiles_dir, account_id)
    wiped_dir = False
    if os.path.isdir(profile_dir):
        try:
            shutil.rmtree(profile_dir, ignore_errors=False)
            wiped_dir = True
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Impossibile cancellare profilo browser ({e}). "
                    "Probabile che il browser sia ancora aperto. Chiudi finestre Chromium e riprova."
                ),
            )

    # Wipe instagrapi session + reset status if challenge/banned
    had_session = bool(account.session_data)
    account.session_data = None
    if account.status in (AccountStatus.challenge_required, AccountStatus.banned):
        account.status = AccountStatus.active
    account.last_login_at = None
    account.updated_at = datetime.utcnow()

    log = ActivityLog(
        account_id=account.id,
        action="session_reset",
        details=json.dumps({
            "browser_profile_wiped": wiped_dir,
            "session_data_wiped": had_session,
            "previous_status": account.status.value,
        }),
    )
    db.add(log)
    await db.commit()
    await db.refresh(account)
    return account


async def _get_or_404(account_id: str, db: AsyncSession) -> InstagramAccount:
    result = await db.execute(select(InstagramAccount).where(InstagramAccount.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account
