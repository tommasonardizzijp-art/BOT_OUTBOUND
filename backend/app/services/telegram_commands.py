"""Telegram command polling for remote admin control.

Campaign pause/resume are intentionally campaign-scoped. Global halt/unhalt
remain available as explicit emergency commands.
"""
import json
from datetime import datetime, timedelta

import httpx
from loguru import logger
from sqlalchemy import select, func

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.account import AccountStatus, InstagramAccount
from app.models.activity_log import ActivityLog
from app.models.anomaly import Anomaly
from app.models.campaign import Campaign, CampaignStatus
from app.services.bot_state_service import get_state, halt, resume
from app.services.campaign_control import (
    CampaignControlError,
    list_pausable_campaigns,
    list_resumable_campaigns,
    pause_campaign_control,
    resume_campaign_control,
)
from app.services.notifier import send_telegram
from app.services.work_enqueue import reenqueue_active_work


_OFFSET_KEY = "bot_outbound:telegram:update_offset"
_LOCK_KEY = "bot_outbound:telegram:poll_lock"
_LOCK_TTL = 30  # seconds — auto-release if worker crashes


def _enabled() -> bool:
    return bool(
        settings.telegram_commands_enabled
        and settings.telegram_bot_token
        and settings.telegram_chat_id
    )


async def poll_telegram_commands(redis) -> int:
    """Poll Telegram getUpdates once. Returns number of accepted commands/callbacks."""
    if not _enabled():
        return 0

    # Distributed lock: skip if another instance is already polling.
    # Prevents 409 Conflict when backlogged cron ticks run concurrently.
    acquired = await redis.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL)
    if not acquired:
        return 0

    try:
        return await _do_poll(redis)
    finally:
        await redis.delete(_LOCK_KEY)


