"""Anomaly detector — records events and reacts (hybrid auto-stop + Telegram).

Hybrid policy (chosen by user):
- Systemic CRITICAL patterns → trigger global kill-switch (BotState.halted=True).
- Single-account issues → stop/cooldown that account; pause only campaigns that
  have no remaining usable DM account.

Critical kinds:
- `account_banned` × N (per hour)
- `challenge` × N (per account, 24h)
- `consecutive_unexpected_errors` (worker self-pause already happened)
- `dm_failed_streak_global` — N campaigns each hit dm_failed_streak in last 1h

Warning kinds (single-target pause, no global halt):
- `dm_failed_streak` (single campaign)
- `dm_recovery_no_evidence_repeat` (after 3 in 1h on same account → pause acct)

Notify-only:
- `worker_crash` × N in 1h
- `dm_recovery_no_evidence` (single occurrence)
- `dm_aborted_pre_send` (defensive — pre-Enter abort)

Hooks call `record_anomaly(...)` then `evaluate_and_react(...)`. Both are
wrapped in try/except by callers — anomaly detector failures must never
break the main flow.
"""
import json
from datetime import datetime, timedelta
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import settings
from app.models.activity_log import ActivityLog
from app.models.anomaly import Anomaly
from app.models.campaign import Campaign, CampaignStatus
from app.models.account import InstagramAccount, AccountStatus
from app.services.campaign_control import pause_campaigns_without_usable_dm_accounts
from app.services.notifier import send_campaign_auto_pause_alert, send_telegram
from app.services.bot_state_service import halt as _halt_bot
from app.utils.events import emit as emit_event


async def record_anomaly(
    db: AsyncSession,
    *,
    kind: str,
    severity: str = "info",
    campaign_id: str | None = None,
    account_id: str | None = None,
    details: dict | None = None,
) -> Anomaly:
    """Persist an anomaly. Caller is responsible for commit."""
    anomaly = Anomaly(
        kind=kind,
        severity=severity,
        campaign_id=campaign_id,
        account_id=account_id,
        details=json.dumps(details or {}),
        created_at=datetime.utcnow(),
    )
    db.add(anomaly)
    await db.flush()
    return anomaly


async def _resolve_names(
    db: AsyncSession, *, campaign_id: str | None, account_id: str | None
) -> tuple[str | None, str | None]:
    """Best-effort fetch of campaign name and account username for nicer messages."""
    campaign_name = None
    username = None
    if campaign_id:
        c = await db.scalar(select(Campaign.name).where(Campaign.id == campaign_id))
        campaign_name = c
    if account_id:
        u = await db.scalar(
            select(InstagramAccount.username).where(InstagramAccount.id == account_id)
        )
        username = u
    return campaign_name, username


