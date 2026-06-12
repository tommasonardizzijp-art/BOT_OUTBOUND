"""
Instagram account rotation and health management.

Responsibilities:
- Pick the next available account (round-robin, weighted by daily usage)
- Record success/failure per account
- Trigger cooldown when rate-limited
- Warm-up protocol for new accounts
"""
import json
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.account import InstagramAccount, AccountStatus
from app.models.activity_log import ActivityLog

# Cooldown escalation tiers (seconds)
COOLDOWN_TIERS = [
    30 * 60,    # 30 min — first rate limit
    2 * 3600,   # 2 hours — second within 24h
    12 * 3600,  # 12 hours — third+
]


def _parse_warmup_limits(spec: str) -> dict[range, int]:
    """Parse "1-3:5,4-7:12,..." into {range(1,4): 5, range(4,8): 12, ...}."""
    out: dict[range, int] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        days_part, _, limit_part = entry.partition(":")
        start_s, _, end_s = days_part.partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else start
        out[range(start, end + 1)] = int(limit_part)
    return out


def _parse_age_caps(spec: str) -> tuple[tuple[int, int | None], ...]:
    """Parse "0:0,3:3,7:8,14:none" into ((0,0),(3,3),(7,8),(14,None))."""
    items: list[tuple[int, int | None]] = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        day_s, _, limit_s = entry.partition(":")
        limit_s = limit_s.strip().lower()
        limit: int | None = None if limit_s in ("none", "null", "") else int(limit_s)
        items.append((int(day_s), limit))
    items.sort(key=lambda x: x[0])
    return tuple(items)


WARMUP_LIMITS = _parse_warmup_limits(settings.warmup_limits)
AGE_BASED_CAPS = _parse_age_caps(settings.age_based_caps)


def get_warmup_limit(warmup_day: int, default_limit: int) -> int:
    """Return the daily limit based on warmup day."""
    if warmup_day == 0:
        return default_limit
    for day_range, limit in WARMUP_LIMITS.items():
        if warmup_day in day_range:
            return limit
    return default_limit  # Past warm-up period


def get_age_based_cap(account_age_days: int) -> int | None:
    """Return hard daily cap based on account age in our system, or None if no cap."""
    cap = None
    for threshold_day, max_dms in AGE_BASED_CAPS:
        if account_age_days >= threshold_day:
            cap = max_dms
    return cap


PROVEN_ACCOUNT_THRESHOLD = settings.proven_account_threshold


def apply_safety_caps(
    base_limit: int,
    warmup_day: int,
    account_age_days: int,
    default_limit: int,
    total_messages_sent: int = 0,
) -> int:
    """
    Compose all daily-limit caps in correct precedence:
      1. base_limit (override or default)
      2. warmup curve (if warmup_day > 0)
      3. age-based hard cap (only if account NOT proven AND young)
    Returns the most restrictive limit.

    Age cap intentionally skipped if total_messages_sent >= PROVEN_ACCOUNT_THRESHOLD,
    because account.created_at = DB row creation, not real IG account age.
    Old IG accounts added recently to the bot would otherwise be wrongly capped.
    """
    effective = base_limit
    warmup_capped = get_warmup_limit(warmup_day, default_limit)
    if warmup_day > 0:
        effective = min(effective, warmup_capped)
    if total_messages_sent < PROVEN_ACCOUNT_THRESHOLD:
        age_cap = get_age_based_cap(account_age_days)
        if age_cap is not None:
            effective = min(effective, age_cap)
    return max(0, effective)


async def record_success(account_id: str, db: AsyncSession) -> None:
    """Record a successful DM sent. Uses atomic UPDATE to avoid race with multi-campaign workers."""
    now = datetime.utcnow()
    await db.execute(
        update(InstagramAccount)
        .where(InstagramAccount.id == account_id)
        .values(
            daily_message_count=InstagramAccount.daily_message_count + 1,
            total_messages_sent=InstagramAccount.total_messages_sent + 1,
            last_activity_at=now,
            updated_at=now,
        )
    )
    await db.commit()