async def _do_poll(redis) -> int:
    offset_raw = await redis.get(_OFFSET_KEY)
    offset = int(offset_raw.decode() if isinstance(offset_raw, bytes) else offset_raw or 0)
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates"
    params = {
        "timeout": max(0, min(settings.telegram_poll_timeout_seconds, 20)),
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset:
        params["offset"] = offset

    try:
        async with httpx.AsyncClient(timeout=settings.telegram_poll_timeout_seconds + 5) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        logger.warning(f"[TelegramCommands] getUpdates failed: {exc}")
        return 0

    processed = 0
    max_update_id = offset - 1 if offset else 0
    for update in payload.get("result", []):
        update_id = int(update.get("update_id", 0))
        max_update_id = max(max_update_id, update_id)

        callback = update.get("callback_query")
        if callback:
            if await _handle_callback(callback):
                processed += 1
            continue

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if chat_id != str(settings.telegram_chat_id):
            continue
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        await _handle_command(text)
        processed += 1

    if max_update_id >= offset:
        await redis.set(_OFFSET_KEY, str(max_update_id + 1))

    return processed


async def _handle_command(text: str) -> None:
    command = text.split()[0].split("@")[0].lower()
    args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""

    if command == "/status":
        await _cmd_status()
    elif command == "/pause":
        await _cmd_pick_campaign("pause")
    elif command == "/resume":
        await _cmd_pick_campaign("resume")
    elif command == "/halt":
        reason = args or "Telegram /halt"
        async with AsyncSessionLocal() as db:
            await halt(reason=reason, kind="telegram_halt", by="telegram", db=db)
            await db.commit()
        await send_telegram(f"*Kill-switch attivato*\nReason: `{_clean(reason)}`", level="critical")
    elif command == "/unhalt":
        async with AsyncSessionLocal() as db:
            changed = await resume(by="telegram", db=db)
            await db.commit()
        counts = await reenqueue_active_work()
        suffix = (
            f"\nScrape jobs: `{counts['scrape_jobs']}`"
            f"\nDM workers: `{counts['dm_jobs']}`"
            f"\nBreak ripristinate: `{counts['breaks_restored']}`"
        )
        if counts.get("auto_paused"):
            suffix += f"\nCampagne auto-pausate (no account DM): `{counts['auto_paused']}`"
        await send_telegram(
            ("*Kill-switch sbloccato*" if changed else "*Kill-switch gia' spento*") + suffix,
            level="info",
        )
    elif command == "/logs":
        await _cmd_logs()
    elif command == "/anomalies":
        await _cmd_anomalies()
    else:
        await send_telegram(
            "Comandi: `/status`, `/pause`, `/resume`, `/halt [motivo]`, `/unhalt`, `/logs`, `/anomalies`",
            level="info",
        )


async def _handle_callback(callback: dict) -> bool:
    callback_id = str(callback.get("id") or "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if chat_id != str(settings.telegram_chat_id):
        return False

    data = str(callback.get("data") or "")
    await _answer_callback(callback_id)

    if data == "noop":
        return True
    if ":" not in data:
        await send_telegram("Azione Telegram non valida.", level="warning")
        return True

    action, campaign_id = data.split(":", 1)
    if action == "pause":
        async with AsyncSessionLocal() as db:
            try:
                campaign = await pause_campaign_control(
                    db, campaign_id, by="telegram", reason="Telegram /pause"
                )
            except CampaignControlError as exc:
                await send_telegram(f"*Pausa non eseguita*\n`{_clean(str(exc))}`", level="warning")
                return True
        await send_telegram(f"*Campagna in pausa*\n{_format_campaign(campaign)}", level="warning")
        return True

    if action == "resume":
        async with AsyncSessionLocal() as db:
            state = await get_state(db)
            if state.halted:
                await send_telegram(
                    "*Ripresa bloccata*: kill-switch globale attivo. Usa `/unhalt` prima.",
                    level="critical",
                )
                return True
            try:
                campaign, counts = await resume_campaign_control(
                    db, campaign_id, by="telegram", enqueue=True
                )
            except CampaignControlError as exc:
                await send_telegram(f"*Ripresa non eseguita*\n`{_clean(str(exc))}`", level="warning")
                return True
        await send_telegram(
            f"*Campagna ripresa*\n{_format_campaign(campaign)}"
            f"\nScrape jobs: `{counts['scrape_jobs']}`"
            f"\nDM workers: `{counts['dm_jobs']}`",
            level="info",
        )
        return True

    await send_telegram("Azione Telegram non riconosciuta.", level="warning")
    return True


async def _cmd_pick_campaign(action: str) -> None:
    async with AsyncSessionLocal() as db:
        state = await get_state(db)
        campaigns = (
            await list_pausable_campaigns(db)
            if action == "pause"
            else await list_resumable_campaigns(db)
        )

    if action == "resume" and state.halted:
        await send_telegram(
            "*Kill-switch attivo*: usa `/unhalt` prima di riprendere campagne.",
            level="critical",
        )
        return

    if not campaigns:
        label = "attive da mettere in pausa" if action == "pause" else "in pausa da riprendere"
        await send_telegram(f"Nessuna campagna {label}.", level="info")
        return

    title = "Scegli campagna da mettere in pausa" if action == "pause" else "Scegli campagna da riprendere"
    lines = [f"*{title}*"]
    for idx, campaign in enumerate(campaigns[:10], start=1):
        lines.append(f"{idx}. {_format_campaign(campaign)}")

    keyboard = [
        [
            {
                "text": f"{idx}. {_button_label(campaign)}",
                "callback_data": f"{action}:{campaign.id}",
            }
        ]
        for idx, campaign in enumerate(campaigns[:10], start=1)
    ]
    if len(campaigns) > 10:
        keyboard.append([{"text": f"+{len(campaigns) - 10} altre: usa dashboard", "callback_data": "noop"}])

    await send_telegram(
        "\n".join(lines),
        level="info",
        reply_markup={"inline_keyboard": keyboard},
    )


async def _cmd_status() -> None:
    async with AsyncSessionLocal() as db:
        state = await get_state(db)
        running_rows = (
            await db.execute(
                select(Campaign)
                .where(
                    Campaign.status.in_(
                        (
                            CampaignStatus.running,
                            CampaignStatus.scraping,
                            CampaignStatus.scraping_and_running,
                            CampaignStatus.scraping_break,
                        )
                    )
                )
                .order_by(Campaign.updated_at.desc())
                .limit(8)
            )
        ).scalars().all()
        paused_rows = (
            await db.execute(
                select(Campaign)
                .where(Campaign.status == CampaignStatus.paused)
                .order_by(Campaign.updated_at.desc())
                .limit(8)
            )
        ).scalars().all()
        active_accounts = await db.scalar(
            select(func.count(InstagramAccount.id)).where(
                InstagramAccount.status.in_((AccountStatus.active, AccountStatus.warming_up))
            )
        ) or 0
        flagged_accounts = await db.scalar(
            select(func.count(InstagramAccount.id)).where(
                InstagramAccount.status.in_(
                    (AccountStatus.cooldown, AccountStatus.challenge_required, AccountStatus.banned)
                )
            )
        ) or 0

    status = "HALTED" if state.halted else "RUNNING"
    reason = f"\nReason: `{_clean(state.halted_reason)}`" if state.halted_reason else ""
    lines = [
        f"*Status:* `{status}`{reason}",
        f"Account attivi: `{active_accounts}`",
        f"Account da controllare: `{flagged_accounts}`",
        "",
        f"*Campagne attive* `{len(running_rows)}`",
    ]
    lines.extend(_format_campaign(c) for c in running_rows[:8])
    lines.append("")
    lines.append(f"*Campagne in pausa* `{len(paused_rows)}`")
    lines.extend(_format_campaign(c) for c in paused_rows[:8])
    await send_telegram("\n".join(lines), level="critical" if state.halted else "info")


async def _cmd_logs() -> None:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(8)
            )
        ).scalars().all()

    if not rows:
        await send_telegram("Nessun activity log disponibile.", level="info")
        return

    lines = ["*Ultimi log*"]
    for row in rows:
        ts = row.created_at.strftime("%H:%M:%S")
        detail = (row.details or "").replace("\n", " ")[:90]
        lines.append(f"`{ts}` {_clean(row.action)} {_clean(detail)}")
    await send_telegram("\n".join(lines), level="info")