_ANOMALY_COPY = {
    "account_banned": {
        "title": "Profilo Instagram bannato",
        "meaning": "Instagram ha segnalato il profilo come bannato. Il worker e' stato fermato.",
        "action": "Non riprendere quel profilo finche' non hai verificato l'account.",
    },
    "challenge": {
        "title": "Instagram chiede una verifica",
        "meaning": "Il profilo deve completare un controllo/challenge prima di continuare.",
        "action": "Apri la sessione del profilo, completa la verifica e poi riattiva l'account.",
    },
    "dm_failed_streak": {
        "title": "Troppi DM falliti di fila",
        "meaning": "Gli ultimi invii del profilo sono falliti consecutivamente.",
        "action": "Controlla profilo, proxy e limiti Instagram prima di riprendere la campagna.",
    },
    "worker_crash": {
        "title": "Worker in crash ripetuto",
        "meaning": "Un processo che invia DM si e' chiuso piu' volte nell'ultima ora.",
        "action": "Controlla log backend, Redis/worker e sessione browser del profilo.",
    },
    "consecutive_unexpected_errors": {
        "title": "Campagna fermata per errori tecnici consecutivi",
        "meaning": "Il worker ha incontrato errori inattesi di seguito e ha fermato la campagna.",
        "action": "Verifica proxy, connessione, browser e sessione Instagram prima di riprendere.",
    },
    "dm_recovery_no_evidence": {
        "title": "DM non confermato, rimesso in retry",
        "meaning": (
            "Un messaggio era rimasto in invio. Il controllo recovery non lo ha trovato "
            "tra i DM inviati, quindi lo ha rimesso in coda una sola volta."
        ),
        "action": "Nessuna azione urgente: tienilo d'occhio se si ripete sullo stesso profilo.",
    },
    "dm_recovery_no_evidence_repeat": {
        "title": "Recovery DM ripetuta sullo stesso profilo",
        "meaning": "Piu' messaggi non risultano confermati per lo stesso profilo.",
        "action": "Controlla la sessione del profilo e valuta una pausa/cooldown.",
    },
    "dm_recovery_giveup": {
        "title": "DM non confermato dopo il retry",
        "meaning": (
            "Il bot aveva gia' riprovato quel messaggio, ma non ha trovato evidenza "
            "che sia stato consegnato. Lo ha marcato fallito per evitare retry infiniti."
        ),
        "action": (
            "Controlla il lead manualmente se e' importante. La campagna non viene "
            "messa in pausa solo per questo evento."
        ),
    },
    "dm_recovery_instagrapi_error": {
        "title": "Recovery DM non riuscita",
        "meaning": "Il controllo automatico dei DM non e' riuscito a leggere la conversazione.",
        "action": "Se si ripete, controlla sessione Instagram e connettivita' del profilo.",
    },
    "dm_aborted_pre_send": {
        "title": "DM annullato prima dell'invio",
        "meaning": "Il bot ha interrotto l'invio prima di premere Enter, quindi il DM non e' partito.",
        "action": "Di solito non serve intervenire: il lead resta disponibile per un retry.",
    },
}


def _details_from_anomaly(anomaly: Anomaly) -> dict:
    try:
        return json.loads(anomaly.details or "{}")
    except Exception:
        return {}


def _short(value, limit: int = 260) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_detail_lines(details: dict) -> list[str]:
    if not details:
        return []
    labels = {
        "follower": "Lead",
        "username": "Profilo",
        "message_id": "Messaggio ID",
        "retry_count": "Retry",
        "streak_size": "Fallimenti di fila",
        "count": "Conteggio",
        "last_error": "Ultimo errore",
        "error": "Errore",
    }
    lines: list[str] = []
    for key, label in labels.items():
        if key not in details or details[key] in (None, ""):
            continue
        value = _short(details[key], 420 if key in ("last_error", "error") else 180)
        if key in ("follower", "username") and not value.startswith("@"):
            value = f"@{value}"
        lines.append(f"{label}: `{value}`")
    return lines


def _format_message(
    anomaly: Anomaly,
    *,
    campaign_name: str | None = None,
    account_username: str | None = None,
    extra: str | None = None,
) -> str:
    copy = _ANOMALY_COPY.get(anomaly.kind, {})
    title = copy.get("title", anomaly.kind)
    meaning = copy.get(
        "meaning",
        "Il sistema ha rilevato un'anomalia durante il lavoro della campagna.",
    )
    action = copy.get(
        "action",
        "Controlla gli ultimi log della campagna e del profilo coinvolto.",
    )
    details = _details_from_anomaly(anomaly)

    lines = [f"*{title}*"]
    if campaign_name:
        lines.append(f"Campagna: *{campaign_name}*")
    if account_username:
        username = account_username if account_username.startswith("@") else f"@{account_username}"
        lines.append(f"Profilo: `{username}`")

    lines.extend(["", f"Cosa significa: {meaning}", f"Cosa fare: {action}"])
    detail_lines = _format_detail_lines(details)
    if detail_lines:
        lines.append("")
        lines.extend(detail_lines)
    if extra:
        lines.append("")
        lines.append(extra)
    lines.append("")
    lines.append(f"Codice tecnico: `{anomaly.kind}` | livello: `{anomaly.severity}`")
    return "\n".join(lines)


