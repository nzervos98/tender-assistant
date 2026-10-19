from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.jobs import scheduler
from app.models import BackgroundJob


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'scheduler.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_startup_catchup_queues_missing_daily_global_ingest(monkeypatch, tmp_path):
    factory = _factory(tmp_path)
    queued = []
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    monkeypatch.setattr(scheduler, 'now_local', lambda: datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(scheduler, 'today_local', lambda: datetime(2026, 9, 16).date())
    monkeypatch.setattr(scheduler, 'scheduled_job', lambda: queued.append(True))

    scheduler.startup_catchup_job()

    assert queued == [True]


def test_startup_catchup_skips_completed_daily_global_ingest(monkeypatch, tmp_path):
    factory = _factory(tmp_path)
    with factory() as db:
        db.add(
            BackgroundJob(
                id='daily-complete',
                job_type='ingest',
                status='completed',
                lock_key='ingest:profile:all',
                payload={'scheduled': True},
                result={'continuation_required': False},
                created_at=datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 9, 16, 7, 0, tzinfo=timezone.utc),
            )
        )
        db.commit()
    queued = []
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    monkeypatch.setattr(scheduler, 'now_local', lambda: datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(scheduler, 'today_local', lambda: datetime(2026, 9, 16).date())
    monkeypatch.setattr(scheduler, 'scheduled_job', lambda: queued.append(True))

    scheduler.startup_catchup_job()

    assert queued == []


def test_scheduled_cleanup_queues_safe_weekly_job(monkeypatch, tmp_path):
    factory = _factory(tmp_path)
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)

    scheduler.scheduled_cleanup_job()

    with factory() as db:
        job = db.query(BackgroundJob).filter_by(job_type='cleanup').one()
        assert job.status == 'queued'
        assert job.profile_id is None
        assert job.payload == {'scheduled': True, 'min_age_days': 30}


def test_startup_cleanup_catchup_queues_missing_weekly_cleanup(monkeypatch, tmp_path):
    factory = _factory(tmp_path)
    queued = []
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    monkeypatch.setattr(scheduler, 'now_local', lambda: datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(scheduler, 'scheduled_cleanup_job', lambda: queued.append(True))

    scheduler.startup_cleanup_catchup_job()

    assert queued == [True]


def test_startup_cleanup_catchup_skips_completed_weekly_cleanup(monkeypatch, tmp_path):
    factory = _factory(tmp_path)
    with factory() as db:
        db.add(
            BackgroundJob(
                id='weekly-cleanup-complete',
                job_type='cleanup',
                status='completed',
                lock_key='cleanup:profile:all',
                payload={'scheduled': True, 'min_age_days': 30},
                result={'deleted_orphan_tenders': 0},
                created_at=datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 9, 20, 5, 1, tzinfo=timezone.utc),
            )
        )
        db.commit()
    queued = []
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    monkeypatch.setattr(scheduler, 'now_local', lambda: datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(scheduler, 'scheduled_cleanup_job', lambda: queued.append(True))

    scheduler.startup_cleanup_catchup_job()

    assert queued == []
