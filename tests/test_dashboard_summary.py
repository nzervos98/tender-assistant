from datetime import timedelta
import sys
import types

sys.modules.setdefault('feedparser', types.SimpleNamespace(parse=lambda *args, **kwargs: types.SimpleNamespace(entries=[])))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db import Base
import app.main as main_module
from app.main import _dashboard_date, _dashboard_query_url, _profile_scoring_signature, _safe_return_url, _split_cpv_preview, _validate_profile_values, dashboard, dashboard_summary, tender_bidding_website, tender_systemic_numbers
from app.models import AppUser, ClientProfile, SystemEvent, Tender, TenderScore
from app.services.timezone import now_utc


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_cpv_preview_keeps_only_matches_visible_and_moves_others_to_overflow():
    preview, overflow = _split_cpv_preview(
        ['72413000-8', '72000000-5', '48000000-8'],
        ['72413000-8'],
    )

    assert preview == ['72413000-8']
    assert overflow == ['72000000-5', '48000000-8']


def test_kimdis_bidding_link_accepts_only_http_urls_and_exposes_systemic_numbers():
    tender = Tender(
        source='khmdhs_notice',
        source_reference='link-1',
        title='Tender',
        raw={
            'biddingWebsite': 'https://nepps.eprocurement.gov.gr/526003',
            'systemicNumbers': [{'systemicNumber': '526003'}, {'systemicNumber': '526003'}],
        },
    )
    assert tender_bidding_website(tender) == 'https://nepps.eprocurement.gov.gr/526003'
    assert tender_systemic_numbers(tender) == ['526003']

    tender.raw['biddingWebsite'] = 'javascript:alert(1)'
    assert tender_bidding_website(tender) == ''


def test_cpv_preview_caps_many_matches_before_overflow():
    matches = [f'code-{number}' for number in range(6)]
    preview, overflow = _split_cpv_preview([*matches, 'other'], matches)

    assert preview == matches[:5]
    assert overflow == [matches[5], 'other']


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
    cancelled = _add_score(db, profile, 'cancelled-high', 91, 3)
    cancelled.tender.cancelled = True
    _add_score(db, profile, 'irrelevant', 95, 3, status='not_relevant')
    db.commit()

    summary = dashboard_summary(db, profile.id)

    assert summary['db_matches'] == 4
    assert summary['matches'] == 2
    assert summary['high'] == 1
    assert summary['expired_matches'] == 1
    assert summary['cancelled_matches'] == 1


def test_dashboard_summary_new_items_means_latest_ingest_only():
    db = _session()
    profile = ClientProfile(slug='latest', name='Latest profile', cpv_codes=['33000000-0'], is_active=True)
    db.add(profile)
    db.flush()
    fresh = _add_score(db, profile, 'fresh', 61, 3, status='new')
    stale_unacted = _add_score(db, profile, 'stale', 61, 3, status='new')
    saved_fresh = _add_score(db, profile, 'saved-fresh', 61, 3, status='saved')
    low_score_fresh = _add_score(db, profile, 'low-score-fresh', 25, 3, status='new')
    fresh.is_new_in_latest_ingest = True
    stale_unacted.is_new_in_latest_ingest = False
    saved_fresh.is_new_in_latest_ingest = True
    low_score_fresh.is_new_in_latest_ingest = True
    db.commit()

    summary = dashboard_summary(db, profile.id)

    assert summary['pending_items'] == 3
    assert summary['new_items'] == 3


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

    assert summary['last_ingest'] is None
    assert summary['last_ingest_profile_payload']['tenders'] == 0
    assert summary['last_ingest_profile_payload']['new_tenders'] == 0
    assert summary['last_ingest_profile_payload']['matches'] == 0


def test_dashboard_summary_does_not_leak_global_ingest_into_new_profile():
    db = _session()
    profile = ClientProfile(slug='new', name='New profile', cpv_codes=['33790000-4'], is_active=True)
    db.add(profile)
    db.flush()
    db.add(SystemEvent(
        event_type='ingest',
        title='Legacy global ingest',
        payload={'tenders': 25, 'new_tenders': 20, 'scores': 25, 'matches': 18},
    ))
    db.commit()

    summary = dashboard_summary(db, profile.id)

    assert summary['last_ingest'] is None
    assert summary['last_ingest_profile_payload']['tenders'] == 0
    assert summary['last_ingest_profile_payload']['matches'] == 0


def test_safe_return_url_allows_only_internal_paths():
    assert _safe_return_url('/?profile_id=2') == '/?profile_id=2'
    assert _safe_return_url('/reports?scope=matches') == '/reports?scope=matches'
    assert _safe_return_url('https://example.com/phish') == '/'
    assert _safe_return_url('//example.com/phish') == '/'
    assert _safe_return_url('/ok\nLocation:https://example.com') == '/'
    assert _safe_return_url('', default='/fallback') == '/fallback'