async def _count_recent(
    db: AsyncSession, *, kind: str, since: datetime,
    campaign_id: str | None = None, account_id: str | None = None,
) -> int:
    stmt = select(func.count(Anomaly.id)).where(
        Anomaly.kind == kind, Anomaly.created_at >= since
    )
    if campaign_id is not None:
        stmt = stmt.where(Anomaly.campaign_id == campaign_id)
    if account_id is not None:
        stmt = stmt.where(Anomaly.account_id == account_id)
    return await db.scalar(stmt) or 0


async def _global_halt(db: AsyncSession, anomaly: Anomaly, reason: str) -> str:
    """Set the global kill-switch. Workers and scrapers stop on their next check."""
    await _halt_bot(
        reason=reason,
        kind=anomaly.kind,
        by="anomaly_detector",
        db=db,
    )
    return (
        "Reazione automatica: kill-switch globale attivo. "
        "Per ripartire serve riattivare il bot da UI/admin o Telegram /unhalt."
    )


async def _pause_one(db: AsyncSession, campaign_id: str) -> bool:
    result = await db.execute(
        update(Campaign).where(
            Campaign.id == campaign_id,
            Campaign.status.in_((CampaignStatus.running, CampaignStatus.scraping_and_running)),
        ).values(status=CampaignStatus.paused, updated_at=datetime.utcnow())
    )
    if (result.rowcount or 0) > 0:
        db.add(
            ActivityLog(
                campaign_id=campaign_id,
                action="campaign_auto_paused",
                details=json.dumps({"reason": "dm_failed_streak"}),
            )
        )
        emit_event(
            campaign_id,
            "campaign_auto_paused",
            "Campagna auto-pausa per streak di DM falliti consecutivi.",
            level="warn",
        )
    return (result.rowcount or 0) > 0


