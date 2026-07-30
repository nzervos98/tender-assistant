from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.services.job_queue import enqueue_job, process_one_job

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)


def scheduled_job() -> None:
    db = SessionLocal()
    try:
        job, created = enqueue_job(db, job_type='ingest', payload={'days': get_settings().ingest_days_back, 'scheduled': True})
        db.commit()
        logger.info('Scheduled ingest %s: job=%s', 'queued' if created else 'already active', job.id)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception('Scheduled ingest failed: %s', exc)
    finally:
        db.close()


def scheduled_rescore_job() -> None:
    """Queue a local full rescore; active-job locking prevents duplicates."""
    db = SessionLocal()
    try:
        job, created = enqueue_job(db, job_type='rescore', payload={'scheduled': True})
        db.commit()
        logger.info('Scheduled rescore %s: job=%s', 'queued' if created else 'already active', job.id)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception('Scheduled rescore failed: %s', exc)
    finally:
        db.close()


def main() -> None:
    settings = get_settings()
    init_db()
    scheduler = BackgroundScheduler(timezone='Europe/Athens')
    scheduler.add_job(
        scheduled_job,
        'cron',
        hour=settings.schedule_hour,
        minute=settings.schedule_minute,
        coalesce=True,
        max_instances=1,
    )
    refresh_minutes = max(1, settings.score_refresh_minutes)
    scheduler.add_job(
        scheduled_rescore_job,
        'interval',
        minutes=refresh_minutes,
        next_run_time=datetime.now(ZoneInfo('Europe/Athens')),
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info('Scheduler running daily at %02d:%02d Europe/Athens', settings.schedule_hour, settings.schedule_minute)
    logger.info('Automatic rescore running at startup and every %s minutes', refresh_minutes)
    while True:
        processed = process_one_job()
        if not processed:
            time.sleep(2)


if __name__ == '__main__':
    main()
