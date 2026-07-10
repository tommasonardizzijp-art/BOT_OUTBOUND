"""Dedicated ARQ worker for cron jobs.

Run: arq app.workers.cron_worker.CronWorkerSettings
"""
from arq import cron

from app.services.work_enqueue import ARQ_CRON_QUEUE, arq_redis_settings
from app.workers.task_queue import (
    check_replies,
    daily_reset,
    recover_sending,
    release_stale_locks,
    telegram_commands,
)


class CronWorkerSettings:
    functions = []
    cron_jobs = [
        cron(daily_reset, hour=0, minute=5),
        cron(release_stale_locks, minute={0, 15, 30, 45}),
        # Reply-check UNA volta al giorno (era ogni 30 min): la lettura inbox via
        # API e' tracciabile come bot: girarla raramente riduce il footprint/rischio
        # checkpoint. Le risposte vengono comunque rilevate (marcate 'replied' in
        # modo permanente al primo passaggio). Ambito ristretto: solo campagne attive
        # + invii recenti (vedi reply_checker + reply_check_max_age_days).
        cron(check_replies, hour={13}, minute={0}),
        cron(recover_sending, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(telegram_commands, minute=set(range(60))),
    ]
    queue_name = ARQ_CRON_QUEUE
    redis_settings = arq_redis_settings()
    max_jobs = 5
    # NON impostare keep_result=0: arq usa la persistenza del result key per il
    # dedup dei tick cron. A 0 ogni poll ri-accoda il tick "mancato" → loop.
    # Default arq (3600s) = dedup corretto.
