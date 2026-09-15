from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BackgroundJob, ClientProfile, DiavgeiaDecision, Tender, TenderChange, TenderScore
from app.services.diavgeia_enrichment import find_and_store_related_diavgeia_decisions
from app.services import job_queue
from app.services.job_queue import claim_next_job, enqueue_job
from app.services.khmdhs_client import KIMDIS_VIEWS, OPERATION_TYPES
from app.services.repository import upsert_tender
from app.services.rescore import rescore_existing_tenders
from app.services.timezone import now_utc


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_background_job_lock_returns_existing_active_job():
    db = _session()
    profile = ClientProfile(slug='ops', name='Ops', cpv_codes=['33790000-4'])
    db.add(profile)
    db.flush()
    first, created = enqueue_job(db, job_type='ingest', profile_id=profile.id, payload={'days': 3})
    second, created_again = enqueue_job(db, job_type='ingest', profile_id=profile.id, payload={'days': 30})
    db.commit()

    assert created is True
    assert created_again is False
    assert second.id == first.id
    assert db.query(BackgroundJob).count() == 1


def test_pdf_analysis_job_is_deduplicated_per_user_and_tender():
    db = _session()
    first, created = enqueue_job(db, job_type='pdf_analysis', requested_by_user_id=4, payload={'tender_id': 12})
    duplicate, duplicate_created = enqueue_job(db, job_type='pdf_analysis', requested_by_user_id=4, payload={'tender_id': 12})
    other_user, other_user_created = enqueue_job(db, job_type='pdf_analysis', requested_by_user_id=5, payload={'tender_id': 12})
    other, other_created = enqueue_job(db, job_type='pdf_analysis', requested_by_user_id=4, payload={'tender_id': 13})

    assert created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert other_user_created is True
    assert other_user.id != first.id
    assert other_created is True
    assert other.lock_key == 'pdf_analysis:user:4:tender:13'