def test_dashboard_date_normalizes_greek_and_iso_dates():
    assert _dashboard_date('7/9/2026')[0] == '2026-09-07'
    assert _dashboard_date('2026-09-30')[1].isoformat() == '2026-09-30'
    assert _dashboard_date('31/02/2026') == ('', None)


def test_dashboard_query_url_preserves_filters_and_resets_page():
    from starlette.requests import Request

    request = Request({
        'type': 'http', 'method': 'GET', 'path': '/',
        'query_string': b'profile_id=2&deadline_from=2026-09-01&match_type=broad&page=4',
        'headers': [],
    })
    url = _dashboard_query_url(request, match_type='exact_full', page=None)
    assert 'profile_id=2' in url
    assert 'deadline_from=2026-09-01' in url
    assert 'match_type=exact_full' in url
    assert 'page=' not in url


def test_profile_validation_rejects_missing_cpv_invalid_budgets_and_unknown_cpv():
    assert 'τουλάχιστον έναν CPV' in ' '.join(_validate_profile_values('', '', '', 'on'))
    assert 'έγκυρος αριθμός' in ' '.join(_validate_profile_values('72413000-8', 'abc', '', 'on'))
    assert 'αρνητικό' in ' '.join(_validate_profile_values('72413000-8', '-1', '', 'on'))
    assert 'μεγαλύτερο' in ' '.join(_validate_profile_values('72413000-8', '5000', '1000', 'on'))
    assert 'δεν υπάρχουν στον κατάλογο' in ' '.join(_validate_profile_values('99999999-9', '', '', 'on'))
    assert _validate_profile_values('72413000-8', '0', '5000.50', 'on') == []


def test_profile_scoring_signature_ignores_copy_but_detects_scoring_changes():
    profile = ClientProfile(
        slug='signature', name='Original', cpv_codes=['72413000-8'],
        preferred_regions=['EL30 — Αττική'], min_budget=1000, is_active=True,
    )
    original = _profile_scoring_signature(profile)

    profile.name = 'Renamed'
    assert _profile_scoring_signature(profile) == original

    profile.max_budget = 5000
    assert _profile_scoring_signature(profile) != original


def test_dashboard_paginates_in_database_and_uses_filtered_all_count(monkeypatch):
    db = _session()
    admin = AppUser(username='admin-page', password_hash='x', role='admin', is_active=True)
    profile = ClientProfile(slug='page-profile', name='Page profile', cpv_codes=['72413000-8'], is_active=True)
    db.add_all([admin, profile])
    db.flush()
    deadline = now_utc() + timedelta(days=5)
    for number in range(25):
        score = 80 if number < 22 else 40
        db.add(TenderScore(
            profile=profile,
            tender=Tender(
                source='khmdhs_notice', source_reference=f'page-{number}',
                title=f'Page {number}', cpv_codes=['72413000-8'],
                final_submission_date=deadline,
            ),
            score=score, rule_score=score, matched_cpv=['72413000-8'],
            cpv_match_type='exact_full', user_status='new',
        ))
    db.commit()
    monkeypatch.setattr(main_module, 'current_user_from_request', lambda _request: admin)
    request = Request({
        'type': 'http', 'method': 'GET', 'path': '/',
        'query_string': b'profile_id=1&min_score=55&page=2&deadline_filter=all',
        'headers': [],
    })

    response = dashboard(
        request=request, db=db, profile_id=str(profile.id), min_score=55,
        page=2, deadline_filter='all',
    )

    assert response.context['pagination']['total_results'] == 22
    assert response.context['match_counts']['all'] == 22
    assert response.context['match_counts']['exact_full'] == 25
    assert len(response.context['scores']) == 2


def test_dashboard_treats_active_deadline_as_default_not_an_applied_filter(monkeypatch):
    db = _session()
    admin = AppUser(username='admin-filter', password_hash='x', role='admin', is_active=True)
    profile = ClientProfile(slug='filter-profile', name='Filter profile', cpv_codes=['72413000-8'], is_active=True)
    db.add_all([admin, profile])
    db.commit()
    monkeypatch.setattr(main_module, 'current_user_from_request', lambda _request: admin)

    active_request = Request({
        'type': 'http', 'method': 'GET', 'path': '/',
        'query_string': f'profile_id={profile.id}&deadline_filter=active'.encode(),
        'headers': [],
    })
    active_response = dashboard(
        request=active_request, db=db, profile_id=str(profile.id), deadline_filter='active',
    )
    assert active_response.context['active_filters'] == []

    all_request = Request({
        'type': 'http', 'method': 'GET', 'path': '/',
        'query_string': f'profile_id={profile.id}&deadline_filter=all'.encode(),
        'headers': [],
    })
    all_response = dashboard(
        request=all_request, db=db, profile_id=str(profile.id), deadline_filter='all',
    )
    assert all_response.context['active_filters'][0]['label'] == 'Προθεσμία: Όλοι'
    assert 'deadline_filter=active' in all_response.context['active_filters'][0]['url']
