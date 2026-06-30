from datetime import timedelta
import sys
import types

sys.modules.setdefault('feedparser', types.SimpleNamespace(parse=lambda *args, **kwargs: types.SimpleNamespace(entries=[])))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import _safe_return_url, dashboard_summary
from app.models import ClientProfile, SystemEvent, Tender, TenderScore
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


def test_dashboard_summary_uses_selected_profile_ingest_payload():
    db = _session()
    profile_a = ClientProfile(slug='a', name='Profile A', cpv_codes=['33000000-0'], is_active=True)
    profile_b = ClientProfile(slug='b', name='Profile B', cpv_codes=['33790000-4'], is_active=True)
    db.add_all([profile_a, profile_b])
    db.flush()
    db.add(SystemEvent(
        event_type='ingest',
        title='Ingest',
        payload={
            'tenders': 1,
            'new_tenders': 1,
            'scores': 1,
            'matches': 1,
            'per_profile': {
                str(profile_a.id): {'tenders': 1, 'new_tenders': 1, 'scores': 1, 'matches': 1},
                str(profile_b.id): {'tenders': 0, 'new_tenders': 0, 'scores': 0, 'matches': 0},
            },
        },
    ))
    db.commit()

    summary = dashboard_summary(db, profile_b.id)

    assert summary['last_ingest_payload']['new_tenders'] == 1
    assert summary['last_ingest_profile_payload']['new_tenders'] == 0
    assert summary['last_ingest_profile_payload']['matches'] == 0


def test_dashboard_summary_uses_zero_payload_when_selected_profile_was_not_in_last_ingest():
    db = _session()
    profile_a = ClientProfile(slug='a2', name='Profile A2', cpv_codes=['33000000-0'], is_active=True)
    profile_b = ClientProfile(slug='b2', name='Profile B2', cpv_codes=['33790000-4'], is_active=True)
    db.add_all([profile_a, profile_b])
    db.flush()
    db.add(SystemEvent(
        event_type='ingest',
        title='Manual ingest for A',
        payload={
            'tenders': 1,
            'new_tenders': 1,
            'scores': 1,
            'matches': 1,
            'profile_id': profile_a.id,
            'per_profile': {
                str(profile_a.id): {'tenders': 1, 'new_tenders': 1, 'scores': 1, 'matches': 1},
            },
        },
    ))
    db.commit()

    summary = dashboard_summary(db, profile_b.id)

    assert summary['last_ingest_profile_payload']['tenders'] == 0
    assert summary['last_ingest_profile_payload']['new_tenders'] == 0
    assert summary['last_ingest_profile_payload']['matches'] == 0


def test_safe_return_url_allows_only_internal_paths():
    assert _safe_return_url('/?profile_id=2') == '/?profile_id=2'
    assert _safe_return_url('/reports?scope=matches') == '/reports?scope=matches'
    assert _safe_return_url('https://example.com/phish') == '/'
    assert _safe_return_url('//example.com/phish') == '/'
    assert _safe_return_url('/ok\nLocation:https://example.com') == '/'
    assert _safe_return_url('', default='/fallback') == '/fallback'
