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
        cron(check_replies, minute={0, 30}),
        cron(recover_sending, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(telegram_commands, minute=set(range(60))),
    ]
    queue_name = ARQ_CRON_QUEUE
    redis_settings = arq_redis_settings()
    max_jobs = 5
    # NON impostare keep_result=0: arq usa la persistenza del result key per il
    # dedup dei tick cron. A 0 ogni poll ri-accoda il tick "mancato" → loop.
    # Default arq (3600s) = dedup corretto.
