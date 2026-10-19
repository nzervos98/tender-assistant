from __future__ import annotations

import logging
import time
from datetime import datetime, time as datetime_time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import BackgroundJob
from app.services.job_queue import enqueue_job, process_one_job
from app.services.timezone import local_day_start, now_local, today_local

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)
CLEANUP_MIN_AGE_DAYS = 30
CLEANUP_WEEKDAY = 6  # Sunday
CLEANUP_HOUR = 4
CLEANUP_MINUTE = 30


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


def startup_catchup_job() -> None:
    """Queue today's ingest if the worker started after the scheduled time.

    An active global ingest is returned by enqueue_job's lock, so restarting in
    the middle of a continuation chain cannot create a duplicate run.
    """
    settings = get_settings()
    local_now = now_local()
    scheduled_at = datetime.combine(
        local_now.date(),
        datetime_time(hour=settings.schedule_hour, minute=settings.schedule_minute),
        tzinfo=local_now.tzinfo,
    )
    if local_now < scheduled_at:
        return
    db = SessionLocal()
    try:
        day_start = local_day_start(today_local())
        candidates = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.job_type == 'ingest',
                BackgroundJob.profile_id.is_(None),
                BackgroundJob.status == 'completed',
                BackgroundJob.finished_at >= day_start,
            )
            .order_by(BackgroundJob.finished_at.desc())
            .limit(200)
            .all()
        )
        completed = any(
            bool((job.payload or {}).get('scheduled'))
            and not bool((job.result or {}).get('continuation_required'))
            for job in candidates
        )
    finally:
        db.close()
    if completed:
        logger.info('Daily global ingest already completed; startup catch-up not needed')
        return
    logger.info('Daily global ingest is missing or incomplete; queueing startup catch-up')
    scheduled_job()


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


def scheduled_cleanup_job() -> None:
    """Queue a conservative weekly orphan-only database sweep."""
    db = SessionLocal()
    try:
        job, created = enqueue_job(
            db,
            job_type='cleanup',
            payload={'scheduled': True, 'min_age_days': CLEANUP_MIN_AGE_DAYS},
        )
        db.commit()
        logger.info('Scheduled orphan cleanup %s: job=%s', 'queued' if created else 'already active', job.id)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception('Scheduled orphan cleanup failed: %s', exc)
    finally:
        db.close()


def startup_cleanup_catchup_job() -> None:
    """Queue this week's sweep if the worker missed its Sunday schedule."""
    local_now = now_local()
    days_since_sunday = (local_now.weekday() - CLEANUP_WEEKDAY) % 7
    scheduled_date = local_now.date() - timedelta(days=days_since_sunday)
    scheduled_at = datetime.combine(
        scheduled_date,
        datetime_time(hour=CLEANUP_HOUR, minute=CLEANUP_MINUTE),
        tzinfo=local_now.tzinfo,
    )
    if local_now < scheduled_at:
        return

    db = SessionLocal()
    try:
        candidates = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.job_type == 'cleanup',
                BackgroundJob.profile_id.is_(None),
                BackgroundJob.created_at >= scheduled_at,
                BackgroundJob.status.in_(('queued', 'running', 'completed')),
            )
            .order_by(BackgroundJob.created_at.desc())
            .limit(100)
            .all()
        )
        already_scheduled = any(bool((job.payload or {}).get('scheduled')) for job in candidates)
    finally:
        db.close()
    if already_scheduled:
        logger.info('Weekly orphan cleanup already queued or completed; startup catch-up not needed')
        return
    logger.info('Weekly orphan cleanup is missing; queueing startup catch-up')
    scheduled_cleanup_job()


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
    scheduler.add_job(
        scheduled_cleanup_job,
        'cron',
        day_of_week='sun',
        hour=CLEANUP_HOUR,
        minute=CLEANUP_MINUTE,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info('Scheduler running daily at %02d:%02d Europe/Athens', settings.schedule_hour, settings.schedule_minute)
    logger.info('Automatic rescore running at startup and every %s minutes', refresh_minutes)
    startup_catchup_job()
    startup_cleanup_catchup_job()
    while True:
        processed = process_one_job()
        if not processed:
            time.sleep(2)


if __name__ == '__main__':
    main()
