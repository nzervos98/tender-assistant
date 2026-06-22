from datetime import timedelta
import sys
import types

sys.modules.setdefault('feedparser', types.SimpleNamespace(parse=lambda *args, **kwargs: types.SimpleNamespace(entries=[])))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import dashboard_summary
from app.models import ClientProfile, Tender, TenderScore
from app.services.timezone import now_utc


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add_score(db, profile, ref, score, days_delta, status='new'):
    tender = Tender(
        source='khmdhs_notice',
        source_reference=ref,
        reference_number=ref,
        title='Tender',
        organization_name='Org',
        final_submission_date=now_utc() + timedelta(days=days_delta) if days_delta is not None else None,
        cpv_codes=['33790000-4'],
    )
    score_row = TenderScore(profile=profile, tender=tender, score=score, rule_score=score, user_status=status)
    db.add(score_row)
    return score_row


def test_dashboard_summary_counts_actionable_items_not_expired_matches():
    db = _session()
    profile = ClientProfile(slug='p', name='Profile', cpv_codes=['33000000-0'], is_active=True)
    db.add(profile)
    db.flush()
    _add_score(db, profile, 'active-review', 61, 3)
    _add_score(db, profile, 'active-high', 81, 3)
    _add_score(db, profile, 'expired-high', 91, -1)
    _add_score(db, profile, 'irrelevant', 95, 3, status='not_relevant')
    db.commit()

    summary = dashboard_summary(db, profile.id)

    assert summary['db_matches'] == 3
    assert summary['matches'] == 2
    assert summary['high'] == 1
    assert summary['expired_matches'] == 1


def test_dashboard_summary_new_items_means_latest_ingest_only():
    db = _session()
    profile = ClientProfile(slug='latest', name='Latest profile', cpv_codes=['33000000-0'], is_active=True)
    db.add(profile)
    db.flush()
    fresh = _add_score(db, profile, 'fresh', 61, 3, status='new')
    stale_unacted = _add_score(db, profile, 'stale', 61, 3, status='new')
    saved_fresh = _add_score(db, profile, 'saved-fresh', 61, 3, status='saved')
    fresh.is_new_in_latest_ingest = True
    stale_unacted.is_new_in_latest_ingest = False
    saved_fresh.is_new_in_latest_ingest = True
    db.commit()

    summary = dashboard_summary(db, profile.id)

    assert summary['pending_items'] == 2
    assert summary['new_items'] == 2
