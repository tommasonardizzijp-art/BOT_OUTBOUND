"""
Human behavior simulation for DM sending sessions.

Controls:
- Session structure (how many DMs per session, breaks between sessions)
- Time-of-day gating (only send during active hours)
- Distraction pauses
"""
import asyncio
import random
from datetime import datetime, timedelta
from loguru import logger
from app.utils.timing import (
    random_delay_seconds,
    distraction_pause_seconds,
    should_take_distraction_pause,
    session_break_seconds,
    session_message_count,
)
from app.config import settings


class SessionManager:
    """Tracks the current sending session and enforces human-like patterns."""

    def __init__(self):
        self.messages_in_session = 0
        self.session_limit = session_message_count()
        self.sessions_today = 0

    def is_active_hour(self) -> bool:
        """Check if current hour is within the configured active window.
        Uses timezone_offset_hours so active_hours_start/end are in local time, not UTC."""
        now = datetime.utcnow() + timedelta(hours=settings.timezone_offset_hours)
        return settings.active_hours_start <= now.hour < settings.active_hours_end

    def should_break_session(self) -> bool:
        """Returns True if we've hit the session message limit."""
        return self.messages_in_session >= self.session_limit

    async def wait_between_messages(self) -> None:
        """Wait a human-like delay before the next message."""
        if should_take_distraction_pause():
            pause = distraction_pause_seconds()
            logger.info(
                f"[human] Distraction pause: {pause:.0f}s "
                f"({pause/60:.1f} min) — simulating human distraction"
            )
            await asyncio.sleep(pause)
        else:
            delay = random_delay_seconds()
            logger.info(f"[human] Inter-message delay: {delay:.0f}s")
            await asyncio.sleep(delay)

    async def take_session_break(self) -> None:
        """Take a break between sessions, then start a new session."""
        break_s = session_break_seconds()
        logger.info(f"Session complete ({self.messages_in_session} messages). "
                    f"Taking {break_s/60:.0f} min break before next session.")
        self.messages_in_session = 0
        self.session_limit = session_message_count()
        self.sessions_today += 1
        await asyncio.sleep(break_s)

    async def take_session_break_interruptible(self, campaign_id: str, db) -> bool:
        """Like take_session_break but checks campaign status every 5s.
        Returns True if break completed normally, False if campaign was paused/stopped.

        Heartbeat: touches campaign.updated_at immediately + every 10 min during the break.
        Without this, the crash-recovery cron (auto-pauses after 30 min of inactivity)
        fires during normal session breaks (20-45 min) and wrongly pauses the campaign.
        """
        from sqlalchemy import select, update as sa_update
        from app.models.campaign import Campaign, CampaignStatus
        from datetime import datetime as _dt

        break_s = session_break_seconds()
        logger.info(f"Session complete ({self.messages_in_session} messages). "
                    f"Taking {break_s/60:.0f} min break (interruptible).")
        self.messages_in_session = 0
        self.session_limit = session_message_count()
        self.sessions_today += 1

        # Immediate heartbeat: ensure updated_at is fresh before the break starts.
        # Prevents crash-recovery cron from seeing stale updated_at right away.
        try:
            await db.execute(
                sa_update(Campaign)
                .where(Campaign.id == campaign_id)
                .values(updated_at=_dt.utcnow())
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"[SessionBreak] Initial heartbeat failed: {e}")

        elapsed = 0.0
        check_interval = 5.0
        heartbeat_interval = 600.0  # every 10 min — well within the 30 min crash window
        last_heartbeat = 0.0

        while elapsed < break_s:
            chunk = min(check_interval, break_s - elapsed)
            await asyncio.sleep(chunk)
            elapsed += chunk

            # Periodic heartbeat during the break
            if elapsed - last_heartbeat >= heartbeat_interval:
                try:
                    await db.execute(
                        sa_update(Campaign)
                        .where(Campaign.id == campaign_id)
                        .values(updated_at=_dt.utcnow())
                    )
                    await db.commit()
                    last_heartbeat = elapsed
                except Exception as e:
                    logger.warning(f"[SessionBreak] Heartbeat failed: {e}")

            db.expire_all()
            result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
            camp = result.scalar_one_or_none()
            if not camp or camp.status not in (CampaignStatus.running, CampaignStatus.scraping_and_running):
                return False
        return True

    def record_message_sent(self) -> None:
        self.messages_in_session += 1

    async def wait_until_active_hours(self, campaign_id: str | None = None, db=None) -> bool:
        """Wait in chunks until active hours; return False if campaign stops."""
        from app.models.campaign import Campaign, CampaignStatus
        from sqlalchemy import select, update as sa_update

        now_local = datetime.utcnow() + timedelta(hours=settings.timezone_offset_hours)
        if self.is_active_hour():
            return True

        next_start = now_local.replace(hour=settings.active_hours_start, minute=random.randint(0, 30), second=0)
        if next_start <= now_local:
            next_start = next_start + timedelta(days=1)

        wait_s = (next_start - now_local).total_seconds()
        logger.info(
            f"Outside active hours (local time {now_local.strftime('%H:%M')}). "
            f"Sleeping up to {wait_s/3600:.1f}h until {next_start.strftime('%H:%M')} local (interruptible)"
        )
        elapsed = 0.0
        last_heartbeat = 0.0
        while elapsed < wait_s:
            chunk = min(30.0, wait_s - elapsed)
            await asyncio.sleep(chunk)
            elapsed += chunk

            if db is not None and campaign_id is not None:
                if elapsed - last_heartbeat >= 600.0:
                    try:
                        await db.execute(
                            sa_update(Campaign)
                            .where(Campaign.id == campaign_id)
                            .values(updated_at=datetime.utcnow())
                        )
                        await db.commit()
                        last_heartbeat = elapsed
                    except Exception as e:
                        logger.warning(f"[ActiveHours] Heartbeat failed: {e}")

                db.expire_all()
                camp = await db.scalar(select(Campaign).where(Campaign.id == campaign_id))
                if not camp or camp.status not in (CampaignStatus.running, CampaignStatus.scraping_and_running):
                    return False

            if self.is_active_hour():
                return True

        return True
