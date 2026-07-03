"""
Real-time worker event feed via Redis.

The ARQ worker and FastAPI server run as separate processes, so we use Redis
(already required for the task queue) as a shared event store.

- emit() is called from the worker process (orchestrator, scraper)
- get_events() is called from the API process (FastAPI endpoint)

Events are stored as a Redis list per campaign, trimmed to last 500 entries,
and expire after 24h of inactivity.
"""
import json
import redis
from datetime import datetime


def _get_redis() -> redis.Redis:
    from app.config import settings
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


_COUNTER_KEY = "worker_event_counter"

# Riferimenti ai task Telegram fire-and-forget: senza, il GC puo' cancellare
# un task ancora in volo (pattern standard asyncio.create_task).
_ALERT_TASKS: set = set()


def emit(campaign_id: str, action: str, detail: str = "", level: str = "info") -> None:
    """Emit a structured worker event. Safe to call from any process."""
    try:
        r = _get_redis()
        event_id = r.incr(_COUNTER_KEY)
        event = {
            "id": event_id,
            "ts": datetime.utcnow().isoformat(timespec="seconds"),
            "campaign_id": campaign_id,
            "action": action,
            "detail": detail,
            "level": level,
        }
        key = f"campaign_events:{campaign_id}"
        r.rpush(key, json.dumps(event))
        r.ltrim(key, -500, -1)  # keep last 500 events per campaign
        r.expire(key, 86400)    # expire after 24h of inactivity
    except Exception:
        pass  # never crash the worker because of event logging

    # Ponte Telegram: ogni stop errore della fase scraping (lista/bio/import/
    # challenge) deve raggiungere l'operatore anche lontano dalla UI. Hook qui
    # e non nei singoli servizi: un punto solo copre tutti i call site, anche
    # futuri. Solo level=error: i warn (pausa globale, cap) sono attesi/benigni.
    if action == "scrape_stopped" and level == "error":
        try:
            import asyncio
            from app.services import notifier

            task = asyncio.get_running_loop().create_task(
                notifier.send_scrape_stop_alert(campaign_id, detail)
            )
            _ALERT_TASKS.add(task)
            task.add_done_callback(_ALERT_TASKS.discard)
        except Exception:
            pass  # nessun event loop (contesto sync) o notifier ko: mai rompere emit


def get_events(campaign_id: str, since_id: int = 0, limit: int = 200) -> list[dict]:
    """Return events for a campaign newer than since_id, most recent last."""
    try:
        r = _get_redis()
        key = f"campaign_events:{campaign_id}"
        raw_events = r.lrange(key, 0, -1)
        events = []
        for raw in raw_events:
            ev = json.loads(raw)
            if ev["id"] > since_id:
                events.append(ev)
        return events[-limit:]
    except Exception:
        return []
