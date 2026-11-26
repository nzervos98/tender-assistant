from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db import Base
from app.jobs import ingest as ingest_module
from app.main import app, profile_create
from app.models import AppUser, BackgroundJob, ClientProfile, Tender, TenderChange, TenderScore
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


def test_diavgeia_refresh_route_is_disabled():
    assert '/tenders/{tender_id}/diavgeia-refresh' not in {route.path for route in app.routes}


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


def test_new_active_profile_queues_initial_thirty_day_ingest():
    db = _session()
    user = AppUser(username='profile-owner', password_hash='not-used', role='user', is_active=True)
    db.add(user)
    db.commit()
    request = Request({
        'type': 'http', 'method': 'POST', 'path': '/profiles', 'headers': [],
        'query_string': b'', 'scheme': 'http', 'server': ('test', 80),
        'client': ('test', 1234),
    })
    request.state.current_user = user

    response = profile_create(
        request=request,
        slug='',
        name='Initial ingest profile',
        description='',
        cpv_codes='72000000-5',
        cpv_prefixes='',
        required_certificates='',
        preferred_regions=[],
        min_budget='',
        max_budget='',
        is_active='on',
        owner_user_id='',
        db=db,
    )

    profile = db.query(ClientProfile).filter_by(name='Initial ingest profile').one()
    job = db.query(BackgroundJob).filter_by(profile_id=profile.id).one()
    assert response.status_code == 303
    assert job.job_type == 'ingest'
    assert job.payload['days'] == 3
    assert job.payload['initial_profile_ingest'] is True
    assert job.payload['initial_profile_stage'] == 'recent'
    assert job.payload['initial_profile_ingest_days'] == 30
    assert job.payload['initial_profile_ingest_quick_days'] == 3


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
    monkeypatch.setattr(job_queue, 'fetch_and_extract_pdf_text', lambda _url, **_kwargs: 'ISO 27001 requirement')

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
    assert set(KIMDIS_VIEWS) == {'opportunities'}
    assert job_queue.JOB_TYPES == {'ingest', 'rescore', 'pdf_analysis', 'cleanup'}


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


def test_quick_profile_ingest_queues_full_backfill_atomically(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'profile-backfill.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(job_queue, 'SessionLocal', factory)

    with factory() as db:
        profile = ClientProfile(slug='quick', name='Quick', cpv_codes=['72000000-5'])
        db.add(profile)
        db.flush()
        current, _ = enqueue_job(
            db,
            job_type='ingest',
            profile_id=profile.id,
            payload={
                'days': 3,
                'initial_profile_ingest': True,
                'initial_profile_stage': 'recent',
                'initial_profile_ingest_days': 30,
                'initial_profile_ingest_quick_days': 3,
            },
        )
        current.status = 'running'
        db.commit()

    job_queue._complete_with_initial_profile_backfill(
        current,
        {'tenders': 12, 'signals': 0},
        current.payload,
    )

    with factory() as db:
        jobs = db.query(BackgroundJob).order_by(BackgroundJob.created_at.asc()).all()
        assert len(jobs) == 2
        assert jobs[0].status == 'completed'
        assert jobs[0].result['backfill_job']['id'] == jobs[1].id
        assert jobs[1].status == 'queued'
        assert jobs[1].payload['days'] == 30
        assert jobs[1].payload['initial_profile_backfill'] is True
        assert jobs[1].payload['initial_profile_ingest_quick_days'] == 3
        assert jobs[1].profile_id == jobs[0].profile_id


def test_initial_backfill_date_window_covers_only_the_older_days(monkeypatch):
    from datetime import date

    monkeypatch.setattr(ingest_module, 'today_local', lambda: date(2026, 11, 25))

    assert ingest_module._date_range(30, exclude_recent_days=3) == ('2026-10-26', '2026-11-21')
    assert ingest_module._date_range(3) == ('2026-11-22', '2026-11-25')


def test_ingest_continuation_uses_cooldown_instead_of_abandoning_chain(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'cooldown-jobs.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(job_queue, 'SessionLocal', factory)

    with factory() as db:
        current, _ = enqueue_job(
            db,
            job_type='ingest',
            payload={'days': 3, 'scheduled': True, 'continuation_attempt': 49},
        )
        current.status = 'running'
        db.commit()

    job_queue._complete_with_ingest_continuation(
        current,
        {'continuation_required': True, 'warnings': []},
        current.payload,
        attempt=49,
    )

    with factory() as db:
        jobs = db.query(BackgroundJob).order_by(BackgroundJob.created_at.asc()).all()
        continuation = jobs[1]
        assert continuation.payload['continuation_attempt'] == 0
        assert continuation.payload['continuation_cycle'] == 1
        assert jobs[0].result['continuation_job']['cooling_down'] is True
        assert continuation.available_at >= jobs[0].finished_at + timedelta(minutes=14)


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


def test_execute_recent_profile_ingest_starts_backfill_without_rescore(monkeypatch):
    captured = {}

    def fake_run_ingest(**kwargs):
        captured.update(kwargs)
        return {'continuation_required': False, 'tenders': 4}

    def fake_backfill(job, result, payload):
        captured['backfill_scheduled'] = True
        captured['backfill_days'] = payload['initial_profile_ingest_days']

    monkeypatch.setattr(job_queue, 'run_ingest', fake_run_ingest)
    monkeypatch.setattr(job_queue, '_complete_with_initial_profile_backfill', fake_backfill)
    job = BackgroundJob(
        id='recent-profile-ingest',
        job_type='ingest',
        status='running',
        lock_key='ingest:profile:3',
        profile_id=3,
        payload={
            'days': 3,
            'initial_profile_stage': 'recent',
            'initial_profile_ingest_days': 30,
        },
        result={},
    )

    job_queue.execute_job(job)

    assert captured['days_back'] == 3
    assert captured['recent_notices_only'] is True
    assert captured['backfill_scheduled'] is True
    assert captured['backfill_days'] == 30
    assert 'rescore_job' not in captured


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
    assert result['orphan_tenders_removed'] == 0


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


def test_profile_rescore_prunes_disconnected_acts_but_keeps_signal_linked_acts():
    from app.models import TenderLink

    db = _session()
    profile = ClientProfile(slug='changed-profile', name='Changed profile', cpv_codes=['03000000-1'], is_active=True)
    orphan = Tender(
        source='khmdhs_notice', source_reference='old-unlinked', title='Old unrelated notice',
        cpv_codes=['72000000-5'],
    )
    linked = Tender(
        source='khmdhs_notice', source_reference='old-linked', title='Old notice linked from a signal',
        cpv_codes=['72000000-5'],
    )
    signal = Tender(
        source='khmdhs_request', source_reference='signal-kept', title='Tracked signal',
        signal_stage='converted',
    )
    db.add_all([profile, orphan, linked, signal])
    db.flush()
    db.add_all([
        TenderScore(tender_id=orphan.id, profile_id=profile.id, score=80, rule_score=80, user_status='new'),
        TenderScore(tender_id=linked.id, profile_id=profile.id, score=80, rule_score=80, user_status='new'),
        TenderLink(
            source_tender_id=signal.id,
            related_tender_id=linked.id,
            relation_type='request_to_notice',
            related_reference=linked.source_reference,
        ),
    ])
    db.commit()

    result = rescore_existing_tenders(db, profile_id=profile.id)
    db.commit()

    assert result['scores_removed'] == 2
    assert result['orphan_tenders_removed'] == 1
    assert db.get(Tender, orphan.id) is None
    assert db.get(Tender, linked.id) is not None


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