async def record_failure(account_id: str, db: AsyncSession, error: str | None = None) -> None:
    """Record a failed DM (logs an activity row). Cooldown escalation is handled
    by the orchestrator, not here."""
    result = await db.execute(select(InstagramAccount).where(InstagramAccount.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        return

    account.last_activity_at = datetime.utcnow()

    # Log the failure
    log = ActivityLog(
        account_id=account_id,
        action="dm_failed",
        details=json.dumps({"error": error}) if error else None,
    )
    db.add(log)
    await db.commit()


async def apply_cooldown(account_id: str, db: AsyncSession, tier: int = 0) -> None:
    """Put an account in cooldown."""
    result = await db.execute(select(InstagramAccount).where(InstagramAccount.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        return

    cooldown_s = COOLDOWN_TIERS[min(tier, len(COOLDOWN_TIERS) - 1)]
    account.status = AccountStatus.cooldown
    account.cooldown_until = datetime.utcnow() + timedelta(seconds=cooldown_s)
    account.updated_at = datetime.utcnow()

    log = ActivityLog(
        account_id=account_id,
        action="cooldown_start",
        details=json.dumps({"duration_minutes": cooldown_s // 60, "tier": tier}),
    )
    db.add(log)
    await db.commit()
    logger.warning(f"Account @{account.username} in cooldown for {cooldown_s//60} minutes (tier {tier})")


async def mark_banned(account_id: str, db: AsyncSession) -> None:
    """Mark an account as banned."""
    result = await db.execute(select(InstagramAccount).where(InstagramAccount.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        return

    account.status = AccountStatus.banned
    account.updated_at = datetime.utcnow()

    log = ActivityLog(account_id=account_id, action="account_banned")
    db.add(log)
    await db.commit()
    logger.error(f"Account @{account.username} has been BANNED by Instagram")


async def advance_warmup_if_needed() -> None:
    """Advance warmup_day for warming_up accounts that haven't been advanced today.
    Uses warmup_advanced_date as an idempotent guard — safe to call at boot AND
    from the daily cron without double-advancing."""
    from app.database import AsyncSessionLocal
    today = datetime.utcnow().date().isoformat()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(InstagramAccount).where(
                InstagramAccount.warmup_day > 0,
                InstagramAccount.status == AccountStatus.warming_up,
            )
        )
        advanced = 0
        for acc in result.scalars().all():
            if acc.warmup_advanced_date == today:
                continue
            acc.warmup_day += 1
            acc.warmup_advanced_date = today
            if acc.warmup_day > 15:
                acc.warmup_day = 0
                acc.status = AccountStatus.active
                logger.info(f"[Warmup] @{acc.username} warm-up completato → active")
            else:
                logger.info(f"[Warmup] @{acc.username} warmup_day → {acc.warmup_day}")
            advanced += 1
        if advanced:
            await db.commit()
            logger.info(f"[Warmup] Avanzati {advanced} account")


async def release_expired_cooldowns(db: AsyncSession) -> None:
    """Re-activate accounts whose cooldown has expired."""
    from sqlalchemy import update
    now = datetime.utcnow()
    await db.execute(
        InstagramAccount.__table__.update()
        .where(
            InstagramAccount.status == AccountStatus.cooldown,
            InstagramAccount.cooldown_until <= now,
        )
        .values(status=AccountStatus.active, cooldown_until=None)
    )
    await db.commit()


def scrape_daily_limit_for(account, campaign) -> int:
    """Effective lookup cap for this account on this campaign."""
    override = getattr(campaign, "scrape_daily_limit", None)
    if override is not None and override > 0:
        return override
    return settings.scrape_daily_limit


def _utc_today_str() -> str:
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%d")


def effective_scrape_lookups(account) -> int:
    """Lookup di OGGI con reset lazy: se scrape_lookups_date != oggi (UTC) il
    contatore appartiene a un giorno passato e vale 0, senza dipendere dal cron
    daily_reset. Il reset persistito avviene al primo bump (bump_scrape_lookup)."""
    if getattr(account, "scrape_lookups_date", None) != _utc_today_str():
        return 0
    return getattr(account, "scrape_lookups_today", 0) or 0


def has_scrape_budget(account, campaign) -> bool:
    return effective_scrape_lookups(account) < scrape_daily_limit_for(account, campaign)


def bump_scrape_lookup(account) -> None:
    """Incremento in-memory date-aware del contatore lookup. Da chiamare prima del
    db.commit() del chiamante (scraper/import). Se il contatore e' di un giorno
    passato lo azzera e aggiorna la data prima di incrementare."""
    today = _utc_today_str()
    if getattr(account, "scrape_lookups_date", None) != today:
        account.scrape_lookups_today = 0
        account.scrape_lookups_date = today
    account.scrape_lookups_today = (account.scrape_lookups_today or 0) + 1


async def increment_scrape_lookup(db, account_id: str) -> None:
    """Atomic +1 on the account's daily scrape lookup counter (date-aware reset)."""
    from sqlalchemy import update, case
    from app.models.account import InstagramAccount
    today = _utc_today_str()
    await db.execute(
        update(InstagramAccount)
        .where(InstagramAccount.id == account_id)
        .values(
            # reset lazy: se la data salvata non e' oggi, riparti da 1, altrimenti +1
            scrape_lookups_today=case(
                (InstagramAccount.scrape_lookups_date == today,
                 InstagramAccount.scrape_lookups_today + 1),
                else_=1,
            ),
            scrape_lookups_date=today,
        )
    )
    await db.commit()