async def evaluate_and_react(db: AsyncSession, anomaly: Anomaly) -> None:
    """Apply auto-stop rules and dispatch Telegram alert. Caller commits."""
    auto_stop = settings.anomaly_auto_stop_enabled
    campaign_name, account_username = await _resolve_names(
        db, campaign_id=anomaly.campaign_id, account_id=anomaly.account_id
    )
    extra: str | None = None
    notify_level = anomaly.severity
    sent_direct_alert = False

    try:
        if anomaly.kind == "account_banned" and anomaly.account_id:
            since = datetime.utcnow() - timedelta(hours=1)
            count = await _count_recent(
                db, kind="account_banned", since=since
            )
            paused = await pause_campaigns_without_usable_dm_accounts(db, anomaly.account_id)
            extra = (
                f"Reazione automatica: profilo isolato. Campagne senza altri profili DM "
                f"utilizzabili messe in pausa: `{paused}`."
            )
            notify_level = "critical"
            if auto_stop and count >= settings.anomaly_ban_threshold_per_hour:
                extra = await _global_halt(
                    db,
                    anomaly,
                    f"Account banned threshold reached ({count}/h)",
                )
                notify_level = "critical"

        elif anomaly.kind == "challenge" and anomaly.account_id:
            since = datetime.utcnow() - timedelta(hours=24)
            count = await _count_recent(
                db, kind="challenge", since=since, account_id=anomaly.account_id
            )
            paused = await pause_campaigns_without_usable_dm_accounts(db, anomaly.account_id)
            extra = (
                f"Reazione automatica: profilo in verifica. Campagne senza altri profili DM "
                f"utilizzabili messe in pausa: `{paused}`."
            )
            notify_level = "error"
            if auto_stop and count >= settings.anomaly_challenge_threshold_per_day:
                extra = await _global_halt(
                    db,
                    anomaly,
                    f"Challenge threshold reached ({count}/24h) for account_id={anomaly.account_id}",
                )
                notify_level = "critical"

        elif anomaly.kind == "dm_failed_streak" and anomaly.campaign_id:
            if auto_stop:
                paused = await _pause_one(db, anomaly.campaign_id)
                if paused:
                    details = _details_from_anomaly(anomaly)
                    await send_campaign_auto_pause_alert(
                        campaign_name=campaign_name,
                        campaign_id=anomaly.campaign_id,
                        reason="dm_failed_streak",
                        level="error",
                        account_username=account_username,
                        details=details,
                    )
                    sent_direct_alert = True
                    extra = "Reazione automatica: campagna messa in pausa dopo DM falliti consecutivi."
                    notify_level = "error"

        elif anomaly.kind == "worker_crash":
            since = datetime.utcnow() - timedelta(hours=1)
            count = await _count_recent(db, kind="worker_crash", since=since)
            if count >= settings.anomaly_worker_crash_threshold_per_hour:
                extra = f"_Worker crashes in last hour:_ {count}"
                notify_level = "error"
            else:
                # Below threshold — don't notify each crash, just record.
                return

        elif anomaly.kind == "consecutive_unexpected_errors":
            if auto_stop:
                extra = await _global_halt(
                    db,
                    anomaly,
                    "Consecutive unexpected worker errors reached safety threshold",
                )
                notify_level = "critical"
            else:
                notify_level = "error"

        elif anomaly.kind == "dm_recovery_no_evidence_repeat" and anomaly.account_id:
            since = datetime.utcnow() - timedelta(hours=1)
            count = await _count_recent(
                db, kind="dm_recovery_no_evidence_repeat", since=since,
                account_id=anomaly.account_id,
            )
            if auto_stop and count >= 3:
                await db.execute(
                    update(InstagramAccount)
                    .where(InstagramAccount.id == anomaly.account_id)
                    .values(
                        status=AccountStatus.cooldown,
                        cooldown_until=datetime.utcnow() + timedelta(hours=1),
                    )
                )
                paused = await pause_campaigns_without_usable_dm_accounts(db, anomaly.account_id)
                extra = (
                    f"Reazione automatica: profilo in cooldown per 1 ora. Campagne senza "
                    f"altri profili DM utilizzabili messe in pausa: `{paused}`."
                )
                notify_level = "error"

        elif anomaly.kind == "dm_recovery_no_evidence":
            notify_level = "warn"

        # NOTE: we intentionally do NOT add a generic "halt on any critical severity" fallback.
        # Only the explicit kind branches above trigger the global kill-switch. Unknown kinds
        # with severity="critical" emit a Telegram alert but do NOT halt — safer default.

        # Send Telegram for severity warn+ or any reacted anomaly.
        if (notify_level in ("warn", "warning", "error", "critical") or extra) and not sent_direct_alert:
            msg = _format_message(
                anomaly,
                campaign_name=campaign_name,
                account_username=account_username,
                extra=extra,
            )
            await send_telegram(msg, level=notify_level)
    except Exception as e:
        logger.warning(f"[AnomalyDetector] reaction failed: {e}")


async def report_anomaly(
    db: AsyncSession,
    *,
    kind: str,
    severity: str = "info",
    campaign_id: str | None = None,
    account_id: str | None = None,
    details: dict | None = None,
) -> None:
    """Convenience: record + react + commit. Best-effort, never raises."""
    try:
        anomaly = await record_anomaly(
            db, kind=kind, severity=severity,
            campaign_id=campaign_id, account_id=account_id, details=details,
        )
        await evaluate_and_react(db, anomaly)
        await db.commit()
    except Exception as e:
        logger.warning(f"[AnomalyDetector] report failed (kind={kind}): {e}")
        try:
            await db.rollback()
        except Exception:
            pass