async def _cmd_anomalies() -> None:
    cutoff = datetime.utcnow() - timedelta(hours=24)
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Anomaly)
                .where(Anomaly.created_at >= cutoff)
                .order_by(Anomaly.created_at.desc())
                .limit(8)
            )
        ).scalars().all()

    if not rows:
        await send_telegram("Nessuna anomalia nelle ultime 24h.", level="info")
        return

    lines = ["*Anomalie ultime 24h*"]
    for row in rows:
        ts = row.created_at.strftime("%H:%M:%S")
        lines.append(f"`{ts}` {_clean(row.kind)} [{_clean(row.severity)}]")
    await send_telegram("\n".join(lines), level="warning")


async def _answer_callback(callback_id: str, text: str | None = None) -> None:
    if not callback_id:
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.debug(f"[TelegramCommands] answerCallbackQuery failed: {exc}")


def _format_campaign(campaign: Campaign) -> str:
    source = "lista importata" if campaign.source_type == "import" else f"@{_clean(campaign.target_username)}"
    return (
        f"*{_clean(campaign.name)}*"
        f" `[{campaign.status.value}]`"
        f" target=`{source}`"
        f" pending=`{campaign.messages_pending}` sent=`{campaign.messages_sent}`"
    )


def _button_label(campaign: Campaign) -> str:
    name = campaign.name[:28]
    return f"{name} [{campaign.status.value}]"


def _clean(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("`", "'").replace("*", "").replace("_", " ")[:500]
