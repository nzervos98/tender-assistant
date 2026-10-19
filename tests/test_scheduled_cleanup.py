from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.jobs import cleanup_db
from app.models import BackgroundJob, ClientProfile, Tender, TenderLink, TenderScore
from app.services.timezone import now_utc


def _db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_orphan_sweep_keeps_recent_scored_and_linked_tenders():
    db = _db()
    old_orphan = Tender(
        source='khmdhs_notice', source_reference='old-orphan', title='Old orphan',
        created_at=now_utc() - timedelta(days=60), updated_at=now_utc() - timedelta(days=45),
    )
    recent_orphan = Tender(
        source='khmdhs_notice', source_reference='recent-orphan', title='Recent orphan',
        created_at=now_utc() - timedelta(days=60), updated_at=now_utc() - timedelta(days=2),
    )
    linked = Tender(
        source='khmdhs_request', source_reference='linked-signal', title='Linked signal',
        created_at=now_utc() - timedelta(days=60), updated_at=now_utc() - timedelta(days=45),
    )
    scored = Tender(
        source='khmdhs_notice', source_reference='scored', title='Scored notice',
        created_at=now_utc() - timedelta(days=60), updated_at=now_utc() - timedelta(days=45),
    )
    target = Tender(
        source='khmdhs_notice', source_reference='target', title='Target notice',
        created_at=now_utc() - timedelta(days=60), updated_at=now_utc() - timedelta(days=45),
    )
    db.add_all([old_orphan, recent_orphan, linked, scored, target])
    db.flush()
    profile = ClientProfile(slug='cleanup-test-profile', name='Cleanup test', cpv_codes=['72000000-5'])
    db.add(profile)
    db.flush()
    db.add_all([
        TenderLink(
            source_tender_id=linked.id,
            related_tender_id=target.id,
            relation_type='request_to_notice',
            related_reference=target.source_reference,
        ),
        TenderScore(tender_id=scored.id, profile_id=profile.id, score=75, rule_score=75),
    ])
    db.commit()

    removed = cleanup_db._delete_orphans(db, dry_run=False, min_age_days=30)
    db.commit()

    assert removed == 1
    assert db.get(Tender, old_orphan.id) is None
    assert db.get(Tender, recent_orphan.id) is not None
    assert db.get(Tender, linked.id) is not None
    assert db.get(Tender, target.id) is not None
    assert db.get(Tender, scored.id) is not None


def test_scheduled_cleanup_uses_orphan_only_safe_options(monkeypatch):
    captured = {}

    def fake_run_cleanup(**kwargs):
        captured.update(kwargs)
        return {'deleted_orphan_tenders': 2}

    monkeypatch.setattr(cleanup_db, 'run_cleanup', fake_run_cleanup)

    result = cleanup_db.run_scheduled_orphan_cleanup(min_age_days=45)

    assert result['deleted_orphan_tenders'] == 2
    assert captured == {
        'clear_pdf_text': False,
        'delete_scores_below': None,
        'delete_orphan_tenders': True,
        'clear_events_older_than_days': None,
        'vacuum': False,
        'dry_run': False,
        'orphan_min_age_days': 45,
    }


def test_cleanup_background_job_runs_scheduled_orphan_cleanup(monkeypatch):
    from app.jobs import cleanup_db as cleanup_module
    from app.services import job_queue

    completed = {}
    monkeypatch.setattr(
        cleanup_module,
        'run_scheduled_orphan_cleanup',
        lambda min_age_days: {'deleted_orphan_tenders': min_age_days},
    )
    monkeypatch.setattr(
        job_queue,
        '_complete',
        lambda job_id, *, result=None, error=None: completed.update(
            {'job_id': job_id, 'result': result, 'error': error}
        ),
    )
    job = BackgroundJob(
        id='orphan-cleanup-job',
        job_type='cleanup',
        status='running',
        lock_key='cleanup:profile:all',
        payload={'scheduled': True, 'min_age_days': 40},
    )

    job_queue.execute_job(job)

    assert completed == {
        'job_id': job.id,
        'result': {'deleted_orphan_tenders': 40},
        'error': None,
    }
