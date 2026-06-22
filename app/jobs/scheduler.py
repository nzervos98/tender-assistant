from __future__ import annotations

import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.jobs.ingest import run_ingest

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)


def scheduled_job() -> None:
    try:
        logger.info('Scheduled ingest started')
        run_ingest()
    except Exception as exc:  # noqa: BLE001
        logger.exception('Scheduled ingest failed: %s', exc)


def main() -> None:
    settings = get_settings()
    scheduler = BackgroundScheduler(timezone='Europe/Athens')
    scheduler.add_job(scheduled_job, 'cron', hour=settings.schedule_hour, minute=settings.schedule_minute)
    scheduler.start()
    logger.info('Scheduler running daily at %02d:%02d Europe/Athens', settings.schedule_hour, settings.schedule_minute)
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    main()