def test_pdf_analysis_job_extracts_text_and_rescores_visible_profiles(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'pdf-job.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(job_queue, 'SessionLocal', factory)
    monkeypatch.setattr(job_queue, 'fetch_and_extract_pdf_text', lambda _url: 'ISO 27001 requirement')

    with factory() as db:
        profile = ClientProfile(
            slug='pdf-profile', name='PDF profile', cpv_codes=['72000000-5'],
            required_certificates=['ISO 27001'], is_active=True,
        )
        tender = Tender(
            source='khmdhs_notice', source_reference='pdf-1', title='IT services',
            cpv_codes=['72000000-5'], attachment_url='https://example.test/tender.pdf',
        )
        db.add_all([profile, tender])
        db.flush()
        job, _ = enqueue_job(
            db,
            job_type='pdf_analysis',
            payload={'tender_id': tender.id, 'profile_ids': [profile.id]},
        )
        job.status = 'running'
        db.commit()

    job_queue.execute_job(job)

    with factory() as db:
        stored_job = db.query(BackgroundJob).filter_by(id=job.id).one()
        stored_tender = db.query(Tender).filter_by(id=tender.id).one()
        stored_score = db.query(TenderScore).filter_by(tender_id=tender.id, profile_id=profile.id).one()
        assert stored_job.status == 'completed'
        assert stored_job.result['characters'] == len('ISO 27001 requirement')
        assert stored_tender.pdf_text == 'ISO 27001 requirement'
        assert stored_score.score == 100


def test_market_feature_is_not_exposed_or_schedulable():
    assert set(OPERATION_TYPES) == {'notice', 'request'}
    assert 'market' not in KIMDIS_VIEWS
    assert job_queue.JOB_TYPES == {'ingest', 'rescore', 'pdf_analysis'}


def test_future_background_job_is_not_claimed_early():
    db = _session()
    future_job, _ = enqueue_job(
        db,
        job_type='ingest',
        payload={'continuation': True},
        available_at=now_utc() + timedelta(minutes=5),
    )
    db.commit()

    assert claim_next_job(db) is None
    future_job.available_at = now_utc() - timedelta(seconds=1)
    db.commit()
    assert claim_next_job(db).id == future_job.id


def test_ingest_continuation_is_queued_atomically(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'jobs.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(job_queue, 'SessionLocal', factory)

    with factory() as db:
        current, _ = enqueue_job(
            db,
            job_type='ingest',
            payload={'days': 3, 'scheduled': True},
        )
        current.status = 'running'
        db.commit()

    job_queue._complete_with_ingest_continuation(
        current,
        {'continuation_required': True, 'warnings': []},
        current.payload,
        attempt=0,
    )

    with factory() as db:
        jobs = db.query(BackgroundJob).order_by(BackgroundJob.created_at.asc()).all()
        assert len(jobs) == 2
        assert jobs[0].status == 'completed'
        assert jobs[1].status == 'queued'
        assert jobs[1].payload['continuation'] is True
        assert jobs[1].payload['continuation_attempt'] == 1
        assert jobs[1].payload['root_ingest_job_id'] == jobs[0].id
        assert jobs[1].available_at > jobs[0].finished_at


def test_execute_job_continues_incomplete_ingest_without_rescore(monkeypatch):
    captured = {}

    def fake_run_ingest(**kwargs):
        captured['incremental'] = kwargs['incremental']
        return {'continuation_required': True, 'warnings': []}

    def fake_continue(job, result, payload, *, attempt):
        captured['continued'] = True
        captured['attempt'] = attempt

    monkeypatch.setattr(job_queue, 'run_ingest', fake_run_ingest)
    monkeypatch.setattr(job_queue, '_complete_with_ingest_continuation', fake_continue)
    job = BackgroundJob(
        id='continuation-test',
        job_type='ingest',
        status='running',
        lock_key='ingest:profile:all',
        payload={'days': 3, 'continuation': True, 'continuation_attempt': 4},
        result={},
    )

    job_queue.execute_job(job)

    assert captured == {'incremental': True, 'continued': True, 'attempt': 4}


def test_tender_change_history_records_deadline_and_cancellation_updates():
    db = _session()
    tender = upsert_tender(db, {
        'source': 'khmdhs_notice',
        'source_reference': '26PROC1',
        'reference_number': '26PROC1',
        'title': 'Tender',
        'cancelled': False,
    }, ingest_run_id='run1')
    db.commit()

    updated = upsert_tender(db, {
        'source': 'khmdhs_notice',
        'source_reference': '26PROC1',
        'reference_number': '26PROC1',
        'title': 'Tender',
        'cancelled': True,
        'cancellation_reason': 'Ματαίωση διαδικασίας',
    }, ingest_run_id='run2')
    db.commit()

    assert updated.id == tender.id
    changes = {row.field_name: row for row in db.query(TenderChange).all()}
    assert changes['cancelled'].old_value == 'False'
    assert changes['cancelled'].new_value == 'True'
    assert changes['cancellation_reason'].new_value == 'Ματαίωση διαδικασίας'


class _UnverifiedDiavgeia:
    def search_by_adam(self, adam, **kwargs):
        return {'info': {'total': 1}, 'decisions': [{'ada': 'ΑΔΑ-1', 'subject': 'Άσχετη πράξη'}]}

    def get_decision(self, ada):
        return {'ada': ada, 'subject': 'Άσχετη πράξη', 'content': 'Δεν περιέχει κωδικό ΚΗΜΔΗΣ'}


def test_diavgeia_does_not_store_unverified_term_result_and_marks_old_rows_stale():
    db = _session()
    tender = Tender(source='khmdhs_notice', source_reference='26PROC000000001', reference_number='26PROC000000001', title='Tender')
    db.add(tender)
    db.flush()
    db.add(DiavgeiaDecision(tender_id=tender.id, ada='OLD', subject='Old', is_current=True))
    db.commit()

    result = find_and_store_related_diavgeia_decisions(db, tender, client=_UnverifiedDiavgeia(), hydrate=True)
    db.commit()

    assert result.stored == 0
    assert db.query(DiavgeiaDecision).filter(DiavgeiaDecision.ada == 'ΑΔΑ-1').count() == 0
    assert db.query(DiavgeiaDecision).filter(DiavgeiaDecision.ada == 'OLD').one().is_current is False


def test_full_rescore_never_scores_market_history_rows():
    db = _session()
    profile = ClientProfile(slug='software', name='Software', cpv_codes=['48000000-8'], is_active=True)
    notice = Tender(
        source='khmdhs_notice', source_reference='n1', title='Notice',
        cpv_codes=['48000000-8'],
    )
    contract = Tender(
        source='khmdhs_contract', source_reference='c1', title='Historical contract',
        cpv_codes=['48000000-8'],
    )
    db.add_all([profile, notice, contract])
    db.commit()

    result = rescore_existing_tenders(db)
    db.commit()

    assert result['tenders'] == 1
    assert db.query(TenderScore).filter(TenderScore.tender_id == notice.id).count() == 1
    assert db.query(TenderScore).filter(TenderScore.tender_id == contract.id).count() == 0


def test_full_rescore_does_not_attach_unrelated_tenders_to_every_profile():
    db = _session()
    web = ClientProfile(slug='web', name='Web', cpv_codes=['72000000-5'], is_active=True)
    agriculture = ClientProfile(slug='agriculture', name='Agriculture', cpv_codes=['03000000-1'], is_active=True)
    tender = Tender(
        source='khmdhs_notice',
        source_reference='web-1',
        title='Web services',
        cpv_codes=['72000000-5'],
    )
    db.add_all([web, agriculture, tender])
    db.flush()
    db.add(TenderScore(tender_id=tender.id, profile_id=agriculture.id, score=0, rule_score=0, user_status='new'))
    db.commit()

    result = rescore_existing_tenders(db)
    db.commit()

    assert db.query(TenderScore).filter_by(tender_id=tender.id, profile_id=web.id).count() == 1
    assert db.query(TenderScore).filter_by(tender_id=tender.id, profile_id=agriculture.id).count() == 0
    assert result['scores_removed'] == 1


def test_full_rescore_preserves_explicitly_saved_unrelated_tender():
    db = _session()
    profile = ClientProfile(slug='agriculture-saved', name='Agriculture', cpv_codes=['03000000-1'], is_active=True)
    tender = Tender(
        source='khmdhs_notice',
        source_reference='saved-web',
        title='Web services',
        cpv_codes=['72000000-5'],
    )
    db.add_all([profile, tender])
    db.flush()
    db.add(TenderScore(tender_id=tender.id, profile_id=profile.id, score=0, rule_score=0, user_status='saved'))
    db.commit()

    rescore_existing_tenders(db)
    db.commit()

    assert db.query(TenderScore).filter_by(tender_id=tender.id, profile_id=profile.id).count() == 1


def test_rescore_counters_include_only_actionable_matches():
    db = _session()
    profile = ClientProfile(slug='counter-scope', name='Counter scope', cpv_codes=['72000000-5'], is_active=True)
    active = Tender(
        source='khmdhs_notice',
        source_reference='counter-active',
        title='Active IT',
        cpv_codes=['72000000-5'],
        final_submission_date=now_utc() + timedelta(days=2),
    )
    expired = Tender(
        source='khmdhs_notice',
        source_reference='counter-expired',
        title='Expired IT',
        cpv_codes=['72000000-5'],
        final_submission_date=now_utc() - timedelta(days=2),
    )
    cancelled = Tender(
        source='khmdhs_notice',
        source_reference='counter-cancelled',
        title='Cancelled IT',
        cpv_codes=['72000000-5'],
        final_submission_date=now_utc() + timedelta(days=2),
        cancelled=True,
    )
    db.add_all([profile, active, expired, cancelled])
    db.commit()

    result = rescore_existing_tenders(db)
    db.commit()

    assert result['scores_updated'] == 3
    assert result['matches'] == 1
    assert result['high'] == 1
