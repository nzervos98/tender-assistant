from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Tender
from app.services.repository import upsert_tender


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _data(ref):
    return {
        'source': 'khmdhs_notice',
        'source_reference': ref,
        'reference_number': ref,
        'title': 'Tender',
        'cpv_codes': ['33790000-4'],
    }


def test_latest_ingest_marker_only_for_first_seen_records():
    db = _session()
    created = upsert_tender(db, _data('26PROCNEW'), ingest_run_id='run1')
    db.commit()

    assert created.is_new_in_latest_ingest is True
    assert created.first_seen_ingest_run_id == 'run1'

    db.query(Tender).update({Tender.is_new_in_latest_ingest: False}, synchronize_session=False)
    existing = upsert_tender(db, _data('26PROCNEW'), ingest_run_id='run2')
    db.commit()

    assert existing.id == created.id
    assert existing.is_new_in_latest_ingest is False
    assert existing.first_seen_ingest_run_id == 'run1'
    assert existing.last_seen_ingest_run_id == 'run2'

from app.models import ClientProfile, TenderScore
from app.services.repository import upsert_score


def test_latest_ingest_marker_is_profile_specific_for_score_rows():
    db = _session()
    profile_a = ClientProfile(slug='a', name='Profile A', cpv_codes=['33000000-0'])
    profile_b = ClientProfile(slug='b', name='Profile B', cpv_codes=['33790000-4'])
    db.add_all([profile_a, profile_b])
    tender = upsert_tender(db, _data('26PROCSHARED'), ingest_run_id='run1')
    db.flush()

    score_a = upsert_score(db, tender.id, profile_a.id, {'score': 61, 'rule_score': 61}, ingest_run_id='run1')
    db.commit()
    assert score_a.is_new_in_latest_ingest is True

    # Same tender already exists in the common tenders table, but it can be new for another profile.
    db.query(TenderScore).filter(TenderScore.profile_id == profile_b.id).update({TenderScore.is_new_in_latest_ingest: False})
    existing_tender = upsert_tender(db, _data('26PROCSHARED'), ingest_run_id='run2')
    score_b = upsert_score(db, existing_tender.id, profile_b.id, {'score': 70, 'rule_score': 70}, ingest_run_id='run2')
    db.commit()

    assert existing_tender.id == tender.id
    assert existing_tender.is_new_in_latest_ingest is False
    assert score_b.is_new_in_latest_ingest is True
    assert score_b.first_seen_ingest_run_id == 'run2'

    # If the same tender-score pair comes back again, it is an update, not a new profile item.
    updated_b = upsert_score(db, existing_tender.id, profile_b.id, {'score': 72, 'rule_score': 72}, ingest_run_id='run3')
    db.commit()
    assert updated_b.is_new_in_latest_ingest is False
    assert updated_b.first_seen_ingest_run_id == 'run2'
    assert updated_b.last_seen_ingest_run_id == 'run3'
