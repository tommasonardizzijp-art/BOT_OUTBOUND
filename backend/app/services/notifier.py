"""Telegram notifier — fire-and-forget alert dispatch.

The notifier is best-effort: if Telegram is misconfigured or unreachable,
log and move on. It MUST NEVER raise, since it's called from error paths
inside the orchestrator and an exception there would mask the real bug.
"""
import asyncio
from datetime import datetime
from pathlib import Path
import httpx
from loguru import logger

from app.config import settings


_LEVEL_EMOJI = {
    "info": "ℹ️",
    "warn": "⚠️",
    "warning": "⚠️",
    "error": "🚨",
    "critical": "🔥",
}


def _telegram_enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def send_telegram(
    message: str,
    level: str = "info",
    *,
    reply_markup: dict | None = None,
) -> None:
    """Send `message` to the configured Telegram chat. Never raises."""
    if not _telegram_enabled():
        logger.debug("[Notifier] Telegram disabled (no token/chat_id) — skipping")
        return

    emoji = _LEVEL_EMOJI.get(level, "ℹ️")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    text = f"{emoji} {message}\n\n_{ts}_"
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    backoffs = [1, 2, 4]
    last_error: Exception | None = None
    for attempt, delay in enumerate(backoffs, start=1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return
                # Dynamic usernames/errors can contain Markdown control chars.
                # Prefer delivering a plain-text notification over dropping it.
                if resp.status_code == 400 and payload.get("parse_mode"):
                    plain_payload = dict(payload)
                    plain_payload.pop("parse_mode", None)
                    plain_resp = await client.post(url, json=plain_payload)
                    if plain_resp.status_code == 200:
                        logger.info("[Notifier] Telegram Markdown rejected; sent plain-text fallback")
                        return
                    resp = plain_resp
                # Retry on 5xx; bail on 4xx (config error) after logging once.
                if 400 <= resp.status_code < 500:
                    logger.warning(
                        f"[Notifier] Telegram returned {resp.status_code}: {resp.text[:200]}"
                    )
                    return
                last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            last_error = e
        if attempt < len(backoffs):
            await asyncio.sleep(delay)

    logger.warning(f"[Notifier] Telegram send failed after retries: {last_error}")


async def send_telegram_photo(photo_path: str, caption: str = "", level: str = "info") -> None:
    """Send a local image to Telegram via sendPhoto. Never raises."""
    if not _telegram_enabled():
        logger.debug("[Notifier] Telegram disabled (no token/chat_id) - skipping photo")
        return

    path = Path(photo_path)
    if not path.exists() or not path.is_file():
        logger.warning(f"[Notifier] Photo path not found: {photo_path}")
        return

    emoji = _LEVEL_EMOJI.get(level, _LEVEL_EMOJI["info"])
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    text = f"{emoji} {caption}\n\n_{ts}_"[:1024]
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
    data = {
        "chat_id": settings.telegram_chat_id,
        "caption": text,
        "parse_mode": "Markdown",
    }

    backoffs = [1, 2, 4]
    last_error: Exception | None = None
    for attempt, delay in enumerate(backoffs, start=1):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                with path.open("rb") as f:
                    files = {"photo": (path.name, f, "image/png")}
                    resp = await client.post(url, data=data, files=files)
                if resp.status_code == 200:
                    return
                if 400 <= resp.status_code < 500:
                    logger.warning(
                        f"[Notifier] Telegram sendPhoto returned {resp.status_code}: {resp.text[:200]}"
                    )
                    return
                last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            last_error = e
        if attempt < len(backoffs):
            await asyncio.sleep(delay)

    logger.warning(f"[Notifier] Telegram sendPhoto failed after retries: {last_error}")


async def capture_and_send_screenshot(page, *, label: str, caption: str, level: str = "error") -> str | None:
    """Capture a Playwright screenshot under data/ and send it to Telegram."""
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label)[:80]
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = Path("data") / f"telegram_{safe_label}_{ts}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True)
    except Exception as exc:
        logger.warning(f"[Notifier] screenshot capture failed: {exc}")
        return None

    await send_telegram_photo(str(path), caption=caption, level=level)
    return str(path)
