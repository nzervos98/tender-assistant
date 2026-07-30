from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BackgroundJob, ClientProfile, DiavgeiaDecision, Tender, TenderChange, TenderScore
from app.services.diavgeia_enrichment import find_and_store_related_diavgeia_decisions
from app.services import job_queue
from app.services.job_queue import claim_next_job, enqueue_job
from app.services.khmdhs_client import KhmdhsClient, build_search_body
from app.services.market_intelligence import market_overview
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


def test_khmdhs_market_fields_and_extended_filters_are_normalized():
    body = build_search_body(
        resource='contract',
        contractor_name='ACME',
        vat_number='123456789',
        public_funding_ref_num='2026EP001',
        estimated_total_cost_from='1000',
        cancel_date_from='2026-07-01',
    )
    assert body['contractorName'] == 'ACME'
    assert body['vatNumber'] == '123456789'
    assert body['publicFundingRefNum'] == '2026EP001'
    assert body['estTotalCostFrom'] == 1000
    assert body['cancelDateFrom'] == '2026-07-01'

    normalized = KhmdhsClient().normalize_record('contract', {
        'referenceNumber': '26SYMV000000001',
        'title': 'Σύμβαση',
        'totalCostWithoutVAT': 5000,
        'contractors': [{'name': 'ACME', 'vatNumber': '123456789'}],
        'publicFundingRefNum': '2026EP001',
        'isModified': True,
    })
    assert normalized['contractor_name'] == 'ACME'
    assert normalized['contractor_vat_number'] == '123456789'
    assert normalized['contract_value'] == 5000
    assert normalized['is_modified'] is True


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


def test_market_overview_aggregates_structured_values():
    db = _session()
    db.add_all([
        Tender(source='khmdhs_contract', source_reference='c1', title='C1', contractor_name='ACME', contract_value=1000, cpv_codes=['33790000-4']),
        Tender(source='khmdhs_payment', source_reference='p1', title='P1', contractor_name='ACME', payment_amount=400, cpv_codes=['33790000-4']),
    ])
    db.commit()

    result = market_overview(db)
    assert result['with_contractor'] == 2
    assert result['total_contract_value'] == 1000
    assert result['total_payments'] == 400
    assert result['contractors'][0]['name'] == 'ACME'
    assert result['cpvs'][0]['records'] == 2


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
