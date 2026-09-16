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
