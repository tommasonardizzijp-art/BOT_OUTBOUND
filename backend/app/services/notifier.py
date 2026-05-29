"""Telegram notifier — fire-and-forget alert dispatch.

The notifier is best-effort: if Telegram is misconfigured or unreachable,
log and move on. It MUST NEVER raise, since it's called from error paths
inside the orchestrator and an exception there would mask the real bug.
"""
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
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


def _clip(value: Any, limit: int = 320) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_operator_details(details: dict[str, Any] | None) -> list[str]:
    if not details:
        return []

    rows: list[str] = []
    inflight = details.get("inflight")
    if isinstance(inflight, dict):
        rows.append(
            f"Messaggi in corso: `{inflight.get('sending', 0)}` | "
            f"lead bloccati: `{inflight.get('locked', 0)}`"
        )

    labels = {
        "previous_status": "Stato precedente",
        "count": "Conteggio",
        "username": "Profilo",
        "last_error": "Ultimo errore",
        "error": "Errore",
    }
    for key, label in labels.items():
        if key in details and details[key] not in (None, ""):
            value = _clip(details[key], 420 if key in ("last_error", "error") else 160)
            if key == "username" and not value.startswith("@"):
                value = f"@{value}"
            rows.append(f"{label}: `{value}`")
    return rows


_AUTO_PAUSE_COPY = {
    "worker_startup_requires_operator_resume": {
        "title": "Campagna messa in pausa al riavvio",
        "meaning": (
            "Il backend e' ripartito e ha trovato una campagna attiva ma senza "
            "un worker recente associato. Per sicurezza l'ha fermata invece di "
            "lasciarla sembrare attiva mentre non sta lavorando."
        ),
        "action": (
            "Controlla che Redis/worker siano avviati e poi usa Riprendi sulla campagna."
        ),
    },
    "zero_workers_enqueued": {
        "title": "Campagna fermata: nessun worker DM avviato",
        "meaning": (
            "La campagna era in stato running, ma al restart non e' stato accodato "
            "nessun profilo DM utilizzabile."
        ),
        "action": (
            "Verifica profili assegnati, stato account e ruolo DM/both, poi riprendi."
        ),
    },
    "dm_failed_streak": {
        "title": "Campagna fermata: troppi DM falliti di fila",
        "meaning": (
            "Lo stesso profilo ha fallito piu' invii consecutivi. Il bot si e' "
            "fermato per evitare di bruciare altri lead."
        ),
        "action": (
            "Apri il profilo Instagram, controlla eventuali blocchi o limiti, poi "
            "decidi se riattivare o sostituire il profilo."
        ),
    },
    "consecutive_unexpected_errors": {
        "title": "Campagna fermata: errori tecnici consecutivi",
        "meaning": (
            "Il worker ha incontrato errori inattesi di seguito. Spesso dipende da "
            "proxy, connessione, browser o sessione Instagram non stabile."
        ),
        "action": (
            "Controlla proxy/connessione e sessione del profilo prima di riprendere."
        ),
    },
}


async def send_campaign_auto_pause_alert(
    *,
    campaign_name: str | None = None,
    campaign_id: str | None = None,
    reason: str,
    level: str = "warning",
    account_username: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Send a readable operator alert when the system pauses a campaign."""
    copy = _AUTO_PAUSE_COPY.get(reason, {})
    title = copy.get("title", "Campagna messa in pausa automaticamente")
    meaning = copy.get(
        "meaning",
        "Il sistema ha rilevato una condizione di rischio e ha fermato la campagna.",
    )
    action = copy.get(
        "action",
        "Apri la campagna, controlla gli ultimi log e riprendi solo dopo la verifica.",
    )

    lines = [f"*{title}*"]
    if campaign_name:
        lines.append(f"Campagna: *{campaign_name}*")
    elif campaign_id:
        lines.append(f"Campagna ID: `{campaign_id}`")
    if account_username:
        username = account_username if account_username.startswith("@") else f"@{account_username}"
        lines.append(f"Profilo: `{username}`")

    lines.extend(
        [
            "",
            f"Cosa significa: {meaning}",
            f"Cosa fare: {action}",
        ]
    )
    detail_lines = _format_operator_details(details)
    if detail_lines:
        lines.append("")
        lines.extend(detail_lines)
    lines.append("")
    lines.append(f"Codice tecnico: `{reason}`")
    await send_telegram("\n".join(lines), level=level)


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
